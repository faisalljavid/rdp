"""
Wayland Portal Manager for ScreenCast and RemoteDesktop.
Uses Gio D-Bus to communicate with org.freedesktop.portal.Desktop.
"""

import os
import sys
import time
import uuid
import queue
import threading
from typing import Callable, Optional

import gi
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from config import save_restore_token


class GLibLoopThread:
    """Runs a GLib MainLoop on a dedicated thread for D-Bus signals and GStreamer."""

    def __init__(self):
        self._loop = None
        self._thread = None
        self._started = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="GLibMainLoop")
        self._thread.start()
        self._started.wait()

    def _run(self):
        ctx = GLib.MainContext.default()
        self._loop = GLib.MainLoop(ctx)
        self._started.set()
        self._loop.run()

    def stop(self):
        if self._loop and self._loop.is_running():
            self._loop.quit()


_glib_thread = None


def ensure_glib_loop():
    """Starts a background GLib thread only if a GTK/Adwaita main loop is not already running."""
    global _glib_thread
    if "gi.repository.Gtk" in sys.modules:
        try:
            from gi.repository import Gtk
            if Gtk.is_initialized():
                return
        except Exception:
            pass
    if _glib_thread is None:
        _glib_thread = GLibLoopThread()
        _glib_thread.start()


class PortalManager:
    """
    Manages the org.freedesktop.portal.RemoteDesktop + ScreenCast combined session.
    Provides screen stream information, PipeWire fd, and input injection.
    """

    PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
    PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
    INTERFACE_REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
    INTERFACE_SCREEN_CAST = "org.freedesktop.portal.ScreenCast"
    INTERFACE_REQUEST = "org.freedesktop.portal.Request"
    INTERFACE_SESSION = "org.freedesktop.portal.Session"

    # Linux evdev button constants
    BTN_LEFT = 272
    BTN_RIGHT = 273
    BTN_MIDDLE = 274

    def __init__(self, on_session_closed: Optional[Callable[[], None]] = None):
        ensure_glib_loop()
        self.bus: Gio.DBusConnection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender_id = self.bus.get_unique_name()[1:].replace(".", "_")

        self.session_handle: Optional[str] = None
        self.pipewire_fd: Optional[int] = None
        self.stream_node_id: Optional[int] = None
        self.stream_width: int = 1920
        self.stream_height: int = 1080
        self.restore_token: Optional[str] = None
        self._last_abs_pos: Optional[tuple[float, float]] = None
        self._use_rel_fallback: bool = False

        self._pending_requests: dict[str, queue.Queue] = {}
        self._signal_sub_id: Optional[int] = None
        self._session_closed_sub_id: Optional[int] = None
        self._on_session_closed = on_session_closed

        self._subscribe_signals()

    def _subscribe_signals(self):
        """Subscribe to portal Request Response signals and Session Closed signals."""
        self._signal_sub_id = self.bus.signal_subscribe(
            self.PORTAL_BUS_NAME,
            self.INTERFACE_REQUEST,
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_request_response,
            None,
        )

        self._session_closed_sub_id = self.bus.signal_subscribe(
            self.PORTAL_BUS_NAME,
            self.INTERFACE_SESSION,
            "Closed",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_session_closed_signal,
            None,
        )

    def _on_request_response(
        self, connection, sender_name, object_path, interface_name, signal_name, parameters, user_data
    ):
        """Dispatches portal response to the waiting queue matching the request path."""
        # parameters signature is (ua{sv}) -> (response_code, results_dict)
        try:
            unpacked = parameters.unpack()
            code, results = unpacked[0], unpacked[1]
            token = object_path.split("/")[-1]
            if token in self._pending_requests:
                self._pending_requests[token].put((code, results))
        except Exception as e:
            print(f"[portal] Error processing Response signal: {e}", file=sys.stderr)

    def _on_session_closed_signal(
        self, connection, sender_name, object_path, interface_name, signal_name, parameters, user_data
    ):
        """Called when compositor or portal closes our session (e.g. screen lock, user revoked)."""
        if self.session_handle and object_path == self.session_handle:
            print("[portal] RemoteDesktop session was closed by compositor/portal.")
            self.session_handle = None
            if self._on_session_closed:
                self._on_session_closed()

    def _wait_for_response(self, token: str, timeout: float = 60.0) -> dict:
        """Wait for the Response signal for a specific request token."""
        q = queue.Queue()
        self._pending_requests[token] = q
        try:
            code, results = q.get(timeout=timeout)
            if code != 0:
                raise PermissionError(
                    f"Portal request '{token}' was denied or cancelled by user (code={code})"
                )
            return results
        except queue.Empty:
            raise TimeoutError(f"Timed out waiting for portal response '{token}' after {timeout}s")
        finally:
            self._pending_requests.pop(token, None)

    def start_session(self, force_reset_token: bool = False) -> tuple[int, int, int, int]:
        """
        Executes the full Wayland RemoteDesktop + ScreenCast portal sequence:
        1. CreateSession (RemoteDesktop)
        2. SelectDevices (Pointer + Keyboard)
        3. SelectSources (ScreenCast monitor with persist_mode and restore_token)
        4. Start (prompts user or uses restore_token)
        5. OpenPipeWireRemote (retrieves PipeWire fd)

        Returns: (pipewire_fd, stream_node_id, width, height)
        """
        # Step 1: CreateSession
        handle_token = f"rdp_{uuid.uuid4().hex[:8]}"
        session_token = f"sess_{uuid.uuid4().hex[:8]}"

        options = {
            "handle_token": GLib.Variant("s", handle_token),
            "session_handle_token": GLib.Variant("s", session_token),
        }

        self.bus.call_sync(
            self.PORTAL_BUS_NAME,
            self.PORTAL_OBJECT_PATH,
            self.INTERFACE_REMOTE_DESKTOP,
            "CreateSession",
            GLib.Variant("(a{sv})", (options,)),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )

        res = self._wait_for_response(handle_token, timeout=10.0)
        self.session_handle = res.get("session_handle")
        if not self.session_handle:
            raise RuntimeError("Failed to obtain session handle from CreateSession")

        print(f"[portal] Session created: {self.session_handle}")

        # Step 2: SelectDevices (RemoteDesktop: 1=keyboard, 2=pointer, 4=touchscreen -> 7)
        dev_token = f"rdp_dev_{uuid.uuid4().hex[:8]}"
        dev_options = {
            "handle_token": GLib.Variant("s", dev_token),
            "types": GLib.Variant("u", 7),
        }

        self.bus.call_sync(
            self.PORTAL_BUS_NAME,
            self.PORTAL_OBJECT_PATH,
            self.INTERFACE_REMOTE_DESKTOP,
            "SelectDevices",
            GLib.Variant("(oa{sv})", (self.session_handle, dev_options)),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        self._wait_for_response(dev_token, timeout=10.0)

        # Step 3: SelectSources (ScreenCast: 1=monitor, cursor_mode=2 embedded)
        # Note: RemoteDesktop sessions cannot persist across restarts (GNOME Wayland security restriction).
        src_token = f"rdp_src_{uuid.uuid4().hex[:8]}"
        src_options = {
            "handle_token": GLib.Variant("s", src_token),
            "types": GLib.Variant("u", 1),  # Monitor
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", 2),  # Embedded cursor
        }

        self.bus.call_sync(
            self.PORTAL_BUS_NAME,
            self.PORTAL_OBJECT_PATH,
            self.INTERFACE_SCREEN_CAST,
            "SelectSources",
            GLib.Variant("(oa{sv})", (self.session_handle, src_options)),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        self._wait_for_response(src_token, timeout=10.0)

        # Step 4: Start (prompts laptop user if restore token is not active)
        start_token = f"rdp_start_{uuid.uuid4().hex[:8]}"
        start_options = {
            "handle_token": GLib.Variant("s", start_token),
        }

        print("[portal] Requesting session start... (Approve dialog on laptop if prompted)")
        self.bus.call_sync(
            self.PORTAL_BUS_NAME,
            self.PORTAL_OBJECT_PATH,
            self.INTERFACE_REMOTE_DESKTOP,
            "Start",
            GLib.Variant("(osa{sv})", (self.session_handle, "", start_options)),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )

        start_res = self._wait_for_response(start_token, timeout=120.0)
        streams = start_res.get("streams", [])
        if not streams:
            raise RuntimeError("Portal Start succeeded but no video streams were returned")

        # First stream: (uint32 node_id, dict properties)
        first_stream = streams[0]
        self.stream_node_id = int(first_stream[0])
        props = first_stream[1]

        if "size" in props:
            self.stream_width = int(props["size"][0])
            self.stream_height = int(props["size"][1])
        else:
            self.stream_width = 1920
            self.stream_height = 1080

        # Save restore token if provided in stream props or results
        new_token = props.get("restore_token") or start_res.get("restore_token")
        if new_token and isinstance(new_token, str):
            self.restore_token = new_token
            save_restore_token(new_token)
            print("[portal] Saved session restore token for future passwordless approval.")

        print(
            f"[portal] Screen capture stream ready: node_id={self.stream_node_id}, "
            f"size={self.stream_width}x{self.stream_height}"
        )

        # Step 5: OpenPipeWireRemote
        pw_options = {}
        res_var, fd_list = self.bus.call_with_unix_fd_list_sync(
            self.PORTAL_BUS_NAME,
            self.PORTAL_OBJECT_PATH,
            self.INTERFACE_SCREEN_CAST,
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session_handle, pw_options)),
            GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )

        handle_index = res_var.unpack()[0]
        self.pipewire_fd = fd_list.get(handle_index)
        print(f"[portal] PipeWire remote connection obtained (fd={self.pipewire_fd})")

        return self.pipewire_fd, self.stream_node_id, self.stream_width, self.stream_height

    def notify_pointer_motion_absolute(self, x: float, y: float):
        """Move cursor to absolute coordinate (x, y) on the stream's logical canvas."""
        if not self.session_handle or self.stream_node_id is None:
            return

        if self._use_rel_fallback:
            if self._last_abs_pos:
                dx = x - self._last_abs_pos[0]
                dy = y - self._last_abs_pos[1]
                if abs(dx) > 0.1 or abs(dy) > 0.1:
                    self.notify_pointer_motion(dx, dy)
            self._last_abs_pos = (x, y)
            return

        # Upstream xdg-desktop-portal bug #2077 causes check_position() to reject
        # absolute pointer events on some Wayland streams with 'Invalid position (0)'.
        # We try absolute positioning; on error, we fall back to relative motion
        # so the laptop cursor tracks smoothly without repeating failing D-Bus calls.
        try:
            self.bus.call_sync(
                self.PORTAL_BUS_NAME,
                self.PORTAL_OBJECT_PATH,
                self.INTERFACE_REMOTE_DESKTOP,
                "NotifyPointerMotionAbsolute",
                GLib.Variant(
                    "(oa{sv}udd)",
                    (self.session_handle, {}, self.stream_node_id, float(x), float(y)),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            self._last_abs_pos = (x, y)
        except Exception:
            self._use_rel_fallback = True
            if self._last_abs_pos:
                dx = x - self._last_abs_pos[0]
                dy = y - self._last_abs_pos[1]
                if abs(dx) > 0.1 or abs(dy) > 0.1:
                    self.notify_pointer_motion(dx, dy)
            self._last_abs_pos = (x, y)

    def notify_pointer_motion(self, dx: float, dy: float):
        """Move cursor relative to current position (trackpad mode)."""
        if not self.session_handle:
            return
        try:
            self.bus.call_sync(
                self.PORTAL_BUS_NAME,
                self.PORTAL_OBJECT_PATH,
                self.INTERFACE_REMOTE_DESKTOP,
                "NotifyPointerMotion",
                GLib.Variant(
                    "(oa{sv}dd)",
                    (self.session_handle, {}, float(dx), float(dy)),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except Exception as e:
            print(f"[portal] notify_pointer_motion error: {e}", file=sys.stderr)

    def notify_pointer_button(self, button: int, state: int):
        """
        Press or release pointer button.
        button: 272 (BTN_LEFT), 273 (BTN_RIGHT), 274 (BTN_MIDDLE)
        state: 1 = pressed, 0 = released
        """
        if not self.session_handle:
            return
        try:
            self.bus.call_sync(
                self.PORTAL_BUS_NAME,
                self.PORTAL_OBJECT_PATH,
                self.INTERFACE_REMOTE_DESKTOP,
                "NotifyPointerButton",
                GLib.Variant(
                    "(oa{sv}iu)",
                    (self.session_handle, {}, int(button), int(state)),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except Exception as e:
            print(f"[portal] notify_pointer_button error: {e}", file=sys.stderr)

    def notify_pointer_axis(self, dx: float, dy: float):
        """
        Scroll wheel / pointer axis movement.
        dx: horizontal scroll delta
        dy: vertical scroll delta
        """
        if not self.session_handle:
            return
        try:
            self.bus.call_sync(
                self.PORTAL_BUS_NAME,
                self.PORTAL_OBJECT_PATH,
                self.INTERFACE_REMOTE_DESKTOP,
                "NotifyPointerAxis",
                GLib.Variant(
                    "(oa{sv}dd)",
                    (self.session_handle, {}, float(dx), float(dy)),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except Exception as e:
            print(f"[portal] notify_pointer_axis error: {e}", file=sys.stderr)

    def notify_keyboard_keycode(self, keycode: int, state: int):
        """
        Press or release key by Linux evdev keycode.
        state: 1 = pressed, 0 = released
        """
        if not self.session_handle:
            return
        try:
            self.bus.call_sync(
                self.PORTAL_BUS_NAME,
                self.PORTAL_OBJECT_PATH,
                self.INTERFACE_REMOTE_DESKTOP,
                "NotifyKeyboardKeycode",
                GLib.Variant(
                    "(oa{sv}iu)",
                    (self.session_handle, {}, int(keycode), int(state)),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except Exception as e:
            print(f"[portal] notify_keyboard_keycode error: {e}", file=sys.stderr)

    def notify_keyboard_keysym(self, keysym: int, state: int):
        """
        Press or release key by XKB keysym.
        state: 1 = pressed, 0 = released
        """
        if not self.session_handle:
            return
        try:
            self.bus.call_sync(
                self.PORTAL_BUS_NAME,
                self.PORTAL_OBJECT_PATH,
                self.INTERFACE_REMOTE_DESKTOP,
                "NotifyKeyboardKeysym",
                GLib.Variant(
                    "(oa{sv}iu)",
                    (self.session_handle, {}, int(keysym), int(state)),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except Exception as e:
            print(f"[portal] notify_keyboard_keysym error: {e}", file=sys.stderr)

    def send_keycode_click(self, keycode: int):
        """Press and release a Linux evdev keycode with a short hold delay."""
        self.notify_keyboard_keycode(keycode, 1)
        time.sleep(0.015)
        self.notify_keyboard_keycode(keycode, 0)

    def send_key_click(self, keysym: int = None, keycode: int = None):
        """
        Press and release a key with debounce hold delay.
        Tries hardware keycode first if provided, falling back to keysym.
        """
        done = False
        if keycode is not None:
            try:
                self.send_keycode_click(keycode)
                done = True
            except Exception as e:
                print(f"[portal] send_keycode_click fallback: {e}", file=sys.stderr)
        if not done and keysym is not None:
            self.notify_keyboard_keysym(keysym, 1)
            time.sleep(0.015)
            self.notify_keyboard_keysym(keysym, 0)

    # Standard evdev keycodes for US layout: (evdev_code, requires_shift)
    ASCII_TO_EVDEV = {
        '\n': (28, False), '\r': (28, False),
        '\t': (15, False),
        '\b': (14, False),
        ' ': (57, False),
        '1': (2, False), '2': (3, False), '3': (4, False), '4': (5, False), '5': (6, False),
        '6': (7, False), '7': (8, False), '8': (9, False), '9': (10, False), '0': (11, False),
        '-': (12, False), '=': (13, False),
        'a': (30, False), 'b': (48, False), 'c': (46, False), 'd': (32, False), 'e': (18, False),
        'f': (33, False), 'g': (34, False), 'h': (35, False), 'i': (23, False), 'j': (36, False),
        'k': (37, False), 'l': (38, False), 'm': (50, False), 'n': (49, False), 'o': (24, False),
        'p': (25, False), 'q': (16, False), 'r': (19, False), 's': (31, False), 't': (20, False),
        'u': (22, False), 'v': (47, False), 'w': (17, False), 'x': (45, False), 'y': (21, False),
        'z': (44, False),
        '[': (26, False), ']': (27, False), ';': (39, False), "'": (40, False), '`': (41, False),
        '\\': (43, False), ',': (51, False), '.': (52, False), '/': (53, False),
        # Shifted characters
        '!': (2, True), '@': (3, True), '#': (4, True), '$': (5, True), '%': (6, True),
        '^': (7, True), '&': (8, True), '*': (9, True), '(': (10, True), ')': (11, True),
        '_': (12, True), '+': (13, True),
        'A': (30, True), 'B': (48, True), 'C': (46, True), 'D': (32, True), 'E': (18, True),
        'F': (33, True), 'G': (34, True), 'H': (35, True), 'I': (23, True), 'J': (36, True),
        'K': (37, True), 'L': (38, True), 'M': (50, True), 'N': (49, True), 'O': (24, True),
        'P': (25, True), 'Q': (16, True), 'R': (19, True), 'S': (31, True), 'T': (20, True),
        'U': (22, True), 'V': (47, True), 'W': (17, True), 'X': (45, True), 'Y': (21, True),
        'Z': (44, True),
        '{': (26, True), '}': (27, True), ':': (39, True), '"': (40, True), '~': (41, True),
        '|': (43, True), '<': (51, True), '>': (52, True), '?': (53, True),
    }

    def send_text(self, text: str):
        """Types string characters with accurate keycode and keysym injection."""
        KEY_LEFTSHIFT = 42
        for char in text:
            if char in self.ASCII_TO_EVDEV:
                keycode, shift = self.ASCII_TO_EVDEV[char]
                if shift:
                    self.notify_keyboard_keycode(KEY_LEFTSHIFT, 1)
                    time.sleep(0.005)
                self.send_keycode_click(keycode)
                if shift:
                    time.sleep(0.005)
                    self.notify_keyboard_keycode(KEY_LEFTSHIFT, 0)
            else:
                code = ord(char)
                if code < 0x100:
                    keysym = code
                else:
                    keysym = 0x01000000 + code
                self.send_key_click(keysym=keysym)
            time.sleep(0.008)

    def close(self):
        """Closes the RemoteDesktop portal session and cleans up file descriptors."""
        if self.session_handle:
            try:
                self.bus.call_sync(
                    self.PORTAL_BUS_NAME,
                    self.session_handle,
                    self.INTERFACE_SESSION,
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
            except Exception:
                pass
            self.session_handle = None

        if self.pipewire_fd is not None:
            try:
                os.close(self.pipewire_fd)
            except Exception:
                pass
            self.pipewire_fd = None


class MockPortalManager:
    """
    Mock Portal Manager for isolated sandbox testing.
    Does not interact with D-Bus or compositor, but logs all received touch/pointer/keyboard events.
    """

    def __init__(self, on_session_closed=None):
        self.stream_width = 1920
        self.stream_height = 1080
        self.stream_node_id = 999
        self.pipewire_fd = -1

    def start_session(self, force_reset_token: bool = False):
        print("[mock-portal] Sandbox session started: simulated 1920x1080 screen.")
        return -1, 999, 1920, 1080

    def notify_pointer_motion_absolute(self, x: float, y: float):
        print(f"[mock-portal] Pointer Absolute Move -> ({x:.1f}, {y:.1f})")

    def notify_pointer_motion(self, dx: float, dy: float):
        print(f"[mock-portal] Pointer Relative Move (Trackpad) -> dx={dx:.1f}, dy={dy:.1f}")

    def notify_pointer_button(self, button: int, state: int):
        name = "LEFT" if button == 272 else ("RIGHT" if button == 273 else str(button))
        action = "PRESS" if state == 1 else "RELEASE"
        print(f"[mock-portal] Pointer Button {name} -> {action}")

    def notify_pointer_axis(self, dx: float, dy: float):
        print(f"[mock-portal] Pointer Scroll Axis -> dx={dx:.2f}, dy={dy:.2f}")

    def notify_keyboard_keycode(self, keycode: int, state: int):
        action = "PRESS" if state == 1 else "RELEASE"
        print(f"[mock-portal] Keyboard Keycode {keycode} -> {action}")

    def notify_keyboard_keysym(self, keysym: int, state: int):
        action = "PRESS" if state == 1 else "RELEASE"
        print(f"[mock-portal] Keyboard Keysym {hex(keysym)} -> {action}")

    def send_keycode_click(self, keycode: int):
        print(f"[mock-portal] Keyboard Keycode Click -> {keycode}")

    def send_key_click(self, keysym: int = None, keycode: int = None):
        print(f"[mock-portal] Keyboard Key Click -> keysym={hex(keysym) if keysym else None}, keycode={keycode}")

    def send_text(self, text: str):
        print(f"[mock-portal] Text Injected -> {repr(text)}")

    def close(self):
        print("[mock-portal] Sandbox session closed.")
