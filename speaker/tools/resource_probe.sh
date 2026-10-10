#!/bin/sh
# Read-only sampling: output goes to stdout. Redirect to a PRIVATE workstation file.
# No signal, service change, audio operation, or remote file write occurs.
SAMPLE_PID=${1:-}
SAMPLE_DURATION=${2:-1800}
SAMPLE_INTERVAL=${3:-2}
case "$SAMPLE_PID:$SAMPLE_DURATION:$SAMPLE_INTERVAL" in *[!0-9:]*|::*|*::*) exit 2 ;; esac
[ "$SAMPLE_PID" -gt 1 ] && [ "$SAMPLE_DURATION" -gt 0 ] && [ "$SAMPLE_DURATION" -le 86400 ] && [ "$SAMPLE_INTERVAL" -ge 1 ] || exit 2
printf '%s\n' 'elapsed_s,pid,rss_kb,hwm_kb,available_kb,process_ticks,total_ticks,idle_ticks,children_rss_kb,children_hwm_kb,tmp_kb,data_free_kb'
start=$(date +%s)
while :; do
    now=$(date +%s);elapsed=$((now-start))
    [ "$elapsed" -le "$SAMPLE_DURATION" ] || break
    [ -r "/proc/$SAMPLE_PID/status" ] || exit 1
    rss=$(awk '/^VmRSS:/{print $2}' "/proc/$SAMPLE_PID/status")
    hwm=$(awk '/^VmHWM:/{print $2}' "/proc/$SAMPLE_PID/status")
    available=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
    ticks=$(awk '{print $14+$15}' "/proc/$SAMPLE_PID/stat")
    cpu=$(awk '/^cpu /{sum=0;for(i=2;i<=9;i++)sum+=$i;print sum "," $5+$6;exit}' /proc/stat)
    child_rss=0;child_hwm=0
    # Traverse each descendant once without assuming one generation of subprocesses.
    pending="$SAMPLE_PID"
    while [ -n "$pending" ]; do
        next=''
        for parent in $pending;do
            children=$(cat "/proc/$parent/task/$parent/children" 2>/dev/null || true)
            for child in $children;do
                [ -r "/proc/$child/status" ] || continue
                value=$(awk '/^VmRSS:/{print $2}' "/proc/$child/status")
                peak=$(awk '/^VmHWM:/{print $2}' "/proc/$child/status")
                child_rss=$((child_rss+${value:-0}));child_hwm=$((child_hwm+${peak:-0}))
                next="$next $child"
            done
        done
        pending=$next
    done
    tmp=$(df -k /tmp | awk 'NR==2{print $3}')
    data=$(df -k /data | awk 'NR==2{print $4}')
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$elapsed" "$SAMPLE_PID" "${rss:-0}" "${hwm:-0}" "$available" "$ticks" "$cpu" "$child_rss" "$child_hwm" "$tmp" "$data"
    sleep "$SAMPLE_INTERVAL"
done
