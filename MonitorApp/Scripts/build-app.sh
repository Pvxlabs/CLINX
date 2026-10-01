#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
swift build -c release
binary_dir="$(swift build -c release --show-bin-path)"
app_dir="$PWD/.build/CLINX Monitor.app"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
cp "$binary_dir/CLINXMonitor" "$app_dir/Contents/MacOS/CLINXMonitor"
cp Info.plist "$app_dir/Contents/Info.plist"

# App icon, drawn from the design's AppIcon spec. Generation is best-effort: a failure
# leaves the bundle on the default icon rather than breaking the build.
icon_path="$app_dir/Contents/Resources/AppIcon.icns"
echo "generating app icon…"
if swift Scripts/make-app-icon.swift "$icon_path" ".build/AppIcon-1024.png"; then
    :
else
    echo "warning: app icon generation failed; the bundle keeps the default icon" >&2
fi

echo "$app_dir"
