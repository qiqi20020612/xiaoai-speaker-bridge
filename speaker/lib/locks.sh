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
