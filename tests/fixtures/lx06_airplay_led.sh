#!/bin/sh

STATE=/tmp/airplay_led.state
LOCK=/tmp/airplay_led.lock

# LX06 18颗LED，沿用原版音量灯的颜色和顺序
LED=/sys/devices/i2c-0/0-003a/led_rgb
lsmod 2>/dev/null | grep -q et6037 && \
    LED=/sys/devices/i2c-0/0-0023/led_rgb

[ -w "$LED" ] || exit 0

ORDER="10 11 12 13 14 15 16 17 0 1 2 3 4 5 6 7 8 9"

draw() {
    n="$1"
    for i in $ORDER; do
        if [ "$n" -gt 0 ]; then
            color="0x903000"
        else
            color="0x0"
        fi
        printf '%s %s\n' "$i" "$color" > "$LED"
        n=$((n - 1))
    done
}

restore_led() {
    # 沿用 xiaoai-patch 原版音量灯的恢复逻辑
    if /etc/init.d/listener status >/dev/null 2>&1; then
        draw 0
    elif command -v shut_led >/dev/null 2>&1 &&
         command -v show_led >/dev/null 2>&1; then
        shut_led 7
        show_led 7
    else
        draw 0
    fi
}

if [ "$1" = "--worker" ]; then
    trap 'rmdir "$LOCK" 2>/dev/null' 0

    last_bars=-1

    while [ -f "$STATE" ]; do
        read pct stamp < "$STATE" || break

        bars=$(( (pct * 18 + 99) / 100 ))

        if [ "$bars" -ne "$last_bars" ]; then
            draw "$bars"
            last_bars="$bars"
        fi

        now=$(date +%s)

        if [ $((now - stamp)) -ge 3 ]; then
            read check_pct check_stamp < "$STATE" || break
            [ "$check_stamp" = "$stamp" ] || continue

            restore_led
            rm -f "$STATE"
            break
        fi

        sleep 0.2
    done

    exit 0
fi

# Shairport Sync 传入 -30.0 ~ 0.0，-144 表示静音
printf '%s\n' "$1" |
    grep -Eq '^-?[0-9]+([.][0-9]+)?$' || exit 0

pct=$(awk -v v="$1" 'BEGIN {
    if (v < -30) v = -30
    if (v > 0) v = 0
    printf "%.0f", (v + 30) * 100 / 30
}')

# 保留最近一次音量，供测试检查
printf '%s %s\n' "$1" "$pct" > /tmp/airplay_led.last

# 原子更新状态，避免滑动音量条时同时写LED
printf '%s %s\n' "$pct" "$(date +%s)" > "$STATE.$$"
mv -f "$STATE.$$" "$STATE"

# 只运行一个LED刷新进程
if mkdir "$LOCK" 2>/dev/null; then
    /data/airplay_volume_led --worker \
        </dev/null >/dev/null 2>&1 &
fi
