# Wayland Remote Desktop (Fedora GNOME)

A minimal, secure personal remote desktop solution designed specifically for Fedora GNOME Wayland. Control your laptop screen from your smartphone browser over your local network—with native touch gestures, virtual keyboard, Libadwaita desktop app, and zero external cloud dependencies.

```
       Laptop (Fedora GNOME Wayland)                          Phone (Browser / PWA)
┌──────────────────────────────────────────────┐       ┌─────────────────────────────────┐
│  Native Desktop GUI (Libadwaita / GTK4)      │       │  HTML5 Canvas Rendering         │
│  • QR Code display & Server toggle switch    │       │  • Hardware-accelerated 30+ FPS │
│  • In-app Password Manager                   │       │  • createImageBitmap renderer   │
├──────────────────────────────────────────────┤       ├─────────────────────────────────┤
│  org.freedesktop.portal.RemoteDesktop        │       │  Touch Gesture Engine           │
│  (ScreenCast + Input Injection via Gio D-Bus)│       │  • Tap = Left Click             │
│                     │                        │       │  • Long press = Right Click     │
│                     ▼                        │  LAN  │  • 1-Finger = Move Cursor       │
│  PipeWire fd & node_id                       │ / WSS │  • 2-Finger = Smooth Scroll     │
│         │                                    │───────┼──• Direct Touch / Trackpad mode │
│         ▼                                    │       ├─────────────────────────────────┤
│  GStreamer Pipeline                          │       │  Virtual Keyboard & Shortcuts   │
│  (pipewiresrc -> jpegenc -> appsink)         │       │  • Esc, Tab, Ctrl, Alt, Super   │
│                     │                        │       │  • Ctrl+C, Ctrl+V, Quick Text   │
│                     ▼                        │       ├─────────────────────────────────┤
│  Async WebSocket & HTTPS Server              │◄──────┼── Local Credential Login        │
│  • Static PWA & Input Routing                │       │  • Mobile Password Autofill     │
│  • Salted scrypt password hashing (auth.json)│       │  • Biometric Unlock (Face/Touch)│
│  • IP rate limiting & session tokens         │       │  • In-app Password Change (⚙️)  │
└──────────────────────────────────────────────┘       └─────────────────────────────────┘
```

---

## 1. Architecture Highlights

1. **Wayland Portal Integration ([`portal.py`](file:///home/faisalljavid/projects/rdp/portal.py))**:
   Uses `org.freedesktop.portal.RemoteDesktop` combined with `org.freedesktop.portal.ScreenCast` via Gio D-Bus (`python3-gobject`). Prompts once for permissions on first launch and reuses `restore_token` for seamless future runs.
2. **Video Capture & Encoding ([`pipeline.py`](file:///home/faisalljavid/projects/rdp/pipeline.py))**:
   Connects to PipeWire via GStreamer `pipewiresrc` using the portal file descriptor and stream node ID. Delivers buffers via `appsink` with frame-dropping backpressure to guarantee zero latency accumulation.
3. **Async Server & Network Transport ([`server.py`](file:///home/faisalljavid/projects/rdp/server.py))**:
   Runs a unified HTTPS and WSS server using `websockets` on port 8443 (or `--no-ssl` for Tailscale). Serves the PWA client, verifies credentials, issues persistent session tokens, and enforces IP rate limiting against brute force.
4. **Local Authentication Engine ([`config.py`](file:///home/faisalljavid/projects/rdp/config.py))**:
   Passwords are stored locally in `~/.config/rdp/auth.json` with `0600` permissions using salted `scrypt` hashing (Python standard library `hashlib.scrypt`). No third-party accounts, external servers, or databases.
5. **Native Desktop GUI ([`gui.py`](file:///home/faisalljavid/projects/rdp/gui.py))**:
   A native Fedora GNOME Libadwaita desktop application with visual QR code display, server toggle switch, and interactive credential management dialog.
6. **Mobile Touch Client & PWA ([`static/`](file:///home/faisalljavid/projects/rdp/static/))**:
   Zero-install client featuring hardware-accelerated canvas rendering, letterboxing-aware absolute coordinate translation, on-screen special keys (`Esc`, `Tab`, `Ctrl`, `Alt`, `Super`, shortcuts), mobile keyboard input, and mobile password manager integration.

---

## 2. Fedora System Requirements & Installation

Install all required Fedora packages:

```bash
sudo dnf install -y \
  python3 \
  python3-pip \
  python3-gobject \
  gstreamer1 \
  gstreamer1-plugins-base \
  gstreamer1-plugins-good \
  pipewire-gstreamer \
  xdg-desktop-portal \
  xdg-desktop-portal-gnome \
  python3-cryptography \
  python3-websockets \
  python3-pillow \
  libadwaita
```

Install Python requirements:

```bash
pip install -r requirements.txt
```

---

## 3. Firewall Configuration (Fedora)

Fedora enables `firewalld` by default in the `FedoraWorkstation` zone. Open port `8443` for your local Wi-Fi / LAN network:

```bash
sudo firewall-cmd --zone=FedoraWorkstation --add-port=8443/tcp --permanent
sudo firewall-cmd --reload
```

To verify:
```bash
sudo firewall-cmd --list-ports
```

---

## 4. How to Run

### Method A: Native Desktop App (Recommended for Everyday Use)

Install the application into your GNOME App Grid:

```bash
./install-desktop.sh
```

Now search for **"Phone Remote Desktop"** in your GNOME application menu or press `Super` and launch it.

The desktop app provides:
- **Visual QR Code**: Point your phone camera at the screen to open the URL.
- **Server Toggle**: Turn remote desktop capture and input listening on/off anytime.
- **Connection URL**: Click the copy icon to copy your LAN URL.
- **In-App Password Manager**: Click **"Change..."** to update your username or password in a native GNOME dialog without touching the terminal.

---

### Method B: Terminal Command

From the project directory:

```bash
python3 main.py
```

#### CLI Flags & Options:
| Flag | Description | Default |
|---|---|---|
| `--set-password` | Interactively set web login username and password | — |
| `--username <str>` | Set or update web login username | Current OS user |
| `--password <str>` | Set or update web login password | Auto-generated on 1st run |
| `--no-ssl` | Disable HTTPS/WSS (plain HTTP/WS, ideal for Tailscale) | False (HTTPS active) |
| `--port <int>` | Port to listen on | `8443` |
| `--fps <int>` | Target video framerate | `30` |
| `--quality <int>` | JPEG compression quality (1-100) | `70` |
| `--scale <float>` | Video scale factor (e.g. `0.75` or `0.5` for 4K displays) | `1.0` |
| `--reset-portal` | Clear saved restore token to force re-selection of screen | False |
| `--mock` | Run in mock sandbox mode (bouncing ball pattern, simulated portal) | False |

---

## 5. Authentication & In-App Password Management

Access to your laptop is protected by a local authentication system:

- **Local Storage**: Credentials are stored in `~/.config/rdp/auth.json` with user-only permissions (`0600`). Passwords are protected by cryptographic salted `scrypt` hashing with constant-time verification.
- **Brute-Force Protection**: Any IP with 5 consecutive failed login attempts is automatically blocked for 60 seconds.
- **Passwordless Reconnects**: Once you log in from your phone, a cryptographic session token is saved in `localStorage`. Subsequent connections log in immediately without prompting you to re-type credentials.
- **Mobile Password Manager Integration**: The login form uses standard `autocomplete="username"` and `autocomplete="current-password"`, allowing Apple Keychain, Google Password Manager, and Samsung Pass to save and autofill credentials using **fingerprint or FaceID**.

### 3 Ways to Change Your Password:

1. **In the Desktop App**: Click **Change...** in the *Security & Credentials* section of the desktop app. Enter your new password and click **Save**. The running server hot-reloads your credentials immediately.
2. **From Your Phone**: While connected in your phone browser, tap the **⚙️ (Settings)** icon in the top toolbar, type your new password, and tap **Save**.
3. **From the Terminal**: Run `python3 main.py --set-password` anytime.

---

## 6. Connecting from Your Phone

1. **Connect to the same Wi-Fi network** (or hotspot) as your laptop.
2. Open your phone browser and navigate to:
   ```
   https://<HOSTNAME>.local:8443/
   ```
   *(or using your laptop's LAN IP, e.g., `https://192.168.1.4:8443/`)*
3. **Save to Home Screen**:
   - In Safari (iOS): Tap **Share** -> **Add to Home Screen**.
   - In Chrome (Android): Tap **Menu (⋮)** -> **Add to Home screen** / **Install app**.
   - The app installs as a standalone full-screen PWA with custom app icon.
4. Sign in with your username and password.

> **Note on Self-Signed HTTPS Warnings**:  
> Because the server generates a self-signed TLS certificate for local network encryption, your browser will display a standard warning (`Your connection is not private`) on your first visit:
> - **iOS Safari**: Tap *Show Details* -> *visit this website*.
> - **Android Chrome**: Tap *Advanced* -> *Proceed to site (unsafe)*.
> - *Tip*: If you use **Tailscale**, run with `--no-ssl` to connect over plain HTTP with zero certificate warnings.

---

## 7. Touch Gestures & Virtual Keyboard

| Action | Phone Gesture / Control | Host Wayland Event |
|---|---|---|
| **Left Click** | Tap | `NotifyPointerMotionAbsolute` + `NotifyPointerButton(272, click)` |
| **Right Click** | Long press (hold finger 500ms) | `NotifyPointerMotionAbsolute` + `NotifyPointerButton(273, click)` + haptic vibration |
| **Move Cursor** | 1-Finger Drag | `NotifyPointerMotionAbsolute(x, y)` |
| **Scroll** | 2-Finger Drag (up / down / left / right) | `NotifyPointerAxis(dx, dy)` |
| **Trackpad Mode** | Tap `👆 Direct` in toolbar to switch to `🖱️ Trackpad` | `NotifyPointerMotion(dx, dy)` (relative trackpad motion) |
| **Special Keys** | Tap `⌨️` in toolbar | On-screen bar with `Esc`, `Tab`, `Ctrl`, `Alt`, `Super`, `Enter`, `Del`, arrows |
| **Latched Modifiers** | Tap `Ctrl`, `Alt`, or `Super` | Modifiers latch active for the next key click (e.g. `Ctrl` + `c`) |
| **Type / Paste Text** | Type into quick input row and tap **Send** | Translates UTF-8 text to keysyms and simulates keystrokes |
| **Native Phone Keyboard** | Tap inside quick text row or tap **📱 Keyboard** | Native phone virtual keyboard opens for typing |
| **Settings / Password** | Tap `⚙️` in toolbar | Opens in-app modal to change login password |
| **Fullscreen** | Tap `⛶` in toolbar | Enters browser fullscreen mode |

---

## 8. Background Autostart (systemd User Service)

To run the remote desktop server automatically whenever you log into your laptop:

1. Run the service installer:
   ```bash
   ./install-service.sh
   ```

2. Enable and start the service:
   ```bash
   systemctl --user enable --now rdp.service
   ```

3. Manage the service:
   ```bash
   # Check status
   systemctl --user status rdp.service

   # View live logs
   journalctl --user -u rdp.service -f

   # Stop service
   systemctl --user stop rdp.service
   ```

---

## 9. Testing in Sandbox Modes

You can test the entire application without affecting your real desktop or mouse pointer:

### Built-in Mock Sandbox (Instant)
```bash
python3 main.py --mock --no-ssl
```
- **Video**: GStreamer feeds an animated bouncing ball test pattern with a live clock overlay at 30 FPS.
- **Input**: Touch taps, drags, scrolls, and keystrokes from your phone are logged to the terminal rather than injected into your desktop.
- **Safety**: 100% safe to test while doing normal work on your laptop.

---

## 10. Troubleshooting

### 1. "Site Can't Be Reached" from Phone
- **Firewall**: Make sure port `8443` is open (`sudo firewall-cmd --zone=FedoraWorkstation --add-port=8443/tcp --permanent && sudo firewall-cmd --reload`).
- **Same Network**: Verify your phone and laptop are connected to the exact same Wi-Fi router or phone mobile hotspot.
- **AP Isolation**: Some public or guest Wi-Fi networks block devices from talking to each other. If so, connect your laptop to your phone's personal hotspot.

### 2. Black Screen on Phone
- Check if GNOME is showing a top-bar screen-sharing indicator or a dialog asking to share your screen. Click **Share** / **Allow**.
- Verify GStreamer PipeWire plugin is installed: `gst-inspect-1.0 pipewiresrc`.

### 3. Screen Locked / Sleep Disconnect
- When the laptop locks or displays sleep, PipeWire pauses frame delivery and GNOME closes the portal session.
- The server automatically detects this and re-establishes the portal session as soon as the laptop is unlocked, without requiring a server restart.

---

## 11. Future Scope & Roadmap

The following enhancements are planned for future versions to expand capabilities beyond local network usage:

### 🌐 1. Worldwide Remote Access (Beyond Local Wi-Fi)
- **Tailscale Mesh Integration**:
  - Connect to your laptop from anywhere in the world (cellular data, coffee shop, travel) without opening router ports or exposing IP addresses to the public internet.
  - End-to-end encrypted WireGuard tunnel with zero certificate warnings (`--no-ssl` mode).
- **Cloudflare Zero Trust / Reverse Proxy Tunnel**:
  - Option to route via `cloudflared` for a custom domain (e.g. `rdp.yourdomain.com`) with automated Cloudflare SSL certificates.

### ⚡ 2. Hardware-Accelerated WebRTC Streaming
- **Ultra-Low Latency Video**:
  - Transition transport from JPEG-over-WebSocket to WebRTC (`webrtcbin` in GStreamer) with H.264, VP9, or AV1 hardware encoding (`vaapih264enc` / `nvh264enc`).
  - Achieves 60 FPS streaming with sub-50ms glass-to-glass latency, consuming 80% less bandwidth over mobile cellular connections.

### 🔊 3. System Audio Streaming
- Capture laptop system audio via PipeWire PulseAudio/ALSA sink.
- Stream synchronized live audio to the phone browser via the Web Audio API or WebRTC audio tracks.

### 🖥️ 4. Multi-Monitor Selection & Virtual Displays
- In multi-monitor desktop setups, provide a switcher in the phone toolbar to jump between displays.
- Support headless virtual displays (`gnome-kiosk` or headless Mutter screen creation) to use the phone as a secondary extended monitor.

### 📋 5. Bidirectional Clipboard Synchronization
- Integrate with Wayland clipboard portal (`org.freedesktop.portal.Clipboard`) and the browser `navigator.clipboard` API.
- Seamlessly copy text or URLs on your laptop and paste on your phone, and vice versa.

### 🔍 6. Pinch-to-Zoom & Pan Navigation
- Add two-finger pinch-to-zoom gestures on mobile.
- Allows zooming into small terminal text, code editors, or browser tabs with 1:1 crisp rendering, followed by two-finger pan navigation.

### ⚡ 7. Wake-on-LAN (WoL) & Remote Power Controls
- Support sending Wake-on-LAN magic packets to wake the laptop from sleep remotely.
- Provide secure lock, suspend, and shutdown controls directly from the phone web interface.
