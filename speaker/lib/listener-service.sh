#!/bin/sh
set -eu
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
action=$1
voice_busy && exit 1
adapter="$BRIDGE_DIR/adapter/listener.sh"

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
