#!/bin/sh
# The only persistent boot entry. Extend the existing Cron contract, not firmware.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
BOOT_FLAG=/tmp/xiaoai_boot.done
LOCK=/tmp/xiaoai_boot.lock
LOG=/tmp/xiaoai_boot.log
BRIDGE_START=/data/xiaoai_bridge/start.sh
read UPTIME_SECONDS REST < /proc/uptime || exit 1
UPTIME_SECONDS=${UPTIME_SECONDS%%.*}
case "$UPTIME_SECONDS" in ''|*[!0-9]*) exit 1 ;; esac
[ "$UPTIME_SECONDS" -ge 90 ] || exit 0

# Kernel flock survives crashes without leaving a stale directory lock.
command -v flock >/dev/null 2>&1 || exit 1
exec 9> "$LOCK"
flock -n 9 || exit 0
# Runtime logs are bounded, and live only in /tmp.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 65536 ]; then mv -f "$LOG" "$LOG.1";fi
if [ -f "$BOOT_FLAG" ]; then
    # After boot, recover only the daemon. Respect an intentionally stopped
    # microphone and never repeat the Squeezelite/audio boot sequence.
    [ -x "$BRIDGE_START" ] || exit 0
    "$BRIDGE_START" status 9>&- >/dev/null 2>&1 && exit 0
    XIAOAI_SKIP_LISTENER_START=1 "$BRIDGE_START" start 9>&- >> "$LOG" 2>&1
    exit $?
fi
printf 'XiaoAI boot tasks: %s\n' "$(date)" >> "$LOG"
if pidof squeezelite >/dev/null 2>&1; then
    if ! /etc/init.d/squeezelite stop >> "$LOG" 2>&1; then
        printf '%s\n' 'ERROR: stop request failed; will retry' >> "$LOG";exit 1
    fi
    # procd stop returns before asynchronous exit. Wait; never kill unrelated services.
    count=0
    while pidof squeezelite >/dev/null 2>&1; do
        if [ "$count" -ge 15 ]; then
            printf '%s\n' 'ERROR: Squeezelite exit timed out; will retry' >> "$LOG";exit 1
        fi
        sleep 1;count=$((count+1))
    done
fi
printf '%s\n' 'Squeezelite inactive' >> "$LOG"
if [ -x "$BRIDGE_START" ]; then
    # Do not inherit the boot flock into a long-lived bridge daemon.
    if ! "$BRIDGE_START" 9>&- >> "$LOG" 2>&1; then
        printf '%s\n' 'ERROR: Bridge not ready; will retry' >> "$LOG";exit 1
    fi
else
    printf '%s\n' 'Bridge not installed; skipped' >> "$LOG"
fi
: > "$BOOT_FLAG"
printf '%s\n' 'Boot tasks completed' >> "$LOG"
