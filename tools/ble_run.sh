#!/bin/sh
# Run a command under build/GoveeBLE.app so it may use Bluetooth, streaming its output here.
#   tools/ble_run.sh .venv/bin/python -m govee_rt ble-scan
# Ctrl-C stops the child cleanly (it gets SIGINT and runs its exit path).
set -eu
cd "$(dirname "$0")/.."
[ -d build/GoveeBLE.app ] || tools/build_ble_app.sh >/dev/null
prog=$1; shift
case "$prog" in /*) ;; *) prog="$PWD/$prog" ;; esac
log=$(mktemp -t govee-ble)
# -n: fresh instance each run; -W: wait for it to exit
open -W -n -a "$PWD/build/GoveeBLE.app" --stdout "$log" --stderr "$log" \
    --env GOVEE_RT_CWD="$PWD" --env PYTHONUNBUFFERED=1 --args "$prog" "$@" &
opener=$!
tail -n +1 -f "$log" &
tailer=$!
finish() { sleep 0.3; kill $tailer 2>/dev/null || true; rm -f "$log"; }
trap 'pkill -INT -f "GoveeBLE.app/Contents/MacOS/GoveeBLE $prog" 2>/dev/null || true; wait $opener 2>/dev/null || true; finish; exit 130' INT TERM
wait $opener || true
finish
