# Wayland Remote Desktop (Fedora GNOME)

A minimal personal remote desktop server that lets you control your Fedora Wayland laptop from your phone browser.

```
       Laptop (Fedora GNOME Wayland)                          Phone (Browser)
┌──────────────────────────────────────────────┐       ┌───────────────────────────┐
│  org.freedesktop.portal.RemoteDesktop        │       │  HTML5 Canvas             │
│  (ScreenCast + Input Injection via Gio D-Bus)│       │  Hardware accelerated     │
│                     │                        │       │  createImageBitmap (30fps)│
│                     ▼                        │       │             ▲             │
│  PipeWire fd & node_id                       │       │             │             │
│         │                                    │  LAN  │             │             │
│         ▼                                    │ / WSS │             │             │
│  GStreamer Pipeline                          │───────┼─────────────┘             │
│  (pipewiresrc -> jpegenc/vaapi -> appsink)   │       │                           │
│                     │                        │       │  Touch Gesture Engine     │
│                     ▼                        │       │  • Tap = Left Click       │
│  Async WebSocket & HTTP Server               │◄──────┼──• Long press = Right Click│
│  (Static Web App + Input Routing + Auth)     │       │  • 1-Finger = Move Cursor │
└──────────────────────────────────────────────┘       │  • 2-Finger = Scroll      │
                                                       │  • On-Screen Special Keys │
                                                       └───────────────────────────┘
```

---

## 1. Architecture Outline

1. **Wayland Portal Integration (`portal.py`)**:
   Uses `org.freedesktop.portal.RemoteDesktop` combined with `org.freedesktop.portal.ScreenCast` via Gio D-Bus (`python3-gobject`). This triggers a single permission prompt on the laptop for both video capture and input injection. Saves and reuses `restore_token` to bypass prompts on subsequent runs.
2. **Video Capture & Encoding (`pipeline.py`)**:
   Connects to PipeWire via GStreamer `pipewiresrc` using the portal file descriptor and stream node ID. Encodes frames on-the-fly (`jpegenc` or hardware encoders) and delivers buffers via `appsink` with frame-dropping backpressure to guarantee zero latency accumulation.
3. **Async Server & Network Transport (`server.py`)**:
   Runs a unified HTTPS and WSS server using `websockets` on port 8443 (or `--no-ssl` for Tailscale). Serves the touch client and streams video frames to the phone with token authentication and IP rate limiting.
4. **Mobile Web Client (`static/`)**:
   Zero-install client featuring hardware-accelerated canvas rendering, touch gesture mapping (letterboxing-aware absolute coordinate translation), on-screen special keys (`Esc`, `Tab`, `Ctrl`, `Alt`, `Super`, `Enter`, arrows), and text forwarding.

---

## 2. Fedora System Requirements & Installation

Run the following command on Fedora to install all required system packages:

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
  python3-pillow
```

Install Python dependencies (for terminal QR code generation):

```bash
pip install -r requirements.txt
```

---

## 3. Firewall Configuration (Fedora)

Fedora enables `firewalld` by default in the `FedoraWorkstation` zone. Open port `8443` for your local network:

```bash
sudo firewall-cmd --zone=FedoraWorkstation --add-port=8443/tcp --permanent
sudo firewall-cmd --reload
```

To verify:
```bash
sudo firewall-cmd --list-ports
```

---

## 4. One Command to Start

From the project directory:

```bash
python3 main.py
```

### Options:
- `--mock`: Run in **mock sandbox mode** (generates a live bouncing ball test video, simulates portal, logs touch & keys without touching real screen/cursor).
- `--no-ssl`: Disable HTTPS/WSS (use plain HTTP/WS, recommended when connecting over **Tailscale**).
- `--port 8443`: Change listening port (default: 8443).
- `--fps 30`: Target framerate (default: 30 FPS).
- `--quality 70`: JPEG compression quality (1-100, default: 70).
- `--scale 0.75`: Scale video resolution down (e.g. 0.75 for 1440p/4K laptop screens to save bandwidth while keeping cursor accuracy).
- `--token <secret>`: Specify a custom connection token (otherwise a secure random token is generated).
- `--reset-portal`: Clears saved portal restore token to force re-selecting which monitor to stream.

---

## 5. Connecting from Your Phone

1. When you run `python3 main.py`, the terminal displays:
   - Your LAN URL (e.g., `https://192.168.1.50:8443/?token=...`)
   - Your Tailscale URL (if Tailscale is active)
   - A **terminal QR code**.
2. **Scan the QR code** with your phone's camera app to open the URL directly.
3. **HTTPS Certificate Note**:
   - Because the host generates a self-signed TLS certificate for local encryption, your mobile browser will display a standard warning (`Your connection is not private`).
   - On iOS Safari: Tap **Show Details** -> **visit this website**.
   - On Android Chrome: Tap **Advanced** -> **Proceed to <ip> (unsafe)**.
   - *Pro-tip*: If you use **Tailscale**, run `python3 main.py --no-ssl` and connect using your phone on Tailscale over plain HTTP. Tailscale encrypts the connection with zero browser certificate warnings!

---

## 6. Touch Gestures & Controls

| Action | Phone Gesture | Wayland Host Event |
|---|---|---|
| **Left Click** | Tap | `NotifyPointerMotionAbsolute` + `NotifyPointerButton(272, click)` |
| **Right Click** | Long press (hold finger 500ms) | `NotifyPointerMotionAbsolute` + `NotifyPointerButton(273, click)` + haptic vibration |
| **Move Cursor** | 1-Finger Drag | `NotifyPointerMotionAbsolute(x, y)` |
| **Scroll** | 2-Finger Drag (up/down/left/right) | `NotifyPointerAxis(dx, dy)` |
| **Trackpad Mode** | Tap `👆 Direct` in toolbar to switch to `🖱️ Trackpad` | `NotifyPointerMotion(dx, dy)` (relative pointer movement) |
| **Special Keys** | Tap `⌨️` in toolbar | On-screen bar with `Esc`, `Tab`, `Ctrl`, `Alt`, `Super`, `Enter`, `Del`, arrows |
| **Latched Modifiers** | Tap `Ctrl`, `Alt`, or `Super` | Keys latch active so the next key click sends a shortcut (e.g. `Ctrl` + `c`) |
| **Type / Paste Text** | Type into quick input row and hit **Send** | Translates characters to XKB keysyms and types them on laptop |
| **Native Phone Keyboard**| Tap inside text box or tap `⌨️` | Native phone keyboard opens for typing |
| **Fullscreen** | Tap `⛶` in toolbar | Enters browser fullscreen mode |

---

## 7. Optional Autostart: systemd User Service

The host runs inside your graphical user session (it requires `DBUS_SESSION_BUS_ADDRESS` and `WAYLAND_DISPLAY`).

To run it as a background user service:

1. Copy the service file to your systemd user directory:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp rdp.service ~/.config/systemd/user/rdp.service
   ```

2. Reload and start:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now rdp.service
   ```

3. Check logs and status:
   ```bash
   systemctl --user status rdp.service
   journalctl --user -u rdp.service -f
   ```

---

## 8. Swapping Transport to WebRTC

The codebase is structured to make swapping to WebRTC straightforward:
- Video source frames originate in `pipeline.py`.
- To swap to WebRTC using GStreamer `webrtcbin`:
  1. In `pipeline.py`, replace `jpegenc ! appsink` with `vah264enc` (or `openh264enc` / `vp8enc`) `! rtph264pay ! webrtcbin name=webrtc`.
  2. In `server.py`, exchange SDP offers/answers and ICE candidates over the existing WebSocket connection (`ws`).
  3. In `app.js`, replace canvas frame drawing with an HTML5 `<video id="remote-video" autoplay playsinline>` element connected to a standard `RTCPeerConnection`.

The current JPEG-over-WebSocket pipeline was chosen as the default because it works on every mobile browser without ICE candidate negotiation or self-signed WebRTC certificate trust restrictions.

---

## 9. Running in a Sandbox / Test Environment

You can test the application in three different isolated environments:

### Method A: Built-in Mock Sandbox (Zero Setup, Instant)
To test the entire mobile web client, video stream latency, touch gestures, and keyboard controls **without** popping up GNOME permission dialogs or affecting your real laptop mouse/desktop:

```bash
python3 main.py --mock
```

- **Video**: GStreamer feeds an animated bouncing ball test pattern with a live clock overlay directly into the JPEG encoder at 30 FPS.
- **Input**: Touch taps, drags, scrolls, and keystrokes from your phone are logged to the host terminal in real time rather than injected into your desktop.
- **Safety**: 100% safe to test while doing normal work on your laptop.

### Method B: Container Sandbox (Podman / Toolbx)
To test in an isolated Linux container on Fedora without installing packages globally:

1. Create a Fedora toolbox container:
   ```bash
   toolbox create rdp-test
   toolbox enter rdp-test
   ```
2. Inside the toolbox, install dependencies and run:
   ```bash
   sudo dnf install -y python3-gobject gstreamer1-plugins-good pipewire-gstreamer python3-websockets python3-cryptography python3-pillow
   python3 main.py --mock
   ```
   *Note: Toolbx shares your session D-Bus and network by default.*

### Method C: Nested Wayland Compositor (Isolated Window)
If you want to test actual Wayland input injection without controlling your main laptop workspace:
1. Run a nested compositor window (e.g., Weston or Mutter):
   ```bash
   mutter --wayland --nested &
   # or: weston &
   ```
2. Set `WAYLAND_DISPLAY=wayland-1` and launch a target application inside it to receive the simulated input.

---

## 10. Troubleshooting

### 1. Black Screen on Phone
- **Check Portal Approval**: On your laptop, check if GNOME is showing a top-bar screen-sharing indicator or a dialog asking to share your screen. Click **Share** / **Allow**.
- **PipeWire Source Missing**: Verify GStreamer PipeWire plugin is installed:
  ```bash
  gst-inspect-1.0 pipewiresrc
  ```
  If missing: `sudo dnf install pipewire-gstreamer`.

### 2. Input Injection Not Working
- Ensure `SelectDevices` requested device types `7` (Keyboard + Pointer + Touchscreen).
- In GNOME 44+, check that your user account has permissions for remote desktop injection (standard on Fedora Workstation).
- Verify the portal returned a valid `stream_node_id` in terminal logs.

### 3. Portal Dialog Not Showing / Approval Issues
- If a stale portal session exists or you want to pick another screen:
  ```bash
  python3 main.py --reset-portal
  ```
  This clears `~/.config/rdp/restore_token` and forces GNOME to present the screen selection prompt.

### 4. High Latency or Stuttering
- **Laptop HiDPI / 4K Scaling**: 4K screens produce large frames. Run with scaling enabled:
  ```bash
  python3 main.py --scale 0.5 --quality 65
  ```
  This cuts bandwidth by 75% while keeping exact touch cursor accuracy.
- **Wi-Fi Congestion**: Connect your laptop to 5 GHz Wi-Fi or Ethernet.
- **Frame Drop**: The server automatically drops frames if the phone's network buffer is full. Look at the latency readout in the top toolbar.

### 5. Screen Locked or Sleep Disconnect
- When the laptop locks or displays turn off, PipeWire stops sending frames and GNOME closes the portal session.
- `main.py` detects this via the `Session.Closed` D-Bus signal and pipeline EOS. Once you unlock the laptop, the app automatically re-establishes the portal session using the saved `restore_token` without requiring a server restart.
