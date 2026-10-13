#!/bin/sh
# Definitions only. The parent retains voice, FD 8 and FD 6 until its complete
# volume/guard cleanup. A child may finish the visual state, never unlock them.
bridge_voice_led_done() {
    [ -n "${XIAOAI_VOICE_CYCLE:-}" ] && [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice-led-done" ] || return 1
    IFS= read -r led_done_cycle < "$BRIDGE_RUNTIME/xiaoai_bridge.voice-led-done" || return 1
    [ "$led_done_cycle" = "$XIAOAI_VOICE_CYCLE" ]
}

bridge_voice_led_finish() {
    [ "${XIAOAI_VOICE_LED_LOCK:-0}" = 1 ] && [ "${XIAOAI_VOICE_AUDIO_LOCK:-0}" = 1 ] || return 0
    case "${XIAOAI_VOICE_CYCLE:-}" in ''|*[!0-9.]*) return 0 ;; esac
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice-cycle" ] || return 0
    IFS= read -r led_active_cycle < "$BRIDGE_RUNTIME/xiaoai_bridge.voice-cycle" || return 0
    [ "$led_active_cycle" = "$XIAOAI_VOICE_CYCLE" ] && voice_busy || return 0
    bridge_listener_pid "${voice_owner:-0}" "${voice_start:-}" || return 0
    # An inherited open file description can re-lock itself. A foreign worker
    # with its own FD cannot acquire the parent's lock and cannot touch this LED.
    flock -x -n 6 || return 1
    bridge_voice_led_done && return 0
    bridge_profile voice_led_finish_begin
    led_result=0
    [ ! -x /bin/show_led ] || /bin/show_led 11 || led_result=1
    bridge_profile voice_led_fade_sent
    if [ -x /bin/shut_led ]; then
        for led_mode in 1 2 3; do /bin/shut_led "$led_mode" || led_result=1;done
    fi
    bridge_profile voice_led_off_sent
    [ "$led_result" = 0 ] || return 1
    led_done_file="$BRIDGE_RUNTIME/xiaoai_bridge.voice-led-done"
    printf '%s\n' "$XIAOAI_VOICE_CYCLE" > "$led_done_file.$$" && mv -f "$led_done_file.$$" "$led_done_file"
}
