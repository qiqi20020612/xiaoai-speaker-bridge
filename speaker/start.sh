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
STARTUP_BUDGET=75
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/lib/locks.sh"
export XIAOAI_BRIDGE_DIR="$DIR" XIAOAI_BRIDGE_RUNTIME="$RUNTIME" XIAOAI_BRIDGE_CONFIG="$CONFIG"
case "$ACTION" in start|stop|status|restart) ;; *) exit 2 ;; esac
start_diagnostic() {
    # All callers supply fixed event names and numeric codes, never config/output.
    [ ! -L "$LOG" ] || return 1
    if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 65280 ];then
        mv -f "$LOG" "$LOG.1" || return 1
    fi
    printf 'time=%s start event=%s code=%s\n' "$(date +%s)" "$1" "$2" >> "$LOG"
}
start_failure() {
    start_diagnostic "$1" "$2" || true
    printf 'Bridge startup failed: %s (code=%s); inspect the runtime bridge log.\n' "$1" "$2" >&2
    return 1
}
startup_clock() {
    if [ -r /proc/uptime ] && read tick rest < /proc/uptime;then
        printf '%s\n' "${tick%%.*}"
    else
        date +%s
    fi
}
# Recovery can stop our process with a missing/broken config or binary.
if [ "$ACTION" != stop ];then
    [ -x "$BIN" ] || { start_failure missing_binary 1;exit 1; }
    [ -r "$CONFIG" ] || { start_failure missing_config 1;exit 1; }
    "$BIN" --check-config >/dev/null 2>&1 || { start_failure invalid_config 2;exit 1; }
fi
command -v flock >/dev/null 2>&1 || { start_failure missing_flock 1;exit 1; }
exec 9> "$LOCK"
bridge_lock_for 5 9 || { start_failure startup_lock_busy 1;exit 1; }

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
    # A Cron/start retry can arrive while the same process is still probing.
    child=$owner
    launched=false
    start_diagnostic waiting_existing_process 0
else
    start_diagnostic launching "$STARTUP_BUDGET"
    "$BIN" 9>&- </dev/null >> "$LOG" 2>&1 &
    child=$!
    launched=true
fi
deadline=$(($(startup_clock)+STARTUP_BUDGET))
last_health=-1
while :;do
    if "$BIN" --health >/dev/null 2>&1;then health=0;else health=$?;fi
    if ! kill -0 "$child" 2>/dev/null;then
        result=1
        if [ "$launched" = true ];then
            if wait "$child";then result=0;else result=$?;fi
        fi
        start_failure process_exited "$result";exit 1
    fi
    [ "$health" -ne 0 ] || break
    if [ "$health" -ne "$last_health" ];then
        case "$health" in
            3) start_diagnostic probe_pending "$health" ;;
            4) start_diagnostic probe_failed_retrying "$health" ;;
            5) start_diagnostic probe_retries_exhausted "$health" ;;
            *) start_diagnostic service_not_ready "$health" ;;
        esac
        last_health=$health
    fi
    if [ "$health" -eq 5 ] || [ "$(startup_clock)" -ge "$deadline" ];then
        # Stop only a process created here; a concurrent existing start is retained.
        [ "$launched" = false ] || kill -TERM "$child" 2>/dev/null || true
        case "$health" in
            3) reason=probe_pending_deadline ;;
            4|5) reason=probe_failed ;;
            *) reason=service_not_ready_deadline ;;
        esac
        start_failure "$reason" "$health";exit 1
    fi
    sleep 1
done
start_diagnostic ready 0
if jq -e '.enable_listener_adapter==true' "$CONFIG" >/dev/null 2>&1; then
    "$DIR/lib/listener-service.sh" start || { start_failure listener_adapter_failed 1;exit 1; }
fi
exit 0
