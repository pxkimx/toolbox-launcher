#!/bin/bash
# Build "Toolbox Launcher.app" in this tool's folder and copy it to ~/Desktop/Toolbox.
#   ./scripts/build_app.sh             build and copy to the Toolbox
#   TOOLBOX=/some/folder ./scripts/build_app.sh     copy somewhere else;  TOOLBOX= skips the copy
# The app holds its own copy of the code (Contents/Resources/app), so run this again after changing anything.
set -e
cd "$(dirname "$0")/.."
V="$(cat VERSION)"
APP="Toolbox Launcher.app"
TOOLBOX="${TOOLBOX-$HOME/Desktop/Toolbox}"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"
cp scripts/app_launcher.sh "$APP/Contents/MacOS/ToolboxLauncher"
chmod +x "$APP/Contents/MacOS/ToolboxLauncher"
cp assets/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
cp -R launcher.py tools.json VERSION README.md static "$APP/Contents/Resources/app/"
find "$APP" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Toolbox Launcher</string>
  <key>CFBundleDisplayName</key><string>Toolbox Launcher</string>
  <key>CFBundleIdentifier</key><string>com.paulhkim.toolbox-launcher</string>
  <key>CFBundleExecutable</key><string>ToolboxLauncher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>$V</string>
  <key>CFBundleShortVersionString</key><string>$V</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF
touch "$APP"
echo "built: $(pwd)/$APP (v$V)"
if [ -n "$TOOLBOX" ] && [ -d "$TOOLBOX" ]; then
  rm -rf "$TOOLBOX/$APP"
  ditto "$APP" "$TOOLBOX/$APP"
  touch "$TOOLBOX/$APP"
  echo "copied to: $TOOLBOX/$APP"
fi
