#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS_DIR="$HOME/.local/share/applications"
ICONS_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

mkdir -p "$APPS_DIR"
mkdir -p "$ICONS_DIR"

# Install icon
cp "$PROJECT_DIR/assets/phone-rdp.svg" "$ICONS_DIR/phone-rdp.svg"

# Install desktop file with absolute paths
sed -e "s|Exec=.*|Exec=$PROJECT_DIR/gui.py|" \
    "$PROJECT_DIR/phone-rdp.desktop" > "$APPS_DIR/phone-rdp.desktop"

chmod +x "$APPS_DIR/phone-rdp.desktop"
chmod +x "$PROJECT_DIR/gui.py"
chmod +x "$PROJECT_DIR/main.py"

# Update desktop database & icon cache
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR"
fi
if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
    gtk4-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

echo "Installed successfully!"
echo "You can now search for 'Phone Remote Desktop' in your GNOME App Grid."
