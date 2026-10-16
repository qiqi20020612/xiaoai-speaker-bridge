#!/bin/sh
# Capture hardware sources before any recording attenuation or TTS.
# HA applies the priority/idle policy; this script only reports observations.
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
bridge_profile_detail source_policy_begin
base=$("$BRIDGE_BIN" --snapshot 2>/dev/null) || base='{"source":null,"uncertain":true}'
bridge_profile_detail source_policy_end
bridge_profile_detail source_airplay_begin
ap=$(dbus_get_all org.gnome.ShairportSync) || ap=''
bridge_profile_detail source_airplay_end
ap_active=$(printf '%s' "$ap" | dbus_property Active)
bridge_profile_detail source_mpd_begin
mpd=$("$BRIDGE_BIN" --mpd-state 2>/dev/null) || mpd=unknown
bridge_profile_detail source_mpd_end
bridge_profile_detail source_bluez_begin
bluez=$(dbus-send --system --print-reply --reply-timeout=600 --dest=org.bluez / \
    org.freedesktop.DBus.ObjectManager.GetManagedObjects 2>/dev/null) || bluez=''
bridge_profile_detail source_bluez_end
bt_active=false
bt_observed=null
[ -z "$bluez" ] || bt_observed=false
printf '%s' "$bluez" | awk '/string "State"/{found=1;next} found&&/variant/{if($0~/string "active"/){ok=1;exit}found=0} END{exit !ok}' && bt_active=true
[ "$bt_active" != true ] || bt_observed=true
printf '%s' "$base" | bridge_json snapshot "${ap_active:-false}" "$bt_active" "$mpd" \
    "${ap_active:-null}" "$bt_observed" "${1:-0}" "${2:-null}" "${3:-null}"
