#!/bin/sh
# Both STT-completed voice input and HA text use this one local pipeline.
set -eu
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
MODE=$1
REQUEST=$2
printf '%s' "$REQUEST" | jq -e '
  (.text|type)=="string" and
  (if has("speak_response") then (.speak_response|type)=="boolean" else true end) and
  (if has("use_context") then (.use_context|type)=="boolean" else true end) and
  (if has("volume") then (.volume|type)=="number" and .volume>=0 and .volume<=100 else true end)
' >/dev/null || fail invalid_request
TEXT=$(request_value '.text')
[ -n "$TEXT" ] && [ "${#TEXT}" -le 4096 ] || fail invalid_text
WORK=$(mktemp -d "$BRIDGE_RUNTIME/xiaoai_text.XXXXXX") || fail local_resource_failed
TTS_OVERRIDE_ACTIVE=false
TTS_RESTORE_VALUE=''
TTS_RESTORE_REVISION=0
VOICE_GUARD_ACTIVE=false
restore_tts_volume() {
    [ "$TTS_OVERRIDE_ACTIVE" = true ] || return 0
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_lock_for 3 7 || return 1
    value=$TTS_RESTORE_VALUE
    if [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" ]; then
        latest=$(jq -r --argjson before "$TTS_RESTORE_REVISION" 'if (.versions.tts // 0)>$before then .volumes.tts // empty else empty end' "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" 2>/dev/null || true)
        [ -n "$latest" ] && value=$latest
    fi
    [ -n "$value" ] && amixer sset notifyvol "${value}%" >/dev/null 2>&1 || return 1
    TTS_OVERRIDE_ACTIVE=false
    flock -u 7
}
cleanup() {
    trap - HUP INT TERM
    restore_tts_volume || true
    if [ "$VOICE_GUARD_ACTIVE" = true ]; then
        command curl -fsS --connect-timeout 1 --max-time 2 \
            -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
            --data-binary "@$WORK/guard-end.json" "${HA_URL%/}/api/xiaoai_speaker/$DEVICE_ID/voice_guard" >/dev/null 2>&1 || true
    fi
    rm -rf "$WORK"
}
trap 'cleanup' 0
trap 'exit 143' HUP INT TERM

# Do not queue general work here. HA queues it; local locks only protect the device.
if [ "$MODE" != voice ] && voice_busy; then fail busy; fi
exec 9> "$BRIDGE_RUNTIME/xiaoai_bridge.session.lock"
if [ "$MODE" = voice ]; then bridge_lock_for 120 9 || fail busy; else flock -n 9 || fail busy; fi
if [ "$MODE" != voice ] && voice_busy; then fail busy; fi

LISTENER_CONFIG=$(config_value '.listener_config // "/data/listener"')
# This trusted local config loads the existing Agent and OAuth variables.
# It is never received from the network or replaced. Only the original OAuth
# renewal function may update its existing HA_TOKEN line.
if [ "$MODE" != announce ]; then
    ORIGINAL_CURL=$(command -v curl)
    [ -r "$LISTENER_CONFIG" ] || fail listener_config_missing
    . "$LISTENER_CONFIG"
    . "$BRIDGE_DIR/lib/conversation.sh"
    bridge_load_conversation_adapter "$ORIGINAL_CURL" || fail conversation_adapter_missing
fi
TTS_PATH=$(config_value '.tts_path // "/data/tts_doubao"')
[ -x "$TTS_PATH" ] || fail tts_unavailable

speak() {
    utterance=$1
    [ -n "$utterance" ] || return 0
    inherited_audio=false
    if [ "$MODE" = voice ] && [ "${XIAOAI_VOICE_AUDIO_LOCK:-0}" = 1 ];then
        inherited_audio=true
    else
        exec 8> "$BRIDGE_RUNTIME/xiaoai_bridge.audio.lock"
        bridge_lock_for 120 8 || fail busy
    fi
    override=$(request_value '.volume // empty')
    saved_tts=$(percent notifyvol)
    rev_before=0
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" ] && rev_before=$(jq -r '.revision // 0' "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json")
    if [ -n "$override" ]; then
        exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
        bridge_lock_for 3 7 || fail busy
        saved_tts=$(percent notifyvol)
        [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" ] && rev_before=$(jq -r '.revision // 0' "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json")
        [ -n "$saved_tts" ] || fail volume_unknown
        TTS_RESTORE_VALUE=$saved_tts
        TTS_RESTORE_REVISION=$rev_before
        TTS_OVERRIDE_ACTIVE=true
        amixer sset notifyvol "${override}%" >/dev/null 2>&1 || fail hardware_control_failed
        flock -u 7
    fi
    printf '{"stage":"synthesizing"}\n'
    "$TTS_PATH" "$utterance" > /dev/null 2>&1 &
    tts_pid=$!
    # Only inspect descendants of this TTS invocation; other media never implies playing.
    while kill -0 "$tts_pid" 2>/dev/null; do
        if [ -r "/proc/$tts_pid/task/$tts_pid/children" ]; then
            children=$(cat "/proc/$tts_pid/task/$tts_pid/children")
            pending=$children
            playing=false
            visited=0
            while [ -n "$pending" ] && [ "$visited" -lt 64 ]; do
                next=''
                for child in $pending; do
                    case "$child" in *[!0-9]*|'') continue ;; esac
                    visited=$((visited+1))
                    comm=$(cat "/proc/$child/comm" 2>/dev/null || true)
                    if [ "$comm" = miplay ]; then playing=true;break;fi
                    next="$next $(cat "/proc/$child/task/$child/children" 2>/dev/null || true)"
                done
                [ "$playing" = true ] && break
                pending=$next
            done
            if [ "$playing" = true ]; then printf '{"stage":"playing"}\n';break;fi
        fi
        sleep 0.25
    done
    result=0;wait "$tts_pid" || result=$?
    restore_tts_volume || result=1
    [ "$inherited_audio" = true ] || flock -u 8
    return "$result"
}

if [ "$MODE" = announce ]; then
    # Announcement never invokes intent detection or touches the conversation cache.
    speak "$TEXT" || fail tts_failed
    printf '{"reply_text":""}\n'
    exit 0
fi

SPEAK_RESPONSE=$(request_value '.speak_response // true')
# jq's // treats false as absent, so test the explicit boolean separately.
[ "$(request_value '.speak_response')" = false ] && SPEAK_RESPONSE=false
USE_CONTEXT=true
[ "$(request_value '.use_context')" = false ] && USE_CONTEXT=false
DEFAULT_AGENT=${EOAIC2_AGENT_ID:-}
[ -n "$DEFAULT_AGENT" ] || fail conversation_adapter_missing
AGENT=$(request_value '.agent_id // empty')
if [ -z "$AGENT" ] && [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.agent.json" ]; then
    AGENT=$(jq -r '.agent_id // empty' "$BRIDGE_RUNTIME/xiaoai_bridge.agent.json")
fi
if [ -n "$AGENT" ]; then
    printf '%s' "$AGENT" | grep -Eq '^conversation\.[a-zA-Z0-9_]+$' || fail invalid_agent
    EOAIC2_AGENT_ID=$AGENT
fi
if [ "$USE_CONTEXT" = false ]; then
    EOAIC2_STATE="$WORK/context.json"
elif [ "$EOAIC2_AGENT_ID" != "$DEFAULT_AGENT" ]; then
    EOAIC2_STATE="$BRIDGE_RUNTIME/xiaoai_bridge.context.$EOAIC2_AGENT_ID.json"
    if [ ! -f "$EOAIC2_STATE" ]; then
        contexts=0
        for item in "$BRIDGE_RUNTIME"/xiaoai_bridge.context.*.json; do
            [ -f "$item" ] && contexts=$((contexts+1))
        done
        [ "$contexts" -lt 8 ] || fail context_limit
    fi
fi
[ -n "${EOAIC2_STATE:-}" ] || fail conversation_adapter_missing

SNAPSHOT=$(request_value '._snapshot // empty')
if [ "$MODE" != voice ] || [ -z "$SNAPSHOT" ]; then
    SNAPSHOT=$("$BRIDGE_DIR/lib/source-snapshot.sh" 2>/dev/null || printf '{"source":null,"uncertain":true}')
fi
DEVICE_ID=$(config_value '.device_id')
if [ "$MODE" = voice ]; then
    guard_id="voice-$$-$(date +%s)"
    jq -nc --arg id "$guard_id" '{request_id:$id}' > "$WORK/guard-end.json"
    guard_body=$(jq -nc --arg id "$guard_id" --arg text "$TEXT" '{request_id:$id,text:$text}')
    guard_reply=$(command curl -fsS --connect-timeout 1 --max-time 2 \
        -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
        --data-binary "$guard_body" "${HA_URL%/}/api/xiaoai_speaker/$DEVICE_ID/voice_guard" 2>/dev/null || true)
    printf '%s' "$guard_reply" | jq -e '.accepted==true' >/dev/null 2>&1 && VOICE_GUARD_ACTIVE=true
fi
PLAN_BODY=$(jq -nc --arg text "$TEXT" --argjson snapshot "$SNAPSHOT" '{text:$text,snapshot:$snapshot}')
# This HA view resolves volume rules only. Conversations still originate below on LX06.
PLAN=$(command curl -sS --connect-timeout 2 --max-time 4 \
    -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
    --data-binary "$PLAN_BODY" "${HA_URL%/}/api/xiaoai_speaker/$DEVICE_ID/volume_plan" 2>/dev/null || true)
reject_volume_plan() {
    if printf '%s' "$PLAN" | jq -e '.error=="unsupported_volume_target" or .error=="volume_unknown" or .error=="invalid_volume"' >/dev/null 2>&1; then
        flock -u 9
        [ "$SPEAK_RESPONSE" = true ] && speak '当前音频来源暂不支持这个音量操作。' || true
        fail unsupported_volume_target
    fi
}
apply_volume_plan() {
    printf '%s' "$PLAN" | jq -c '.plan[]' > "$WORK/plan.jsonl"
    while IFS= read -r item; do
        channel=$(printf '%s' "$item" | jq -r .target)
        level=$(printf '%s' "$item" | jq -r .level)
        volume_write "$channel" "$level" > /dev/null || fail hardware_control_failed
    done < "$WORK/plan.jsonl"
    COMMIT=$(printf '%s' "$PLAN" | jq -c '{commit:true,plan:.plan}')
    command curl -fsS --connect-timeout 2 --max-time 4 \
        -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
        --data-binary "$COMMIT" "${HA_URL%/}/api/xiaoai_speaker/$DEVICE_ID/volume_plan" >/dev/null 2>&1 || true
    reply=$(printf '%s' "$PLAN" | jq -r '.reply_text')
}
reject_volume_plan
if printf '%s' "$PLAN" | jq -e '.handled==true' >/dev/null 2>&1; then
    apply_volume_plan
else
    # Reuse the existing local intent stage before the existing Agent.
    intent=$(command curl -fsS --connect-timeout 1 --max-time 2 --form-string "text=$TEXT" http://127.0.0.1/intent 2>/dev/null || true)
    case "$intent" in
        BluetoothPair)
            timeout 30 /bin/bluetooth_pair >/dev/null 2>&1 || fail local_intent_failed
            reply='' ;;
        BluetoothDisconnect)
            timeout 8 bluetoothctl disconnect >/dev/null 2>&1 || fail local_intent_failed
            reply='' ;;
        Pause)
            /bin/play_button >/dev/null 2>&1 || fail local_intent_failed
            reply='' ;;
        VolumeUp|VolumeDown)
            PLAN_BODY=$(jq -nc --arg text "$TEXT" --arg local_intent "$intent" --argjson snapshot "$SNAPSHOT" '{text:$text,local_intent:$local_intent,snapshot:$snapshot}')
            PLAN=$(command curl -sS --connect-timeout 2 --max-time 4 \
                -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
                --data-binary "$PLAN_BODY" "${HA_URL%/}/api/xiaoai_speaker/$DEVICE_ID/volume_plan" 2>/dev/null || true)
            reject_volume_plan
            if printf '%s' "$PLAN" | jq -e '.handled==true' >/dev/null 2>&1;then
                apply_volume_plan
            else
                # If HA's integration is unloaded, keep the original idle/MPD
                # local intent available. Never pretend that it controls AirPlay.
                source=$(printf '%s' "$SNAPSHOT" | jq -r '.source // ""')
                case "$source" in ''|mpd) ;; *) fail volume_policy_unavailable ;; esac
                current=$(percent mysoftvol);[ -n "$current" ] || fail volume_unknown
                [ "$intent" = VolumeUp ] && delta=10 || delta=-15
                value=$((current+delta));[ "$value" -gt 100 ] && value=100;[ "$value" -lt 0 ] && value=0
                volume_write master "$value" >/dev/null || fail hardware_control_failed
                reply=''
            fi
            ;;
        *)
            if [ "$MODE" != voice ] && voice_busy;then fail busy;fi
            payload=$(jq -nc --arg text "$TEXT" --arg language "${STT_LANGUAGE:-zh-CN}" \
                --arg agent "$EOAIC2_AGENT_ID" '{text:$text,language:$language,agent_id:$agent}')
            had_state=false
            [ -f "$EOAIC2_STATE" ] && { cp -p "$EOAIC2_STATE" "$WORK/previous.json";had_state=true; }
            # The explicitly loaded original Agent injects conversation_id and
            # owns successful sliding-cache updates; a confirmed 401 renews once.
            if ! bridge_conversation_request "$payload"; then
                bridge_restore_conversation_state || fail context_restore_failed
                fail conversation_failed
            fi
            response=$(cat "$WORK/conversation.json")
            if ! printf '%s' "$response" | jq -e '(.conversation_id|type)=="string" and (.conversation_id|length)>0 and (.response|type)=="object" and .response.response_type!="error"' >/dev/null 2>&1; then
                # Existing wrappers can update state on error responses; undo only under the shared lock.
                bridge_restore_conversation_state || fail context_restore_failed
                fail conversation_failed
            fi
            reply=$(printf '%s' "$response" | jq -r '.response.speech.plain.speech // ""')
            # Context edits have finished. Never wait for voice's audio gate while
            # retaining the session lock needed by that same voice conversation.
            flock -u 9
            if [ "${TTS_NO_MATCH:-1}" != 1 ]; then
                code=$(printf '%s' "$response" | jq -r '.response.data.code // ""')
                case "$code" in no_intent_match|no_valid_targets) reply='' ;; esac
            fi
            if [ "$MODE" = voice ] && [ "${STT_REPEAT_MESSAGE:-0}" = 1 ]; then
                speak "$TEXT" || fail tts_failed
            fi
            ;;
    esac
fi
flock -u 9
[ "$SPEAK_RESPONSE" = true ] && speak "$reply" || { [ "$SPEAK_RESPONSE" = false ] || fail tts_failed; }
jq -nc --arg reply "$reply" --argjson snapshot "$SNAPSHOT" '{reply_text:$reply,source:$snapshot.source,uncertain:$snapshot.uncertain}'
