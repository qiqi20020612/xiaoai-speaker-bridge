#!/bin/sh
# Task-local decoder entry points. Original TTS, miplay and audio bytes stay intact.
TTS_PLAYBACK_DIR=''
TTS_PLAYBACK_ERROR=''

bridge_tts_gate_prepare() {
    TTS_PLAYBACK_DIR=$WORK/playback
    TTS_PLAYBACK_ERROR=''
    mkdir "$TTS_PLAYBACK_DIR" || return 1
    for tts_decoder in mpg123 ogg123 aplay; do
        tts_executable=$(command -v "$tts_decoder" 2>/dev/null) || continue
        case "$tts_executable" in /*) ;; *) return 1 ;; esac
        printf '%s\n' "$tts_executable" > "$TTS_PLAYBACK_DIR/$tts_decoder.path" || return 1
        ln -s "$BRIDGE_DIR/lib/tts-playback.sh" "$TTS_PLAYBACK_DIR/$tts_decoder" || return 1
    done
    bridge_mark_owner "$TTS_PLAYBACK_DIR/active"
}

bridge_tts_gate_alive() {
    [ -n "${XIAOAI_TTS_GATE:-}" ] && [ -r "$XIAOAI_TTS_GATE/active" ] || return 1
    read tts_gate_owner tts_gate_start < "$XIAOAI_TTS_GATE/active" || return 1
    bridge_owner_alive "$tts_gate_owner" "$tts_gate_start"
}

bridge_tts_gate_close() {
    [ -n "$TTS_PLAYBACK_DIR" ] || return 0
    if [ -f "$TTS_PLAYBACK_DIR/failed" ]; then
        IFS= read -r TTS_PLAYBACK_ERROR < "$TTS_PLAYBACK_DIR/failed" || TTS_PLAYBACK_ERROR=tts_failed
        case "$TTS_PLAYBACK_ERROR" in
            busy|volume_unknown|state_write_failed|hardware_control_failed|tts_failed) ;;
            *) TTS_PLAYBACK_ERROR=tts_failed ;;
        esac
    fi
    # Close before taking Volume Lock: a decoder waiting for that lock must not
    # apply a new duck after task cleanup has restored the previous journal.
    rm -f "$TTS_PLAYBACK_DIR/active"
    TTS_PLAYBACK_DIR=''
    # A decoder may own the journal. Serialize with it even if it has not yet
    # committed the file; the task still owns Audio Lock throughout this step.
    BRIDGE_DUCK_ACTIVE=true
}

case "${0##*/}" in
    mpg123|ogg123|aplay)
        set -eu
        tts_decoder=${0##*/}
        . "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
        . "$BRIDGE_DIR/lib/ducking.sh"
        MODE=${XIAOAI_TTS_MODE:-announce}
        case "$MODE" in announce|send_text) ;; *) exit 2 ;; esac
        [ -n "${XIAOAI_TTS_GATE:-}" ] || exit 2
        IFS= read -r tts_executable < "$XIAOAI_TTS_GATE/$tts_decoder.path"
        case "$tts_executable" in /*) ;; *) exit 2 ;; esac
        tts_player_pid=''
        tts_player_start=''
        tts_player_started=false
        fail() {
            printf '%s\n' "$1" > "$XIAOAI_TTS_GATE/failed" || exit 1
            exit 1
        }
        tts_player_cleanup() {
            tts_player_result=$?
            trap '' HUP INT TERM
            if [ -n "$tts_player_pid" ] && [ -n "$tts_player_start" ] &&
               bridge_owner_alive "$tts_player_pid" "$tts_player_start"; then
                kill -TERM "$tts_player_pid" 2>/dev/null || true
                wait "$tts_player_pid" 2>/dev/null || true
            fi
            bridge_duck_restore || bridge_recovery_schedule || true
            if [ "$tts_player_started" = false ] && [ "$tts_player_result" -ne 0 ]; then
                # A safety failure must fail the task, not make a private TTS
                # provider synthesize its fallback and possibly repeat speech.
                if [ -s "$XIAOAI_TTS_GATE/failed" ] ||
                   printf '%s\n' tts_failed > "$XIAOAI_TTS_GATE/failed"; then exit 0;fi
            fi
            exit "$tts_player_result"
        }
        trap 'tts_player_cleanup' 0
        trap 'exit 143' HUP INT TERM
        bridge_tts_gate_alive || exit 143
        if [ "${XIAOAI_TTS_PLAYER_ACTIVE:-0}" = 1 ]; then
            trap - 0 HUP INT TERM
            exec "$tts_executable" "$@"
        fi
        bridge_duck_begin
        bridge_tts_gate_alive || exit 143
        tts_player_started=true
        # Explicitly preserve stdin: non-interactive ash otherwise redirects
        # asynchronous commands to /dev/null, breaking pipe-based decoders.
        exec 3<&0
        XIAOAI_TTS_PLAYER_ACTIVE=1 "$tts_executable" "$@" <&3 3<&- &
        tts_player_pid=$!
        exec 3<&-
        tts_player_start=$(bridge_pid_start "$tts_player_pid" 2>/dev/null || true)
        # Optional progress hint, never a reason to interrupt audio already started.
        printf '%s %s\n' "$tts_player_pid" "$tts_player_start" > "$XIAOAI_TTS_GATE/started" || true
        tts_player_result=0
        wait "$tts_player_pid" || tts_player_result=$?
        tts_player_pid=''
        exit "$tts_player_result"
        ;;
esac
