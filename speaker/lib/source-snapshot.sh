#!/bin/sh
# Capture hardware sources before any recording attenuation or TTS.
# HA applies the priority/idle policy; this script only reports observations.
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
base=$("$BRIDGE_BIN" --snapshot 2>/dev/null || printf '{"source":null,"uncertain":true}')
ap=$(dbus_get_all org.gnome.ShairportSync || true)
ap_active=$(printf '%s' "$ap" | dbus_property Active)
mpd=$("$BRIDGE_BIN" --mpd-state 2>/dev/null || printf unknown)
bluez=$(dbus-send --system --print-reply --reply-timeout=600 --dest=org.bluez / \
    org.freedesktop.DBus.ObjectManager.GetManagedObjects 2>/dev/null || true)
bt_active=false
bt_observed=null
[ -z "$bluez" ] || bt_observed=false
printf '%s' "$bluez" | awk '/string "State"/{found=1;next} found&&/variant/{if($0~/string "active"/){ok=1;exit}found=0} END{exit !ok}' && bt_active=true
[ "$bt_active" != true ] || bt_observed=true
printf '%s' "$base" | bridge_json snapshot "${ap_active:-false}" "$bt_active" "$mpd" \
    "${ap_active:-null}" "$bt_observed" "${1:-0}" "${2:-null}" "${3:-null}"
