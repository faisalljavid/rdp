#!/usr/bin/env python3
"""
Wayland Remote Desktop Host for Fedora GNOME.
Streams screen via PipeWire & GStreamer, receives touch & keyboard from phone.
"""

import os
import sys
import time
import socket
import signal
import asyncio
import subprocess
from typing import Optional

from config import (
    parse_args,
    get_or_create_auth_token,
    create_ssl_context,
)
from portal import PortalManager, MockPortalManager
from pipeline import VideoPipeline
from server import RdpServer


def get_lan_ip() -> str:
    """Detects active LAN IPv4 address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_tailscale_ip() -> Optional[str]:
    """Detects Tailscale IPv4 address if active."""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "tailscale0"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # format: 4: tailscale0    inet 100.x.y.z/32 ...
        parts = out.strip().split()
        if len(parts) >= 4:
            return parts[3].split("/")[0]
    except Exception:
        pass
    return None


def print_qr_code(url: str):
    """Prints terminal ASCII QR code if qrcode library is available."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        print("\nScan this QR code with your phone camera to connect:\n")
        qr.print_ascii(invert=True)
        print()
    except Exception:
        pass


class RemoteDesktopApp:
    def __init__(self, args):
        self.args = args
        self.auth_token = get_or_create_auth_token(args.token)
        self.ssl_context = create_ssl_context(args.no_ssl)

        self.portal: Optional[PortalManager] = None
        self.pipeline: Optional[VideoPipeline] = None
        self.server: Optional[RdpServer] = None
        self.is_reconnecting = False
        self._shutdown_event = asyncio.Event()

    def on_session_closed(self):
        """Called when compositor or portal closes session (e.g. screen lock)."""
        print("[app] Portal session closed or disconnected. Triggering reconnect...")
        asyncio.create_task(self.reconnect_session())

    async def reconnect_session(self):
        """Cleanly tears down old pipeline and reconnects portal session."""
        if self.is_reconnecting:
            return
        self.is_reconnecting = True

        print("[app] Waiting 2 seconds before session restart...")
        await asyncio.sleep(2)

        if self.pipeline:
            self.pipeline.stop()
            self.pipeline = None

        if self.portal:
            self.portal.close()
            self.portal = None

        retry_count = 0
        while not self._shutdown_event.is_set():
            retry_count += 1
            try:
                print(f"[app] Re-establishing portal session (attempt {retry_count})...")
                self.setup_portal_and_pipeline()
                self.is_reconnecting = False
                print("[app] Session re-established successfully.")
                break
            except Exception as e:
                print(f"[app] Reconnect attempt failed: {e}. Retrying in 4s...")
                await asyncio.sleep(4)

    def setup_portal_and_pipeline(self):
        """Initializes portal session and GStreamer capture pipeline."""
        if self.args.mock:
            self.portal = MockPortalManager(on_session_closed=self.on_session_closed)
            pw_fd, node_id, width, height = self.portal.start_session()
        else:
            self.portal = PortalManager(on_session_closed=self.on_session_closed)
            pw_fd, node_id, width, height = self.portal.start_session(
                force_reset_token=self.args.reset_portal
            )

        # Update server's portal reference
        if self.server:
            self.server.portal = self.portal

        self.pipeline = VideoPipeline(
            fd=pw_fd,
            node_id=node_id,
            stream_width=width,
            stream_height=height,
            fps=self.args.fps,
            quality=self.args.quality,
            scale=self.args.scale,
            is_mock=self.args.mock,
            on_frame=self.on_video_frame,
            on_error_or_eos=self.on_session_closed,
        )
        self.pipeline.start()

    def on_video_frame(self, frame_bytes: bytes):
        """Forward encoded frame to WebSocket server."""
        if self.server:
            self.server.broadcast_frame(frame_bytes)

    def print_startup_banner(self):
        scheme = "http" if self.args.no_ssl else "https"
        lan_ip = get_lan_ip()
        tailscale_ip = get_tailscale_ip()

        lan_url = f"{scheme}://{lan_ip}:{self.args.port}/?token={self.auth_token}"

        print("=" * 64)
        if self.args.mock:
            print("  Wayland Remote Desktop [SANDBOX / TEST MODE]")
            print("  (Bouncing ball test pattern, no real screen capture or input)")
        else:
            print("  Wayland Remote Desktop (Fedora GNOME)")
        print("=" * 64)
        print(f"  • Screen Resolution : {self.portal.stream_width}x{self.portal.stream_height}")
        print(f"  • Target Framerate  : {self.args.fps} FPS")
        print(f"  • JPEG Quality      : {self.args.quality}%")
        print(f"  • Auth Token        : {self.auth_token}")
        print(f"  • Security Protocol : {'Plain HTTP/WS' if self.args.no_ssl else 'HTTPS/WSS (Self-signed TLS)'}")
        print("-" * 64)
        print(f"  Connect from your phone:")
        print(f"    LAN URL       : \033[1;32m{lan_url}\033[0m")
        if tailscale_ip:
            ts_url = f"{scheme}://{tailscale_ip}:{self.args.port}/?token={self.auth_token}"
            print(f"    Tailscale URL : \033[1;36m{ts_url}\033[0m")
        print("=" * 64)

        print_qr_code(lan_url)

        if not self.args.no_ssl:
            print("Note on Self-Signed HTTPS:")
            print("  Your phone browser will show a standard certificate warning once.")
            print("  Tap 'Advanced' -> 'Proceed to site' (or 'Accept the Risk and Continue').")
            print("  Tip: For zero cert warnings, run with --no-ssl over Tailscale!")
            print("-" * 64)

    async def run(self):
        # 1. Setup portal & GStreamer
        self.setup_portal_and_pipeline()

        # 2. Setup server
        self.server = RdpServer(
            host=self.args.host,
            port=self.args.port,
            auth_token=self.auth_token,
            portal=self.portal,
            ssl_context=self.ssl_context,
        )
        await self.server.start()

        # 3. Print Banner & QR
        self.print_startup_banner()

        # 4. Wait until interrupted
        await self._shutdown_event.wait()

    def shutdown(self):
        print("\n[app] Shutting down Remote Desktop...")
        self._shutdown_event.set()

        if self.pipeline:
            self.pipeline.stop()
            self.pipeline = None

        if self.portal:
            self.portal.close()
            self.portal = None


def main():
    args = parse_args()
    app = RemoteDesktopApp(args)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal():
        app.shutdown()
        # Schedule stopping event loop
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(app.run())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        app.shutdown()
        try:
            # cancel pending tasks
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
        except Exception:
            pass
        print("[app] Goodbye.")


if __name__ == "__main__":
    main()
