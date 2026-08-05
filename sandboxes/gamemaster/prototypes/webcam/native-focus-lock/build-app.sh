#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP="$ROOT/dist/Focus Lock.app"
MACOS="$APP/Contents/MacOS"
mkdir -p "$MACOS"

xcrun clang++ -std=c++17 -fobjc-arc -mmacosx-version-min=14.0 \
  -framework Foundation -framework AVFoundation -framework IOKit \
  "$ROOT/main.mm" -o "$MACOS/focus-lock"

cat >"$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>focus-lock</string>
  <key>CFBundleIdentifier</key><string>local.hhh.focus-lock</string>
  <key>CFBundleName</key><string>Focus Lock</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSCameraUsageDescription</key><string>Focus Lock adjusts UVC webcam focus for camera calibration.</string>
</dict></plist>
PLIST

codesign --force --sign - "$APP"
echo "Built $APP"
