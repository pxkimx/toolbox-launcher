#!/bin/bash
# Toolbox Launcher: double-click launcher. Starts the app (if it is not already running) and opens it in the browser.
# The app quits by itself when its browser tab is closed (launcher.py, Hub.watch); this script then ends and
# the icon leaves the Dock. If this script is quit or force-quit from the Dock, the app notices and stops too.
APPDIR="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
PORT="${TOOLBOX_LAUNCHER_PORT:-8773}"     # not $PORT: that name is common, and an inherited value would move the app
URL="http://localhost:$PORT"
LOG="$HOME/Library/Logs/toolbox-launcher.log"
alert() { osascript -e "display alert \"Toolbox Launcher\" message \"$1\"" >/dev/null 2>&1; }
ours() { curl -s -m 1 "$URL/api/ping" 2>/dev/null | grep -q toolbox-launcher; }

if ours; then open "$URL"; exit 0; fi
# only a program *listening* on the port: plain "lsof -ti tcp:$PORT" also matches a browser's end of an old connection
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  alert "Port $PORT is used by another program. Quit it and open Toolbox Launcher again."
  exit 1
fi

PY=""
# any Python 3.9+ will do: the app uses the standard library only
for c in /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 /opt/anaconda3/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$c" ] && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null && { PY="$c"; break; }
done
[ -n "$PY" ] || { alert "Python 3.9 or newer was not found on this Mac."; exit 1; }

mkdir -p "$(dirname "$LOG")"
cd "$APPDIR" || { alert "The app's files are missing: $APPDIR"; exit 1; }
TOOLBOX_LAUNCHER_APP_PID=$$ "$PY" launcher.py --port "$PORT" > /dev/null 2>> "$LOG" &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT INT TERM
for _ in $(seq 1 40); do
  if ours; then
    open "$URL"
    wait $PID          # the icon stays in the Dock while the app runs, and leaves when it quits
    exit 0
  fi
  kill -0 $PID 2>/dev/null || break
  sleep 0.25
done
alert "Toolbox Launcher could not start. Details are in ~/Library/Logs/toolbox-launcher.log"
exit 1
