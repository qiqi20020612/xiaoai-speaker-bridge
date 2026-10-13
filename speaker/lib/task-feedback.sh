#!/bin/sh
# Sourced by a task worker. Uses only explicitly verified LX06 LED primitives;
# no animation loop or extra resident process. Voice always has priority.
BRIDGE_FEEDBACK_OWNED=false
BRIDGE_TASK_LED=''
bridge_feedback_setup() {
    [ "$MODE" != voice ] && [ "$TASK_LED_VERIFIED" = true ] || return 0
    for path in /sys/devices/i2c-0/0-003a/led_rgb /sys/devices/i2c-0/0-0023/led_rgb; do
        if [ -w "$path" ]; then BRIDGE_TASK_LED=$path;break;fi
    done
}
bridge_feedback_color() {
    pixel=0
    while [ "$pixel" -lt 18 ]; do
        voice_busy && return 1
        printf '%s %s\n' "$pixel" "$1" > "$BRIDGE_TASK_LED" || return 1
        pixel=$((pixel+1))
    done
}
bridge_feedback_stage() {
    [ -n "$BRIDGE_TASK_LED" ] && ! voice_busy || return 0
    exec 6> "$BRIDGE_RUNTIME/xiaoai_bridge.led.lock"
    bridge_lock_for 0 6 || return 0
    if voice_busy || { feedback_busy && [ "${feedback_owner:-}" != "$$" ]; }; then
        flock -u 6;return 0
    fi
    bridge_mark_owner "$BRIDGE_RUNTIME/xiaoai_bridge.feedback"
    BRIDGE_FEEDBACK_OWNED=true
    case "$1" in
        processing) color=20624 ;; # Amber: work that may produce no speech.
        synthesizing) color=8388688 ;; # Purple: requested speech is being prepared.
        playing) color=8421376 ;; # Blue: miplay observed under this TTS process.
        *) flock -u 6;return 0 ;;
    esac
    bridge_feedback_color "$color" || true
    flock -u 6
}
bridge_feedback_end() {
    [ "$BRIDGE_FEEDBACK_OWNED" = true ] || return 0
    exec 6> "$BRIDGE_RUNTIME/xiaoai_bridge.led.lock"
    bridge_lock_for 0 6 || return 0
    owner='';[ ! -r "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" ] || read owner owner_start < "$BRIDGE_RUNTIME/xiaoai_bridge.feedback"
    if [ "$owner" != "$$" ]; then flock -u 6;return 0;fi
    rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.feedback"
    if ! voice_busy; then
        color=0
        if [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.led.json" ]; then
            color=$(bridge_json led-color "$BRIDGE_RUNTIME/xiaoai_bridge.led.json" 2>/dev/null || printf 0)
        fi
        bridge_feedback_color "$color" || true
    fi
    flock -u 6
    # Replay the original, adapted volume hook after releasing ownership.
    if ! voice_busy && [ -x "$BRIDGE_DIR/adapter/airplay-led.sh" ] && [ -r "$BRIDGE_RUNTIME/airplay_led.last" ]; then
        read raw pct < "$BRIDGE_RUNTIME/airplay_led.last"
        "$BRIDGE_DIR/adapter/airplay-led.sh" "$raw" >/dev/null 2>&1 || true
    fi
}
