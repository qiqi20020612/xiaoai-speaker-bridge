#!/bin/sh
# Temporary mysoftvol attenuation only for an explicitly verified AirPlay route.
# Sender volume/DBus and notifyvol are never modified here.
BRIDGE_DUCK_ACTIVE=false
BRIDGE_DUCK_SAVED=''
BRIDGE_DUCK_REVISION=0
bridge_duck_begin() {
    [ "$MODE" != voice ] && [ -n "$DUCK_VOLUME" ] && [ "$DUCK_VERIFIED" = true ] || return 0
    duck_snapshot=$("$BRIDGE_DIR/lib/source-snapshot.sh" 2>/dev/null) || return 0
    printf '%s' "$duck_snapshot" | jq -e '.active_sources==["airplay"] and .uncertain==false' >/dev/null 2>&1 || return 0
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_lock_for 3 7 || fail busy
    voice_busy && { flock -u 7;fail busy; }
    saved=$(percent mysoftvol)
    [ -n "$saved" ] || { flock -u 7;fail volume_unknown; }
    awk -v target="$DUCK_VOLUME" -v current="$saved" 'BEGIN {exit !(target<current)}' || { flock -u 7;return 0; }
    rev=0
    [ ! -r "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json" ] || rev=$(jq -r '.revision // 0' "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json")
    bridge_volume_journal "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" "{\"master\":$saved}" || { flock -u 7;fail state_write_failed; }
    jq --argjson applied "$DUCK_VOLUME" '.applied_master=$applied' "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" |
        atomic_json "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" || { flock -u 7;fail state_write_failed; }
    bridge_mark_owner "$BRIDGE_RUNTIME/xiaoai_bridge.duck"
    BRIDGE_DUCK_SAVED=$saved
    BRIDGE_DUCK_REVISION=$rev
    BRIDGE_DUCK_ACTIVE=true
    amixer sset mysoftvol "${DUCK_VOLUME}%" >/dev/null 2>&1 || fail hardware_control_failed
    flock -u 7
    bridge_profile duck_applied
}
bridge_duck_restore() {
    [ "$BRIDGE_DUCK_ACTIVE" = true ] || return 0
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_lock_for 3 7 || return 1
    if ! voice_busy; then
        bridge_volume_recover_locked "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" || { flock -u 7;return 1; }
        rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.duck"
    fi
    BRIDGE_DUCK_ACTIVE=false
    flock -u 7
    bridge_profile duck_restored_or_transferred
}
