#!/bin/sh
# Build build/GoveeBLE.app: a tiny bundle that lets a child python use Bluetooth on macOS.
# The bundle is ad-hoc signed, so rebuilding it changes its identity and macOS forgets the
# Bluetooth grant; only rebuild when the launcher changes.
# See tools/ble_launcher.c for why. Run once; the first Bluetooth use shows a permission
# prompt for "GoveeBLE".
#
#   tools/build_ble_app.sh
#   tools/ble_run.sh .venv/bin/python -m govee_rt ble-scan
set -eu
cd "$(dirname "$0")/.."
APP=build/GoveeBLE.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.govee-rt.ble</string>
  <key>CFBundleName</key><string>GoveeBLE</string>
  <key>CFBundleExecutable</key><string>GoveeBLE</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>NSBluetoothAlwaysUsageDescription</key>
  <string>govee-rt talks to the Govee light strip over Bluetooth.</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>govee-rt listens to the computer's own audio (BlackHole) to sync the lights to music.</string>
</dict>
</plist>
PLIST
clang -O2 -Wall -o "$APP/Contents/MacOS/GoveeBLE" tools/ble_launcher.c
codesign --force --sign - "$APP"
echo "built $APP"
