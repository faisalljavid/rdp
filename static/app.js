/**
 * Wayland Remote Desktop Client
 * Hardware-accelerated Canvas rendering, touch gesture engine,
 * coordinate mapper, and virtual keyboard controls.
 */

(function () {
  // DOM Elements
  const canvas = document.getElementById("display-canvas");
  const ctx = canvas.getContext("2d", { alpha: false });
  const viewport = document.getElementById("viewport");
  const statusPill = document.getElementById("status-pill");
  const statusText = document.getElementById("status-text");
  const statsText = document.getElementById("stats-text");
  const touchIndicator = document.getElementById("touch-indicator");

  const btnMode = document.getElementById("btn-mode");
  const modeIcon = document.getElementById("mode-icon");
  const modeLabel = document.getElementById("mode-label");
  const btnKbd = document.getElementById("btn-kbd");
  const btnFullscreen = document.getElementById("btn-fullscreen");
  const keyboardDrawer = document.getElementById("keyboard-drawer");
  const quickTextInput = document.getElementById("quick-text-input");
  const btnSendText = document.getElementById("btn-send-text");
  const hiddenKeyInput = document.getElementById("hidden-key-input");

  const authModal = document.getElementById("auth-modal");
  const tokenInput = document.getElementById("token-input");
  const btnAuthSubmit = document.getElementById("btn-auth-submit");
  const authError = document.getElementById("auth-error");

  // State
  let ws = null;
  let authToken = null;
  let streamWidth = 1920;
  let streamHeight = 1080;
  let isConnected = false;
  let isTrackpadMode = false; // false = Direct Touch, true = Trackpad

  // Performance metrics
  let frameCount = 0;
  let fps = 0;
  let lastFpsUpdate = performance.now();
  let rttMs = 0;

  // Active modifier keys (latched)
  const activeModifiers = new Set();

  // Keysym mapping for common keys
  const KEYSYM_MAP = {
    Escape: 0xff1b,
    Tab: 0xff09,
    Return: 0xff0d,
    Enter: 0xff0d,
    BackSpace: 0xff08,
    Backspace: 0xff08,
    Delete: 0xffff,
    Control_L: 0xffe3,
    Control: 0xffe3,
    Alt_L: 0xffe9,
    Alt: 0xffe9,
    Super_L: 0xffeb,
    Meta: 0xffeb,
    Shift_L: 0xffe1,
    Shift: 0xffe1,
    Left: 0xff51,
    ArrowLeft: 0xff51,
    Up: 0xff52,
    ArrowUp: 0xff52,
    Right: 0xff53,
    ArrowRight: 0xff53,
    Down: 0xff54,
    ArrowDown: 0xff54,
    Home: 0xff50,
    End: 0xff57,
    Page_Up: 0xff55,
    PageUp: 0xff55,
    Page_Down: 0xff56,
    PageDown: 0xff56,
  };

  // --- Initialization & Auth ---
  function getQueryParam(name) {
    const params = new URLSearchParams(window.location.search);
    return params.get(name);
  }

  authToken = getQueryParam("token") || localStorage.getItem("rdp_auth_token");

  function saveToken(token) {
    authToken = token;
    localStorage.setItem("rdp_auth_token", token);
  }

  // --- WebSocket Connection ---
  function connect() {
    updateStatus("connecting", "Connecting...");

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    let url = `${protocol}//${host}/ws`;
    if (authToken) {
      url += `?token=${encodeURIComponent(authToken)}`;
    }

    try {
      ws = new WebSocket(url);
      ws.binaryType = "blob";
    } catch (e) {
      console.error("WebSocket connection error:", e);
      scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      // If we don't have a token, show prompt
      if (!authToken) {
        showAuthModal();
      }
      startPing();
    };

    ws.onmessage = async (evt) => {
      // Binary message: Video frame (JPEG blob)
      if (evt.data instanceof Blob) {
        handleVideoFrame(evt.data);
        return;
      }

      // JSON message
      try {
        const msg = JSON.parse(evt.data);
        handleJsonMessage(msg);
      } catch (err) {
        console.error("Invalid JSON message:", err);
      }
    };

    ws.onclose = (evt) => {
      isConnected = false;
      updateStatus("disconnected", "Disconnected");
      if (evt.code === 4003) {
        showAuthModal("Authentication failed or rate limited.");
      } else {
        scheduleReconnect();
      }
    };

    ws.onerror = () => {
      ws.close();
    };
  }

  let reconnectTimeout = null;
  function scheduleReconnect() {
    if (reconnectTimeout) return;
    reconnectTimeout = setTimeout(() => {
      reconnectTimeout = null;
      connect();
    }, 2000);
  }

  function startPing() {
    setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN && isConnected) {
        ws.send(JSON.stringify({ type: "ping", ts: performance.now() }));
      }
    }, 2000);
  }

  function handleJsonMessage(msg) {
    if (msg.type === "auth_result") {
      if (msg.success) {
        hideAuthModal();
        isConnected = true;
        updateStatus("connected", "Connected");
        if (msg.screen) {
          streamWidth = msg.screen.width || 1920;
          streamHeight = msg.screen.height || 1080;
          resizeCanvas();
        }
      } else {
        isConnected = false;
        showAuthModal(msg.error || "Authentication failed");
      }
    } else if (msg.type === "pong") {
      const now = performance.now();
      rttMs = Math.round(now - (msg.ts || now));
      updateStats();
    }
  }

  // --- Frame Rendering ---
  let isRendering = false;
  async function handleVideoFrame(blob) {
    frameCount++;
    const now = performance.now();
    if (now - lastFpsUpdate >= 1000) {
      fps = frameCount;
      frameCount = 0;
      lastFpsUpdate = now;
      updateStats();
    }

    if (isRendering) return; // Drop frame if previous draw call still in progress
    isRendering = true;

    try {
      if ("createImageBitmap" in window) {
        const bitmap = await createImageBitmap(blob);
        ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        bitmap.close();
      } else {
        // Fallback using Object URL
        const img = new Image();
        const url = URL.createObjectURL(blob);
        img.onload = () => {
          ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
          URL.revokeObjectURL(url);
        };
        img.src = url;
      }
    } catch (e) {
      console.warn("Frame render error:", e);
    } finally {
      isRendering = false;
    }
  }

  function resizeCanvas() {
    canvas.width = streamWidth;
    canvas.height = streamHeight;
  }

  // --- UI Updates ---
  function updateStatus(state, text) {
    statusPill.className = `status-pill ${state}`;
    statusText.textContent = text;
  }

  function updateStats() {
    statsText.textContent = `${fps} FPS • ${rttMs}ms`;
  }

  function showAuthModal(errMsg = "") {
    authModal.classList.remove("hidden");
    if (errMsg) {
      authError.textContent = errMsg;
      authError.classList.remove("hidden");
    } else {
      authError.classList.add("hidden");
    }
    tokenInput.focus();
  }

  function hideAuthModal() {
    authModal.classList.add("hidden");
  }

  btnAuthSubmit.addEventListener("click", () => {
    const val = tokenInput.value.trim();
    if (!val) return;
    saveToken(val);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "auth", token: val }));
    } else {
      connect();
    }
  });

  // --- Coordinate Mapping ---
  function mapClientToStreamCoords(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    const streamAspect = streamWidth / streamHeight;
    const viewAspect = rect.width / rect.height;

    let rw, rh, rx, ry;
    if (viewAspect > streamAspect) {
      // Pillarbox (bars on left & right)
      rh = rect.height;
      rw = rect.height * streamAspect;
      rx = (rect.width - rw) / 2;
      ry = 0;
    } else {
      // Letterbox (bars on top & bottom)
      rw = rect.width;
      rh = rect.width / streamAspect;
      rx = 0;
      ry = (rect.height - rh) / 2;
    }

    let localX = clientX - rect.left - rx;
    let localY = clientY - rect.top - ry;

    // Clamp within active image area
    localX = Math.max(0, Math.min(rw, localX));
    localY = Math.max(0, Math.min(rh, localY));

    const normX = localX / rw;
    const normY = localY / rh;

    return {
      x: normX * streamWidth,
      y: normY * streamHeight,
    };
  }

  function showRipple(clientX, clientY) {
    touchIndicator.style.left = `${clientX}px`;
    touchIndicator.style.top = `${clientY}px`;
    touchIndicator.classList.remove("active");
    // Trigger reflow
    void touchIndicator.offsetWidth;
    touchIndicator.classList.add("active");
    setTimeout(() => {
      touchIndicator.classList.remove("active");
    }, 250);
  }

  // --- Input Sending Helpers ---
  function sendJson(payload) {
    if (ws && ws.readyState === WebSocket.OPEN && isConnected) {
      ws.send(JSON.stringify(payload));
    }
  }

  function sendPointerMove(x, y) {
    sendJson({ type: "pointer_move", x, y });
  }

  function sendPointerButton(button, state) {
    sendJson({ type: "pointer_button", button, state });
  }

  function sendPointerAxis(dx, dy) {
    sendJson({ type: "pointer_axis", dx, dy });
  }

  function sendKeyClick(keysym) {
    sendJson({ type: "key_click", keysym });
  }

  // --- Touch Gesture Engine ---
  let touchStartTime = 0;
  let touchStartX = 0;
  let touchStartY = 0;
  let lastTouchX = 0;
  let lastTouchY = 0;
  let isDragging = false;
  let isLongPressed = false;
  let longPressTimer = null;

  // Two-finger scroll tracking
  let isTwoFinger = false;
  let initialMidX = 0;
  let initialMidY = 0;
  let lastMidX = 0;
  let lastMidY = 0;

  viewport.addEventListener(
    "touchstart",
    (e) => {
      e.preventDefault();

      if (e.touches.length === 1) {
        isTwoFinger = false;
        const touch = e.touches[0];
        touchStartTime = performance.now();
        touchStartX = touch.clientX;
        touchStartY = touch.clientY;
        lastTouchX = touch.clientX;
        lastTouchY = touch.clientY;
        isDragging = false;
        isLongPressed = false;

        const mapped = mapClientToStreamCoords(touch.clientX, touch.clientY);

        // Schedule long-press (Right Click)
        clearTimeout(longPressTimer);
        longPressTimer = setTimeout(() => {
          if (!isDragging && e.touches.length === 1) {
            isLongPressed = true;
            if (navigator.vibrate) navigator.vibrate(50);
            showRipple(touch.clientX, touch.clientY);
            // Right Click = BTN_RIGHT (273)
            if (!isTrackpadMode) {
              sendPointerMove(mapped.x, mapped.y);
            }
            sendPointerButton(273, 1);
            sendPointerButton(273, 0);
          }
        }, 500);

      } else if (e.touches.length === 2) {
        // Two-finger scroll start
        clearTimeout(longPressTimer);
        isTwoFinger = true;
        initialMidX = (e.touches[0].clientX + e.touches[1].clientX) / 2;
        initialMidY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
        lastMidX = initialMidX;
        lastMidY = initialMidY;
      }
    },
    { passive: false }
  );

  viewport.addEventListener(
    "touchmove",
    (e) => {
      e.preventDefault();

      if (e.touches.length === 1 && !isTwoFinger) {
        const touch = e.touches[0];
        const dist = Math.hypot(touch.clientX - touchStartX, touch.clientY - touchStartY);

        if (dist > 8) {
          isDragging = true;
          clearTimeout(longPressTimer);

          if (isTrackpadMode) {
            // Trackpad Mode: relative pointer movement
            const dx = (touch.clientX - lastTouchX) * 1.5;
            const dy = (touch.clientY - lastTouchY) * 1.5;
            // Send relative movement via axis or updated coordinates
            sendJson({ type: "pointer_move_relative", dx, dy });
          } else {
            // Direct Touch Mode: absolute cursor placement
            const mapped = mapClientToStreamCoords(touch.clientX, touch.clientY);
            sendPointerMove(mapped.x, mapped.y);
          }

          lastTouchX = touch.clientX;
          lastTouchY = touch.clientY;
        }

      } else if (e.touches.length === 2) {
        // Two-finger scroll
        clearTimeout(longPressTimer);
        const midX = (e.touches[0].clientX + e.touches[1].clientX) / 2;
        const midY = (e.touches[0].clientY + e.touches[1].clientY) / 2;

        const deltaX = (lastMidX - midX) * 0.8;
        const deltaY = (lastMidY - midY) * 0.8;

        if (Math.abs(deltaX) > 1 || Math.abs(deltaY) > 1) {
          sendPointerAxis(deltaX, deltaY);
          lastMidX = midX;
          lastMidY = midY;
        }
      }
    },
    { passive: false }
  );

  viewport.addEventListener(
    "touchend",
    (e) => {
      e.preventDefault();
      clearTimeout(longPressTimer);

      if (!isTwoFinger && !isLongPressed && !isDragging) {
        // Tap -> Left Click (BTN_LEFT = 272)
        showRipple(lastTouchX, lastTouchY);
        if (!isTrackpadMode) {
          const mapped = mapClientToStreamCoords(lastTouchX, lastTouchY);
          sendPointerMove(mapped.x, mapped.y);
        }
        sendPointerButton(272, 1);
        sendPointerButton(272, 0);
      }

      if (e.touches.length === 0) {
        isTwoFinger = false;
        isDragging = false;
        isLongPressed = false;
      }
    },
    { passive: false }
  );

  viewport.addEventListener("touchcancel", () => {
    clearTimeout(longPressTimer);
    isDragging = false;
    isLongPressed = false;
    isTwoFinger = false;
  });

  // --- Toolbar Controls ---
  btnMode.addEventListener("click", () => {
    isTrackpadMode = !isTrackpadMode;
    if (isTrackpadMode) {
      modeIcon.textContent = "🖱️";
      modeLabel.textContent = "Trackpad";
    } else {
      modeIcon.textContent = "👆";
      modeLabel.textContent = "Direct";
    }
  });

  btnKbd.addEventListener("click", () => {
    keyboardDrawer.classList.toggle("hidden");
    if (!keyboardDrawer.classList.contains("hidden")) {
      hiddenKeyInput.focus();
    }
  });

  btnFullscreen.addEventListener("click", () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
    } else {
      document.exitFullscreen().catch(() => {});
    }
  });

  // --- Special Keys & Keyboard Drawer ---
  document.querySelectorAll(".key-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const keyName = btn.getAttribute("data-key");
      const keysym = KEYSYM_MAP[keyName];
      if (!keysym) return;

      if (btn.classList.contains("mod-key")) {
        // Modifier key latching (Ctrl, Alt, Super)
        if (activeModifiers.has(keysym)) {
          activeModifiers.delete(keysym);
          btn.classList.remove("active");
          sendJson({ type: "key_up", keysym });
        } else {
          activeModifiers.add(keysym);
          btn.classList.add("active");
          sendJson({ type: "key_down", keysym });
        }
      } else {
        // Normal special key click
        sendKeyClick(keysym);

        // If modifiers were active, release them after key chord
        activeModifiers.forEach((modKeysym) => {
          sendJson({ type: "key_up", keysym: modKeysym });
        });
        activeModifiers.clear();
        document.querySelectorAll(".mod-key").forEach((m) => m.classList.remove("active"));
      }
    });
  });

  // Text send button
  btnSendText.addEventListener("click", () => {
    const text = quickTextInput.value;
    if (text) {
      sendJson({ type: "text", text });
      quickTextInput.value = "";
    }
  });

  quickTextInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      btnSendText.click();
    }
  });

  // Native phone keyboard interception via hidden input
  hiddenKeyInput.addEventListener("keydown", (e) => {
    const key = e.key;
    if (KEYSYM_MAP[key]) {
      e.preventDefault();
      sendKeyClick(KEYSYM_MAP[key]);
    } else if (key.length === 1) {
      e.preventDefault();
      sendJson({ type: "text", text: key });
    }
  });

  window.addEventListener("resize", resizeCanvas);

  // Auto connect on load
  connect();
})();
