#!/bin/sh
set -eu
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
REQUEST=$(cat)
[ "${#REQUEST}" -le 12288 ] || fail request_too_large
printf '%s' "$REQUEST" | jq -e 'type=="object"' >/dev/null || fail invalid_json

probe() {
    controls=$(amixer scontrols 2>/dev/null || true)
    has_master=false; has_tts=false; has_bt=false; has_headphone=false
    printf '%s' "$controls" | grep -q "'mysoftvol'" && has_master=true
    printf '%s' "$controls" | grep -q "'notifyvol'" && has_tts=true
    printf '%s' "$controls" | grep -q "'bluetooth'" && has_bt=true
    printf '%s' "$controls" | grep -q "'headphone volume'" && has_headphone=true
    master=null; tts=null; bt=null; headphone=null
    [ "$has_master" = true ] && master=$(percent mysoftvol)
    [ "$has_tts" = true ] && tts=$(percent notifyvol)
    [ "$has_bt" = true ] && bt=$(percent bluetooth)
    [ "$has_headphone" = true ] && headphone=$(percent 'headphone volume')
    master=${master:-null}; tts=${tts:-null}; bt=${bt:-null}; headphone=${headphone:-null}
    ap=$(dbus_get_all org.gnome.ShairportSync || true)
    apr=$(dbus_get_all org.gnome.ShairportSync.RemoteControl || true)
    ap_active=$(printf '%s' "$ap" | dbus_property Active)
    ap_raw=$(printf '%s' "$ap" | dbus_property Volume)
    ap_available=$(printf '%s' "$apr" | dbus_property Available)
    ap_state=$(printf '%s' "$apr" | dbus_property PlayerState)
    ap_read=false;[ -n "$ap_active" ] && [ -n "$ap_raw" ] && ap_read=true
    ap_active=${ap_active:-null};ap_raw=${ap_raw:-null};ap_available=${ap_available:-false}
    ap_pct=null; ap_muted=null
    if [ "$ap_raw" != null ]; then
        ap_muted=false
        ap_pct=$(awk -v v="$ap_raw" 'BEGIN {if(v<=-144){print 0;exit} if(v< -30)v=-30;if(v>0)v=0;printf "%.2f",(v+30)/0.3}')
        awk -v v="$ap_raw" 'BEGIN{exit !(v<=-144)}' && ap_muted=true
    fi
    [ -n "$ap_state" ] || { [ "$ap_active" = true ] && ap_state=playing || ap_state=stopped; }
    [ "$ap_read" = true ] || ap_state=unknown
    bt_player=$(timeout 1 bluetoothctl player.show 2>/dev/null || true)
    bt_state=$(printf '%s' "$bt_player" | awk '/Status:/ {print $2;exit}')
    bt_name=$(printf '%s' "$bt_player" | sed -n 's/.*Name: //p' | head -n 1)
    bt_active=false;[ "$bt_state" = playing ] && bt_active=true
    bluez=$(dbus-send --system --print-reply --reply-timeout=600 --dest=org.bluez / \
        org.freedesktop.DBus.ObjectManager.GetManagedObjects 2>/dev/null || true)
    bt_read=false;[ -n "$bluez" ] || [ -n "$bt_state" ] && bt_read=true
    [ "$bt_read" = true ] || bt_active=null
    printf '%s' "$bluez" | awk '/string "State"/{found=1;next} found&&/variant/{if($0~/string "active"/){ok=1;exit}found=0} END{exit !ok}' && bt_active=true
    listener=false; /etc/init.d/listener status >/dev/null 2>&1 && listener=true
    hook=false;hooks_ready && hook=true
    write_master=$has_master;write_bt=$has_bt
    if [ "$listener" = true ] && [ "$hook" = false ]; then write_master=false;write_bt=false;fi
    tts_path=$(config_value '.tts_path // "/data/tts_doubao"')
    can_tts=false;[ -x "$tts_path" ] && { [ "$hook" = true ] || [ "$listener" = false ]; } && can_tts=true
    can_conversation=false
    [ -r /data/eoai_agent.sh ] && { [ "$hook" = true ] || [ "$listener" = false ]; } && can_conversation=true
    airplay_write=false;flag '.verified.airplay_volume' && [ "$ap_read" = true ] && airplay_write=true
    ap_actions='[]';bt_actions='[]'
    if flag '.verified.airplay_playback' && [ "$ap_available" = true ]; then
        ap_actions='["play","pause","stop","next","previous","mute","unmute"]'
    fi
    if flag '.verified.bluetooth_playback' && [ -n "$bt_state" ]; then
        bt_actions='["play","pause","stop","next","previous"]'
    fi
    ir=false
    flag '.verified.infrared' && [ -w /sys/ir_tx_gpio/ir_data ] && ir=true
    announcement_ducking=false
    flag '.verified.announcement_ducking' && announcement_ducking=true
    recovery_pending=false
    bridge_has_pending_recovery && recovery_pending=true
    [ "$recovery_pending" = false ] || announcement_ducking=false
    uptime=$(cut -d. -f1 /proc/uptime)
    available=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
    total=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
    read load rest < /proc/loadavg
    rss=null; peak=null
    if [ -n "${XIAOAI_BRIDGE_PID:-}" ] && [ -r "/proc/$XIAOAI_BRIDGE_PID/status" ]; then
        rss=$(awk '/^VmRSS:/{print $2}' "/proc/$XIAOAI_BRIDGE_PID/status")
        peak=$(awk '/^VmHWM:/{print $2}' "/proc/$XIAOAI_BRIDGE_PID/status")
    fi
    rss=${rss:-null};peak=${peak:-null}
    remaining=null
    if [ -r "$BRIDGE_RUNTIME/eoai_conversation.json" ]; then
        remaining=$(jq -r --argjson now "$(date +%s)" 'if (.ts|type)=="number" then [0, 300-($now-.ts)]|max else null end' "$BRIDGE_RUNTIME/eoai_conversation.json" 2>/dev/null || printf null)
    fi
    jq -nc \
        --argjson master "$master" --argjson tts "$tts" --argjson bt "$bt" --argjson headphone "$headphone" \
        --argjson has_master "$has_master" --argjson has_tts "$has_tts" --argjson has_bt "$has_bt" --argjson has_headphone "$has_headphone" \
        --argjson write_master "$write_master" --argjson write_bt "$write_bt" \
        --argjson ap_active "$ap_active" --argjson ap_raw "$ap_raw" --argjson ap_pct "$ap_pct" --argjson ap_muted "$ap_muted" --arg ap_state "$ap_state" \
        --argjson ap_actions "$ap_actions" --argjson bt_actions "$bt_actions" --argjson bt_active "$bt_active" --arg bt_state "${bt_state:-unknown}" --arg bt_name "$bt_name" \
        --argjson ap_read "$ap_read" --argjson bt_read "$bt_read" --argjson ap_available "$ap_available" \
        --argjson listener "$listener" --argjson hook "$hook" --argjson can_tts "$can_tts" --argjson can_conversation "$can_conversation" \
        --argjson ap_write "$airplay_write" --argjson ir "$ir" \
        --argjson announcement_ducking "$announcement_ducking" \
        --argjson recovery_pending "$recovery_pending" \
        --argjson uptime "$uptime" --argjson available "$available" --argjson total "$total" --argjson load "$load" --argjson rss "$rss" --argjson peak "$peak" \
        --argjson remaining "$remaining" \
        '{capabilities:{volume:{master:$write_master,tts:$has_tts,bluetooth:$write_bt,headphone:$has_headphone,airplay:$ap_write},volume_read:{master:$has_master,tts:$has_tts,bluetooth:$has_bt,headphone:$has_headphone,airplay:$ap_read},
          playback:{airplay:{read:$ap_read,actions:$ap_actions},bluetooth:{read:$bt_read,actions:$bt_actions}},
          announce:$can_tts,conversation:$can_conversation,voice_hooks:$hook,listener:true,infrared:$ir,announcement_ducking:$announcement_ducking},
          state:{volume_recovery_pending:$recovery_pending,volumes:{master:$master,tts:$tts,bluetooth:$bt,headphone:$headphone,airplay:$ap_pct},
          airplay:{active:$ap_active,available:$ap_available,state:$ap_state,raw_volume:$ap_raw,volume:$ap_pct,muted:$ap_muted},
          bluetooth:{active:$bt_active,state:$bt_state,connected_name:$bt_name},listener:{running:$listener},
          system:{uptime:$uptime,memory_available:$available,memory_total:$total,cpu_load:$load,bridge_rss:$rss,bridge_peak_rss:$peak},
          conversation:{remaining:$remaining}}}'
}

case "$1" in
    probe) probe ;;
    volume)
        volume_write "$(request_value '.target')" "$(request_value '.level')"
        ;;
    announce|send_text)
        exec "$BRIDGE_DIR/lib/text.sh" "$1" "$REQUEST"
        ;;
    playback)
        source=$(request_value '.source');action=$(request_value '.action')
        case "$action" in play|pause|stop|next|previous|mute|unmute) ;; *) fail unsupported_playback_action ;; esac
        if [ "$source" = airplay ]; then
            flag '.verified.airplay_playback' || fail unsupported_playback_action
            [ "$(dbus_get_all org.gnome.ShairportSync.RemoteControl | dbus_property Available)" = true ] || fail playback_unavailable
            case "$action" in play) method=Play ;; pause) method=Pause ;; stop) method=Stop ;; next) method=Next ;; previous) method=Previous ;; mute|unmute) method=ToggleMute ;; esac
            if [ "$action" = mute ] || [ "$action" = unmute ]; then
                raw=$(dbus_get_all org.gnome.ShairportSync | dbus_property Volume)
                [ -n "$raw" ] || fail state_unknown
                muted=false;awk -v v="$raw" 'BEGIN{exit !(v<=-144)}' && muted=true
                if { [ "$action" = mute ] && [ "$muted" = true ]; } || { [ "$action" = unmute ] && [ "$muted" = false ]; }; then
                    printf '{"completed":true}\n';exit 0
                fi
            fi
            dbus-send --system --print-reply --reply-timeout=1500 --dest=org.gnome.ShairportSync \
                /org/gnome/ShairportSync "org.gnome.ShairportSync.RemoteControl.$method" \
                >/dev/null 2>&1 || fail hardware_control_failed
        elif [ "$source" = bluetooth ]; then
            flag '.verified.bluetooth_playback' || fail unsupported_playback_action
            case "$action" in mute|unmute) fail unsupported_playback_action ;; esac
            [ -n "$(timeout 1 bluetoothctl player.show 2>/dev/null | awk '/Status:/{print $2;exit}')" ] || fail playback_unavailable
            timeout 3 bluetoothctl "player.$action" >/dev/null 2>&1 || fail hardware_control_failed
        else
            # HA talks to MPD directly; it can never stand in for other audio sources.
            fail unsupported_playback_source
        fi
        printf '{"completed":true}\n'
        ;;
    listener)
        action=$(request_value '.action')
        case "$action" in start|stop|restart) ;; *) fail unsupported_action ;; esac
        voice_busy && fail busy
        "$BRIDGE_DIR/lib/listener-service.sh" "$action" >/dev/null 2>&1 || fail listener_control_failed
        printf '{"completed":true}\n'
        ;;
    reset_context)
        exec 9> "$BRIDGE_RUNTIME/xiaoai_bridge.session.lock"
        flock -n 9 || fail busy
        voice_busy && fail busy
        config=$(config_value '.listener_config // "/data/listener"')
        [ -r "$config" ] || fail listener_config_missing
        . "$config"
        state=${EOAIC2_STATE:-}
        case "$state" in /tmp/*) ;; *) fail conversation_adapter_missing ;; esac
        if [ -r "$BRIDGE_RUNTIME/xiaoai_bridge.agent.json" ]; then
            agent=$(jq -r '.agent_id // empty' "$BRIDGE_RUNTIME/xiaoai_bridge.agent.json")
            bridge_valid_agent "$agent" || fail invalid_agent
            [ "$agent" != "${EOAIC2_AGENT_ID:-}" ] && state="$BRIDGE_RUNTIME/xiaoai_bridge.context.$agent.json"
        fi
        rm -f "$state"
        printf '{"completed":true}\n'
        ;;
    agent)
        agent=$(request_value '.agent_id')
        if [ "$agent" != null ]; then
            bridge_valid_agent "$agent" || fail invalid_agent
        fi
        exec 9> "$BRIDGE_RUNTIME/xiaoai_bridge.session.lock"
        flock -n 9 || fail busy
        voice_busy && fail busy
        if [ "$agent" = null ];then
            rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.agent.json"
        else
            printf '%s' "$REQUEST" | jq -c '{agent_id:.agent_id}' | atomic_json "$BRIDGE_RUNTIME/xiaoai_bridge.agent.json"
        fi
        printf '{"completed":true}\n'
        ;;
    ir)
        flag '.verified.infrared' || fail unsupported_infrared
        [ -w /sys/ir_tx_gpio/ir_data ] || fail unsupported_infrared
        [ "$(request_value '.carrier // 38000')" = 38000 ] || fail unsupported_carrier
        code=$(request_value '.code')
        printf '%s' "$code" | grep -Eq '^-?[0-9]+(,-?[0-9]+)*$' || fail invalid_ir
        printf '%s' "$code" | awk -F, 'NF>512{exit 1}{for(i=1;i<=NF;i++)if($i< -1000000||$i>1000000)exit 1}' || fail invalid_ir
        printf '%s' "$code" > /sys/ir_tx_gpio/ir_data || fail hardware_control_failed
        printf '{"completed":true}\n'
        ;;
    *) fail unsupported_action ;;
esac
