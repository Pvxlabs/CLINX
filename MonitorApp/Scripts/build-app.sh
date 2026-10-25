#!/bin/sh
set -eu
export PYTHONDONTWRITEBYTECODE=1
cd "$(dirname "$0")/.."
signing_identity="${CLINX_SIGNING_IDENTITY:-CLINX Monitor Local Development}"
if ! security find-certificate -c "$signing_identity" >/dev/null 2>&1; then
    echo 'Missing signing certificate. Run Scripts/setup-local-signing.sh once, or set CLINX_SIGNING_IDENTITY.' >&2
    exit 1
fi
swift build -c release
binary_dir="$(swift build -c release --show-bin-path)"
app_dir="$PWD/.build/CLINX.app"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources" "$app_dir/Contents/Helpers"
cp "$binary_dir/CLINXMonitor" "$app_dir/Contents/MacOS/CLINXMonitor"
cp Info.plist "$app_dir/Contents/Info.plist"
cp ThirdPartyNotices.txt "$app_dir/Contents/Resources/ThirdPartyNotices.txt"

# Remove build-owned retired runtime files. The current App has no Python runtime.
python3 -B ../tools/build/runtime_sources.py --native-monitor-resources "$app_dir/Contents/Resources"
# Provider protocol, paired Node TLS/IPC, routing scope and request ledger run in Rust on this Mac.
# Build for the same native architecture as the Swift application; never copy
# a Linux release executable or inherit a stale helper from a previous bundle.
provider_target=$(rustc -vV | sed -n 's/^host: //p')
case "$provider_target" in
    aarch64-apple-darwin|x86_64-apple-darwin) ;;
    *) echo 'A native macOS Rust toolchain is required to build the Provider helper.' >&2; exit 1 ;;
esac
cargo build --release --locked --target "$provider_target" --manifest-path ../kernel/Cargo.toml --bin clinx-provider --bin clinx-node --bin clinx-node-daemon
provider_binary_dir=$(cargo metadata --no-deps --format-version 1 --manifest-path ../kernel/Cargo.toml | python3 -c 'import json,sys; print(json.load(sys.stdin)["target_directory"])')
for helper in clinx-provider clinx-node clinx-node-daemon; do
    rm -f "$app_dir/Contents/Resources/$helper"
    cp "$provider_binary_dir/$provider_target/release/$helper" "$app_dir/Contents/Helpers/$helper"
    chmod 755 "$app_dir/Contents/Helpers/$helper"
    if [ "$helper" = clinx-node ]; then
        test "$("$app_dir/Contents/Helpers/$helper" --network-contract)" = RUST_NODE_NETWORK_V1
        test "$("$app_dir/Contents/Helpers/$helper" --observation-contract)" = RUST_OBSERVATION_V1
        test "$("$app_dir/Contents/Helpers/$helper" --collector-contract)" = RUST_COLLECTOR_V1
        test "$("$app_dir/Contents/Helpers/$helper" --client-runtime-contract)" = RUST_CLIENT_RUNTIME_V1
        test "$("$app_dir/Contents/Helpers/$helper" --thin-runtime-contract)" = RUST_THIN_RUNTIME_V1
        test "$("$app_dir/Contents/Helpers/$helper" --device-runtime-contract)" = RUST_DEVICE_RUNTIME_V1
    fi
    if [ "$helper" = clinx-node-daemon ]; then
        test "$("$app_dir/Contents/Helpers/$helper" --daemon-contract)" = RUST_NODE_DAEMON_V1
    fi
    codesign --force --sign "$signing_identity" --timestamp=none "$app_dir/Contents/Helpers/$helper"
    codesign --verify --strict "$app_dir/Contents/Helpers/$helper"
done
cp Scripts/CLINXNodeService "$app_dir/Contents/Resources/CLINXNodeService"
chmod 755 "$app_dir/Contents/Resources/CLINXNodeService"

# OPAQUE enrollment, Ed25519 identity, DNS-SD and Collector now run in the
# signed Rust helpers. No Python interpreter, native wheel or pip install.

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
