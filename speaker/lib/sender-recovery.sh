#!/bin/sh
# Only a PENDING Duck needs this on-demand check. Never sends a volume command
# to Shairport/iPhone: resolve its current shared-mixer gain from the running
# process's explicit, unchanged configuration and the real ALSA dB TLV.
bridge_sender_target() (
    case "$recovery_file" in
        "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json") ;;
        *) jq -e '.sender_recovery==true' "$recovery_file" >/dev/null 2>&1 || exit 1 ;;
    esac
    sender_baseline=$(bridge_json journal-fields "$recovery_file") || exit 1
    { IFS= read -r sender_baseline;IFS= read -r sender_revision; } <<EOF
$sender_baseline
EOF
    if [ "$recovery_active" = false ]; then
        printf '%s%%\n0\n0\nnone\n' "$sender_baseline"
        exit 0
    fi
    [ "$recovery_active" = true ] || exit 1
    sender_fields=$(printf '%s' "$recovery_ap" | dbus_property Version VolumeControlProfile)
    { IFS= read -r sender_version;IFS= read -r sender_profile; } <<EOF
$sender_fields
EOF
    [ "$sender_version" = 4.3.5 ] || exit 1
    sender_pid=$(pidof shairport-sync 2>/dev/null) || exit 1
    case "$sender_pid" in ''|*[!0-9]*) exit 1 ;; esac
    sender_start=$(bridge_pid_start "$sender_pid") || exit 1
    [ -n "$sender_start" ] || exit 1
    sender_args=$(tr '\000' '\n' < "/proc/$sender_pid/cmdline") || exit 1
    sender_config='';sender_option='';sender_first=true
    while IFS= read -r sender_arg; do
        if [ "$sender_first" = true ]; then
            case "$sender_arg" in */shairport-sync|shairport-sync) ;; *) exit 1 ;; esac
            sender_first=false;continue
        fi
        case "$sender_option" in
            config) [ -z "$sender_config" ] || exit 1;sender_config=$sender_arg;sender_option='';continue ;;
            output) [ "$sender_arg" = alsa ] || exit 1;sender_option='';continue ;;
        esac
        case "$sender_arg" in
            -c|--configfile) sender_option=config ;;
            --configfile=*) [ -z "$sender_config" ] || exit 1;sender_config=${sender_arg#*=} ;;
            -o|--output) sender_option=output ;;
            --output=alsa|-d|--daemon|-v|--verbose) ;;
            *) exit 1 ;;
        esac
    done <<EOF
$sender_args
EOF
    [ -z "$sender_option" ] || exit 1
    case "$sender_config" in /*) ;; *) exit 1 ;; esac
    sender_control=$(amixer cget name=mysoftvol 2>/dev/null) || exit 1
    sender_target=$(printf '%s\n' "$sender_control" | "$BRIDGE_JSON_BIN" --airplay-volume-target \
        "$sender_config" "$recovery_raw" "$sender_profile" "$sender_baseline" "$sender_start") || exit 1
    case "$sender_target" in *%)
        # Do not lift a cap for mute until Shairport has actually muted its
        # stream; the writable main property can be ahead of that callback.
        sender_remote=$(dbus_get_all org.gnome.ShairportSync.RemoteControl) || exit 1
        printf '%s' "$sender_remote" | dbus_property AirplayVolume | jq -e '.==-144' >/dev/null 2>&1 || exit 1
        ;;
    esac
    bridge_owner_alive "$sender_pid" "$sender_start" || exit 1
    printf '%s\n%s\n%s\n%s\n' "$sender_target" "$sender_pid" "$sender_start" "$sender_profile"
)

# Runs only inside the caller's volume-locked recovery subshell.
bridge_sender_restore() {
    recovery_file=$1;recovery_ap=$2;recovery_raw=$3;recovery_active=$4
    sender_result=$(bridge_sender_target) || { return 1; }
    { IFS= read -r sender_target;IFS= read -r sender_pid;IFS= read -r sender_start;IFS= read -r sender_profile; } <<EOF
$sender_result
EOF
    # Commit before writing: SIGKILL or another slider update must not
    # turn our own repair into an untracked, apparently external value.
    sender_marked=$(bridge_json sender-recovery < "$recovery_file") || return 1
    atomic_json "$recovery_file" "$sender_marked" || return 1
    if ! amixer -- sset mysoftvol "$sender_target" >/dev/null 2>&1; then return 1;fi
    sender_after=$(dbus_get_all org.gnome.ShairportSync) || sender_after=''
    sender_after_fields=$(printf '%s' "$sender_after" | dbus_property Volume Active)
    { IFS= read -r sender_after_raw;IFS= read -r sender_after_active || sender_after_active=''; } <<EOF
$sender_after_fields
EOF
    sender_after_info=$(printf '%s' "$sender_after" | dbus_property Version VolumeControlProfile)
    { IFS= read -r sender_after_version;IFS= read -r sender_after_profile || sender_after_profile=''; } <<EOF
$sender_after_info
EOF
    # RemoteControl is published from volume metadata after Shairport applies
    # its gain, rounded to two decimals by player_send_volume_metadata(). Main
    # Volume must still match exactly; do not round away a concurrent update.
    sender_committed=$recovery_raw
    if [ "$sender_profile" != none ]; then
        sender_remote=$(dbus_get_all org.gnome.ShairportSync.RemoteControl) || sender_remote=''
        sender_committed=$(printf '%s' "$sender_remote" | dbus_property AirplayVolume)
    fi
    if [ "$sender_after_active" != "$recovery_active" ] ||
       ! printf '%s' "$sender_after_raw" | jq -e 'type=="number" and ((isnan or isinfinite)|not)' >/dev/null 2>&1 ||
       ! printf '%s' "$sender_committed" | jq -e 'type=="number" and ((isnan or isinfinite)|not)' >/dev/null 2>&1 ||
       ! LC_ALL=C awk -v before="$recovery_raw" -v after="$sender_after_raw" -v committed="$sender_committed" -v profile="$sender_profile" \
           'BEGIN{expected=(profile=="none" ? before+0 : sprintf("%.2f",before)+0);exit !(after!="" && committed!="" && before+0==after+0 && expected==committed+0)}' ||
       { [ "$sender_profile" != none ] &&
         { [ "$sender_after_version" != 4.3.5 ] || [ "$sender_after_profile" != "$sender_profile" ] ||
           ! bridge_owner_alive "$sender_pid" "$sender_start"; }; }; then
        return 1
    fi
}
