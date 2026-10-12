#!/bin/sh
# Explicit, separately reviewed activation. Default is read-only.
# Adds ONE source line before the existing KWS bootstrap; never replaces its settings.
set -eu
umask 077
DIR=/data/xiaoai_bridge
CONFIG=/data/listener
LINE='. /data/xiaoai_bridge/lib/bootstrap.sh'
MODE=${1:---dry-run}
case "$MODE" in --dry-run|--apply|--remove) ;; *) exit 2 ;; esac
[ -r "$CONFIG" ] || exit 1
if [ "$MODE" != --remove ];then
    . "$DIR/lib/adapter-check.sh"
    bridge_check_adapter_directory "$DIR/adapter" /data/porcupine_launcher_kws || exit 1
fi
if [ "$MODE" = --dry-run ]; then
    printf '%s\n' 'Proposed listener change: add one bridge include immediately before the existing KWS bootstrap.'
    printf '%s\n' 'Existing KWS, STT, OAuth, Agent, TTS and AirPlay values remain verbatim.'
    [ -x "$DIR/adapter/listener.sh" ] && [ -r "$DIR/adapter/review.json" ] || exit 1
    sh -n "$DIR/adapter/listener.sh"
    exit 0
fi
[ "$(id -u)" -eq 0 ] || exit 1
command -v flock >/dev/null 2>&1 || exit 1
exec 9> /tmp/xiaoai_bridge.activate.lock
flock -n 9 || exit 1
. "$DIR/lib/common.sh"
voice_busy && { printf '%s\n' 'Voice interaction is active; retry when idle';exit 1; }
before=$(sha256sum "$CONFIG" | awk '{print $1}')
if [ "$MODE" = --remove ]; then
    # Preserve current refreshed OAuth tokens; do not restore an old credential backup.
    awk -v line="$LINE" '$0!=line' "$CONFIG" > "$CONFIG.xiaoai.new"
else
    [ -x "$DIR/adapter/listener.sh" ] && [ -r "$DIR/adapter/review.json" ] || exit 1
    expected=$(jq -er '.files["listener.sh"].source_sha256' "$DIR/adapter/review.json")
    actual=$(sha256sum /data/porcupine_launcher_kws | awk '{print $1}')
    [ "$expected" = "$actual" ] || { printf '%s\n' 'Original launcher changed; regenerate and review adapter first' >&2;exit 1; }
    expected_output=$(jq -er '.files["listener.sh"].output_sha256' "$DIR/adapter/review.json")
    actual_output=$(sha256sum "$DIR/adapter/listener.sh" | awk '{print $1}')
    [ "$expected_output" = "$actual_output" ] || { printf '%s\n' 'Prepared adapter no longer matches the reviewed file';exit 1; }
    [ -f /data/listener.bak.xiaoai_bridge ] || cp -p "$CONFIG" /data/listener.bak.xiaoai_bridge
    awk -v line="$LINE" '
        $0==line {next}
        /^\. \/data\/kws_bootstrap\.sh[[:space:]]*$/ {print line;found++}
        {print}
        END {if(found!=1)exit 1}
    ' "$CONFIG" > "$CONFIG.xiaoai.new" || { rm -f "$CONFIG.xiaoai.new";exit 1; }
fi
chmod 600 "$CONFIG.xiaoai.new"
sh -n "$CONFIG.xiaoai.new" || { rm -f "$CONFIG.xiaoai.new";exit 1; }
[ "$before" = "$(sha256sum "$CONFIG" | awk '{print $1}')" ] || { rm -f "$CONFIG.xiaoai.new";printf '%s\n' 'Listener changed concurrently; retry after refresh';exit 1; }
mv -f "$CONFIG.xiaoai.new" "$CONFIG"
if [ "$MODE" = --apply ]; then
    jq '.enable_listener_adapter=true' "$DIR/config.json" > "$DIR/config.json.new"
    chmod 600 "$DIR/config.json.new";mv -f "$DIR/config.json.new" "$DIR/config.json"
    "$DIR/lib/listener-service.sh" restart 9>&-
else
    jq '.enable_listener_adapter=false' "$DIR/config.json" > "$DIR/config.json.new"
    chmod 600 "$DIR/config.json.new";mv -f "$DIR/config.json.new" "$DIR/config.json"
fi
