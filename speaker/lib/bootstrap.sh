#!/bin/sh
# A single reviewed include before the existing KWS bootstrap makes reloads persistent.
# It changes no KWS/STT/TTS/OAuth/Agent settings, and falls back if adapters are absent.
case "${0##*/}" in
    listener.sh) exec 4>&- ;;
    ha_oauth_refresh.sh)
        # The original Cron owns its directory lock. Hold the new FD until its
        # refresh exits; newly launched Listener children must close this FD.
        . /data/xiaoai_bridge/lib/locks.sh
        exec 4> /tmp/xiaoai_bridge.config.lock
        bridge_lock_for 35 4 || return 1
        ;;
    porcupine_launcher)
        exec 4>&-
        if [ -x /data/xiaoai_bridge/adapter/listener.sh ] && \
           [ -r /data/xiaoai_bridge/config.json ] && \
           jq -e '.enable_listener_adapter==true' /data/xiaoai_bridge/config.json >/dev/null 2>&1; then
            exec /data/xiaoai_bridge/adapter/listener.sh
        fi
        ;;
esac
