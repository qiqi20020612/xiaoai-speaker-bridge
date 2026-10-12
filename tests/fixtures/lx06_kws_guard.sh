#!/bin/sh
# Sourced by porcupine_launcher_kws.
# Supervisor for the Chinese KWS client.

KWS_GUARD_CHILD=""
KWS_GUARD_LOCK="/tmp/kws_guard.lock"

# Terminate all descendants, children first.
kws_guard_descendants() (
    parent="$1"

    for status in /proc/[0-9]*/status; do
        [ -r "$status" ] || continue

        ppid=$(awk '/^PPid:/ {print $2; exit}' "$status" 2>/dev/null)
        [ "$ppid" = "$parent" ] || continue

        child=${status#/proc/}
        child=${child%/status}

        kws_guard_descendants "$child"
        kill -TERM "$child" 2>/dev/null
    done
)

kws_guard_cleanup() {
    if [ -n "$KWS_GUARD_CHILD" ]; then
        echo "KWS guard: stopping process tree" >&2

        # Stop rec, sox, nc, timeout and their descendants.
        kws_guard_descendants "$KWS_GUARD_CHILD"

        # Then stop the client itself.
        kill -TERM "$KWS_GUARD_CHILD" 2>/dev/null

        sleep 1

        # Last resort if the client is still blocked.
        if kill -0 "$KWS_GUARD_CHILD" 2>/dev/null; then
            kws_guard_descendants "$KWS_GUARD_CHILD"

            cmd=$(tr '\0' ' ' \
                < "/proc/$KWS_GUARD_CHILD/cmdline" 2>/dev/null)

            case "$cmd" in
                *"/data/kws_client"*)
                    kill -KILL "$KWS_GUARD_CHILD" 2>/dev/null
                    ;;
            esac
        fi

        wait "$KWS_GUARD_CHILD" 2>/dev/null
        KWS_GUARD_CHILD=""
    fi

    rm -rf "$KWS_GUARD_LOCK"
}

kws_guard_exit() {
    trap - HUP INT TERM
    kws_guard_cleanup
    exit 143
}

kws_guard() {
    # Prevent a second supervisor from starting.
    if [ -d "$KWS_GUARD_LOCK" ]; then
        owner=$(cat "$KWS_GUARD_LOCK/owner" 2>/dev/null)

        case "$owner" in
            ''|*[!0-9]*) owner=0 ;;
        esac

        oldcmd=$(tr '\0' ' ' \
            < "/proc/$owner/cmdline" 2>/dev/null)

        case "$oldcmd" in
            *"/data/porcupine_launcher_kws"*)
                echo "KWS guard: another instance is active" >&2
                exit 125
                ;;
        esac

        # Previous supervisor is gone: remove stale lock.
        rm -rf "$KWS_GUARD_LOCK"
    fi

    mkdir "$KWS_GUARD_LOCK" 2>/dev/null || exit 125
    echo "$$" > "$KWS_GUARD_LOCK/owner"

    trap 'kws_guard_exit' HUP INT TERM

    /data/kws_client &
    KWS_GUARD_CHILD=$!

    # Shell builtin wait can be interrupted by TERM.
    wait "$KWS_GUARD_CHILD"
    result=$?

    KWS_GUARD_CHILD=""
    rm -rf "$KWS_GUARD_LOCK"

    trap - HUP INT TERM

    return "$result"
}
