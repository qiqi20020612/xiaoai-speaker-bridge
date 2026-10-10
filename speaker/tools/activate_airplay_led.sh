#!/bin/sh
# Coordinate the existing LED program without changing Shairport configuration.
# Requires a private, hash-reviewed sibling. Default is a read-only preview.
set -eu
umask 077
DIR=/data/xiaoai_bridge
ORIGINAL=/data/airplay_volume_led
BACKUP=/data/airplay_volume_led.bak.xiaoai_bridge
CANDIDATE=$DIR/adapter/airplay-led.sh
MODE=${1:---dry-run}
case "$MODE" in --dry-run|--apply|--remove) ;; *) exit 2 ;; esac
[ -r "$DIR/adapter/review.json" ] && [ -x "$CANDIDATE" ] || exit 1
expected=$(jq -er '.files["airplay-led.sh"].source_sha256' "$DIR/adapter/review.json")
reviewed=$(jq -er '.files["airplay-led.sh"].output_sha256' "$DIR/adapter/review.json")
hash() { sha256sum "$1" | awk '{print $1}'; }
[ "$(hash "$CANDIDATE")" = "$reviewed" ] || exit 1
sh -n "$CANDIDATE"
if [ "$MODE" = --dry-run ]; then
    printf '%s\n' 'Plan: preserve Shairport settings and its hook path; guard the existing LED draw function and worker while voice is active.'
    printf '%s\n' 'Review adapter/airplay-led.sh.diff before any apply operation.'
    exit 0
fi
[ "$(id -u)" -eq 0 ] || exit 1
exec 9> /tmp/xiaoai_bridge.airplay-led.lock
flock -n 9 || exit 1
. "$DIR/lib/common.sh"
voice_busy && { printf '%s\n' 'Voice is active; retry when idle';exit 1; }
current=$(hash "$ORIGINAL")
if [ "$MODE" = --remove ];then
    [ -r "$BACKUP" ] || exit 0
    [ "$current" = "$reviewed" ] && [ "$(hash "$BACKUP")" = "$expected" ] || {
        printf '%s\n' 'LED program changed since activation; review a merge before removal';exit 1;
    }
    cp -p "$BACKUP" "$ORIGINAL.xiaoai.new"
else
    [ "$current" != "$reviewed" ] || exit 0
    [ "$current" = "$expected" ] || { printf '%s\n' 'LED source changed; regenerate and review its sibling';exit 1; }
    if [ -r "$BACKUP" ];then [ "$(hash "$BACKUP")" = "$expected" ] || exit 1;else cp -p "$ORIGINAL" "$BACKUP";fi
    cp -p "$CANDIDATE" "$ORIGINAL.xiaoai.new"
fi
chmod 700 "$ORIGINAL.xiaoai.new"
[ "$(hash "$ORIGINAL")" = "$current" ] || { rm -f "$ORIGINAL.xiaoai.new";exit 1; }
mv -f "$ORIGINAL.xiaoai.new" "$ORIGINAL"
printf '%s\n' 'LED guard updated. Shairport configuration and software volume curve were unchanged.'
