#!/bin/sh
# Optional hook generated into a sibling Shairport configuration; never edits the live one.
. "${XIAOAI_BRIDGE_DIR:-/data/xiaoai_bridge}/lib/common.sh"
raw=${1:-}
printf '%s' "$raw" | grep -Eq '^-?[0-9]+([.][0-9]+)?$' || exit 1
pct=$(awk -v v="$raw" 'BEGIN {if(v< -30)v=-30;if(v>0)v=0;printf "%.0f",(v+30)/0.3}')
printf '%s %s\n' "$raw" "$pct" > "$BRIDGE_RUNTIME/airplay_led.last.$$"
mv -f "$BRIDGE_RUNTIME/airplay_led.last.$$" "$BRIDGE_RUNTIME/airplay_led.last"
event=$(jq -nc --argjson raw "$raw" '{type:"airplay_volume",raw_volume:$raw}')
"$BRIDGE_BIN" --emit "$event" >/dev/null 2>&1 || true
voice_busy && exit 0
# A generated copy of the original LED hook includes a voice gate inside its worker loop.
[ -x "$BRIDGE_DIR/adapter/airplay-led.sh" ] && "$BRIDGE_DIR/adapter/airplay-led.sh" "$raw"
exit 0
