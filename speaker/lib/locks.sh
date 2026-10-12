#!/bin/sh
# BusyBox flock has no -w. Keep the kernel lock on the caller's inherited FD.
# Poll once a second, with a monotonic deadline on Linux and a bounded fallback.
bridge_lock_for() (
    [ "$#" -eq 2 ] || exit 2
    case "$1" in ''|*[!0-9]*) exit 2 ;; esac
    case "$2" in [0-9]) ;; *) exit 2 ;; esac
    remaining=$1
    monotonic=false
    if [ -r /proc/uptime ] && read started rest < /proc/uptime;then
        deadline=$((${started%%.*}+remaining))
        fraction=${started#*.}
        monotonic=true
    fi
    while ! flock -x -n "$2"; do
        [ "$remaining" -gt 0 ] || exit 1
        sleep 1 || exit 1
        remaining=$((remaining-1))
        if [ "$monotonic" = true ];then
            read current rest < /proc/uptime || exit 1
            seconds=${current%%.*}
            if [ "$seconds" -gt "$deadline" ] || \
               { [ "$seconds" -eq "$deadline" ] && [ "${current#*.}" -ge "$fraction" ]; };then
                exit 1
            fi
        fi
    done
)

# The installed OAuth Cron already owns this mkdir lock before sourcing Listener.
# Cooperate with that 0.1.3 writer as well as the new config FD; never break an
# unowned/stale lock automatically or echo any authentication values.
bridge_oauth_enter() {
    BRIDGE_OAUTH_DIRECTORY=$1/ha_oauth_refresh.lock
    oauth_wait=${2:-35}
    while ! mkdir "$BRIDGE_OAUTH_DIRECTORY" 2>/dev/null; do
        [ "$oauth_wait" -gt 0 ] || return 1
        sleep 1;oauth_wait=$((oauth_wait-1))
    done
    printf '%s\n' "$$" > "$BRIDGE_OAUTH_DIRECTORY/bridge-owner"
}
bridge_oauth_leave() {
    [ -n "${BRIDGE_OAUTH_DIRECTORY:-}" ] || return 0
    oauth_owner=''
    [ ! -r "$BRIDGE_OAUTH_DIRECTORY/bridge-owner" ] || read oauth_owner < "$BRIDGE_OAUTH_DIRECTORY/bridge-owner"
    if [ "$oauth_owner" = "$$" ]; then
        rm -f "$BRIDGE_OAUTH_DIRECTORY/bridge-owner"
        rmdir "$BRIDGE_OAUTH_DIRECTORY" 2>/dev/null || return 1
    fi
    BRIDGE_OAUTH_DIRECTORY=''
}
