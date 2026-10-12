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
printf '%s' "$base" | jq -c --argjson ap "${ap_active:-false}" --argjson bt "$bt_active" --arg mpd "$mpd" \
    --argjson ap_observed "${ap_active:-null}" --argjson bt_observed "$bt_observed" --argjson pid "${1:-0}" --argjson master "${2:-null}" --argjson bluetooth "${3:-null}" \
    '[if $ap then "airplay" else empty end, if $bt then "bluetooth" else empty end, if $mpd=="play" then "mpd" else empty end] as $sources |
     .source as $old | .active_sources=$sources | .source=(if ($sources|index($old)) then $old elif ($sources|length)==1 then $sources[0] else null end) |
     .observed_sources={airplay:$ap_observed,bluetooth:$bt_observed,mpd:(if $mpd=="unknown" then null else $mpd=="play" end)} |
     (.priority // ["airplay","bluetooth","mpd"]) as $priority |
     .observed_sources as $observed |
     .source as $selected |
     .uncertain=(($sources|length)>1 or any($priority[]; . as $s | $observed[$s]==null and ($selected==null or ($priority|index($s))<($priority|index($selected))))) |
     if $pid>0 then .+{type:"voice_begin",pid:$pid,volumes:{master:$master,bluetooth:$bluetooth}} else . end'
