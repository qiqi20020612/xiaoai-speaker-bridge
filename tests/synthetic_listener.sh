#!/bin/sh
CONFIG_FILE=/data/listener
[ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"
set_volume(){ :; }
# Set lower rate if cannot convert later
. /data/kws_guard.sh
while true; do
kws_guard
SAVED_VOL=`current_volume`
set_volume 10
/bin/show_led 1
STT_TEXT="test"
    if [ "${API_AVAILABLE}" = "1" ]; then
      intent_run test
    fi
    original_conversation
    fi # INTENT_SUCCESS
set_volume $SAVED_VOL
done
