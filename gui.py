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

from config import parse_args, get_or_create_auth_token, get_or_create_credentials
from main import RemoteDesktopApp, get_lan_ip, get_tailscale_ip


class PhoneRdpWindow(Adw.ApplicationWindow):
    def __init__(self, app, cli_args):
        super().__init__(application=app, title="Phone Remote Desktop")
        self.cli_args = cli_args
        self.auth_token = get_or_create_auth_token(cli_args.token)
        self.credentials, self.is_new_creds = get_or_create_credentials(cli_args.username, cli_args.password)
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

        # Credentials Group
        creds_group = Adw.PreferencesGroup()
        creds_group.set_title("Security & Credentials")

        # Username Row
        self.user_row = Adw.ActionRow()
        self.user_row.set_title("Username")
        self.user_row.set_subtitle(self.credentials.get("username", ""))
        creds_group.add(self.user_row)

        # Password Row
        self.pass_row = Adw.ActionRow()
        self.pass_row.set_title("Password")
        if self.is_new_creds and "initial_password" in self.credentials:
            self.pass_row.set_subtitle(f"Initial: {self.credentials['initial_password']}")
        else:
            self.pass_row.set_subtitle("••••••••")

        change_pw_btn = Gtk.Button()
        change_pw_btn.set_label("Change...")
        change_pw_btn.set_tooltip_text("Change login credentials")
        change_pw_btn.set_valign(Gtk.Align.CENTER)
        change_pw_btn.add_css_class("flat")
        change_pw_btn.connect("clicked", self._on_change_password_clicked)
        self.pass_row.add_suffix(change_pw_btn)
        creds_group.add(self.pass_row)

        # Assemble layout
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.append(header)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        main_box.append(self.qr_picture)
        main_box.append(pref_group)
        main_box.append(creds_group)
        scroll.set_child(main_box)

        content_box.append(scroll)
        self.set_content(content_box)

        # Generate QR code & start server
        self._update_qr_code(self.lan_url)
        self._start_rdp_server()

        self.connect("close-request", self._on_close_request)

    def _build_url(self) -> str:
        import socket
        scheme = "http" if self.cli_args.no_ssl else "https"
        hostname = socket.gethostname().split('.')[0]
        return f"{scheme}://{hostname}.local:{self.cli_args.port}/?token={self.auth_token}"

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

    def _on_change_password_clicked(self, btn):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Change Login Credentials",
            body="Set the username and password used to sign in from your phone.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_enabled("save", False)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        group = Adw.PreferencesGroup()

        user_entry = Adw.EntryRow(title="Username")
        user_entry.set_text(self.credentials.get("username", ""))

        new_pw_entry = Adw.PasswordEntryRow(title="New Password")
        confirm_pw_entry = Adw.PasswordEntryRow(title="Confirm Password")

        def check_valid(*args):
            u = user_entry.get_text().strip()
            p1 = new_pw_entry.get_text()
            p2 = confirm_pw_entry.get_text()
            valid = bool(u) and bool(p1) and (p1 == p2)
            dialog.set_response_enabled("save", valid)

        user_entry.connect("notify::text", check_valid)
        new_pw_entry.connect("notify::text", check_valid)
        confirm_pw_entry.connect("notify::text", check_valid)

        group.add(user_entry)
        group.add(new_pw_entry)
        group.add(confirm_pw_entry)

        dialog.set_extra_child(group)

        def on_response(diag, response_id):
            if response_id == "save":
                new_user = user_entry.get_text().strip()
                new_pw = new_pw_entry.get_text()
                if new_user and new_pw:
                    self._update_credentials(new_user, new_pw)

        dialog.connect("response", on_response)
        dialog.present()

    def _update_credentials(self, new_user: str, new_pw: str):
        from config import save_credentials
        self.credentials = save_credentials(new_user, new_pw)
        self.is_new_creds = False

        if self.rdp_app:
            self.rdp_app.update_credentials(self.credentials)

        self.user_row.set_subtitle(new_user)
        self.pass_row.set_subtitle("Password updated!")
        GLib.timeout_add_seconds(3, lambda: self.pass_row.set_subtitle("••••••••") or False)

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
