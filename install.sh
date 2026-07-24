#!/usr/bin/env bash
# Installs ClaudePulse to autostart on login for the current user.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$REPO_DIR/claude_pulse.py"
AUTOSTART_DIR="$HOME/.config/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/claude-pulse.desktop"

chmod +x "$SCRIPT_PATH"
mkdir -p "$AUTOSTART_DIR"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=ClaudePulse
Comment=Claude Pro usage in the system tray
Exec=$(command -v python3) $SCRIPT_PATH
Icon=utilities-system-monitor-symbolic
Terminal=false
Categories=Utility;
X-GNOME-Autostart-enabled=true
EOF

echo "Installed autostart entry: $DESKTOP_FILE"
echo "It will launch automatically on your next login."
echo "To start it right now: python3 $SCRIPT_PATH &"
