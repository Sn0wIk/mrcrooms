#!/bin/bash
# Dev tunnel via localhost.run with auto-restart.
# Free anonymous tunnels die periodically; this loop restarts them.
# Current public URL is written to instance/tunnel_url.txt
set -u
cd "$(dirname "$0")/.."
URL_FILE=instance/tunnel_url.txt
LOG=instance/tunnel.log
KNOWN=instance/tunnel_known_hosts
PORT=${PORT:-8010}
mkdir -p instance

while :; do
    : > "$LOG"
    ssh -T \
        -o BatchMode=yes \
        -o IdentitiesOnly=yes \
        -o IdentityFile=none \
        -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="$KNOWN" \
        -o ServerAliveInterval=30 \
        -o ExitOnForwardFailure=yes \
        -o ConnectTimeout=15 \
        -R 80:127.0.0.1:"$PORT" \
        nokey@localhost.run >"$LOG" 2>&1 &
    SSH_PID=$!

    # wait for the domain line
    URL=""
    for _ in $(seq 1 15); do
        URL=$(grep -oE 'https://[a-z0-9]+\.lhr\.life' "$LOG" | head -1 || true)
        [ -n "$URL" ] && break
        sleep 1
    done
    echo "$URL" > "$URL_FILE"
    echo "[tunnel] ${URL:-<no url yet>} pid=$SSH_PID $(date '+%H:%M:%S')"

    # watch: ssh alive AND public health ok
    while kill -0 "$SSH_PID" 2>/dev/null; do
        sleep 20
        if [ -n "$URL" ] && ! curl -fsS -m 10 "$URL/health" >/dev/null 2>&1; then
            echo "[tunnel] health check failed, restarting $(date '+%H:%M:%S')"
            kill "$SSH_PID" 2>/dev/null
            break
        fi
    done
    wait "$SSH_PID" 2>/dev/null
    echo "[tunnel] reconnecting in 3s"
    sleep 3
done
