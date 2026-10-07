#!/usr/bin/env python3
"""
Native GNOME / Libadwaita desktop application for Phone Remote Desktop.
Provides a modern visual window with toggle switch, QR code, and copyable connection link.
"""

import os
import io
import sys
import signal
import threading
import asyncio

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Adw, GLib, Gio, GdkPixbuf

import qrcode

from config import parse_args, get_or_create_auth_token, get_or_create_credentials
from main import RemoteDesktopApp, get_lan_ip


SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <method name="ProvideXdgActivationToken">
      <arg name="token" type="s" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
  </interface>
</node>
"""


class StatusNotifierTray:
    """
    Pure D-Bus StatusNotifierItem (org.kde.StatusNotifierItem) and DBusMenu
    (com.canonical.dbusmenu) implementation for top panel tray integration in GNOME / KDE.
    Avoids any GTK 3 dependencies so it works seamlessly inside GTK 4 / Libadwaita.
    """

    def __init__(self, app_id="phone-rdp", title="Phone Remote Desktop", icon_name="phone-rdp", on_show=None, on_quit=None):
        self.app_id = app_id
        self.title = title
        self.icon_name = icon_name
        self.on_show = on_show
        self.on_quit = on_quit
        self.is_registered = False

        self._bus_conn = None
        self._owner_id = None
        self._sni_reg_id = None
        self._menu_reg_id = None

        self._sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        self._menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]

        bus_name = f"org.freedesktop.StatusNotifierItem-{os.getpid()}-1"
        self._bus_name = bus_name
        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            bus_name,
            Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired,
            self._on_name_acquired,
            self._on_name_lost,
        )

    def _on_bus_acquired(self, conn, name):
        self._bus_conn = conn
        try:
            self._sni_reg_id = conn.register_object(
                "/StatusNotifierItem",
                self._sni_info,
                self._sni_method_call,
                self._sni_get_prop,
                None,
            )
            self._menu_reg_id = conn.register_object(
                "/MenuBar",
                self._menu_info,
                self._menu_method_call,
                self._menu_get_prop,
                None,
            )

            # Register with desktop StatusNotifierWatcher
            conn.call_sync(
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher",
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (name,)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            self.is_registered = True
        except Exception as e:
            err_str = str(e)
            if "ServiceUnknown" in err_str or "NameHasNoOwner" in err_str:
                self.is_registered = False
            else:
                # Registered with watcher even if watcher threw on signal broadcast
                self.is_registered = True

    def _on_name_acquired(self, conn, name):
        pass

    def _on_name_lost(self, conn, name):
        self.is_registered = False

    def _sni_method_call(self, connection, sender, object_path, interface_name, method_name, parameters, invocation):
        if method_name in ("Activate", "SecondaryActivate"):
            if self.on_show:
                GLib.idle_add(self.on_show)
        invocation.return_value(None)

    def _sni_get_prop(self, connection, sender, object_path, interface_name, property_name):
        props = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.app_id),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconThemePath": GLib.Variant("s", os.path.expanduser("~/.local/share/icons")),
            "Menu": GLib.Variant("o", "/MenuBar"),
            "ItemIsMenu": GLib.Variant("b", False),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionMovieName": GLib.Variant("s", ""),
        }
        return props.get(property_name)

    def _menu_method_call(self, connection, sender, object_path, interface_name, method_name, parameters, invocation):
        if method_name == "GetLayout":
            raw_item1 = (
                1,
                {"label": GLib.Variant("s", "Show Phone Remote Desktop"), "enabled": GLib.Variant("b", True)},
                [],
            )
            raw_item2 = (
                2,
                {"label": GLib.Variant("s", "Exit"), "enabled": GLib.Variant("b", True)},
                [],
            )
            raw_root = (
                0,
                {},
                [GLib.Variant("(ia{sv}av)", raw_item1), GLib.Variant("(ia{sv}av)", raw_item2)],
            )
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (1, raw_root)))
        elif method_name == "GetGroupProperties":
            items = [
                (1, {"label": GLib.Variant("s", "Show Phone Remote Desktop"), "enabled": GLib.Variant("b", True)}),
                (2, {"label": GLib.Variant("s", "Exit"), "enabled": GLib.Variant("b", True)}),
            ]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (items,)))
        elif method_name == "GetProperty":
            mid, prop_name = parameters.unpack()
            labels = {1: "Show Phone Remote Desktop", 2: "Exit"}
            if prop_name == "label" and mid in labels:
                invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", labels[mid]),)))
            elif prop_name == "enabled":
                invocation.return_value(GLib.Variant("(v)", (GLib.Variant("b", True),)))
            else:
                invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))
        elif method_name == "Event":
            mid, event_type, data, ts = parameters.unpack()
            if event_type == "clicked":
                if mid == 1 and self.on_show:
                    GLib.idle_add(self.on_show)
                elif mid == 2 and self.on_quit:
                    GLib.idle_add(self.on_quit)
            invocation.return_value(None)
        elif method_name == "EventGroup":
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method_name == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method_name == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _menu_get_prop(self, connection, sender, object_path, interface_name, property_name):
        props = {
            "Version": GLib.Variant("u", 3),
            "Status": GLib.Variant("s", "normal"),
            "TextDirection": GLib.Variant("s", "ltr"),
            "IconThemePath": GLib.Variant("as", []),
        }
        return props.get(property_name)

    def destroy(self):
        if self._bus_conn:
            if self._sni_reg_id:
                try:
                    self._bus_conn.unregister_object(self._sni_reg_id)
                except Exception:
                    pass
                self._sni_reg_id = None
            if self._menu_reg_id:
                try:
                    self._bus_conn.unregister_object(self._menu_reg_id)
                except Exception:
                    pass
                self._menu_reg_id = None
        if self._owner_id:
            try:
                Gio.bus_unown_name(self._owner_id)
            except Exception:
                pass
            self._owner_id = None
        self.is_registered = False


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
        creds_group.set_title("Security &amp; Credentials")

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

        # Tray Icon setup
        self.tray = StatusNotifierTray(
            app_id="phone-rdp",
            title="Phone Remote Desktop",
            icon_name=self._get_tray_icon(),
            on_show=self._show_window,
            on_quit=self._quit_application,
        )

        self.connect("close-request", self._on_close_request)

    def _build_url(self) -> str:
        scheme = "http" if self.cli_args.no_ssl else "https"
        lan_ip = get_lan_ip()
        if lan_ip and lan_ip != "127.0.0.1":
            return f"{scheme}://{lan_ip}:{self.cli_args.port}/?token={self.auth_token}"
        import socket
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

    def _get_tray_icon(self) -> str:
        system_icon = os.path.expanduser("~/.local/share/icons/hicolor/scalable/apps/phone-rdp.svg")
        if os.path.isfile(system_icon):
            return system_icon
        repo_icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "phone-rdp.svg")
        if os.path.isfile(repo_icon):
            return repo_icon
        return "phone-rdp"

    def _show_window(self):
        self.set_visible(True)
        self.present()

    def _quit_application(self):
        self._stop_rdp_server()
        if self.tray:
            self.tray.destroy()
            self.tray = None
        app = self.get_application()
        if app:
            app.quit()

    def _on_close_request(self, window):
        if self.tray and self.tray.is_registered:
            self.set_visible(False)
            return True
        self._quit_application()
        return False


class PhoneRdpApplication(Adw.Application):
    def __init__(self, cli_args):
        super().__init__(
            application_id="io.github.phoneremote.desktop",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.cli_args = cli_args
        self.main_window = None

    def do_activate(self):
        if not self.main_window:
            self.hold()
            self.main_window = PhoneRdpWindow(self, self.cli_args)
        self.main_window.set_visible(True)
        self.main_window.present()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    cli_args = parse_args()
    app = PhoneRdpApplication(cli_args)
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
