#!/usr/bin/env bash
# Install systemd user service for Phone Remote Desktop
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"

mkdir -p "${USER_SYSTEMD_DIR}"

cat <<EOF > "${USER_SYSTEMD_DIR}/rdp.service"
[Unit]
Description=Phone Remote Desktop Host (Fedora GNOME)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=${SCRIPT_DIR}/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=graphical-session.target
EOF

systemctl --user daemon-reload
echo "[✓] rdp.service installed to ${USER_SYSTEMD_DIR}/rdp.service"
echo ""
echo "To enable and start automatically on login, run:"
echo "  systemctl --user enable --now rdp.service"
echo ""
echo "To check status:"
echo "  systemctl --user status rdp.service"
echo ""
echo "To stop:"
echo "  systemctl --user stop rdp.service"
