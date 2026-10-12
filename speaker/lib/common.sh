#!/bin/sh
# Trusted local helpers. Network data is never sourced, evaluated or executed.
umask 077
BRIDGE_CONFIG=${XIAOAI_BRIDGE_CONFIG:-/data/xiaoai_bridge/config.json}
BRIDGE_RUNTIME=${XIAOAI_BRIDGE_RUNTIME:-/tmp}
BRIDGE_DIR=${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}
BRIDGE_BIN=${XIAOAI_BRIDGE_BIN:-$BRIDGE_DIR/bin/xiaoai_bridge}
. "${XIAOAI_BRIDGE_HELPER_DIR:-$BRIDGE_DIR/lib}/locks.sh"

# Opt-in, bounded phase trace. Only phase names/PID/monotonic timestamps; no text,
# request payloads, credentials or responses. Linux read/printf are shell builtins.
bridge_profile() {
    [ "${XIAOAI_PROFILE:-0}" = 1 ] || return 0
    profile_file="$BRIDGE_RUNTIME/xiaoai_bridge.profile"
    if [ -f "$profile_file" ] && [ "$(wc -c < "$profile_file")" -ge 65536 ]; then
        mv -f "$profile_file" "$profile_file.1"
    fi
    profile_clock=unavailable
    [ ! -r /proc/uptime ] || read profile_clock profile_rest < /proc/uptime
    printf 'pid=%s phase=%s uptime=%s\n' "$$" "$1" "$profile_clock" >> "$profile_file"
}

fail() { printf '{"error":"%s"}\n' "$1"; exit 1; }
config_value() { jq -r "$1" "$BRIDGE_CONFIG" 2>/dev/null; }
flag() { [ "$(config_value "$1 // false")" = true ]; }
request_value() { printf '%s' "$REQUEST" | jq -r "$1"; }

voice_busy() {
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice" ] || return 1
    read voice_owner < "$BRIDGE_RUNTIME/xiaoai_bridge.voice" || return 1
    case "$voice_owner" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$voice_owner" 2>/dev/null
}

feedback_busy() {
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" ] || return 1
    read feedback_owner < "$BRIDGE_RUNTIME/xiaoai_bridge.feedback" || return 1
    case "$feedback_owner" in ''|*[!0-9]*) return 1 ;; esac
    [ "$feedback_owner" -gt 1 ] && kill -0 "$feedback_owner" 2>/dev/null
}

bridge_led_busy() { voice_busy || feedback_busy; }

hooks_ready() {
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready" ] || return 1
    read hook_owner < "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready" || return 1
    case "$hook_owner" in ''|*[!0-9]*) return 1 ;; esac
    [ "$hook_owner" -gt 1 ] && kill -0 "$hook_owner" 2>/dev/null
}

atomic_json() {
    json_target=$1
    json_tmp="$json_target.$$"
    cat > "$json_tmp" || { rm -f "$json_tmp"; return 1; }
    jq -e . "$json_tmp" >/dev/null 2>&1 || { rm -f "$json_tmp"; return 1; }
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

# Store only desired hardware values and a revision, not HA policy or a task queue.
volume_write() (
    channel=$1
    level=$2
    case "$channel" in master|tts|bluetooth|headphone|airplay) ;; *) fail unsupported_volume_target ;; esac
    printf '%s' "$level" | grep -Eq '^[0-9]+([.][0-9]+)?$' || fail invalid_volume
    awk -v n="$level" 'BEGIN {exit !(n>=0 && n<=100)}' || fail invalid_volume
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_lock_for 3 7 || fail busy
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
        if ! voice_busy || [ "$channel" = tts ] || [ "$channel" = headphone ]; then
            amixer sset "$mixer" "${level}%" >/dev/null 2>&1 || fail hardware_control_failed
        fi
    fi
    file="$BRIDGE_RUNTIME/xiaoai_bridge.desired.json"
    old='{"revision":0,"volumes":{},"versions":{}}'
    [ -r "$file" ] && old=$(cat "$file")
    printf '%s' "$old" | jq -c --arg k "$channel" --argjson v "$level" \
        '.revision=((.revision // 0)+1) | .volumes[$k]=$v | .versions[$k]=.revision' | atomic_json "$file" || fail state_write_failed
    applied=true
    voice_busy && case "$channel" in master|bluetooth) applied=false ;; esac
    printf '{"target":"%s","level":%s,"applied":%s,"deferred":%s}\n' \
        "$channel" "$level" "$applied" "$( [ "$applied" = true ] && printf false || printf true )"
)
