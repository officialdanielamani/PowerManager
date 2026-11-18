#!/bin/bash
# Browser test of the hardware pages against a real server and the simulated multi-channel meter.
# Needs Python with server/requirements-dev.txt, Node.js and Playwright (npm i -g playwright; npx playwright install chromium).
#   server/tools/e2e/run.sh            (port 5055; PORT=... to change)
set -e
cd "$(dirname "$0")"
PORT=${PORT:-5055}
PY=${PYTHON:-python3}
W=$(mktemp -d)
trap 'kill $SERVER_PID $SIM_PID 2>/dev/null || true; rm -rf "$W"' EXIT
$PY dev_server.py "$W/data" "$PORT" > "$W/server.log" 2>&1 &
SERVER_PID=$!
for i in $(seq 1 30); do curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
$PY ../sim_meter_mc.py --state "$W/mc.json" pair --server "http://127.0.0.1:$PORT" --code ABCD2345 --channels 2
# --speed 0.2: the 10 s restart countdown takes 2 s; --bus: the addresses the simulated I2C bus answers
$PY ../sim_meter_mc.py --state "$W/mc.json" run --speed 0.2 --offset-w 6 --load 600,300 --bus 0x50,0x51,0x20,0x21,0x68 > "$W/sim.log" 2>&1 &
SIM_PID=$!
sleep 4
NODE_PATH=${NODE_PATH:-$(npm root -g)} node ui_smoke.js "http://127.0.0.1:$PORT" || { echo "--- server log"; tail -20 "$W/server.log"; echo "--- simulator log"; tail -20 "$W/sim.log"; exit 1; }
