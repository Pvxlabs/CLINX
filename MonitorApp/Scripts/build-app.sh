#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
signing_identity="${CLINX_SIGNING_IDENTITY:-CLINX Monitor Local Development}"
if ! security find-certificate -c "$signing_identity" >/dev/null 2>&1; then
    echo 'Missing signing certificate. Run Scripts/setup-local-signing.sh once, or set CLINX_SIGNING_IDENTITY.' >&2
    exit 1
fi
swift build -c release
binary_dir="$(swift build -c release --show-bin-path)"
app_dir="$PWD/.build/CLINX Monitor.app"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
cp "$binary_dir/CLINXMonitor" "$app_dir/Contents/MacOS/CLINXMonitor"
cp Info.plist "$app_dir/Contents/Info.plist"

# Matching, narrowly scoped adapter source; secrets travel only on inherited pipes.
discovery_dir="$app_dir/Contents/Resources/Discovery"
mkdir -p "$discovery_dir/local_discovery"
cp ../local_discovery/*.py "$discovery_dir/local_discovery/"
cp ../execution_semantics.py "$discovery_dir/"

# Bundle the user-session node helper and the provider-neutral protocol. The
# helper is launched by the App's LaunchAgent controller; it is not a root
# daemon and is never sourced from the development checkout at runtime.
cp ../node_protocol.py "$app_dir/Contents/Resources/"
cp Scripts/node_service_entrypoint.py "$app_dir/Contents/Resources/"
cp Scripts/CLINXNodeService "$app_dir/Contents/Resources/CLINXNodeService"
chmod 755 "$app_dir/Contents/Resources/CLINXNodeService"

# App icon, drawn from the design's AppIcon spec. Generation is best-effort: a failure
# leaves the bundle on the default icon rather than breaking the build.
icon_path="$app_dir/Contents/Resources/AppIcon.icns"
echo "generating app icon…"
if swift Scripts/make-app-icon.swift "$icon_path" ".build/AppIcon-1024.png"; then
    :
else
    echo "warning: app icon generation failed; the bundle keeps the default icon" >&2
fi

# Keep the Keychain identity stable across rebuilds: certificate + bundle identifier,
# instead of the changing binary hash produced by ad-hoc signing.
codesign --force --sign "$signing_identity" --timestamp=none "$app_dir"
codesign --verify --strict "$app_dir"
echo "$app_dir"
