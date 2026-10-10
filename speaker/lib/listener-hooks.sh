#!/bin/sh
# Sourced AFTER the original launcher's function definitions, in a generated sibling.
# The original launcher, listener config, Agent, KWS and TTS files remain intact.
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
printf '%s\n' "$$" > "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready"

bridge_voice_begin() {
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    flock -w 3 7 || return 1
    printf '%s\n' "$$" > "$BRIDGE_RUNTIME/xiaoai_bridge.voice"
    rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.led.json"
    BRIDGE_VOICE_REVISION=0
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" ] && \
        BRIDGE_VOICE_REVISION=$(jq -r '.revision // 0' "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json")
    BRIDGE_BT_SAVED=$(percent bluetooth)
    BRIDGE_MASTER_SAVED=$(percent mysoftvol)
    flock -u 7
    BRIDGE_VOICE_SNAPSHOT=$("$BRIDGE_DIR/lib/source-snapshot.sh" 2>/dev/null || printf '{"source":null,"uncertain":true}')
    event=$(printf '%s' "$BRIDGE_VOICE_SNAPSHOT" | jq -c --argjson pid "$$" --argjson master "${BRIDGE_MASTER_SAVED:-null}" --argjson bluetooth "${BRIDGE_BT_SAVED:-null}" '.+{type:"voice_begin",pid:$pid,volumes:{master:$master,bluetooth:$bluetooth}}')
    "$BRIDGE_BIN" --emit "$event" >/dev/null 2>&1 || true
    # Reserve the shared audio gate before microphone capture. C yields its own
    # announcement when this event arrives; a Conversation releases session lock
    # before waiting for audio, so the voice pipeline cannot deadlock with it.
    exec 8> "$BRIDGE_RUNTIME/xiaoai_bridge.audio.lock"
    flock -w 120 8 || return 1
    export XIAOAI_VOICE_AUDIO_LOCK=1
    trap 'bridge_voice_exit' HUP INT TERM
}

bridge_voice_exit() {
    trap - HUP INT TERM
    SAVED_VOL=${SAVED_VOL:-${BRIDGE_MASTER_SAVED:-0}}
    bridge_restore_volume || true
    command -v kws_guard_cleanup >/dev/null 2>&1 && kws_guard_cleanup
    exit 143
}

bridge_error_tts() {
    if [ "${XIAOAI_VOICE_AUDIO_LOCK:-0}" = 1 ];then
        "$SPEAK" error
    else
        flock -w 120 "$BRIDGE_RUNTIME/xiaoai_bridge.audio.lock" "$SPEAK" error
    fi
}

bridge_process_text() {
    payload=$(jq -nc --arg text "$STT_TEXT" --argjson snapshot "$BRIDGE_VOICE_SNAPSHOT" \
        '{text:$text,speak_response:true,use_context:true,_snapshot:$snapshot}') || return 1
    "$BRIDGE_DIR/lib/text.sh" voice "$payload" >/dev/null
}

bridge_restore_volume() {
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    flock -w 3 7 || return 1
    master=$SAVED_VOL
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
    rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.voice"
    flock -u 7
    if [ "${XIAOAI_VOICE_AUDIO_LOCK:-0}" = 1 ];then flock -u 8;fi
    export XIAOAI_VOICE_AUDIO_LOCK=0
    trap - HUP INT TERM
    "$BRIDGE_BIN" --emit '{"type":"voice_end"}' >/dev/null 2>&1 || true
}
