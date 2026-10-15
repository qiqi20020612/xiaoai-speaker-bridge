#!/bin/sh
# Trusted local helpers. Network data is never sourced, evaluated or executed.
umask 077
BRIDGE_CONFIG=${XIAOAI_BRIDGE_CONFIG:-/data/xiaoai_bridge/config.json}
BRIDGE_RUNTIME=${XIAOAI_BRIDGE_RUNTIME:-/tmp}
BRIDGE_DIR=${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}
BRIDGE_BIN=${XIAOAI_BRIDGE_BIN:-$BRIDGE_DIR/bin/xiaoai_bridge}
BRIDGE_JSON_BIN=${XIAOAI_BRIDGE_JSON_BIN:-$BRIDGE_BIN}
. "${XIAOAI_BRIDGE_HELPER_DIR:-$BRIDGE_DIR/lib}/locks.sh"

bridge_mark_owner() {
    printf '%s %s\n' "$$" "$(bridge_pid_start "$$" 2>/dev/null || true)" > "$1"
}
bridge_listener_pid() {
    bridge_owner_alive "$1" "${2:-}" || return 1
    tr '\000' '\n' < "/proc/$1/cmdline" 2>/dev/null |
        grep -qxF "${XIAOAI_LISTENER_PATH:-$BRIDGE_DIR/adapter/listener.sh}"
}

# Opt-in, bounded phase trace. Only phase names/PID/monotonic timestamps; no text,
# request payloads, credentials or responses. Linux read/printf are shell builtins.
bridge_profile() {
    [ "${XIAOAI_PROFILE:-0}" = 1 ] || return 0
    profile_file="$BRIDGE_RUNTIME/xiaoai_bridge.profile"
    [ ! -L "$profile_file" ] || return 0
    if [ -f "$profile_file" ] && [ "$(wc -c < "$profile_file")" -ge 65536 ]; then
        mv -f "$profile_file" "$profile_file.1" || return 0
    fi
    profile_clock=unavailable
    [ ! -r /proc/uptime ] || read profile_clock profile_rest < /proc/uptime
    profile_cycle=${XIAOAI_VOICE_CYCLE:-}
    profile_suffix=''
    case "$profile_cycle" in ''|*[!0-9.]*) ;; *) profile_suffix=" cycle=$profile_cycle" ;; esac
    printf 'pid=%s phase=%s uptime=%s%s\n' "$$" "$1" "$profile_clock" "$profile_suffix" >> "$profile_file" || true
}

# Extra query/lock spans use the same logger, with a separate opt-in because
# logging itself has observer cost. Disabled calls execute only shell builtins.
bridge_profile_detail() {
    [ "${XIAOAI_PROFILE_DETAIL:-0}" != 1 ] || bridge_profile "$1"
}

bridge_diagnostic_error() {
    # A stable error code only. Never include text, config, OAuth or responses.
    [ "${BRIDGE_TEXT_DIAGNOSTICS:-0}" = 1 ] || return 0
    case "$1" in ''|*[!a-z_]*) return 0 ;; esac
    diagnostic_file="$BRIDGE_RUNTIME/xiaoai_bridge.text-errors"
    [ ! -L "$diagnostic_file" ] || return 0
    if [ -f "$diagnostic_file" ] && [ "$(wc -c < "$diagnostic_file")" -ge 65536 ]; then
        mv -f "$diagnostic_file" "$diagnostic_file.1" || return 0
    fi
    printf 'pid=%s error=%s\n' "$$" "$1" >> "$diagnostic_file"
}
fail() { bridge_diagnostic_error "$1" || true;printf '{"error":"%s"}\n' "$1"; exit 1; }
bridge_json() { "$BRIDGE_JSON_BIN" --json-op "$@"; }
config_value() { jq -r "$1" "$BRIDGE_CONFIG" 2>/dev/null; }
flag() { [ "$(config_value "$1 // false")" = true ]; }
request_value() { printf '%s' "$REQUEST" | jq -r "$1"; }

voice_busy() {
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice" ] || return 1
    read voice_owner voice_start < "$BRIDGE_RUNTIME/xiaoai_bridge.voice" || return 1
    bridge_owner_alive "$voice_owner" "$voice_start"
}

feedback_busy() {
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" ] || return 1
    read feedback_owner feedback_start < "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" || return 1
    bridge_owner_alive "$feedback_owner" "$feedback_start"
}

bridge_led_busy() { voice_busy || feedback_busy; }

duck_busy() {
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.duck" ] || return 1
    read duck_owner duck_start < "$BRIDGE_RUNTIME/xiaoai_bridge.duck" || return 1
    bridge_owner_alive "$duck_owner" "$duck_start"
}

hooks_ready() {
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready" ] || return 1
    read hook_owner hook_start hook_version < "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready" || return 1
    [ "$hook_version" = 4 ] && bridge_listener_pid "$hook_owner" "$hook_start"
}

atomic_json() {
    json_target=$1
    json_tmp="$json_target.$$"
    cat > "$json_tmp" || { rm -f "$json_tmp"; return 1; }
    bridge_json validate < "$json_tmp" >/dev/null 2>&1 || { rm -f "$json_tmp"; return 1; }
    chmod 600 "$json_tmp"
    mv -f "$json_tmp" "$json_target"
}

dbus_get_all() {
    dbus-send --system --print-reply --reply-timeout=600 \
        --dest=org.gnome.ShairportSync /org/gnome/ShairportSync \
        org.freedesktop.DBus.Properties.GetAll "string:$1" 2>/dev/null
}

# dbus-send's property output is parsed as text, never as executable shell.
dbus_property() {
    awk -v key="$1" '
        $0 ~ "string \"" key "\"" { found=1; next }
        found && /variant/ {
            sub(/^.*variant[[:space:]]+/, "")
            sub(/^(boolean|double|int32|int64|string)[[:space:]]+/, "")
            sub(/^"/, ""); sub(/"$/, "")
            print; exit
        }'
}

percent() {
    amixer get "$1" 2>/dev/null | awk 'match($0,/\[[0-9]+%\]/) {v=substr($0,RSTART+1,RLENGTH-3); print v; exit}'
}

mixer_name() {
    case "$1" in
        master) printf '%s' mysoftvol ;;
        tts) printf '%s' notifyvol ;;
        bluetooth) printf '%s' bluetooth ;;
        headphone) printf '%s' 'headphone volume' ;;
        *) return 1 ;;
    esac
}

[ "${BRIDGE_VOLUME_RECOVERY_LOADED:-0}" = 1 ] ||
    . "${XIAOAI_BRIDGE_HELPER_DIR:-$BRIDGE_DIR/lib}/volume-recovery.sh"
. "${XIAOAI_BRIDGE_HELPER_DIR:-$BRIDGE_DIR/lib}/voice-led.sh"

# Store only desired hardware values and a revision, not HA policy or a task queue.
volume_write() (
    channel=$1
    level=$2
    case "$channel" in master|tts|bluetooth|headphone|airplay) ;; *) fail unsupported_volume_target ;; esac
    printf '%s' "$level" | grep -Eq '^[0-9]+([.][0-9]+)?$' || fail invalid_volume
    awk -v n="$level" 'BEGIN {exit !(n>=0 && n<=100)}' || fail invalid_volume
    # An explicit new target must remain usable to resolve an ambiguous sender
    # recovery; its revision supersedes the retained baseline under this lock.
    if ! voice_busy; then bridge_recover_pending || true;fi
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_lock_for 3 7 || fail busy
    file="$BRIDGE_RUNTIME/xiaoai_bridge.desired.json"
    old='{"revision":0,"volumes":{},"versions":{}}'
    [ -r "$file" ] && old=$(cat "$file")
    desired_update=$(printf '%s' "$old" | bridge_json desired-update "$channel" "$level") || fail state_write_failed
    if [ "$channel" = airplay ]; then
        flag '.verified.airplay_volume' || fail unsupported_volume_target
        [ "$(dbus_get_all org.gnome.ShairportSync.RemoteControl | dbus_property Available)" = true ] || fail volume_unavailable
        value=$(awk -v n="$level" 'BEGIN { if(n==0)print -144;else printf "%.6f", n*0.3-30 }')
        dbus-send --system --print-reply --reply-timeout=1500 \
            --dest=org.gnome.ShairportSync /org/gnome/ShairportSync \
            org.gnome.ShairportSync.RemoteControl.SetAirplayVolume "double:$value" \
            >/dev/null 2>&1 || fail hardware_control_failed
    else
        mixer=$(mixer_name "$channel") || fail unsupported_volume_target
        amixer get "$mixer" >/dev/null 2>&1 || fail unsupported_volume_target
        case "$channel" in
            master|bluetooth)
                if /etc/init.d/listener status >/dev/null 2>&1 && ! hooks_ready; then
                    fail voice_adapter_not_ready
                fi
                ;;
        esac
        # During recording, preserve attenuation; the listener's safe restore applies the new value.
        defer=false
        voice_busy && case "$channel" in master|bluetooth) defer=true ;; esac
        duck_busy && [ "$channel" = master ] && defer=true
        if [ "$defer" = false ]; then
            amixer sset "$mixer" "${level}%" >/dev/null 2>&1 || fail hardware_control_failed
        fi
    fi
    printf '%s' "$desired_update" | atomic_json "$file" || fail state_write_failed
    applied=true
    voice_busy && case "$channel" in master|bluetooth) applied=false ;; esac
    duck_busy && [ "$channel" = master ] && applied=false
    bridge_recovery_schedule
    printf '{"target":"%s","level":%s,"applied":%s,"deferred":%s}\n' \
        "$channel" "$level" "$applied" "$( [ "$applied" = true ] && printf false || printf true )"
)
