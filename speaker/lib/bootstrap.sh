#!/bin/sh
# A single reviewed include before the existing KWS bootstrap makes reloads persistent.
# It changes no KWS/STT/TTS/OAuth/Agent settings, and falls back if adapters are absent.
case "${0##*/}" in
    porcupine_launcher)
        if [ -x /data/xiaoai_bridge/adapter/listener.sh ] && \
           [ -r /data/xiaoai_bridge/config.json ] && \
           jq -e '.enable_listener_adapter==true' /data/xiaoai_bridge/config.json >/dev/null 2>&1; then
            exec /data/xiaoai_bridge/adapter/listener.sh
        fi
        ;;
esac
