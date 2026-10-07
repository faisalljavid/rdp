"""
Async HTTP and WebSocket server for Wayland Remote Desktop.
Handles static asset serving, auth token verification, rate limiting,
frame dispatching with backpressure drop, and input routing.
"""

import os
import sys
import json
import time
import asyncio
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Optional, Set

import websockets
from websockets.asyncio.server import serve, ServerConnection, Request, Response
from websockets.datastructures import Headers

from portal import PortalManager

STATIC_DIR = Path(__file__).parent / "static"


class RateLimiter:
    """Simple sliding-window IP rate limiter for authentication attempts."""

    def __init__(self, max_attempts: int = 5, window_seconds: float = 60.0):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.failed_attempts: dict[str, list[float]] = {}

    def is_blocked(self, ip: str) -> bool:
        now = time.time()
        attempts = self.failed_attempts.get(ip, [])
        # Keep only recent attempts within window
        valid_attempts = [t for t in attempts if now - t < self.window_seconds]
        self.failed_attempts[ip] = valid_attempts
        return len(valid_attempts) >= self.max_attempts

    def record_failure(self, ip: str):
        now = time.time()
        if ip not in self.failed_attempts:
            self.failed_attempts[ip] = []
        self.failed_attempts[ip].append(now)

    def record_success(self, ip: str):
        self.failed_attempts.pop(ip, None)


class ClientSession:
    """Represents a connected client WebSocket session."""

    def __init__(self, ws: ServerConnection, client_ip: str):
        self.ws = ws
        self.client_ip = client_ip
        self.authenticated = False
        self.is_sending = False

    async def send_frame(self, frame_bytes: bytes):
        """Sends binary JPEG frame with backpressure protection."""
        if self.is_sending or not self.authenticated:
            return
        self.is_sending = True
        try:
            await self.ws.send(frame_bytes)
        except Exception:
            pass
        finally:
            self.is_sending = False

    async def send_json(self, data: dict):
        try:
            await self.ws.send(json.dumps(data))
        except Exception:
            pass


class RdpServer:
    def __init__(
        self,
        host: str,
        port: int,
        auth_token: str,
        portal: PortalManager,
        ssl_context=None,
    ):
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.portal = portal
        self.ssl_context = ssl_context

        self.rate_limiter = RateLimiter(max_attempts=5, window_seconds=60.0)
        self.clients: Set[ClientSession] = set()
        self.server = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def broadcast_frame(self, frame_bytes: bytes):
        """Called by GStreamer thread on each encoded frame."""
        if not self.clients or not self.loop:
            return

        for client in list(self.clients):
            if client.authenticated and not client.is_sending:
                asyncio.run_coroutine_threadsafe(client.send_frame(frame_bytes), self.loop)

    def _get_client_ip(self, connection: ServerConnection) -> str:
        try:
            remote_addr = connection.remote_address
            if isinstance(remote_addr, tuple):
                return remote_addr[0]
            return str(remote_addr)
        except Exception:
            return "unknown"

    def process_http_request(self, connection: ServerConnection, request: Request) -> Optional[Response]:
        """Serves static files (HTML, CSS, JS) over the same port."""
        parsed = urlparse(request.path)
        path = parsed.path

        # If it's a websocket upgrade request to /ws, allow it through
        if path == "/ws":
            return None

        # Static file mapping
        if path == "/" or path == "/index.html":
            file_path = STATIC_DIR / "index.html"
            content_type = "text/html; charset=utf-8"
        elif path == "/style.css":
            file_path = STATIC_DIR / "style.css"
            content_type = "text/css; charset=utf-8"
        elif path == "/app.js":
            file_path = STATIC_DIR / "app.js"
            content_type = "application/javascript; charset=utf-8"
        elif path == "/favicon.ico":
            return Response(204, "No Content", Headers(), b"")
        else:
            return Response(
                404,
                "Not Found",
                Headers([("Content-Type", "text/plain")]),
                b"404 Not Found",
            )

        if not file_path.exists():
            return Response(
                404,
                "Not Found",
                Headers([("Content-Type", "text/plain")]),
                b"Static asset missing",
            )

        try:
            body = file_path.read_bytes()
            headers = Headers([
                ("Content-Type", content_type),
                ("Cache-Control", "no-cache, no-store, must-revalidate"),
            ])
            return Response(200, "OK", headers, body)
        except Exception as e:
            return Response(
                500,
                "Internal Error",
                Headers([("Content-Type", "text/plain")]),
                f"Error: {e}".encode(),
            )

    async def handle_ws_client(self, ws: ServerConnection):
        """Manages a WebSocket client connection."""
        client_ip = self._get_client_ip(ws)
        session = ClientSession(ws, client_ip)
        self.clients.add(session)

        # Check for token in URL query parameter (e.g. /ws?token=XYZ)
        parsed = urlparse(ws.request.path)
        query_params = parse_qs(parsed.query)
        token_param = query_params.get("token", [None])[0]

        if self.rate_limiter.is_blocked(client_ip):
            print(f"[auth] Blocked connection attempt from {client_ip} (rate limit exceeded)")
            await session.send_json({
                "type": "auth_result",
                "success": False,
                "error": "Too many failed attempts. Temporarily blocked for 60 seconds.",
            })
            await ws.close(4003, "Rate limited")
            self.clients.discard(session)
            return

        if token_param:
            if token_param == self.auth_token:
                session.authenticated = True
                self.rate_limiter.record_success(client_ip)
                print(f"[auth] Client {client_ip} authenticated via URL parameter.")
                await session.send_json({
                    "type": "auth_result",
                    "success": True,
                    "screen": {
                        "width": self.portal.stream_width,
                        "height": self.portal.stream_height,
                    },
                })
            else:
                self.rate_limiter.record_failure(client_ip)
                print(f"[auth] Failed token attempt from {client_ip}")
                await session.send_json({
                    "type": "auth_result",
                    "success": False,
                    "error": "Invalid authentication token",
                })

        try:
            async for raw_msg in ws:
                if isinstance(raw_msg, bytes):
                    continue  # Ignore binary data from client

                try:
                    msg = json.loads(raw_msg)
                except Exception:
                    continue

                msg_type = msg.get("type")

                # Auth challenge message
                if msg_type == "auth":
                    if self.rate_limiter.is_blocked(client_ip):
                        await session.send_json({
                            "type": "auth_result",
                            "success": False,
                            "error": "Rate limit exceeded. Try again later.",
                        })
                        continue

                    candidate_token = msg.get("token", "")
                    if candidate_token == self.auth_token:
                        session.authenticated = True
                        self.rate_limiter.record_success(client_ip)
                        print(f"[auth] Client {client_ip} successfully authenticated.")
                        await session.send_json({
                            "type": "auth_result",
                            "success": True,
                            "screen": {
                                "width": self.portal.stream_width,
                                "height": self.portal.stream_height,
                            },
                        })
                    else:
                        self.rate_limiter.record_failure(client_ip)
                        print(f"[auth] Failed token attempt from {client_ip}")
                        await session.send_json({
                            "type": "auth_result",
                            "success": False,
                            "error": "Invalid token",
                        })
                    continue

                # Ignore non-auth messages from unauthenticated clients
                if not session.authenticated:
                    continue

                # Latency Ping/Pong
                if msg_type == "ping":
                    await session.send_json({"type": "pong", "ts": msg.get("ts")})
                    continue

                # Input events
                if msg_type == "pointer_move":
                    x = msg.get("x", 0.0)
                    y = msg.get("y", 0.0)
                    self.portal.notify_pointer_motion_absolute(x, y)

                elif msg_type == "pointer_move_relative":
                    dx = msg.get("dx", 0.0)
                    dy = msg.get("dy", 0.0)
                    self.portal.notify_pointer_motion(dx, dy)

                elif msg_type == "pointer_button":
                    btn = msg.get("button", 272)
                    state = msg.get("state", 0)
                    self.portal.notify_pointer_button(btn, state)

                elif msg_type == "pointer_axis":
                    dx = msg.get("dx", 0.0)
                    dy = msg.get("dy", 0.0)
                    self.portal.notify_pointer_axis(dx, dy)

                elif msg_type == "key_down":
                    keysym = msg.get("keysym")
                    if keysym is not None:
                        self.portal.notify_keyboard_keysym(keysym, 1)

                elif msg_type == "key_up":
                    keysym = msg.get("keysym")
                    if keysym is not None:
                        self.portal.notify_keyboard_keysym(keysym, 0)

                elif msg_type == "key_click":
                    keysym = msg.get("keysym")
                    if keysym is not None:
                        self.portal.send_key_click(keysym)

                elif msg_type == "text":
                    text = msg.get("text", "")
                    if text:
                        self.portal.send_text(text)

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(session)
            print(f"[server] Client disconnected: {client_ip}")

    async def start(self):
        """Starts the combined HTTP/WebSocket server."""
        self.loop = asyncio.get_running_loop()
        print(f"[server] Starting server on {self.host}:{self.port} (SSL: {bool(self.ssl_context)})")
        self.server = await serve(
            self.handle_ws_client,
            self.host,
            self.port,
            ssl=self.ssl_context,
            process_request=self.process_http_request,
            ping_interval=20,
            ping_timeout=20,
            max_size=10_000_000,
        )

    async def stop(self):
        """Stops the server and closes all client connections."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
