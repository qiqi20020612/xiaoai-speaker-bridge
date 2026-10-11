#!/bin/sh
# Fixed interface: /data/xiaoai_bridge/start.sh [start|stop|status|restart]
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
DIR=${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}
RUNTIME=${XIAOAI_BRIDGE_RUNTIME:-/tmp}
CONFIG=${XIAOAI_BRIDGE_CONFIG:-$DIR/config.json}
BIN=$DIR/bin/xiaoai_bridge
PIDFILE=$RUNTIME/xiaoai_bridge.pid
LOG=$RUNTIME/xiaoai_bridge.log
LOCK=$RUNTIME/xiaoai_bridge.start.lock
ACTION=${1:-start}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/lib/locks.sh"
export XIAOAI_BRIDGE_DIR="$DIR" XIAOAI_BRIDGE_RUNTIME="$RUNTIME" XIAOAI_BRIDGE_CONFIG="$CONFIG"
case "$ACTION" in start|stop|status|restart) ;; *) exit 2 ;; esac
# Recovery can stop our process with a missing/broken config or binary.
if [ "$ACTION" != stop ];then
    [ -x "$BIN" ] && [ -r "$CONFIG" ] || exit 1
    "$BIN" --check-config || exit 1
fi
command -v flock >/dev/null 2>&1 || exit 1
exec 9> "$LOCK"
bridge_lock_for 5 9 || exit 1

owned_pid() {
    [ -r "$PIDFILE" ] || return 1
    read owner < "$PIDFILE" || return 1
    case "$owner" in ''|*[!0-9]*) return 1 ;; esac
    [ "$owner" -gt 1 ] && kill -0 "$owner" 2>/dev/null || return 1
    # PID reuse must never authorize stopping another process.
    if [ -L "/proc/$owner/exe" ]; then
        running=$(readlink "/proc/$owner/exe" 2>/dev/null || true)
        [ "$running" = "$BIN" ] || [ "$running" = "$BIN (deleted)" ] || return 1
    else
        cmd=$(ps -p "$owner" -o command= 2>/dev/null || true)
        case "$cmd" in "$BIN"|"$BIN "*) ;; *) return 1 ;; esac
    fi
}

stop_bridge() {
    owned_pid || { rm -f "$PIDFILE";return 0; }
    kill -TERM "$owner" || return 1
    count=0
    while owned_pid; do
        [ "$count" -lt 15 ] || return 1
        sleep 1;count=$((count+1))
    done
    rm -f "$PIDFILE"
}

case "$ACTION" in
    status) owned_pid && "$BIN" --health;exit $? ;;
    stop) stop_bridge;exit $? ;;
    restart) stop_bridge ;;
    start) ;;
    *) exit 2 ;;
esac
if owned_pid; then
    if "$BIN" --health; then
        if jq -e '.enable_listener_adapter==true' "$CONFIG" >/dev/null 2>&1; then
            "$DIR/lib/listener-service.sh" start || exit 1
        fi
        exit 0
    fi
    # A live unhealthy process is not replaced with a duplicate.
    exit 1
fi
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 65536 ]; then mv -f "$LOG" "$LOG.1";fi
"$BIN" 9>&- </dev/null >> "$LOG" 2>&1 &
child=$!
count=0
while ! "$BIN" --health >/dev/null 2>&1; do
    kill -0 "$child" 2>/dev/null || exit 1
    if [ "$count" -ge 10 ]; then kill -TERM "$child" 2>/dev/null || true;exit 1;fi
    sleep 1;count=$((count+1))
done
if jq -e '.enable_listener_adapter==true' "$CONFIG" >/dev/null 2>&1; then
    "$DIR/lib/listener-service.sh" start || exit 1
fi
exit 0
