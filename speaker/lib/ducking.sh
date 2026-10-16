#!/bin/sh
# Temporary mysoftvol attenuation for a positively observed AirPlay-only route.
# Sender volume/DBus and notifyvol are never modified here.
BRIDGE_DUCK_ACTIVE=false
bridge_duck_begin() {
    [ "$MODE" != voice ] || return 0
    duck_snapshot=$("$BRIDGE_DIR/lib/source-snapshot.sh" 2>/dev/null) || return 0
    printf '%s' "$duck_snapshot" | jq -e '.active_sources==["airplay"] and .uncertain==false and
        .observed_sources=={airplay:true,bluetooth:false,mpd:false}' >/dev/null 2>&1 || return 0
    exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
    bridge_lock_for 3 7 || fail busy
    voice_busy && { flock -u 7;fail busy; }
    saved=$(percent mysoftvol)
    [ -n "$saved" ] || { flock -u 7;fail volume_unknown; }
    # Same 10% default as native wake attenuation: an absolute ceiling, never
    # a second multiplier or an increase of already quiet background audio.
    [ "$saved" -gt 10 ] || { flock -u 7;return 0; }
    bridge_volume_journal "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" "{\"master\":$saved}" || { flock -u 7;fail state_write_failed; }
    # Keep the later journal observation independent from the earlier snapshot.
    # Without a current sender baseline, leave music untouched and keep TTS.
    duck_ready=$(jq -r '.airplay_active=="true" and
        (try (.airplay_raw|tonumber|((isnan or isinfinite)|not)) catch false)' \
        "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json") || { flock -u 7;fail state_write_failed; }
    if [ "$duck_ready" != true ]; then
        rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" || { flock -u 7;fail state_write_failed; }
        flock -u 7;return 0
    fi
    duck_applied=$(bridge_json applied 10 < "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json") || { flock -u 7;fail state_write_failed; }
    atomic_json "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" "$duck_applied" || { flock -u 7;fail state_write_failed; }
    bridge_mark_owner "$BRIDGE_RUNTIME/xiaoai_bridge.duck" || { flock -u 7;fail state_write_failed; }
    BRIDGE_DUCK_ACTIVE=true
    amixer sset mysoftvol 10% >/dev/null 2>&1 || fail hardware_control_failed
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
