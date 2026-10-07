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
app_dir="$PWD/.build/CLINX.app"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
cp "$binary_dir/CLINXMonitor" "$app_dir/Contents/MacOS/CLINXMonitor"
cp Info.plist "$app_dir/Contents/Info.plist"
cp ThirdPartyNotices.txt "$app_dir/Contents/Resources/ThirdPartyNotices.txt"

# Matching, narrowly scoped adapter source; secrets travel only on inherited pipes.
discovery_dir="$app_dir/Contents/Resources/Discovery"
mkdir -p "$discovery_dir/local_discovery"
cp ../local_discovery/*.py "$discovery_dir/local_discovery/"
cp ../execution_semantics.py "$discovery_dir/"

# Bundle the user-session node helper and the provider-neutral protocol. The
# helper is launched by the App's LaunchAgent controller; it is not a root
# daemon and is never sourced from the development checkout at runtime.
cp ../node_protocol.py "$app_dir/Contents/Resources/"
cp ../node_runtime.py "$app_dir/Contents/Resources/"
cp ../node_execution_adapter.py "$app_dir/Contents/Resources/"
cp ../native_history.py "$app_dir/Contents/Resources/"
cp ../network_observation.py ../observation_source.py "$app_dir/Contents/Resources/"
# Keep the canonical CLINX execution adapter self-contained in the installed
# helper. These modules are imported only when an explicit execution_config is
# approved; read-only nodes do not start them.
for canonical_module in \
    app_server.py bridge.py completion_runtime.py execution_liveness.py \
    execution_policy.py execution_semantics.py host_contract.py host_executor.py \
    m9_integration.py native_provider.py production_workflows.py result_ingestion.py \
    task_registry.py thread_identity.py tool_delivery.py; do
    cp "../$canonical_module" "$app_dir/Contents/Resources/"
done
cp Scripts/node_service_entrypoint.py "$app_dir/Contents/Resources/"
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
uv pip install --python "$runtime_dir/bin/python" -r ../requirements-discovery.txt maturin==1.9.6
mkdir -p .build/discovery-wheels
CARGO_PROFILE_RELEASE_DEBUG=1 CARGO_PROFILE_RELEASE_STRIP=none \
    "$runtime_dir/bin/maturin" build --release --locked --manifest-path ../native/opaque_pairing/Cargo.toml --out .build/discovery-wheels
uv pip install --python "$runtime_dir/bin/python" --reinstall .build/discovery-wheels/clinx_opaque-0.1.0-*-macosx_*.whl

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
