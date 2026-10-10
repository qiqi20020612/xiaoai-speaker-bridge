#!/bin/sh
# Run locally ON THE SPEAKER only after reviewing the package and private adapter diffs.
# Default is a read-only dry run. No SSH credentials are embedded in this package.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
PACKAGE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST=/data/xiaoai_bridge
BOOT=/data/xiaoai_boot.sh
CRON=/data/etc/crontabs/root
BACKUP=/data/xiaoai_bridge.backup
MODE=${1:---dry-run}

case "$MODE" in --dry-run|--apply|--rollback|--uninstall) ;; *) printf '%s\n' 'Unknown installer option' >&2;exit 2 ;; esac
if [ "$MODE" = --dry-run ]; then
    [ -x "$PACKAGE/bin/xiaoai_bridge" ] || { printf '%s\n' 'Missing bridge binary';exit 1; }
    "$PACKAGE/bin/xiaoai_bridge" --version
    sh -n "$PACKAGE/start.sh" && sh -n "$PACKAGE/xiaoai_boot.sh"
    for file in "$PACKAGE"/lib/*.sh;do sh -n "$file";done
    printf '%s\n' 'Plan: preserve listener, KWS, Agent, TTS and AirPlay configurations.'
    printf '%s\n' 'Plan: back up existing boot script and crontab, deploy /data/xiaoai_bridge, and retain one Cron entry.'
    printf '%s\n' 'Plan: sibling adapters are inactive unless explicitly enabled in the local config.'
    [ -f "$BOOT" ] && printf '%s\n' 'Existing boot entry will be backed up and extended.'
    [ -r "$CRON" ] && printf '%s\n' 'Existing Cron tasks will be retained.'
    df -k /data
    exit 0
fi
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'Root is required on the speaker' >&2;exit 1; }
command -v flock >/dev/null 2>&1 || exit 1
exec 9> /tmp/xiaoai_bridge.install.lock
flock -n 9 || { printf '%s\n' 'Another installation is active' >&2;exit 1; }

restore_listener() {
    [ -x "$DEST/tools/activate.sh" ] && "$DEST/tools/activate.sh" --remove || return 1
    if [ -x "$DEST/lib/listener-service.sh" ]; then
        "$DEST/lib/listener-service.sh" stop >/dev/null 2>&1 || return 1
    fi
    SILENT=1 /etc/init.d/listener start >/dev/null 2>&1 || return 1
}

restore_led() {
    if [ -r /data/airplay_volume_led.bak.xiaoai_bridge ]; then
        "$DEST/tools/activate_airplay_led.sh" --remove 9>&- || return 1
    fi
}

if [ "$MODE" = --rollback ]; then
    [ -d "$BACKUP" ] || { printf '%s\n' 'No backup exists';exit 1; }
    [ ! -e "$DEST.failed" ] || { printf '%s\n' 'Failed package already retained; archive it before rollback';exit 1; }
    restore_led || exit 1
    if [ -x "$DEST/start.sh" ];then "$DEST/start.sh" stop 9>&- || exit 1;fi
    if [ -r "$DEST/config.json" ] && jq -e '.enable_listener_adapter' "$DEST/config.json" >/dev/null 2>&1;then restore_listener || exit 1;fi
    [ -f "$BACKUP/xiaoai_boot.sh" ] && cp -p "$BACKUP/xiaoai_boot.sh" "$BOOT"
    # Keep current Cron tasks (including OAuth refreshes added since installation).
    # Its bridge boot line remains safe even when the bridge is absent.
    [ -d "$DEST" ] && mv "$DEST" "$DEST.failed"
    if [ -d "$BACKUP/bridge" ]; then
        mv "$BACKUP/bridge" "$DEST"
        "$DEST/start.sh" 9>&- || exit 1
    fi
    /etc/init.d/cron restart
    printf '%s\n' 'Rolled back. The bridge include was removed; original voice settings and current Cron tasks were retained.'
    exit 0
fi
if [ "$MODE" = --uninstall ]; then
    [ ! -e /data/xiaoai_bridge.uninstalled ] || { printf '%s\n' 'An uninstalled copy already exists; archive it first';exit 1; }
    restore_led || exit 1
    if [ -x "$DEST/start.sh" ];then "$DEST/start.sh" stop 9>&- || exit 1;fi
    if [ -r "$DEST/config.json" ] && jq -e '.enable_listener_adapter' "$DEST/config.json" >/dev/null 2>&1;then restore_listener || exit 1;fi
    # Keep the existing boot/Cron mechanism so Squeezelite stays disabled.
    # Retain the reviewed sibling copies in a recoverable directory until removal is deliberate.
    [ -d "$DEST" ] && mv "$DEST" /data/xiaoai_bridge.uninstalled
    printf '%s\n' 'Bridge removed from its active path; boot script safely skips it. Backup retained.'
    exit 0
fi

[ -x "$PACKAGE/bin/xiaoai_bridge" ] || exit 1
for dependency in jq flock amixer curl;do command -v "$dependency" >/dev/null 2>&1 || { printf 'Missing dependency: %s\n' "$dependency" >&2;exit 1; };done
[ -r "$PACKAGE/SHA256SUMS" ] || { printf '%s\n' 'Use a packaged, checksummed release';exit 1; }
(cd "$PACKAGE" && sha256sum -c SHA256SUMS >/dev/null) || { printf '%s\n' 'Package checksum verification failed';exit 1; }
if [ -d "$BACKUP" ] && [ -f "$DEST/SHA256SUMS" ] && [ -f "$PACKAGE/SHA256SUMS" ] && cmp -s "$DEST/SHA256SUMS" "$PACKAGE/SHA256SUMS";then
    "$DEST/start.sh" 9>&-;printf '%s\n' 'Same package already installed; no changes made';exit 0
fi
[ ! -d "$BACKUP" ] || { printf '%s\n' 'Archive the previous backup before upgrading; refusing to overwrite it' >&2;exit 1; }
STAGE=/data/xiaoai_bridge.new
[ ! -e "$STAGE" ] || { printf '%s\n' 'Inspect an existing staging directory first';exit 1; }
[ ! -e /data/xiaoai_bridge.previous ] || { printf '%s\n' 'Previous package still retained; refusing to overwrite it';exit 1; }
# Space estimate includes both package and backup. Refuse rather than exhausting /data.
need=$(du -sk "$PACKAGE" | awk '{print $1}')
old=0;[ -d "$DEST" ] && old=$(du -sk "$DEST" | awk '{print $1}')
available=$(df -k /data | awk 'NR==2{print $4}')
[ "$available" -ge $((need+old+512)) ] || { printf '%s\n' 'Insufficient /data free space';exit 1; }
mkdir "$BACKUP"
[ -f "$BOOT" ] && cp -p "$BOOT" "$BACKUP/xiaoai_boot.sh"
[ -f "$CRON" ] && cp -p "$CRON" "$BACKUP/crontab.root"
[ -d "$DEST" ] && cp -a "$DEST" "$BACKUP/bridge"
mkdir "$STAGE"
cp -a "$PACKAGE/lib" "$PACKAGE/bin" "$PACKAGE/tools" "$STAGE/"
cp -p "$PACKAGE/start.sh" "$PACKAGE/config.example.json" "$STAGE/"
[ -f "$PACKAGE/SHA256SUMS" ] && cp -p "$PACKAGE/SHA256SUMS" "$STAGE/"
if [ -d "$PACKAGE/adapter" ];then
    cp -a "$PACKAGE/adapter" "$STAGE/adapter"
elif [ -d "$DEST/adapter" ];then
    cp -a "$DEST/adapter" "$STAGE/adapter"
fi
if [ -r "$DEST/config.json" ]; then
    cp -p "$DEST/config.json" "$STAGE/config.json"
elif [ -r "$PACKAGE/config.json" ]; then
    cp -p "$PACKAGE/config.json" "$STAGE/config.json"
else
    # A new random token and device UUID belong only to this local installation.
    device_id=$(cat /proc/sys/kernel/random/uuid)
    token=$(head -c 32 /dev/urandom | hexdump -ve '1/1 "%02x"')
    jq --arg id "$device_id" --arg token "$token" '.device_id=$id|.token=$token' \
        "$PACKAGE/config.example.json" > "$STAGE/config.json"
fi
chmod 600 "$STAGE/config.json"
chmod 700 "$STAGE/start.sh" "$STAGE/bin/xiaoai_bridge" "$STAGE"/lib/*.sh
chmod 700 "$STAGE"/tools/*.sh
"$STAGE/bin/xiaoai_bridge" --config "$STAGE/config.json" --check-config || exit 1
if [ -x "$DEST/start.sh" ];then "$DEST/start.sh" stop 9>&- || exit 1;fi
[ -d "$DEST" ] && mv "$DEST" /data/xiaoai_bridge.previous
mv "$STAGE" "$DEST"
cp -p "$PACKAGE/xiaoai_boot.sh" "$BOOT"
chmod 700 "$BOOT"
mkdir -p /data/etc/crontabs
[ -e "$CRON" ] || : > "$CRON"
# Remove only duplicates of our exact standard line, preserving every other task verbatim.
awk '!seen[$0]++ || $0!="* * * * * /bin/sh /data/xiaoai_boot.sh"' "$CRON" > "$CRON.xiaoai.new"
grep -qxF '* * * * * /bin/sh /data/xiaoai_boot.sh' "$CRON.xiaoai.new" || \
    printf '%s\n' '* * * * * /bin/sh /data/xiaoai_boot.sh' >> "$CRON.xiaoai.new"
chmod 600 "$CRON.xiaoai.new"
mv -f "$CRON.xiaoai.new" "$CRON"
/etc/init.d/cron restart
# Do not rewrite the already-completed boot flag or repeatedly stop Squeezelite during installation.
"$DEST/start.sh" 9>&- || { printf '%s\n' 'Startup failed; use --rollback after inspecting /tmp/xiaoai_bridge.log';exit 1; }
printf '%s\n' 'Installed. Read config.json locally to pair HA; never publish its token.'
