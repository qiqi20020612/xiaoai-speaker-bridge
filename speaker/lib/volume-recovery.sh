#!/bin/sh
# Journals survive lock failures and killed workers. No work happens on source;
# GET probes remain read-only. All mixer writes use the shared volume FD.
BRIDGE_VOLUME_RECOVERY_LOADED=1
bridge_volume_journal() {
    journal_path=$1
    journal_values=$2
    journal_duck=null
    [ -z "${3:-}" ] || journal_duck=$(cat "$3") || return 1
    journal_desired='{}'
    [ ! -r "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" ] || journal_desired=$(cat "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json")
    journal_revision=0
    [ ! -r "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" ] ||
        journal_revision=$(jq -r '.revision // 0' "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json")
    journal_ap=$(dbus_get_all org.gnome.ShairportSync || true)
    journal_raw=$(printf '%s' "$journal_ap" | dbus_property Volume)
    journal_active=$(printf '%s' "$journal_ap" | dbus_property Active)
    jq -nc --argjson values "$journal_values" --argjson revision "$journal_revision" \
        --argjson owner "$$" --arg start "$(bridge_pid_start "$$" 2>/dev/null || true)" \
        --arg raw "$journal_raw" --arg active "$journal_active" --argjson duck "$journal_duck" \
        --argjson current "${4:-null}" --argjson desired "$journal_desired" \
        '{owner:$owner,start:$start,revision:$revision,volumes:$values,airplay_raw:$raw,airplay_active:$active} |
        if $duck!=null then .revision=$duck.revision |
          if ($active=="" or ($current==$duck.applied_master)) then
            .airplay_raw=$duck.airplay_raw | .airplay_active=$duck.airplay_active | .applied_master=$duck.applied_master
          elif ($raw!=$duck.airplay_raw or $active!=$duck.airplay_active) and
               (($desired.versions.master // 0)<=$duck.revision) then .volumes.master=$current
          else .applied_master=$duck.applied_master end
        else . end' |
        atomic_json "$journal_path"
}

# Called with FD 7 already held. Keep the journal on any error; remove it only
# after every channel is restored, or deliberately yielded to a newer sender.
bridge_volume_recover_locked() (
    recovery_file=$1
    [ -r "$recovery_file" ] || exit 0
    recovery_desired='{"versions":{},"volumes":{}}'
    [ ! -r "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" ] ||
        recovery_desired=$(cat "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json")
    recovery_ap=$(dbus_get_all org.gnome.ShairportSync || true)
    recovery_raw=$(printf '%s' "$recovery_ap" | dbus_property Volume)
    recovery_active=$(printf '%s' "$recovery_ap" | dbus_property Active)
    recovery_master=$(percent mysoftvol)
    recovery_plan=$(printf '%s' "$recovery_desired" | jq -r \
        --slurpfile saved "$recovery_file" --arg raw "$recovery_raw" --arg active "$recovery_active" --arg master "$recovery_master" '
        . as $d | $saved[0] as $s | $s.volumes | to_entries[] |
        if (.value|type)!="number" then error("invalid_journal")
        elif ($d.versions[.key] // 0)>$s.revision then [.key,($d.volumes[.key]|tostring)]|@tsv
        elif (.key=="master" and ($s.airplay_active=="true" or $active=="true") and
            (($s.airplay_raw!=$raw) or ($s.airplay_active!=$active))) then
          if $active=="" then [.key,"pending"]|@tsv
          elif ($s.applied_master!=null and ($s.applied_master|tostring)==$master) then [.key,"pending"]|@tsv
          else empty end
        else [.key,(.value|tostring)]|@tsv end' 2>/dev/null) || exit 1
    printf '%s\n' "$recovery_plan" | {
      recovery_failed=false
      while read -r recovery_channel recovery_value; do
        [ -n "$recovery_channel" ] || continue
        if [ "$recovery_value" = pending ]; then recovery_failed=true;continue;fi
        recovery_mixer=$(mixer_name "$recovery_channel") || exit 1
        if ! amixer sset "$recovery_mixer" "${recovery_value}%" >/dev/null 2>&1; then recovery_failed=true;continue;fi
        if [ "${2:-}" != keep ]; then
            jq --arg channel "$recovery_channel" 'del(.volumes[$channel])' "$recovery_file" | atomic_json "$recovery_file" || exit 1
        fi
      done
      [ "$recovery_failed" = false ]
    } || exit 1
    [ "${2:-}" = keep ] || rm -f "$recovery_file"
)

bridge_volume_recover() (
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_lock_for "${2:-3}" 7 || exit 1
    bridge_volume_recover_locked "$1" "${3:-}"
)

bridge_recover_pending() (
    # Active owners retain their attenuation. Voice journals are eligible after
    # voice_end even though the persistent listener process is still alive.
    recovery_failed=0
    for recovery_kind in voice-volume duck tts-volume; do
        recovery_path="$BRIDGE_RUNTIME/xiaoai_bridge.$recovery_kind.json"
        [ -r "$recovery_path" ] || continue
        # A failed master restore must not strand the independent notifyvol.
        # Voice owns master/bluetooth, but dead TTS owners can safely restore
        # their own channel while that voice is active, respecting revisions.
        if voice_busy && [ "$recovery_kind" != tts-volume ]; then recovery_failed=1;continue;fi
        if [ "$recovery_kind" != voice-volume ]; then
            recovery_owner=$(jq -r '.owner // 0' "$recovery_path")
            recovery_start=$(jq -r '.start // ""' "$recovery_path")
            if bridge_owner_alive "$recovery_owner" "$recovery_start"; then
                [ "${2:-}" != wait ] || recovery_failed=1
                continue
            fi
        fi
        if ! bridge_volume_recover "$recovery_path" "${1:-3}"; then recovery_failed=1;continue;fi
        [ "$recovery_kind" != duck ] || rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.duck"
    done
    exit "$recovery_failed"
)

bridge_has_pending_recovery() {
    voice_busy && return 1
    [ ! -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" ] || return 0
    for recovery_kind in duck tts-volume; do
        recovery_path="$BRIDGE_RUNTIME/xiaoai_bridge.$recovery_kind.json"
        [ -r "$recovery_path" ] || continue
        recovery_owner=$(jq -r '.owner // 0' "$recovery_path")
        recovery_start=$(jq -r '.start // ""' "$recovery_path")
        bridge_owner_alive "$recovery_owner" "$recovery_start" || return 0
    done
    return 1
}

bridge_recovery_schedule() {
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" ] ||
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" ] ||
    [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.tts-volume.json" ] || return 0
    "$BRIDGE_DIR/lib/volume-recovery.sh" recover 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- \
        </dev/null >/dev/null 2>&1 &
}

if [ "${0##*/}" = volume-recovery.sh ]; then
    . "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
    exec 3> "$BRIDGE_RUNTIME/xiaoai_bridge.recovery.lock"
    bridge_lock_for 0 3 || exit 0
    recovery_attempt=0
    while [ "$recovery_attempt" -lt 20 ]; do
        bridge_recover_pending 1 wait && exit 0
        sleep 1
        recovery_attempt=$((recovery_attempt+1))
    done
    exit 1
fi
