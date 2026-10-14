#!/bin/sh
# Both STT-completed voice input and HA text use this one local pipeline.
set -eu
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
. "$BRIDGE_DIR/lib/task-feedback.sh"
. "$BRIDGE_DIR/lib/ducking.sh"
BRIDGE_TEXT_DIAGNOSTICS=1
MODE=$1
REQUEST=$2
case "$MODE" in voice|send_text|announce) ;; *) fail invalid_request ;; esac
bridge_profile task_begin
# Read every request field with one jq startup. The final field is the raw
# multiline text; it is read as data, never evaluated as shell code.
PARSED=$(printf '%s' "$REQUEST" | jq -r '
  if ((.text|type)=="string" and
  (if has("speak_response") then (.speak_response|type)=="boolean" else true end) and
  (if has("use_context") then (.use_context|type)=="boolean" else true end) and
  (if has("volume") then (.volume|type)=="number" and .volume>=0 and .volume<=100 else true end) and
  (if has("duck_volume") then (.duck_volume|type)=="number" and .duck_volume>=0 and .duck_volume<=100 else true end) and
  (has("agent_id")|not) and
  (if has("_snapshot") then (._snapshot|type)=="object" and
    (._snapshot|if has("source") then .source==null or (.source|type)=="string" else true end) and
    (._snapshot|if has("uncertain") then (.uncertain|type)=="boolean" else true end)
   else true end) and
  (.text|explode|index(0)|not)) then
  (if has("speak_response") then .speak_response else true end),
  (if has("use_context") then .use_context else true end),
  (.volume // ""), (.duck_volume // ""), ((._snapshot // {})|tojson), .text
  else error("invalid_request") end' 2>/dev/null) || fail invalid_request
{
    IFS= read -r SPEAK_RESPONSE
    IFS= read -r USE_CONTEXT
    IFS= read -r OVERRIDE
    IFS= read -r DUCK_VOLUME
    IFS= read -r SNAPSHOT
    TEXT=$(cat)
} <<EOF
$PARSED
EOF
unset PARSED
bridge_profile task_parsed
[ -n "$TEXT" ] && [ "${#TEXT}" -le 4096 ] || fail invalid_text
WORK=$(mktemp -d "$BRIDGE_RUNTIME/xiaoai_text.XXXXXX") || fail local_resource_failed
TTS_OVERRIDE_ACTIVE=false
VOICE_GUARD_ACTIVE=false
restore_tts_volume() {
    [ "$TTS_OVERRIDE_ACTIVE" = true ] || return 0
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_lock_for 3 7 || return 1
    bridge_volume_recover_locked "$BRIDGE_RUNTIME/xiaoai_bridge.tts-volume.json" || { flock -u 7;return 1; }
    TTS_OVERRIDE_ACTIVE=false
    flock -u 7
}
cleanup() {
    # A second TERM must not interrupt bounded volume/guard cleanup. C still
    # enforces its existing final kill deadline for permanently blocked I/O.
    trap '' HUP INT TERM
    bridge_profile cleanup_begin
    restore_tts_volume || true
    bridge_duck_restore || true
    bridge_profile cleanup_volumes_done
    bridge_feedback_end || true
    bridge_profile task_cleanup
    if [ "$VOICE_GUARD_ACTIVE" = true ]; then
        bridge_profile guard_end_begin
        command curl -fsS --connect-timeout 1 --max-time 2 \
            -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
            --data-binary "@$WORK/guard-end.json" "${HA_URL%/}/api/xiaoai_speaker/$DEVICE_ID/voice_guard" >/dev/null 2>&1 || true
        bridge_profile guard_end_done
    fi
    rm -rf "$WORK"
    bridge_oauth_leave || true
    [ "$MODE" = voice ] || bridge_recovery_schedule
    bridge_profile task_finished
}
trap 'cleanup' 0
trap 'exit 143' HUP INT TERM

# Do not queue general work here. HA queues it; local locks only protect the device.
if [ "$MODE" != voice ] && voice_busy; then fail busy; fi
exec 9> "$BRIDGE_RUNTIME/xiaoai_bridge.session.lock"
bridge_profile session_wait
if [ "$MODE" = voice ]; then bridge_lock_for 120 9 || fail busy; else flock -n 9 || fail busy; fi
bridge_profile session_ready
if [ "$MODE" != voice ] && voice_busy; then fail busy; fi

CONFIG_FIELDS=$(config_value '(.listener_config // "/data/listener"), (.tts_path // "/data/tts_doubao"), (.device_id // ""), (.verified.announcement_ducking // false), (.eoai_agent_script // "/data/eoai_agent.sh"), (.ha_auth_source // "/data/porcupine_launcher_kws")')
{
    IFS= read -r LISTENER_CONFIG
    IFS= read -r TTS_PATH
    IFS= read -r DEVICE_ID
    IFS= read -r DUCK_VERIFIED
    IFS= read -r BRIDGE_CONFIG_EOAI
    IFS= read -r BRIDGE_CONFIG_AUTH
} <<EOF
$CONFIG_FIELDS
EOF
unset CONFIG_FIELDS
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
bridge_feedback_setup
bridge_feedback_begin
[ -x "$TTS_PATH" ] || fail tts_unavailable
bridge_profile config_ready

speak() {
    utterance=$1
    [ -n "$utterance" ] || return 0
    inherited_audio=false
    if [ "$MODE" = voice ] && [ "${XIAOAI_VOICE_AUDIO_LOCK:-0}" = 1 ];then
        inherited_audio=true
    else
        exec 8> "$BRIDGE_RUNTIME/xiaoai_bridge.audio.lock"
        bridge_profile audio_wait
        bridge_lock_for 120 8 || fail busy
        bridge_profile audio_ready
    fi
    if [ "$MODE" != voice ]; then bridge_recover_pending || fail volume_recovery_pending;fi
    override=$OVERRIDE
    if [ -n "$override" ]; then
        exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
        bridge_lock_for 3 7 || fail busy
        saved_tts=$(percent notifyvol)
        [ -n "$saved_tts" ] || fail volume_unknown
        bridge_volume_journal "$BRIDGE_RUNTIME/xiaoai_bridge.tts-volume.json" "{\"tts\":$saved_tts}" || { flock -u 7;fail state_write_failed; }
        TTS_OVERRIDE_ACTIVE=true
        amixer sset notifyvol "${override}%" >/dev/null 2>&1 || fail hardware_control_failed
        flock -u 7
    fi
    bridge_duck_begin
    printf '{"stage":"synthesizing"}\n'
    bridge_profile tts_launch
    "$TTS_PATH" "$utterance" > /dev/null 2>&1 &
    tts_pid=$!
    # Inspect this invocation even when the kernel omits the children interface.
    # Descendants are optional; unrelated media never implies playing.
    while kill -0 "$tts_pid" 2>/dev/null; do
        pending=$tts_pid
        playing=false
        visited=0
        while [ -n "$pending" ] && [ "$visited" -lt 64 ]; do
            next=''
            for child in $pending; do
                case "$child" in *[!0-9]*|'') continue ;; esac
                visited=$((visited+1))
                comm=$(cat "/proc/$child/comm" 2>/dev/null || true)
                case "$comm" in miplay|mpg123|ogg123|aplay) playing=true;break ;; esac
                next="$next $(cat "/proc/$child/task/$child/children" 2>/dev/null || true)"
            done
            [ "$playing" = true ] && break
            pending=$next
        done
        if [ "$playing" = true ]; then printf '{"stage":"playing"}\n';bridge_profile tts_playing;break;fi
        sleep 0.25
    done
    result=0;wait "$tts_pid" || result=$?
    bridge_profile tts_finished
    bridge_profile tts_restore_begin
    restore_tts_volume || result=1
    bridge_profile tts_volume_restored
    bridge_duck_restore || result=1
    bridge_profile tts_duck_restored
    [ "$inherited_audio" = true ] || flock -u 8
    return "$result"
}

if [ "$MODE" = announce ]; then
    # Announcement never invokes intent detection or touches the conversation cache.
    speak "$TEXT" || fail tts_failed
    printf '{"reply_text":""}\n'
    exit 0
fi

[ -n "${EOAIC2_AGENT_ID:-}" ] || fail conversation_adapter_missing
# Retired Bridge overrides are never loaded; the original EOAIC2 config wins.
if [ "$USE_CONTEXT" = false ]; then
    EOAIC2_STATE="$WORK/context.json"
fi
[ -n "${EOAIC2_STATE:-}" ] || fail conversation_adapter_missing

if [ "$MODE" != voice ] || [ "$SNAPSHOT" = '{}' ]; then
    SNAPSHOT=$("$BRIDGE_DIR/lib/source-snapshot.sh" 2>/dev/null || printf '{"source":null,"uncertain":true}')
fi
bridge_profile sources_ready
if [ "$MODE" = voice ]; then
    guard_id="voice-$$-$(date +%s)"
    jq -nc --arg id "$guard_id" '{request_id:$id}' > "$WORK/guard-end.json"
    guard_body=$(jq -nc --arg id "$guard_id" --arg text "$TEXT" '{request_id:$id,text:$text}')
    guard_reply=$(command curl -fsS --connect-timeout 1 --max-time 2 \
        -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
        --data-binary "$guard_body" "${HA_URL%/}/api/xiaoai_speaker/$DEVICE_ID/voice_guard" 2>/dev/null || true)
    printf '%s' "$guard_reply" | jq -e '.accepted==true' >/dev/null 2>&1 && VOICE_GUARD_ACTIVE=true
    bridge_profile voice_guard_finished
fi
PLAN_BODY=$(jq -nc --arg text "$TEXT" --argjson snapshot "$SNAPSHOT" '{text:$text,snapshot:$snapshot}')
# This HA view resolves volume rules only. Conversations still originate below on LX06.
PLAN=$(command curl -sS --connect-timeout 2 --max-time 4 \
    -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
    --data-binary "$PLAN_BODY" "${HA_URL%/}/api/xiaoai_speaker/$DEVICE_ID/volume_plan" 2>/dev/null || true)
bridge_profile volume_plan_finished
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
    bridge_profile intent_finished
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
            bridge_profile conversation_begin
            if ! bridge_conversation_request "$payload"; then
                bridge_restore_conversation_state || fail context_restore_failed
                fail conversation_failed
            fi
            bridge_profile conversation_finished
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
# The response is now settled and the last TTS/temporary volume restore ended.
# Finish only this voice's visual state before serialization/HA guard cleanup;
# the parent retains audio/voice/LED ownership and its volume journal throughout.
if [ "$MODE" = voice ]; then bridge_voice_led_finish || true;fi
bridge_profile result_serialize_begin
response_json=$(bridge_json reply "$reply" "$SNAPSHOT") || fail result_serialization_failed
printf '%s\n' "$response_json"
bridge_profile result_serialize_done
