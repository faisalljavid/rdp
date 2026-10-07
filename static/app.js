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
  const quickTextForm = document.getElementById("quick-text-form");
  const quickTextInput = document.getElementById("quick-text-input");
  const btnSendText = document.getElementById("btn-send-text");
  const btnSendEnter = document.getElementById("btn-send-enter");
  const btnFocusSoftKbd = document.getElementById("btn-focus-soft-kbd");
  const btnCtrlC = document.getElementById("btn-ctrl-c");
  const btnCtrlV = document.getElementById("btn-ctrl-v");
  const btnCtrlZ = document.getElementById("btn-ctrl-z");
  const hiddenKeyInput = document.getElementById("hidden-key-input");

  const authModal = document.getElementById("auth-modal");
  const loginForm = document.getElementById("login-form");
  const usernameInput = document.getElementById("username-input");
  const passwordInput = document.getElementById("password-input");
  const authError = document.getElementById("auth-error");

  const btnSettings = document.getElementById("btn-settings");
  const settingsModal = document.getElementById("settings-modal");
  const settingsForm = document.getElementById("settings-form");
  const settingsUsernameInput = document.getElementById("settings-username-input");
  const settingsNewPwInput = document.getElementById("settings-new-pw-input");
  const settingsConfirmPwInput = document.getElementById("settings-confirm-pw-input");
  const settingsError = document.getElementById("settings-error");
  const settingsSuccess = document.getElementById("settings-success");
  const btnSettingsCancel = document.getElementById("btn-settings-cancel");

  // State
  let ws = null;
  let authToken = null;
  let currentUsername = "";
  let pendingLogin = null;
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

  // Key mapping for special keys: keysym and Linux evdev keycode
  const KEY_INFO = {
    Escape: { keysym: 0xff1b, keycode: 1 },
    Tab: { keysym: 0xff09, keycode: 15 },
    Return: { keysym: 0xff0d, keycode: 28 },
    Enter: { keysym: 0xff0d, keycode: 28 },
    BackSpace: { keysym: 0xff08, keycode: 14 },
    Backspace: { keysym: 0xff08, keycode: 14 },
    Delete: { keysym: 0xffff, keycode: 111 },
    Control_L: { keysym: 0xffe3, keycode: 29 },
    Control: { keysym: 0xffe3, keycode: 29 },
    Alt_L: { keysym: 0xffe9, keycode: 56 },
    Alt: { keysym: 0xffe9, keycode: 56 },
    Super_L: { keysym: 0xffeb, keycode: 125 },
    Meta: { keysym: 0xffeb, keycode: 125 },
    Shift_L: { keysym: 0xffe1, keycode: 42 },
    Shift: { keysym: 0xffe1, keycode: 42 },
    Space: { keysym: 0x0020, keycode: 57 },
    " ": { keysym: 0x0020, keycode: 57 },
    Left: { keysym: 0xff51, keycode: 105 },
    ArrowLeft: { keysym: 0xff51, keycode: 105 },
    Up: { keysym: 0xff52, keycode: 103 },
    ArrowUp: { keysym: 0xff52, keycode: 103 },
    Right: { keysym: 0xff53, keycode: 106 },
    ArrowRight: { keysym: 0xff53, keycode: 106 },
    Down: { keysym: 0xff54, keycode: 108 },
    ArrowDown: { keysym: 0xff54, keycode: 108 },
    Home: { keysym: 0xff50, keycode: 102 },
    End: { keysym: 0xff57, keycode: 107 },
    Page_Up: { keysym: 0xff55, keycode: 104 },
    PageUp: { keysym: 0xff55, keycode: 104 },
    Page_Down: { keysym: 0xff56, keycode: 109 },
    PageDown: { keysym: 0xff56, keycode: 109 },
  };

  // --- Initialization & Auth ---
  function getQueryParam(name) {
    const params = new URLSearchParams(window.location.search);
    return params.get(name);
  }

  function saveToken(token) {
    if (!token) return;
    authToken = token;
    try {
      localStorage.setItem("rdp_auth_token", token);
    } catch (e) {}
  }

  const urlToken = getQueryParam("token");
  if (urlToken) {
    saveToken(urlToken);
  } else {
    try {
      authToken = localStorage.getItem("rdp_auth_token");
    } catch (e) {}
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
      if (pendingLogin) {
        ws.send(JSON.stringify({ type: "login", ...pendingLogin }));
        pendingLogin = null;
      } else if (authToken) {
        ws.send(JSON.stringify({ type: "auth", session_token: authToken }));
      } else {
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
        authToken = null;
        try { localStorage.removeItem("rdp_auth_token"); } catch (e) {}
        showAuthModal("Authentication failed or session expired.");
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
        if (msg.session_token) {
          saveToken(msg.session_token);
        }
        if (msg.username) {
          currentUsername = msg.username;
        }
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
        authToken = null;
        try { localStorage.removeItem("rdp_auth_token"); } catch (e) {}
        showAuthModal(msg.error || "Authentication failed");
      }
    } else if (msg.type === "change_password_result") {
      if (msg.success) {
        if (msg.session_token) {
          saveToken(msg.session_token);
        }
        if (msg.username) {
          currentUsername = msg.username;
        }
        if (settingsSuccess) {
          settingsSuccess.textContent = "Password updated successfully!";
          settingsSuccess.classList.remove("hidden");
        }
        if (settingsError) {
          settingsError.classList.add("hidden");
        }
        setTimeout(() => {
          hideSettingsModal();
        }, 1200);
      } else {
        if (settingsError) {
          settingsError.textContent = msg.error || "Failed to update password.";
          settingsError.classList.remove("hidden");
        }
        if (settingsSuccess) {
          settingsSuccess.classList.add("hidden");
        }
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
    if (usernameInput) {
      if (!usernameInput.value) {
        usernameInput.focus();
      } else if (passwordInput) {
        passwordInput.focus();
      }
    }
  }

  function hideAuthModal() {
    authModal.classList.add("hidden");
    if (passwordInput) {
      passwordInput.value = "";
    }
  }

  if (loginForm) {
    loginForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const username = usernameInput ? usernameInput.value.trim() : "";
      const password = passwordInput ? passwordInput.value : "";
      if (!username || !password) return;

      if (authError) {
        authError.classList.add("hidden");
      }

      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "login", username, password }));
      } else {
        pendingLogin = { username, password };
        connect();
      }
    });
  }

  // --- Settings / Change Password Modal ---
  function showSettingsModal() {
    if (!settingsModal) return;
    settingsModal.classList.remove("hidden");
    if (settingsError) settingsError.classList.add("hidden");
    if (settingsSuccess) settingsSuccess.classList.add("hidden");
    if (settingsUsernameInput) {
      settingsUsernameInput.value = currentUsername || (usernameInput ? usernameInput.value : "");
    }
    if (settingsNewPwInput) {
      settingsNewPwInput.value = "";
      settingsNewPwInput.focus();
    }
    if (settingsConfirmPwInput) {
      settingsConfirmPwInput.value = "";
    }
  }

  function hideSettingsModal() {
    if (settingsModal) {
      settingsModal.classList.add("hidden");
    }
    if (settingsSuccess) {
      settingsSuccess.classList.add("hidden");
    }
    if (settingsError) {
      settingsError.classList.add("hidden");
    }
  }

  if (btnSettings) {
    btnSettings.addEventListener("click", showSettingsModal);
  }

  if (btnSettingsCancel) {
    btnSettingsCancel.addEventListener("click", hideSettingsModal);
  }

  if (settingsForm) {
    settingsForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const user = settingsUsernameInput ? settingsUsernameInput.value.trim() : "";
      const p1 = settingsNewPwInput ? settingsNewPwInput.value : "";
      const p2 = settingsConfirmPwInput ? settingsConfirmPwInput.value : "";

      if (!p1) {
        if (settingsError) {
          settingsError.textContent = "Password cannot be empty.";
          settingsError.classList.remove("hidden");
        }
        return;
      }

      if (p1 !== p2) {
        if (settingsError) {
          settingsError.textContent = "Passwords do not match.";
          settingsError.classList.remove("hidden");
        }
        return;
      }

      if (settingsError) settingsError.classList.add("hidden");
      if (settingsSuccess) settingsSuccess.classList.add("hidden");

      if (ws && ws.readyState === WebSocket.OPEN && isConnected) {
        ws.send(JSON.stringify({
          type: "change_password",
          username: user,
          new_password: p1
        }));
      } else {
        if (settingsError) {
          settingsError.textContent = "Not connected to server.";
          settingsError.classList.remove("hidden");
        }
      }
    });
  }

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

  function sendKeyClick(keysym, keycode = null) {
    sendJson({ type: "key_click", keysym, keycode });
  }

  function sendKeyDown(keysym, keycode = null) {
    sendJson({ type: "key_down", keysym, keycode });
  }

  function sendKeyUp(keysym, keycode = null) {
    sendJson({ type: "key_up", keysym, keycode });
  }

  function sendText(text) {
    if (text) {
      sendJson({ type: "text", text });
    }
  }

  function sendKeyChord(modKeycode, modKeysym, keycode, keysym) {
    sendKeyDown(modKeysym, modKeycode);
    setTimeout(() => {
      sendKeyClick(keysym, keycode);
      setTimeout(() => {
        sendKeyUp(modKeysym, modKeycode);
      }, 20);
    }, 20);
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

  // --- Native Phone Keyboard State & Sentinel Management ---
  const DUMMY_SENTINEL = "   "; // 3 spaces sentinel to guarantee delete events on mobile keyboards
  let lastHiddenValue = DUMMY_SENTINEL;
  hiddenKeyInput.value = DUMMY_SENTINEL;

  function resetHiddenInput() {
    hiddenKeyInput.value = DUMMY_SENTINEL;
    lastHiddenValue = DUMMY_SENTINEL;
  }

  function focusSoftKeyboard() {
    resetHiddenInput();
    hiddenKeyInput.focus();
  }

  btnKbd.addEventListener("click", () => {
    const isHidden = keyboardDrawer.classList.contains("hidden");
    if (isHidden) {
      keyboardDrawer.classList.remove("hidden");
      btnKbd.classList.add("active");
      focusSoftKeyboard();
    } else {
      keyboardDrawer.classList.add("hidden");
      btnKbd.classList.remove("active");
      hiddenKeyInput.blur();
    }
  });

  if (btnFocusSoftKbd) {
    btnFocusSoftKbd.addEventListener("click", () => {
      focusSoftKeyboard();
    });
  }

  btnFullscreen.addEventListener("click", () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
    } else {
      document.exitFullscreen().catch(() => {});
    }
  });

  // --- Special Keys & Keyboard Drawer ---
  document.querySelectorAll(".key-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const keyName = btn.getAttribute("data-key");
      if (!keyName) return;

      const keyObj = KEY_INFO[keyName];
      if (!keyObj) return;

      const { keysym, keycode } = keyObj;

      if (btn.classList.contains("mod-key")) {
        // Modifier key latching (Ctrl, Alt, Super)
        if (activeModifiers.has(keysym)) {
          activeModifiers.delete(keysym);
          btn.classList.remove("active");
          sendKeyUp(keysym, keycode);
        } else {
          activeModifiers.add(keysym);
          btn.classList.add("active");
          sendKeyDown(keysym, keycode);
        }
      } else {
        // Normal special key click
        sendKeyClick(keysym, keycode);

        // If modifiers were active, release them after key chord
        activeModifiers.forEach((modKeysym) => {
          const modObj = Object.values(KEY_INFO).find((v) => v.keysym === modKeysym);
          sendKeyUp(modKeysym, modObj ? modObj.keycode : null);
        });
        activeModifiers.clear();
        document.querySelectorAll(".mod-key").forEach((m) => m.classList.remove("active"));
      }
    });
  });

  // Dedicated Shortcut Buttons
  if (btnCtrlC) {
    btnCtrlC.addEventListener("click", () => {
      sendKeyChord(29, 0xffe3, 46, 0x0063); // Ctrl + C
    });
  }
  if (btnCtrlV) {
    btnCtrlV.addEventListener("click", () => {
      sendKeyChord(29, 0xffe3, 47, 0x0076); // Ctrl + V
    });
  }
  if (btnCtrlZ) {
    btnCtrlZ.addEventListener("click", () => {
      sendKeyChord(29, 0xffe3, 44, 0x007a); // Ctrl + Z
    });
  }

  // --- Quick Text Form & Input ---
  function submitQuickText(andEnter = false) {
    const text = quickTextInput.value;
    if (text) {
      sendText(text);
      if (andEnter) {
        setTimeout(() => sendKeyClick(0xff0d, 28), 30);
      }
      quickTextInput.value = "";
    } else if (andEnter) {
      sendKeyClick(0xff0d, 28);
    }
  }

  if (quickTextForm) {
    quickTextForm.addEventListener("submit", (e) => {
      e.preventDefault();
      submitQuickText(false);
    });
  }

  if (btnSendText) {
    btnSendText.addEventListener("click", () => {
      submitQuickText(false);
    });
  }

  if (btnSendEnter) {
    btnSendEnter.addEventListener("click", () => {
      submitQuickText(true);
    });
  }

  quickTextInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submitQuickText(false);
    }
  });

  // --- Native Soft Keyboard Interception (Android & iOS) ---
  hiddenKeyInput.addEventListener("beforeinput", (e) => {
    if (e.inputType === "deleteContentBackward") {
      e.preventDefault();
      sendKeyClick(0xff08, 14); // Backspace
      resetHiddenInput();
      return;
    }
    if (e.inputType === "deleteContentForward") {
      e.preventDefault();
      sendKeyClick(0xffff, 111); // Delete
      resetHiddenInput();
      return;
    }
    if (e.inputType === "insertLineBreak" || e.inputType === "insertParagraph") {
      e.preventDefault();
      sendKeyClick(0xff0d, 28); // Enter
      resetHiddenInput();
      return;
    }
    if (e.data) {
      e.preventDefault();
      sendText(e.data);
      resetHiddenInput();
      return;
    }
  });

  hiddenKeyInput.addEventListener("input", (e) => {
    const currentVal = hiddenKeyInput.value;
    if (e.inputType === "deleteContentBackward" || currentVal.length < lastHiddenValue.length) {
      const deleteCount = Math.max(1, lastHiddenValue.length - currentVal.length);
      for (let i = 0; i < deleteCount; i++) {
        sendKeyClick(0xff08, 14);
      }
    } else if (currentVal.length > lastHiddenValue.length) {
      const inserted = currentVal.slice(lastHiddenValue.length);
      if (inserted) {
        sendText(inserted);
      }
    } else if (e.data) {
      sendText(e.data);
    }
    resetHiddenInput();
  });

  hiddenKeyInput.addEventListener("compositionend", (e) => {
    if (e.data) {
      sendText(e.data);
    }
    resetHiddenInput();
  });

  hiddenKeyInput.addEventListener("keydown", (e) => {
    const key = e.key;
    // IME keypresses on mobile virtual keyboards (Android) emit keyCode 229 or "Unidentified"
    if (key === "Unidentified" || e.keyCode === 229) {
      return; // Handled by beforeinput / input
    }

    if (key === "Backspace") {
      e.preventDefault();
      sendKeyClick(0xff08, 14);
      resetHiddenInput();
    } else if (key === "Enter") {
      e.preventDefault();
      sendKeyClick(0xff0d, 28);
      resetHiddenInput();
    } else if (key === "Tab") {
      e.preventDefault();
      sendKeyClick(0xff09, 15);
      resetHiddenInput();
    } else if (key === "Escape") {
      e.preventDefault();
      sendKeyClick(0xff1b, 1);
      resetHiddenInput();
    } else if (KEY_INFO[key]) {
      e.preventDefault();
      const { keysym, keycode } = KEY_INFO[key];
      sendKeyClick(keysym, keycode);
      resetHiddenInput();
    } else if (key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
      e.preventDefault();
      sendText(key);
      resetHiddenInput();
    }
  });

  window.addEventListener("resize", resizeCanvas);

  // Auto connect on load
  connect();
})();
