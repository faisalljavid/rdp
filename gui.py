#!/usr/bin/env python3
"""
Native GNOME / Libadwaita desktop application for Phone Remote Desktop.
Provides a modern visual window with toggle switch, QR code, and copyable connection link.
"""

import io
import sys
import threading
import asyncio
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf

import qrcode

from config import parse_args, get_or_create_auth_token
from main import RemoteDesktopApp, get_lan_ip, get_tailscale_ip


class PhoneRdpWindow(Adw.ApplicationWindow):
    def __init__(self, app, cli_args):
        super().__init__(application=app, title="Phone Remote Desktop")
        self.cli_args = cli_args
        self.auth_token = get_or_create_auth_token(cli_args.token)
        self.rdp_app = None
        self.rdp_thread = None
        self.rdp_loop = None

        self.set_default_size(440, 620)
        self.set_resizable(False)

        # HeaderBar
        header = Adw.HeaderBar()
        self.set_title("Phone Remote Desktop")

        # Main Box
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        main_box.set_margin_top(16)
        main_box.set_margin_bottom(24)
        main_box.set_margin_start(20)
        main_box.set_margin_end(20)

        # Status Banner
        self.status_page = Adw.StatusPage()
        self.status_page.set_title("Phone Remote Desktop")
        self.status_page.set_description("Scan the QR code from your phone camera to connect")
        self.status_page.set_icon_name("network-wireless-symbolic")

        # QR Code Display
        self.qr_picture = Gtk.Picture()
        self.qr_picture.set_can_shrink(False)
        self.qr_picture.set_size_request(240, 240)
        self.qr_picture.set_halign(Gtk.Align.CENTER)

        # Preferences Group (Connection details)
        pref_group = Adw.PreferencesGroup()
        pref_group.set_title("Connection Details")

        # URL Row with Copy Button
        self.url_row = Adw.ActionRow()
        self.url_row.set_title("Connection URL")
        self.lan_url = self._build_url()
        self.url_row.set_subtitle(self.lan_url)

        copy_btn = Gtk.Button()
        copy_btn.set_icon_name("edit-copy-symbolic")
        copy_btn.set_tooltip_text("Copy connection URL")
        copy_btn.set_valign(Gtk.Align.CENTER)
        copy_btn.connect("clicked", self._on_copy_clicked)
        self.url_row.add_suffix(copy_btn)
        pref_group.add(self.url_row)

        # Server Switch Row
        self.switch_row = Adw.SwitchRow()
        self.switch_row.set_title("Remote Access Server")
        self.switch_row.set_subtitle("Capturing screen and listening for phone input")
        self.switch_row.set_active(True)
        self.switch_row.connect("notify::active", self._on_switch_toggled)
        pref_group.add(self.switch_row)

        # Assemble layout
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.append(header)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        main_box.append(self.qr_picture)
        main_box.append(pref_group)
        scroll.set_child(main_box)

        content_box.append(scroll)
        self.set_content(content_box)

        # Generate QR code & start server
        self._update_qr_code(self.lan_url)
        self._start_rdp_server()

        self.connect("close-request", self._on_close_request)

    def _build_url(self) -> str:
        scheme = "http" if self.cli_args.no_ssl else "https"
        ip = get_lan_ip()
        return f"{scheme}://{ip}:{self.cli_args.port}/?token={self.auth_token}"

    def _update_qr_code(self, url: str):
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(url)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        stream = Gio.MemoryInputStream.new_from_data(png_bytes)
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        self.qr_picture.set_pixbuf(pixbuf)

    def _on_copy_clicked(self, btn):
        clipboard = self.get_display().get_clipboard()
        clipboard.set(self.lan_url)
        self.url_row.set_subtitle("Copied to clipboard!")
        GLib.timeout_add_seconds(2, lambda: self.url_row.set_subtitle(self.lan_url) or False)

    def _start_rdp_server(self):
        if self.rdp_thread and self.rdp_thread.is_alive():
            return

        def run_loop():
            self.rdp_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.rdp_loop)
            self.rdp_app = RemoteDesktopApp(self.cli_args)
            try:
                self.rdp_loop.run_until_complete(self.rdp_app.run())
            except Exception as e:
                print(f"[gui] Server loop exited: {e}", file=sys.stderr)

        self.rdp_thread = threading.Thread(target=run_loop, daemon=True, name="RdpServerThread")
        self.rdp_thread.start()

    def _stop_rdp_server(self):
        if self.rdp_app:
            self.rdp_app.shutdown()
        if self.rdp_loop and self.rdp_loop.is_running():
            self.rdp_loop.call_soon_threadsafe(self.rdp_loop.stop)
        self.rdp_app = None
        self.rdp_thread = None

    def _on_switch_toggled(self, switch_row, param):
        if switch_row.get_active():
            self._start_rdp_server()
            self.switch_row.set_subtitle("Capturing screen and listening for phone input")
        else:
            self._stop_rdp_server()
            self.switch_row.set_subtitle("Server stopped")

    def _on_close_request(self, window):
        self._stop_rdp_server()
        return False


class PhoneRdpApplication(Adw.Application):
    def __init__(self, cli_args):
        super().__init__(
            application_id="io.github.phoneremote.desktop",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.cli_args = cli_args

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = PhoneRdpWindow(self, self.cli_args)
        win.present()


def main():
    cli_args = parse_args()
    app = PhoneRdpApplication(cli_args)
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
