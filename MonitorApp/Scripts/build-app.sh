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
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
cp "$binary_dir/CLINXMonitor" "$app_dir/Contents/MacOS/CLINXMonitor"
cp Info.plist "$app_dir/Contents/Info.plist"
cp ThirdPartyNotices.txt "$app_dir/Contents/Resources/ThirdPartyNotices.txt"

# Copy the declared Helper protocol closure. Execution uses the paired Rust
# authority; the shared inventory rejects legacy business cores in both bundles.
python3 -B ../tools/build/runtime_sources.py --monitor-resources "$app_dir/Contents/Resources"
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
