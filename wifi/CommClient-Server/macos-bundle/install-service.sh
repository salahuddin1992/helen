#!/usr/bin/env bash
# Helen-Server — macOS launchd service installer
# يجعل السيرفر يبدأ تلقائياً عند تشغيل النظام
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/local.helen.server.plist"

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.helen.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT_DIR/start.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/logs/launchd.err.log</string>
</dict>
</plist>
EOF

# unload لو سبق وحُمّل
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "[OK] Helen-Server installed as launchd service"
echo "     plist: $PLIST_PATH"
echo
echo "  للتحكم:"
echo "    launchctl start  local.helen.server"
echo "    launchctl stop   local.helen.server"
echo "    launchctl unload $PLIST_PATH    # uninstall"
