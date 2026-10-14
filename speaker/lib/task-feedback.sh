#!/bin/sh
# Native Launcher sequence only; ownership uses the existing LED lock/marker.
BRIDGE_FEEDBACK_OWNED=false
BRIDGE_FEEDBACK_AVAILABLE=false
BRIDGE_FEEDBACK_START=''
bridge_feedback_setup() {
    [ "$MODE" != voice ] && [ -x /bin/show_led ] && [ -x /bin/shut_led ] || return 0
    BRIDGE_FEEDBACK_START=$(bridge_pid_start "$$" 2>/dev/null || true)
    BRIDGE_FEEDBACK_AVAILABLE=true
}

bridge_feedback_restore_airplay() {
    voice_busy && return 0
    feedback_busy && return 0
    if [ -x "$BRIDGE_DIR/adapter/airplay-led.sh" ] && [ -r "$BRIDGE_RUNTIME/airplay_led.last" ]; then
        # The saved event survives disconnects. Reuse the bounded read-only
        # activity query; inactive/unknown AirPlay must leave native lamps off.
        feedback_airplay=$(dbus_get_all org.gnome.ShairportSync || true)
        [ "$(printf '%s' "$feedback_airplay" | dbus_property Active)" = true ] || return 0
        read feedback_raw feedback_pct < "$BRIDGE_RUNTIME/airplay_led.last" || return 0
        # The reviewed adapter checks the same LED lock and owners again.
        "$BRIDGE_DIR/adapter/airplay-led.sh" "$feedback_raw" \
            3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- </dev/null >/dev/null 2>&1 || true
    fi
}

# Called only while holding FD 6. Native children must not retain Bridge locks.
bridge_feedback_finish_native() {
    # A voice reservation can precede acquisition of FD 6. Keep the task marker
    # in that interval so voice release can perform the deferred AirPlay replay.
    voice_busy && return 1
    feedback_native_result=0
    if [ -x /bin/show_led ]; then
        /bin/show_led 11 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- </dev/null >/dev/null 2>&1 || feedback_native_result=1
    else feedback_native_result=1;fi
    if [ -x /bin/shut_led ]; then
        for feedback_mode in 1 2 3; do
            voice_busy && return 1
            /bin/shut_led "$feedback_mode" 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- </dev/null >/dev/null 2>&1 || feedback_native_result=1
        done
    else feedback_native_result=1;fi
    return "$feedback_native_result"
}

bridge_feedback_stage() {
    case "$1" in processing|synthesizing|playing) ;; *) return 0 ;; esac
    [ "$BRIDGE_FEEDBACK_AVAILABLE" = true ] && ! voice_busy || return 0
    exec 6> "$BRIDGE_RUNTIME/xiaoai_bridge.led.lock"
    bridge_lock_for 0 6 || return 0
    if voice_busy; then flock -u 6;return 0;fi
    # Later phases keep the original loading animation; do not restart it or
    # let an older task reclaim ownership removed/replaced by another owner.
    if [ "$BRIDGE_FEEDBACK_OWNED" = true ]; then flock -u 6;return 0;fi
    if feedback_busy; then flock -u 6;return 0;fi
    if [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" ]; then
        bridge_feedback_finish_native || { flock -u 6;return 0; }
    fi
    bridge_mark_owner "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" || { flock -u 6;return 0; }
    BRIDGE_FEEDBACK_OWNED=true
    # Launcher: wake 1, loading 2, then 11/shut 1/2/3. Its show_led 3 is
    # commented out; synthesis/playback retain 2, with no recording or STT.
    /bin/show_led 1 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- </dev/null >/dev/null 2>&1 || true
    if ! voice_busy; then
        /bin/show_led 2 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- </dev/null >/dev/null 2>&1 || true
    fi
    flock -u 6
}

bridge_feedback_end() {
    [ "$BRIDGE_FEEDBACK_OWNED" = true ] || return 0
    exec 6> "$BRIDGE_RUNTIME/xiaoai_bridge.led.lock"
    bridge_lock_for 0 6 || return 0
    feedback_owner='';feedback_start=''
    [ ! -r "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" ] || read feedback_owner feedback_start < "$BRIDGE_RUNTIME/xiaoai_bridge.feedback"
    if [ "$feedback_owner" != "$$" ] || [ "$feedback_start" != "$BRIDGE_FEEDBACK_START" ]; then
        BRIDGE_FEEDBACK_OWNED=false
        flock -u 6;return 0
    fi
    bridge_feedback_finish_native || { flock -u 6;return 0; }
    rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.feedback"
    BRIDGE_FEEDBACK_OWNED=false
    flock -u 6
    bridge_feedback_restore_airplay
}

# One-shot recovery on startup/existing Cron, never a new resident process.
# Voice already finished: resume the same live task, or replay AirPlay if dead.
bridge_feedback_recover() (
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" ] || exit 0
    exec 6> "$BRIDGE_RUNTIME/xiaoai_bridge.led.lock"
    bridge_lock_for 0 6 || exit 0
    if voice_busy; then flock -u 6;exit 0;fi
    if [ "${1:-}" = after-voice ]; then
        feedback_owner='';feedback_start=''
        read feedback_owner feedback_start < "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" || { flock -u 6;exit 0; }
        if [ "$feedback_owner" != "${2:-}" ] || [ "$feedback_start" != "${3:-}" ]; then
            flock -u 6;exit 0
        fi
    fi
    if feedback_busy; then
        # A Conversation cannot be cancelled mid-action; it may resume its TTS
        # after voice. Never restart an unrelated/newer task's animation.
        if [ "${1:-}" = after-voice ] && [ -x /bin/show_led ] && [ -x /bin/shut_led ]; then
            /bin/show_led 2 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- </dev/null >/dev/null 2>&1 || true
        fi
        flock -u 6;exit 0
    fi
    # The voice cycle's done marker proves all native finish calls succeeded.
    # Without that proof, retry under the LED lock and keep recovery on failure.
    if [ "${1:-}" != after-voice ] || [ "${4:-false}" != true ]; then
        bridge_feedback_finish_native || { flock -u 6;exit 0; }
    fi
    rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.feedback"
    flock -u 6
    bridge_feedback_restore_airplay
)

if [ "${0##*/}" = task-feedback.sh ]; then
    case "${1:-}" in recover|after-voice) ;; *) exit 2 ;; esac
    . "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
    bridge_feedback_recover "$@"
fi
