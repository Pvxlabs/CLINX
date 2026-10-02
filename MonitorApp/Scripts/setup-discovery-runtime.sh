#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
runtime="$HOME/Library/Application Support/CLINX Monitor/DiscoveryRuntime"
if [ ! -x "$runtime/bin/python" ]; then
    uv venv --python 3.13 "$runtime"
fi
uv pip install --python "$runtime/bin/python" -r requirements-discovery.txt maturin==1.9.6
# Xcode 27's linker can emit an unaligned LINKEDIT pool without debug information.
# Keep symbols at build time; do not patch the signed Mach-O after packaging.
CARGO_PROFILE_RELEASE_DEBUG=1 CARGO_PROFILE_RELEASE_STRIP=none \
    "$runtime/bin/maturin" build --release --locked --manifest-path native/opaque_pairing/Cargo.toml --out .validation/lan-pairing-wheels
uv pip install --python "$runtime/bin/python" --reinstall .validation/lan-pairing-wheels/clinx_opaque-0.1.0-*-macosx_*.whl
"$runtime/bin/python" -c 'from local_discovery.pairing import production_backend; production_backend()'
