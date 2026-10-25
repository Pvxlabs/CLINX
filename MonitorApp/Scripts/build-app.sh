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

# Copy the declared Helper protocol closure. Execution uses the paired Rust
# authority; the shared inventory rejects legacy business cores in both bundles.
python3 -B ../tools/build/runtime_sources.py --monitor-resources "$app_dir/Contents/Resources"
# Provider protocol, paired Node TLS/IPC, routing scope and request ledger run in Rust on this Mac.
# Build for the same native architecture as the Swift application; never copy
# a Linux release executable or inherit a stale helper from a previous bundle.
provider_target=$(rustc -vV | sed -n 's/^host: //p')
case "$provider_target" in
    aarch64-apple-darwin|x86_64-apple-darwin) ;;
    *) echo 'A native macOS Rust toolchain is required to build the Provider helper.' >&2; exit 1 ;;
esac
cargo build --release --locked --target "$provider_target" --manifest-path ../kernel/Cargo.toml --bin clinx-provider --bin clinx-node
provider_binary_dir=$(cargo metadata --no-deps --format-version 1 --manifest-path ../kernel/Cargo.toml | python3 -c 'import json,sys; print(json.load(sys.stdin)["target_directory"])')
for helper in clinx-provider clinx-node; do
    rm -f "$app_dir/Contents/Resources/$helper"
    cp "$provider_binary_dir/$provider_target/release/$helper" "$app_dir/Contents/Helpers/$helper"
    chmod 755 "$app_dir/Contents/Helpers/$helper"
    if [ "$helper" = clinx-node ]; then
        test "$("$app_dir/Contents/Helpers/$helper" --network-contract)" = RUST_NODE_NETWORK_V1
    fi
    codesign --force --sign "$signing_identity" --timestamp=none "$app_dir/Contents/Helpers/$helper"
    codesign --verify --strict "$app_dir/Contents/Helpers/$helper"
done
cp Scripts/CLINXNodeService "$app_dir/Contents/Resources/CLINXNodeService"
chmod 755 "$app_dir/Contents/Resources/CLINXNodeService"

# The node helper is launched from the installed bundle, so its interpreter
# and native discovery wheel must travel with the app.  Build this runtime in
# the bundle instead of depending on a checkout, PYTHONPATH, or user-global
# Python installation at runtime.
runtime_dir="$app_dir/Contents/Resources/DiscoveryRuntime"
if ! command -v uv >/dev/null 2>&1; then
    echo 'Missing uv; cannot build the bundled discovery runtime.' >&2
    exit 1
fi
if [ -d "$runtime_dir" ]; then
    chmod -R u+w "$runtime_dir"
fi
uv venv --clear --python 3.13 "$runtime_dir"
# uv intentionally symlinks the interpreter into a venv.  Symlinks escaping an
# app bundle are rejected by codesign, so materialize file links before the
# bundle is signed; the interpreter still uses the system Python framework,
# which is an OS dependency rather than a checkout dependency.
"${runtime_dir}/bin/python" - "$runtime_dir" <<'PY'
import os
import shutil
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
for path in sorted(root.rglob("*")):
    if not path.is_symlink():
        continue
    target = path.resolve()
    if not target.is_file():
        raise SystemExit(f"unsupported symlink in app runtime: {path}")
    temporary = path.with_name(path.name + ".materialized")
    shutil.copy2(target, temporary)
    os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
    path.unlink()
    temporary.replace(path)
PY
uv pip install --python "$runtime_dir/bin/python" -r ../requirements-discovery.txt
mkdir -p .build/discovery-wheels
CARGO_PROFILE_RELEASE_DEBUG=1 CARGO_PROFILE_RELEASE_STRIP=none \
    uv tool run --python 3.13 --from maturin==1.9.6 maturin build --release --locked \
    --interpreter "$runtime_dir/bin/python" --manifest-path ../native/opaque_pairing/Cargo.toml --out .build/discovery-wheels
uv pip install --python "$runtime_dir/bin/python" --reinstall .build/discovery-wheels/clinx_opaque-0.1.0-*-macosx_*.whl

# A signed runtime is not an installation target or a bytecode cache location.
chmod -R a-w "$runtime_dir"

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
