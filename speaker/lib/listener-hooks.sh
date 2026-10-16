#!/bin/sh
# Sourced AFTER the original launcher's function definitions, in a generated sibling.
# The original launcher, listener config, Agent, KWS and TTS files remain intact.
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
# Readiness is published by the generated launcher after model/date/STT setup.
bridge_listener_initialized() {
    bridge_listener_pid "$$" "$(bridge_pid_start "$$" 2>/dev/null || true)" || return 1
    bridge_recover_pending || return 1
    BRIDGE_LISTENER_INITIALIZED=1
    trap 'bridge_voice_exit' HUP INT TERM
    trap 'bridge_voice_cleanup' 0
}
bridge_listener_ready() {
    [ "${BRIDGE_LISTENER_INITIALIZED:-0}" = 1 ] || return 1
    bridge_owner_alive "${KWS_GUARD_CHILD:-0}" || return 1
    printf '%s %s 4\n' "$$" "$(bridge_pid_start "$$" 2>/dev/null || true)" > "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready"
}

bridge_voice_detected() {
    BRIDGE_VOICE_SEQUENCE=$((${BRIDGE_VOICE_SEQUENCE:-0}+1))
    voice_cycle_start=$(bridge_pid_start "$$" 2>/dev/null || true)
    export XIAOAI_VOICE_CYCLE="$$.${voice_cycle_start:-0}.$BRIDGE_VOICE_SEQUENCE"
    bridge_profile wake
    BRIDGE_WAKE_DETECTED=1
}

bridge_voice_begin() {
    BRIDGE_PREEMPTED_FEEDBACK_OWNER='';BRIDGE_PREEMPTED_FEEDBACK_START=''
    [ "${BRIDGE_WAKE_DETECTED:-0}" = 1 ] || bridge_voice_detected
    BRIDGE_WAKE_DETECTED=0
    bridge_profile wake_setup_begin
    if ! voice_busy || [ "${voice_owner:-0}" != "$$" ]; then bridge_recover_pending || return 1;fi
    BRIDGE_VOICE_VOLUME_SAVED=false
    # Reuse only a valid identity. A transient detection read failure must not
    # create PID-only ownership; stop before acquiring resources if retry fails.
    case "${voice_cycle_start-}" in
        ''|*[!0-9]*)
            voice_cycle_start=$(bridge_pid_start "$$" 2>/dev/null) || return 1
            case "$voice_cycle_start" in ''|*[!0-9]*) return 1 ;; esac
            ;;
    esac
    bridge_mark_owner "$BRIDGE_RUNTIME/xiaoai_bridge.voice" "${voice_cycle_start-}" || return 1
    trap 'bridge_voice_exit' HUP INT TERM
    printf '%s\n' "$XIAOAI_VOICE_CYCLE" > "$BRIDGE_RUNTIME/xiaoai_bridge.voice-cycle" || { bridge_voice_release;return 1; }
    exec 6> "$BRIDGE_RUNTIME/xiaoai_bridge.led.lock"
    bridge_profile_detail wake_led_lock_wait
    bridge_lock_for 1 6 || { bridge_voice_release; return 1; }
    bridge_profile_detail wake_led_lock_ready
    export XIAOAI_VOICE_LED_LOCK=1
    # Retain and remember the existing task, without a second ownership scheme.
    if [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" ]; then
        read BRIDGE_PREEMPTED_FEEDBACK_OWNER BRIDGE_PREEMPTED_FEEDBACK_START < "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" || true
    fi
    # This is the original, verified LX06 wake primitive, moved ahead of locks,
    # jq and DBus. Recording/attenuation still waits for the complete snapshot.
    [ ! -x /bin/show_led ] || /bin/show_led 1
    bridge_profile wake_led
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_profile_detail wake_volume_lock_wait
    bridge_lock_for 3 7 || { bridge_voice_release; return 1; }
    bridge_profile_detail wake_volume_lock_ready
    BRIDGE_DUCK_TRANSFER=false
    bridge_profile_detail wake_bluetooth_read_begin
    BRIDGE_BT_SAVED=$(percent bluetooth)
    bridge_profile_detail wake_bluetooth_read_end
    bridge_profile_detail wake_master_read_begin
    BRIDGE_MASTER_SAVED=$(percent mysoftvol)
    bridge_profile_detail wake_master_read_end
    BRIDGE_HARDWARE_MASTER=$BRIDGE_MASTER_SAVED
    [ -n "$BRIDGE_MASTER_SAVED" ] && [ -n "$BRIDGE_BT_SAVED" ] || { flock -u 7;bridge_voice_release;return 1; }
    if [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" ]; then
        BRIDGE_DUCK_TRANSFER=true
    fi
    values="{\"master\":$BRIDGE_MASTER_SAVED,\"bluetooth\":$BRIDGE_BT_SAVED}"
    handoff=''
    [ "$BRIDGE_DUCK_TRANSFER" != true ] || handoff="$BRIDGE_RUNTIME/xiaoai_bridge.duck.json"
    # One atomic commit: neither a failed write nor an unsuccessful audio
    # reservation may discard the remote duck's recoverable baseline.
    bridge_profile_detail wake_journal_begin
    bridge_volume_journal "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" "$values" "$handoff" "$BRIDGE_HARDWARE_MASTER" "${voice_cycle_start-}" || { flock -u 7;bridge_voice_release;return 1; }
    bridge_profile_detail wake_journal_end
    BRIDGE_VOICE_VOLUME_SAVED=true
    journal_fields=$(bridge_json journal-fields "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json") || {
        flock -u 7;bridge_restore_volume;return 1;
    }
    { IFS= read -r BRIDGE_MASTER_SAVED;IFS= read -r BRIDGE_VOICE_REVISION; } <<EOF
$journal_fields
EOF
    flock -u 7
    bridge_profile wake_volumes
    if ! BRIDGE_VOICE_SNAPSHOT=$("$BRIDGE_DIR/lib/source-snapshot.sh" "$$" "${BRIDGE_MASTER_SAVED:-null}" "${BRIDGE_BT_SAVED:-null}" 2>/dev/null); then
        BRIDGE_VOICE_SNAPSHOT=$(printf '{"type":"voice_begin","pid":%s,"source":null,"uncertain":true}' "$$")
    fi
    bridge_profile wake_snapshot
    "$BRIDGE_BIN" --emit "$BRIDGE_VOICE_SNAPSHOT" >/dev/null 2>&1 || true
    bridge_profile wake_event
    # Reserve the shared audio gate before microphone capture. C yields its own
    # announcement when this event arrives; a Conversation releases session lock
    # before waiting for audio, so the voice pipeline cannot deadlock with it.
    exec 8> "$BRIDGE_RUNTIME/xiaoai_bridge.audio.lock"
    bridge_lock_for "${XIAOAI_AUDIO_WAIT:-120}" 8 || { bridge_restore_volume; return 1; }
    export XIAOAI_VOICE_AUDIO_LOCK=1
    if [ "$BRIDGE_DUCK_TRANSFER" = true ]; then
        rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.duck" "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json"
    fi
    bridge_profile wake_audio_ready
}

bridge_voice_attenuate() {
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_profile_detail attenuation_lock_wait
    bridge_lock_for 3 7 || return 1
    bridge_profile_detail attenuation_lock_ready
    # The original set_volume launches background amixer children outside this
    # lock. Keep the same two channels but finish both writes before recording.
    if [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" ]; then
        applied_json=$(bridge_json applied "$1" < "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json") || { flock -u 7;return 1; }
        atomic_json "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" "$applied_json" || { flock -u 7;return 1; }
    fi
    amixer sset mysoftvol "${1}%" >/dev/null 2>&1 &&
        amixer sset bluetooth "${1}%" >/dev/null 2>&1
    attenuation_result=$?
    flock -u 7
    bridge_profile wake_attenuated
    return "$attenuation_result"
}

bridge_record_response() {
    bridge_profile recording_ready
    bridge_profile recording_launch
    if [ "${XIAOAI_PROFILE:-0}" = 1 ]; then
        ${RECORD_COMMAND} | "$BRIDGE_JSON_BIN" --trace-pcm "$BRIDGE_RUNTIME/xiaoai_bridge.profile" "$XIAOAI_VOICE_CYCLE"
    else
        ${RECORD_COMMAND}
    fi
}

# The private Listener's empty-STT miplay wrapper calls set_volume before its
# notification. Keep that behavior under the same lock, without discarding the
# journal needed for final cleanup and newer HA volume requests.
set_volume() {
    if [ "${BRIDGE_VOICE_VOLUME_SAVED:-false}" = true ]; then
        bridge_volume_recover "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" 3 keep
    else
        bridge_voice_attenuate "$1"
    fi
}

bridge_voice_release() {
    bridge_voice_led_verified=false
    if [ "${XIAOAI_VOICE_AUDIO_LOCK:-0}" = 1 ]; then flock -u 8;fi
    export XIAOAI_VOICE_AUDIO_LOCK=0
    if [ "${XIAOAI_VOICE_LED_LOCK:-0}" = 1 ]; then
        bridge_voice_led_done && bridge_voice_led_verified=true
        # Also cover a failed snapshot/audio reservation after show_led 1.
        if ! bridge_voice_led_done && [ -x /bin/shut_led ]; then
            for bridge_led_mode in 1 2 3; do /bin/shut_led "$bridge_led_mode";done
        fi
        flock -u 6
    fi
    export XIAOAI_VOICE_LED_LOCK=0
    owner='';[ ! -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice" ] || read owner owner_start < "$BRIDGE_RUNTIME/xiaoai_bridge.voice"
    if [ "$owner" = "$$" ]; then
        rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.voice" "$BRIDGE_RUNTIME/xiaoai_bridge.voice-cycle" "$BRIDGE_RUNTIME/xiaoai_bridge.voice-led-done"
        "$BRIDGE_BIN" --emit '{"type":"voice_end"}' >/dev/null 2>&1 || true
        if [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" ]; then
            "$BRIDGE_DIR/lib/task-feedback.sh" after-voice \
                "${BRIDGE_PREEMPTED_FEEDBACK_OWNER:-}" "${BRIDGE_PREEMPTED_FEEDBACK_START:-}" \
                "$bridge_voice_led_verified" \
                3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- </dev/null >/dev/null 2>&1 || true
        fi
    fi
}

bridge_voice_cleanup() {
    trap - 0 HUP INT TERM
    if [ "${BRIDGE_VOICE_VOLUME_SAVED:-false}" = true ]; then bridge_restore_volume || true
    else bridge_voice_release;fi
    owner='';[ ! -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready" ] || read owner owner_start owner_version < "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready"
    [ "$owner" != "$$" ] || rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready"
    if command -v kws_guard_cleanup >/dev/null 2>&1; then kws_guard_cleanup || true;fi
    bridge_oauth_leave || true
    bridge_recovery_schedule
}

bridge_voice_exit() {
    bridge_voice_cleanup
    exit 143
}

bridge_error_tts() {
    if [ "${XIAOAI_VOICE_AUDIO_LOCK:-0}" = 1 ];then
        "$SPEAK" error
    else
        (
            exec 8> "$BRIDGE_RUNTIME/xiaoai_bridge.audio.lock"
            bridge_lock_for 120 8 || exit 1
            "$SPEAK" error
        )
    fi
}

bridge_process_text() {
    payload=$(jq -nc --arg text "$STT_TEXT" --argjson snapshot "$BRIDGE_VOICE_SNAPSHOT" \
        '{text:$text,speak_response:true,use_context:true,_snapshot:$snapshot}') || return 1
    "$BRIDGE_DIR/lib/text.sh" voice "$payload" >/dev/null
    text_result=$?
    bridge_profile listener_text_return
    return "$text_result"
}

bridge_restore_volume() {
    bridge_profile voice_restore_begin
    restore_result=0
    bridge_volume_recover "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" || restore_result=1
    BRIDGE_VOICE_VOLUME_SAVED=false
    # Lock/mixer failure must never retain audio, voice or LED ownership.
    bridge_voice_release
    bridge_profile voice_restore_done
    bridge_recovery_schedule
    return "$restore_result"
}

# Cover failures during model/date/STT/OAuth initialization, before readiness.
trap 'bridge_voice_exit' HUP INT TERM
trap 'bridge_voice_cleanup' 0
