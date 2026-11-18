#!/bin/sh
# Run mac-to-tv as a background service on macOS (a per-user launchd agent).
#
#   ./service.sh install [movies_folder]   start now, at every login, and restart if it crashes
#   ./service.sh uninstall                 stop and remove the service
#   ./service.sh status                    is it running?
#   ./service.sh logs                      follow server.log
#   ./service.sh plist [movies_folder]     print the launchd file without installing it
#
# While it runs, the server keeps the Mac awake when it is plugged in (it starts `caffeinate` itself). Nothing else on
# the Mac is changed: on battery, or with the lid closed, it sleeps as usual and the server pauses until it wakes.
set -eu

LABEL=com.mac-to-tv
DIR=$(cd "$(dirname "$0")" && pwd)
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TARGET="gui/$(id -u)"

plist() {
  python=$(command -v python3) || { echo "python3 not found" >&2; exit 1; }
  folder=""
  [ $# -gt 0 ] && folder="    <string>$(cd "$1" && pwd)</string>"
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$python</string><string>$DIR/server.py</string><string>--lan</string>
$folder
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <!-- launchd starts with a bare PATH: add Homebrew so yt-dlp and ffmpeg are found -->
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DIR/server.log</string>
  <key>StandardErrorPath</key><string>$DIR/server.log</string>
</dict></plist>
EOF
}

case "${1:-}" in
  install)
    shift
    mkdir -p "$HOME/Library/LaunchAgents"
    launchctl bootout "$TARGET/$LABEL" 2>/dev/null || true
    plist "$@" > "$PLIST"
    launchctl bootstrap "$TARGET" "$PLIST"
    sleep 3
    echo "Installed. It starts at login and restarts by itself. Addresses:"
    grep '→' "$DIR/server.log" | tail -3 || echo "(not started yet: ./service.sh logs)"
    ;;
  uninstall)
    launchctl bootout "$TARGET/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed."
    ;;
  status)
    launchctl print "$TARGET/$LABEL" 2>/dev/null | grep -E '^\s*(state|pid|last exit code) =' || echo "Not installed."
    ;;
  logs)
    tail -n 30 -f "$DIR/server.log"
    ;;
  plist)
    shift
    plist "$@"
    ;;
  *)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
