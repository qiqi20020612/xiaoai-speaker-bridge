#!/bin/sh
# Sourced AFTER the original launcher's function definitions, in a generated sibling.
# The original launcher, listener config, Agent, KWS and TTS files remain intact.
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
printf '%s\n' "$$" > "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready"

bridge_voice_begin() {
    BRIDGE_VOICE_VOLUME_SAVED=false
    printf '%s\n' "$$" > "$BRIDGE_RUNTIME/xiaoai_bridge.voice"
    trap 'bridge_voice_exit' HUP INT TERM
    bridge_profile wake
    # This is the original, verified LX06 wake primitive, moved ahead of locks,
    # jq and DBus. Recording/attenuation still waits for the complete snapshot.
    [ ! -x /bin/show_led ] || /bin/show_led 1
    bridge_profile wake_led
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_lock_for 3 7 || { bridge_voice_release; return 1; }
    rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.led.json"
    BRIDGE_VOICE_REVISION=0
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" ] && \
        BRIDGE_VOICE_REVISION=$(jq -r '.revision // 0' "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json")
    BRIDGE_BT_SAVED=$(percent bluetooth)
    BRIDGE_MASTER_SAVED=$(percent mysoftvol)
    BRIDGE_VOICE_VOLUME_SAVED=true
    flock -u 7
    bridge_profile wake_volumes
    BRIDGE_VOICE_SNAPSHOT=$("$BRIDGE_DIR/lib/source-snapshot.sh" "$$" "${BRIDGE_MASTER_SAVED:-null}" "${BRIDGE_BT_SAVED:-null}" 2>/dev/null || printf '{"type":"voice_begin","pid":%s,"source":null,"uncertain":true}' "$$")
    bridge_profile wake_snapshot
    "$BRIDGE_BIN" --emit "$BRIDGE_VOICE_SNAPSHOT" >/dev/null 2>&1 || true
    bridge_profile wake_event
    # Reserve the shared audio gate before microphone capture. C yields its own
    # announcement when this event arrives; a Conversation releases session lock
    # before waiting for audio, so the voice pipeline cannot deadlock with it.
    exec 8> "$BRIDGE_RUNTIME/xiaoai_bridge.audio.lock"
    bridge_lock_for 120 8 || { bridge_voice_release; return 1; }
    export XIAOAI_VOICE_AUDIO_LOCK=1
    bridge_profile recording_ready
}

bridge_voice_release() {
    rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.voice"
    "$BRIDGE_BIN" --emit '{"type":"voice_end"}' >/dev/null 2>&1 || true
    trap - HUP INT TERM
}

bridge_voice_exit() {
    trap - HUP INT TERM
    if [ "${BRIDGE_VOICE_VOLUME_SAVED:-false}" = true ]; then
        SAVED_VOL=${BRIDGE_MASTER_SAVED:-0}
        bridge_restore_volume || true
    else
        bridge_voice_release
    fi
    command -v kws_guard_cleanup >/dev/null 2>&1 && kws_guard_cleanup
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
}

bridge_restore_volume() {
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_lock_for 3 7 || return 1
    master=${BRIDGE_MASTER_SAVED:-$SAVED_VOL}
    bluetooth=${BRIDGE_BT_SAVED:-$SAVED_VOL}
    if [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" ]; then
        desired=$(jq -c --argjson before "${BRIDGE_VOICE_REVISION:-0}" \
            '{master:(if (.versions.master // 0)>$before then .volumes.master else null end),bluetooth:(if (.versions.bluetooth // 0)>$before then .volumes.bluetooth else null end)}' "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json")
        next_master=$(printf '%s' "$desired" | jq -r '.master // empty')
        next_bt=$(printf '%s' "$desired" | jq -r '.bluetooth // empty')
        [ -n "$next_master" ] && master=$next_master
        [ -n "$next_bt" ] && bluetooth=$next_bt
    fi
    amixer sset mysoftvol "${master}%" >/dev/null 2>&1 || true
    [ -n "$bluetooth" ] && amixer sset bluetooth "${bluetooth}%" >/dev/null 2>&1 || true
    flock -u 7
    if [ "${XIAOAI_VOICE_AUDIO_LOCK:-0}" = 1 ];then flock -u 8;fi
    export XIAOAI_VOICE_AUDIO_LOCK=0
    bridge_voice_release
}
