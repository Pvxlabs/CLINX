#!/bin/sh
# Compatibility command: build and validate the bundled Rust discovery helper.
set -eu
cd "$(dirname "$0")/../.."
exec sh MonitorApp/Scripts/build-app.sh
