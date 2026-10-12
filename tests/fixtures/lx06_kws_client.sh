#!/bin/sh

SERVER=192.0.2.20
PORT=8765
LED_EFFECT=6

REC_PID=""
SOX_PID=""
NC_PID=""
PIPE_DIR=""

led_on() {
    /bin/show_led "$LED_EFFECT"
}

led_off() {
    /bin/shut_led "$LED_EFFECT"
}

cleanup() {
    for pid in "$REC_PID" "$SOX_PID" "$NC_PID"; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    done

    for pid in "$REC_PID" "$SOX_PID" "$NC_PID"; do
        [ -n "$pid" ] && wait "$pid" 2>/dev/null
    done

    [ -n "$PIPE_DIR" ] && rm -rf "$PIPE_DIR"

    REC_PID=""
    SOX_PID=""
    NC_PID=""
    PIPE_DIR=""
}

trap 'cleanup; exit 143' HUP INT TERM
trap 'cleanup' EXIT

run_kws() {
    PIPE_DIR=$(mktemp -d /tmp/kws.XXXXXX) || return 1

    mkfifo "$PIPE_DIR/raw" \
           "$PIPE_DIR/mono" \
           "$PIPE_DIR/reply" || {
        cleanup
        return 1
    }

    rec -q -t alsa mico_record \
        -b 16 -c 8 -r 16000 \
        -e signed-integer -t raw - \
        > "$PIPE_DIR/raw" &
    REC_PID=$!

    sox -t raw -r 16000 \
        -e signed-integer -b 16 -c 8 \
        "$PIPE_DIR/raw" \
        -t raw -r 16000 \
        -e signed-integer -b 16 -c 2 \
        "$PIPE_DIR/mono" remix 1 6 &
    SOX_PID=$!

    nc "$SERVER" "$PORT" \
        < "$PIPE_DIR/mono" \
        > "$PIPE_DIR/reply" &
    NC_PID=$!

    while IFS= read -r EVENT; do
        case "$EVENT" in
            READY)
                led_off
                echo "KWS connected, LED off" >&2
                ;;

            WAKE)
echo "$(cut -d" " -f1 /proc/uptime) WAKE_RX" >> /tmp/kws_timing.log
                led_off
                cleanup
echo "$(cut -d" " -f1 /proc/uptime) CLEANUP_DONE" >> /tmp/kws_timing.log
                echo "KWS: 小爱同学 activated" >&2
                return 0
                ;;
        esac
    done < "$PIPE_DIR/reply"

    cleanup
    return 1
}

while true; do
    if run_kws; then
        exit 0
    fi

    led_on
    echo "KWS offline, LED 6 on, Alexa fallback" >&2

    timeout 12 \
        /usr/bin/porcupine \
        /usr/lib/libpv_porcupine.so \
        /usr/share/porcupine/model.pv \
        "/usr/share/porcupine/keywords/hey google_raspberry-pi.ppn" \
        0.6 mico_record

    if [ "$?" -eq 0 ]; then
        led_off
        exit 0
    fi

    sleep 1
done
