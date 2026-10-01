#!/bin/sh
# Capture native on-screen chrome separately from the app's cacheDisplay render.
set -eu
cd "$(dirname "$0")/.."
capture_dir="${1:?pass a fresh capture directory}"
mkdir -p "$capture_dir"
capture_dir="$(cd "$capture_dir" && pwd)"
if [ -e "$capture_dir/ready.txt" ] || [ -e "$capture_dir/complete.txt" ]; then
    echo "Use a fresh directory so evidence is not overwritten." >&2
    exit 1
fi
./Scripts/build-app.sh
codesign --force --sign - '.build/CLINX Monitor.app'
open -n '.build/CLINX Monitor.app' --env CLINX_CAPTURE_DIR="$capture_dir" \
    --env CLINX_CAPTURE_GEOMETRY=1 --env CLINX_CAPTURE_HOLD=1
last_name=""
attempts=0
while [ ! -e "$capture_dir/complete.txt" ]; do
    if [ -e "$capture_dir/ready.txt" ]; then
        name="$(sed -n '1p' "$capture_dir/ready.txt")"
        window_id="$(sed -n '2p' "$capture_dir/ready.txt")"
        if [ "$name" != "$last_name" ] && [ -n "$window_id" ]; then
            screencapture -x -o -l "$window_id" "$capture_dir/$name-screen.png"
            touch "$capture_dir/continue.txt"
            last_name="$name"
            attempts=0
        fi
    fi
    attempts=$((attempts + 1))
    if [ "$attempts" -gt 600 ]; then
        echo "Timed out waiting for capture; inspect $capture_dir." >&2
        exit 1
    fi
    sleep 0.1
done
echo "$capture_dir"
