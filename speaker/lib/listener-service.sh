#!/bin/sh
set -eu
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
action=$1
voice_busy && exit 1
adapter="$BRIDGE_DIR/adapter/listener.sh"

# HA keeps explicit targets, while the native toggle owns mute sound/LEDs and
# procd startup. Internal start/stop/restart below remain silent.
if [ "${2:-}" = user ] && { [ "$action" = start ] || [ "$action" = stop ]; }; then
    listener_status() {
        if /etc/init.d/listener status >/dev/null 2>&1; then
            listener_running=true
        else
            listener_result=$?
            [ "$listener_result" -eq 1 ] || return 1
            listener_running=false
        fi
    }
    target=false;limit=15
    [ "$action" != start ] || { target=true;limit=${XIAOAI_LISTENER_WAIT:-90}; }
    listener_status || exit 1
    if [ "$listener_running" != "$target" ]; then
        if [ "$target" = true ] && flag '.enable_listener_adapter'; then
            # Native start loads /data/listener from porcupine_launcher. Refuse
            # a missing/late include instead of starting an unguarded Listener.
            [ -x "$adapter" ] && [ -r "$BRIDGE_DIR/lib/bootstrap.sh" ] || exit 1
            awk -v bridge=". $BRIDGE_DIR/lib/bootstrap.sh" '
                {sub(/[[:space:]]+$/, "")}
                $0==bridge {b=NR;bridges++}
                $0==". /data/kws_bootstrap.sh" {k=NR;guards++}
                END {exit !(bridges==1 && guards==1 && b<k)}
            ' /data/listener || exit 1
        fi
        # Do not inherit silent-maintenance flags into a user/physical-key action.
        (unset SILENT MUTE_LED; /etc/init.d/listener toggle) || exit 1
    fi
    count=0
    while :; do
        listener_status || exit 1
        if [ "$listener_running" = "$target" ]; then
            if [ "$target" = false ] || ! flag '.enable_listener_adapter'; then exit 0;fi
            native_listener_pid=''
            if hooks_ready &&
               { read -r native_listener_pid < /var/run/listener.pid || [ -n "$native_listener_pid" ]; } &&
               [ "$native_listener_pid" = "${hook_owner:-}" ]; then exit 0;fi
        fi
        [ "$count" -lt "$limit" ] || exit 1
        sleep 1;count=$((count+1))
    done
fi

wait_listener_exit() {
    count=0
    while /etc/init.d/listener status >/dev/null 2>&1; do
        [ "$count" -lt 15 ] || return 1
        sleep 1;count=$((count+1))
    done
}

if [ "$action" = stop ] || [ "$action" = restart ]; then
    SILENT=1 /etc/init.d/listener stop || exit 1
    wait_listener_exit || exit 1
    [ "$action" = stop ] && exit 0
fi
if flag '.enable_listener_adapter'; then
    [ -x "$adapter" ] || exit 1
    # Already running the reviewed sibling: do not restart the microphone.
    hooks_ready && exit 0
    if /etc/init.d/listener status >/dev/null 2>&1; then
        SILENT=1 /etc/init.d/listener stop || exit 1
        wait_listener_exit || exit 1
    fi
    command=$(jq -nc --arg launcher "$adapter" '{name:"listener",instances:{instance1:{command:["/bin/sh",$launcher],respawn:[3600,5,0],pidfile:"/var/run/listener.pid"}}}')
    ubus call service set "$command" >/dev/null || exit 1
else
    SILENT=1 /etc/init.d/listener start || exit 1
fi
count=0
while :; do
    if /etc/init.d/listener status >/dev/null 2>&1; then
        if ! flag '.enable_listener_adapter' || hooks_ready; then exit 0;fi
    fi
    [ "$count" -lt "${XIAOAI_LISTENER_WAIT:-90}" ] || exit 1
    sleep 1;count=$((count+1))
done
