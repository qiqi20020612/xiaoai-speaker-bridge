#!/bin/sh
# Execute a checked package from /tmp, not from its installed/retained directory.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
PACKAGE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
DEST=/data/xiaoai_bridge
BOOT=/data/xiaoai_boot.sh
CRON=/data/etc/crontabs/root
BACKUP=/data/xiaoai_bridge.backup
STAGE=/data/xiaoai_bridge.new
RUNTIME=/tmp
BOOT_LINE='* * * * * /bin/sh /data/xiaoai_boot.sh'
LISTENER_LINE='. /data/xiaoai_bridge/lib/bootstrap.sh'
MODE=${1:---dry-run}
case "$MODE" in --dry-run|--apply|--rollback|--uninstall) ;; *) exit 2 ;; esac
[ "$PACKAGE" != "$DEST" ] || { printf '%s\n' 'Extract the checked recovery package to /tmp before running the installer';exit 1; }
. "$PACKAGE/lib/locks.sh"
. "$PACKAGE/lib/adapter-check.sh"

check_install_adapters() {
    selected_adapter=''
    selected_config=''
    for candidate in "$DEST/config.json" "$BACKUP/retry/config.json" "$PACKAGE/config.json";do
        if [ -r "$candidate" ];then selected_config=$candidate;break;fi
    done
    for candidate in "$PACKAGE/adapter" "$DEST/adapter" "$BACKUP/retry/adapter";do
        if [ -d "$candidate" ];then selected_adapter=$candidate;break;fi
    done
    if [ -n "$selected_adapter" ];then
        bridge_check_adapter_directory "$selected_adapter" /data/porcupine_launcher_kws || return 1
    elif [ -n "$selected_config" ] && jq -e '.enable_listener_adapter==true' "$selected_config" >/dev/null 2>&1;then
        bridge_adapter_reject 'Enabled Listener adapter is missing.';return 1
    fi
    # An activated older LED copy cannot be silently retained with a new review:
    # that would race remote feedback and make later removal/rollback unverifiable.
    if [ -n "$selected_adapter" ] && [ -r "$selected_adapter/airplay-led.sh" ] && \
       cmp -s "$selected_adapter/airplay-led.sh" /data/airplay_volume_led;then
        [ -r /data/airplay_volume_led.bak.xiaoai_bridge ] && \
        [ "$(jq -r '.files["airplay-led.sh"].source_sha256' "$selected_adapter/review.json")" = "$(sha256sum /data/airplay_volume_led.bak.xiaoai_bridge | awk '{print $1}')" ] || {
            printf '%s\n' 'Active AirPlay LED guard requires a readable backup matching its reviewed original source.' >&2
            return 1;
        }
    elif [ -r /data/airplay_volume_led.bak.xiaoai_bridge ] && \
         ! cmp -s /data/airplay_volume_led.bak.xiaoai_bridge /data/airplay_volume_led;then
        printf '%s\n' 'Active AirPlay LED guard differs from the new private adapter. Run the currently installed tools/activate_airplay_led.sh --remove before upgrading, then regenerate/review and reactivate the new guard after installation.' >&2
        return 1
    fi
    # Default native feedback needs the original AirPlay hook to cooperate too.
    # Require its reviewed sibling before any installation mutation, no flag.
    if [ -x /bin/show_led ] && [ -x /bin/shut_led ] && [ -r /data/airplay_volume_led ];then
        if [ -z "$selected_adapter" ] || [ ! -x "$selected_adapter/airplay-led.sh" ];then
            bridge_adapter_reject 'Native task effects require the reviewed AirPlay LED guard in the private adapter directory.'
            return 1
        fi
        if ! cmp -s "$selected_adapter/airplay-led.sh" /data/airplay_volume_led && \
           [ "$(jq -r '.files["airplay-led.sh"].source_sha256' "$selected_adapter/review.json")" != "$(sha256sum /data/airplay_volume_led | awk '{print $1}')" ];then
            bridge_adapter_reject 'AirPlay LED source no longer matches its private review.'
            return 1
        fi
    fi
}
adapters_unchanged() {
    [ -n "$selected_adapter" ] || { [ ! -d "$DEST/adapter" ];return; }
    [ -d "$DEST/adapter" ] || return 1
    for name in listener.sh kws-guard.sh review.json airplay-led.sh;do
        if [ -e "$selected_adapter/$name" ] || [ -e "$DEST/adapter/$name" ];then
            cmp -s "$selected_adapter/$name" "$DEST/adapter/$name" || return 1
        fi
    done
    if [ -x /bin/show_led ] && [ -x /bin/shut_led ] && [ -x "$DEST/adapter/airplay-led.sh" ];then
        cmp -s "$DEST/adapter/airplay-led.sh" /data/airplay_volume_led || return 1
    fi
}

if [ "$MODE" = --dry-run ]; then
    for dependency in jq sha256sum;do command -v "$dependency" >/dev/null 2>&1 || exit 1;done
    [ -x "$PACKAGE/bin/xiaoai_bridge" ] || exit 1
    "$PACKAGE/bin/xiaoai_bridge" --version
    # Probe the actual FD-lock interface without creating files in /data or /tmp.
    exec 9< "$PACKAGE/start.sh"
    bridge_lock_for 0 9 || { printf '%s\n' 'BusyBox-compatible FD locking is unavailable';exit 1; }
    flock -u 9
    sh -n "$PACKAGE/start.sh" && sh -n "$PACKAGE/xiaoai_boot.sh"
    for file in "$PACKAGE"/lib/*.sh;do sh -n "$file";done
    check_install_adapters || exit 1
    printf '%s\n' 'Plan: preserve listener, KWS, Agent, TTS, AirPlay and unrelated Cron tasks.'
    printf '%s\n' 'Plan: snapshot boot/Cron/bridge; restore on failed startup; retain recovery copies.'
    printf '%s\n' 'Plan: BusyBox FD locks with bounded waits; no GNU flock options.'
    [ -r "$BACKUP/status" ] && { printf 'Previous transaction: ';cat "$BACKUP/status"; }
    df -k /data
    exit 0
fi
[ "$(id -u)" -eq 0 ] || exit 1
for dependency in flock jq sha256sum;do command -v "$dependency" >/dev/null 2>&1 || exit 1;done
exec 9> "$RUNTIME/xiaoai_bridge.install.lock"
flock -x -n 9 || { printf '%s\n' 'Another installation is active';exit 1; }
# Block both the old and new Cron boot script for the entire transaction.
exec 8> "$RUNTIME/xiaoai_boot.lock"
flock -x -n 8 || { printf '%s\n' 'Boot tasks are active; retry when idle';exit 1; }

phase() { [ -r "$BACKUP/status" ] && cat "$BACKUP/status" || printf legacy; }
set_phase() { printf '%s\n' "$1" > "$BACKUP/status.new" && mv -f "$BACKUP/status.new" "$BACKUP/status"; }
retain() {
    retained=$2
    suffix=0
    while [ -e "$retained" ] || [ -L "$retained" ];do suffix=$((suffix+1));retained="$2.$suffix";done
    mv "$1" "$retained"
}
voice_idle() {
    [ -r "$RUNTIME/xiaoai_bridge.voice" ] || return 0
    # 0.1.3 writes PID alone; new hooks write PID plus Linux starttime.
    read -r voice_pid voice_start < "$RUNTIME/xiaoai_bridge.voice" || return 0
    if bridge_owner_alive "$voice_pid" "$voice_start";then
        printf '%s\n' 'Voice interaction is active; retry when idle' >&2
        return 1
    fi
}
# Never invoke a broken installed start.sh. Stop needs neither config nor binary.
service() {
    XIAOAI_BRIDGE_DIR="$DEST" XIAOAI_BRIDGE_RUNTIME="$RUNTIME" \
    XIAOAI_BRIDGE_CONFIG="$DEST/config.json" XIAOAI_SKIP_LISTENER_START=1 "$PACKAGE/start.sh" "$1" 4>&- 8>&- 9>&-
}
restart_cron() { /etc/init.d/cron restart 4>&- 8>&- 9>&-; }
install_diagnostic() {
    log="$RUNTIME/xiaoai_bridge.log"
    [ ! -L "$log" ] || return 1
    if [ -f "$log" ] && [ "$(wc -c < "$log")" -gt 65280 ];then
        mv -f "$log" "$log.1" || return 1
    fi
    printf 'time=%s installation event=%s\n' "$(date +%s)" "$1" >> "$log"
}

remove_listener_include() {
    file=/data/listener
    pending="$RUNTIME/xiaoai_bridge.listener_restore.pending"
    found=false
    [ -r "$file" ] && grep -qxF "$LISTENER_LINE" "$file" && found=true
    [ "$found" = true ] || [ -f "$pending" ] || return 0
    voice_idle || return 1
    if [ "$found" = true ];then
        before=$(sha256sum "$file" | awk '{print $1}')
        awk -v line="$LISTENER_LINE" '$0!=line' "$file" > "$file.xiaoai.new" || return 1
        chmod 600 "$file.xiaoai.new"
        sh -n "$file.xiaoai.new" || return 1
        [ "$before" = "$(sha256sum "$file" | awk '{print $1}')" ] || {
            rm -f "$file.xiaoai.new";printf '%s\n' 'Listener changed concurrently; retry';return 1;
        }
        : > "$pending"
        mv -f "$file.xiaoai.new" "$file" || return 1
    fi
    # Only a bridge include just removed authorizes reloading the original chain.
    if /etc/init.d/listener status >/dev/null 2>&1;then
        SILENT=1 /etc/init.d/listener stop 4>&- 8>&- 9>&- || return 1
        waited=0
        while /etc/init.d/listener status >/dev/null 2>&1;do
            [ "$waited" -lt 15 ] || return 1
            sleep 1;waited=$((waited+1))
        done
    fi
    SILENT=1 /etc/init.d/listener start 4>&- 8>&- 9>&- || return 1
    rm -f "$pending"
}
restore_led() {
    original=/data/airplay_volume_led
    saved=/data/airplay_volume_led.bak.xiaoai_bridge
    if [ ! -r "$saved" ];then
        if grep -qF 'xiaoai_bridge/lib/common.sh' "$original" 2>/dev/null;then
            printf '%s\n' 'Active AirPlay LED guard is missing its original backup; retain bridge helpers.' >&2
            return 1
        fi
        return 0
    fi
    # An identical guarded backup is not an original and cannot authorize
    # detaching helpers during rollback/uninstall. Validate its source hash.
    if cmp -s "$saved" "$original" && ! grep -qF 'xiaoai_bridge/lib/common.sh' "$original";then return 0;fi
    review="$DEST/adapter/review.json"
    [ -r "$review" ] || { printf '%s\n' 'LED guard needs its private review before removal';return 1; }
    source_hash=$(jq -er '.files["airplay-led.sh"].source_sha256' "$review") || return 1
    output_hash=$(jq -er '.files["airplay-led.sh"].output_sha256' "$review") || return 1
    [ "$source_hash" = "$(sha256sum "$saved" | awk '{print $1}')" ] && \
    [ "$output_hash" = "$(sha256sum "$original" | awk '{print $1}')" ] || {
        printf '%s\n' 'LED program changed; retain bridge helpers until a reviewed merge';return 1;
    }
    # A compatible upgrade keeps the activated guard. Recovery must keep it too
    # when the restored Bridge owns that exact reviewed copy; uninstall removes it.
    if [ "${1:-}" = snapshot ] && [ -f "$BACKUP/airplay_guard_active" ] && \
       cmp -s "$BACKUP/bridge/adapter/airplay-led.sh" "$original";then
        return 0
    fi
    cp -p "$saved" "$original.xiaoai.new" && mv -f "$original.xiaoai.new" "$original"
}
capture_retry() {
    [ -d "$DEST" ] || return 0
    mkdir -p "$BACKUP/retry"
    [ ! -r "$DEST/config.json" ] || cp -p "$DEST/config.json" "$BACKUP/retry/config.json" || return 1
    if [ -d "$DEST/adapter" ] && [ ! -d "$BACKUP/retry/adapter" ];then
        cp -a "$DEST/adapter" "$BACKUP/retry/adapter" || return 1
    fi
}
disable_adapter() {
    file=$1/config.json
    [ -r "$file" ] || return 0
    # Keep identity/token/options; the original voice chain is now selected.
    if jq -e 'type=="object"' "$file" >/dev/null 2>&1;then
        jq '.enable_listener_adapter=false' "$file" > "$file.new" && \
        chmod 600 "$file.new" && mv -f "$file.new" "$file" || return 1
    fi
}
restore_boot_cron() {
    if [ -f "$BACKUP/xiaoai_boot.sh" ];then
        if [ -f "$BOOT" ] && ! cmp -s "$BOOT" "$BACKUP/xiaoai_boot.sh" && \
           ! cmp -s "$BOOT" "$BACKUP/installed_boot" && ! cmp -s "$BOOT" "$PACKAGE/xiaoai_boot.sh";then
            printf '%s\n' 'Boot script changed independently; inspect before restoration';return 1
        fi
        cp -p "$BACKUP/xiaoai_boot.sh" "$BOOT" || return 1
    elif [ -r "$BACKUP/ready" ] && [ -f "$BOOT" ];then
        cmp -s "$BOOT" "$BACKUP/installed_boot" || return 1
        rm -f "$BOOT" || return 1
    fi
    # Undo only a boot line introduced by this install. Preserve newer OAuth/tasks.
    if [ -f "$CRON" ] && ! { [ -r "$BACKUP/crontab.root" ] && grep -qxF "$BOOT_LINE" "$BACKUP/crontab.root"; };then
        before_cron=$(sha256sum "$CRON" | awk '{print $1}')
        awk -v line="$BOOT_LINE" '$0!=line' "$CRON" > "$CRON.xiaoai.new" || return 1
        [ "$before_cron" = "$(sha256sum "$CRON" | awk '{print $1}')" ] || {
            rm -f "$CRON.xiaoai.new";printf '%s\n' 'Cron changed concurrently; retry recovery';return 1;
        }
        chmod 600 "$CRON.xiaoai.new" && mv -f "$CRON.xiaoai.new" "$CRON" || return 1
    fi
    restart_cron
}
stop_listener() {
    SILENT=1 /etc/init.d/listener stop 4>&- 8>&- 9>&- || return 1
    listener_wait=0
    while /etc/init.d/listener status >/dev/null 2>&1; do
        [ "$listener_wait" -lt 15 ] || return 1
        sleep 1;listener_wait=$((listener_wait+1))
    done
}
restore_listener_state() {
    if [ -r "$BACKUP/listener_include" ] && [ -r /data/listener ] &&
       ! grep -qxF "$LISTENER_LINE" /data/listener; then
        awk -v line="$LISTENER_LINE" '
            /^\. \/data\/kws_bootstrap\.sh[[:space:]]*$/ {print line;found++}
            {print} END {if(found!=1)exit 1}' /data/listener > /data/listener.xiaoai.new || return 1
        chmod 600 /data/listener.xiaoai.new
        mv -f /data/listener.xiaoai.new /data/listener || return 1
    fi
    if [ -r "$BACKUP/listener_running" ]; then
        SILENT=1 /etc/init.d/listener start 4>&- 8>&- 9>&- || return 1
    fi
}
bridge_idle() {
    [ -r "$RUNTIME/xiaoai_bridge.pid" ] && [ -r "$DEST/config.json" ] || return 0
    read idle_pid < "$RUNTIME/xiaoai_bridge.pid" || return 1
    case "$idle_pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$idle_pid" 2>/dev/null || return 0
    idle_token=$(jq -er '.token' "$DEST/config.json") || return 1
    idle_port=$(jq -r '.port // 8766' "$DEST/config.json")
    idle_bind=$(jq -r '.bind // "0.0.0.0"' "$DEST/config.json")
    [ "$idle_bind" != 0.0.0.0 ] || idle_bind=127.0.0.1
    idle_state=$(curl -fsS --connect-timeout 1 --max-time 2 -H "Authorization: Bearer $idle_token" \
        "http://$idle_bind:$idle_port/api/state" 2>/dev/null) || return 1
    unset idle_token
    printf '%s' "$idle_state" | jq -e '.audio_active==false and .voice.active==false' >/dev/null 2>&1
}

recover_snapshot() {
    if [ -z "${BRIDGE_OAUTH_DIRECTORY:-}" ]; then
        bridge_oauth_enter "$RUNTIME" 35 || return 1
        bridge_lock_for 35 4 || return 1
    fi
    voice_idle || return 1
    service stop || return 1
    if /etc/init.d/listener status >/dev/null 2>&1; then stop_listener || return 1;fi
    restore_led snapshot || return 1
    if [ ! -d "$BACKUP/bridge" ]; then remove_listener_include || return 1;fi
    capture_retry || return 1
    disable_adapter "$BACKUP/retry" || return 1
    restore_boot_cron || return 1
    [ ! -d "$DEST" ] || retain "$DEST" "$DEST.failed" || return 1
    # Interrupted staging/replacement copies are retained, never silently overwritten.
    [ ! -d "$STAGE" ] || retain "$STAGE" "$DEST.incomplete" || return 1
    [ ! -d "$DEST.previous" ] || retain "$DEST.previous" "$BACKUP/legacy_previous" || return 1
    if [ -d "$BACKUP/bridge" ];then
        cp -a "$BACKUP/bridge" "$STAGE" || return 1
        mv "$STAGE" "$DEST" || return 1
        service start || return 1
        restore_listener_state || return 1
    fi
}

bridge_oauth_enter "$RUNTIME" "${XIAOAI_OAUTH_WAIT:-35}" || {
    printf '%s\n' 'OAuth refresh is active; retry when its directory lock is released' >&2;exit 1;
}
trap 'bridge_oauth_leave' 0
trap 'exit 143' HUP INT TERM
exec 4> "$RUNTIME/xiaoai_bridge.config.lock"
bridge_lock_for 35 4 || exit 1
# Refuse before rollback/uninstall can change transaction metadata as well.
voice_idle || exit 1

if [ "$MODE" = --rollback ];then
    [ -d "$BACKUP" ] || { printf '%s\n' 'No backup exists; use --uninstall to detach a partial installation';exit 1; }
    case "$(phase)" in
        rolled_back|failed_restored) printf '%s\n' 'Already restored; retained copies are unchanged';exit 0 ;;
        uninstalled) printf '%s\n' 'Already uninstalled; boot/Cron remain enabled';exit 0 ;;
        backup_incomplete) printf '%s\n' 'Snapshot was incomplete; no active files were switched. Retry --apply';exit 1 ;;
    esac
    # Import the old format once, so an interrupted legacy recovery is resumable.
    if [ "$(phase)" = legacy ];then
        [ -f "$BACKUP/installed_boot" ] || cp -p "$PACKAGE/xiaoai_boot.sh" "$BACKUP/installed_boot"
        : > "$BACKUP/ready"
        set_phase recovery_required
    fi
    if [ ! -r "$BACKUP/ready" ] && [ "$(phase)" != legacy ];then
        printf '%s\n' 'No complete pre-install snapshot; finish --uninstall instead of guessing original files';exit 1
    fi
    recover_snapshot || { set_phase recovery_required;printf '%s\n' 'Recovery incomplete; retain all copies and retry the checked installer';exit 1; }
    set_phase rolled_back
    printf '%s\n' 'Restored boot and original voice chain. Unrelated/current Cron tasks and private retry config retained.'
    exit 0
fi
if [ "$MODE" = --uninstall ];then
    if [ -d "$DEST" ] || [ -d "$BACKUP" ];then mkdir -p "$BACKUP";set_phase uninstalling;fi
    finish_remove() {
        result=$?
        trap - 0 HUP INT TERM
        bridge_oauth_leave || true
        if [ "$result" -ne 0 ] && [ -d "$BACKUP" ];then set_phase recovery_required;fi
        exit "$result"
    }
    trap finish_remove 0
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    voice_idle || exit 1
    service stop || exit 1
    restore_led || exit 1
    remove_listener_include || exit 1
    if [ -d "$DEST" ];then
        mkdir -p "$BACKUP"
        capture_retry || exit 1
        disable_adapter "$BACKUP/retry" || exit 1
    fi
    [ ! -d "$DEST" ] || retain "$DEST" "$DEST.uninstalled"
    [ ! -d "$STAGE" ] || retain "$STAGE" "$DEST.incomplete"
    # Maintain Squeezelite's Cron contract even after a partial/absent install.
    [ -f "$BOOT" ] || cp -p "$PACKAGE/xiaoai_boot.sh" "$BOOT"
    mkdir -p /data/etc/crontabs
    [ -e "$CRON" ] || : > "$CRON"
    grep -qxF "$BOOT_LINE" "$CRON" || printf '%s\n' "$BOOT_LINE" >> "$CRON"
    restart_cron
    [ ! -d "$BACKUP" ] || set_phase uninstalled
    printf '%s\n' 'Bridge detached; Squeezelite boot/Cron and retained copies remain. Repeated removal is safe.'
    exit 0
fi

for dependency in amixer curl;do command -v "$dependency" >/dev/null 2>&1 || exit 1;done
[ -x "$PACKAGE/bin/xiaoai_bridge" ] && [ -r "$PACKAGE/SHA256SUMS" ] || exit 1
(cd "$PACKAGE" && sha256sum -c SHA256SUMS >/dev/null) || exit 1
voice_idle || exit 1
check_install_adapters || exit 1
[ ! -e "$STAGE" ] && [ ! -e "$DEST.previous" ] || {
    printf '%s\n' 'Unresolved staging/previous copy: inspect and recover before applying';exit 1;
}
need=$(du -sk "$PACKAGE" | awk '{print $1}')
old=0;[ -d "$DEST" ] && old=$(du -sk "$DEST" | awk '{print $1}')
available=$(df -k /data | awk 'NR==2{print $4}')
# Full snapshot plus staged package and a second restoration copy after failure.
[ "$available" -ge $((need+2*old+512)) ] || {
    printf '%s\n' 'Insufficient /data space for upgrade and failure restoration' >&2;exit 1;
}
bridge_idle || { printf '%s\n' 'Bridge tasks are active or state is unavailable; retry when idle' >&2;exit 1; }
OLD_BACKUP=''
if [ -d "$BACKUP" ];then
    case "$(phase)" in
        installed)
            if [ -r "$DEST/SHA256SUMS" ] && cmp -s "$DEST/SHA256SUMS" "$PACKAGE/SHA256SUMS" && adapters_unchanged;then
                service start;printf '%s\n' 'Same checked package already installed';exit 0
            fi ;;
        rolled_back|failed_restored|uninstalled|backup_incomplete) ;;
        *) printf '%s\n' 'Pending/legacy installation: run this checked package with --rollback or --uninstall first';exit 1 ;;
    esac
    retain "$BACKUP" "$BACKUP.retained"
    OLD_BACKUP=$retained
fi
APPLY_ACTIVE=false
OLD_STOPPED=false
LISTENER_STOPPED=false
finish_apply() {
    result=$?
    trap - 0 HUP INT TERM
    [ "$result" -ne 0 ] || { bridge_oauth_leave;exit 0; }
    set +e
    if [ "$APPLY_ACTIVE" = true ];then
        if recover_snapshot;then
            set_phase failed_restored
            install_diagnostic failed_restored || true
            printf '%s\n' 'Installation failed; previous boot/Cron/voice state restored. Inspect log, then retry --apply.' >&2
        else
            set_phase recovery_required
            install_diagnostic recovery_required || true
            printf '%s\n' 'Automatic restoration incomplete; run this checked package --rollback before retrying.' >&2
        fi
    else
        [ ! -d "$STAGE" ] || retain "$STAGE" "$DEST.incomplete"
        listener_resume_ok=true
        if [ "$LISTENER_STOPPED" = true ]; then
            SILENT=1 /etc/init.d/listener start 4>&- 8>&- 9>&- || listener_resume_ok=false
        fi
        if { [ "$OLD_STOPPED" = true ] && ! service start; } || [ "$listener_resume_ok" = false ];then
            set_phase recovery_required
        else
            set_phase failed_restored
        fi
    fi
    bridge_oauth_leave || true
    exit "$result"
}
trap finish_apply 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir "$BACKUP"
set_phase backup_incomplete
LISTENER_WAS_RUNNING=false
if /etc/init.d/listener status >/dev/null 2>&1; then LISTENER_WAS_RUNNING=true;fi
[ ! -d "$DEST" ] || { service stop;OLD_STOPPED=true; }
if [ "$LISTENER_WAS_RUNNING" = true ]; then
    LISTENER_STOPPED=true
    stop_listener
fi
# No old Bridge/Listener writer is alive, and the existing OAuth Cron is excluded.
[ ! -f "$BOOT" ] || cp -p "$BOOT" "$BACKUP/xiaoai_boot.sh"
[ ! -f "$CRON" ] || cp -p "$CRON" "$BACKUP/crontab.root"
[ ! -d "$DEST" ] || cp -a "$DEST" "$BACKUP/bridge"
if [ -n "$selected_adapter" ] && cmp -s "$selected_adapter/airplay-led.sh" /data/airplay_volume_led;then
    : > "$BACKUP/airplay_guard_active"
fi
[ "$LISTENER_WAS_RUNNING" != true ] || : > "$BACKUP/listener_running"
if [ -r /data/listener ] && grep -qxF "$LISTENER_LINE" /data/listener; then : > "$BACKUP/listener_include";fi
cp -p "$PACKAGE/xiaoai_boot.sh" "$BACKUP/installed_boot"
: > "$BACKUP/ready"
set_phase prepared
mkdir "$STAGE"
cp -a "$PACKAGE/lib" "$PACKAGE/bin" "$PACKAGE/tools" "$STAGE/"
cp -p "$PACKAGE/start.sh" "$PACKAGE/config.example.json" "$PACKAGE/SHA256SUMS" "$STAGE/"
awk '$2!="install.sh" && $2!="xiaoai_boot.sh" {print}' "$PACKAGE/SHA256SUMS" > "$STAGE/INSTALLED_SHA256SUMS"
if [ -d "$PACKAGE/adapter" ];then cp -a "$PACKAGE/adapter" "$STAGE/adapter"
elif [ -d "$DEST/adapter" ];then cp -a "$DEST/adapter" "$STAGE/adapter"
elif [ -n "$OLD_BACKUP" ] && [ -d "$OLD_BACKUP/retry/adapter" ];then cp -a "$OLD_BACKUP/retry/adapter" "$STAGE/adapter"
fi
if [ -r "$DEST/config.json" ];then cp -p "$DEST/config.json" "$STAGE/config.json"
elif [ -n "$OLD_BACKUP" ] && [ -r "$OLD_BACKUP/retry/config.json" ];then cp -p "$OLD_BACKUP/retry/config.json" "$STAGE/config.json"
elif [ -r "$PACKAGE/config.json" ];then cp -p "$PACKAGE/config.json" "$STAGE/config.json"
else
    device_id=$(cat /proc/sys/kernel/random/uuid)
    token=$(head -c 32 /dev/urandom | hexdump -ve '1/1 "%02x"')
    jq --arg id "$device_id" --arg token "$token" '.device_id=$id|.token=$token' "$PACKAGE/config.example.json" > "$STAGE/config.json"
fi
chmod 600 "$STAGE/config.json"
chmod 700 "$STAGE/start.sh" "$STAGE/bin/xiaoai_bridge" "$STAGE"/lib/*.sh "$STAGE"/tools/*.sh
"$STAGE/bin/xiaoai_bridge" --config "$STAGE/config.json" --check-config
if [ -d "$STAGE/adapter" ];then
    bridge_check_adapter_directory "$STAGE/adapter" /data/porcupine_launcher_kws || exit 1
elif jq -e '.enable_listener_adapter==true' "$STAGE/config.json" >/dev/null 2>&1;then
    bridge_adapter_reject 'Enabled Listener adapter is missing from the staged installation.';exit 1
fi
# Mark before the first directory move: power loss is recovered from the snapshot.
set_phase replacing
APPLY_ACTIVE=true
[ ! -d "$DEST" ] || mv "$DEST" "$BACKUP/replaced"
mv "$STAGE" "$DEST"
cp -p "$PACKAGE/xiaoai_boot.sh" "$BOOT"
chmod 700 "$BOOT"
mkdir -p /data/etc/crontabs
[ -e "$CRON" ] || : > "$CRON"
before_cron=$(sha256sum "$CRON" | awk '{print $1}')
awk -v line="$BOOT_LINE" '!seen[$0]++ || $0!=line' "$CRON" > "$CRON.xiaoai.new"
grep -qxF "$BOOT_LINE" "$CRON.xiaoai.new" || printf '%s\n' "$BOOT_LINE" >> "$CRON.xiaoai.new"
chmod 600 "$CRON.xiaoai.new"
[ "$before_cron" = "$(sha256sum "$CRON" | awk '{print $1}')" ] || {
    rm -f "$CRON.xiaoai.new";printf '%s\n' 'Cron changed concurrently; retry installation';exit 1;
}
mv -f "$CRON.xiaoai.new" "$CRON"
restart_cron
if [ -x /bin/show_led ] && [ -x /bin/shut_led ] && [ -x "$DEST/adapter/airplay-led.sh" ] && \
   ! cmp -s "$DEST/adapter/airplay-led.sh" /data/airplay_volume_led;then
    # Reuse the hash/owner/worker checks of the existing activation tool. A
    # failure is part of this transaction, with the old guard state restored.
    XIAOAI_BRIDGE_DIR="$DEST" XIAOAI_BRIDGE_RUNTIME="$RUNTIME" \
    XIAOAI_BRIDGE_CONFIG="$DEST/config.json" "$DEST/tools/activate_airplay_led.sh" --apply 4>&- 8>&- 9>&-
fi
# Keep an existing completed boot flag; no installation-time Squeezelite stop.
if ! service start;then
    install_diagnostic startup_failed || true
    printf '%s\n' 'Startup failed; restoring the installation transaction' >&2
    printf 'Startup diagnostics: %s/xiaoai_bridge.log (and .log.1 if rotated)\n' "$RUNTIME" >&2
    exit 1
fi
# Resume the previous microphone state; the new helper waits for initialization,
# process identity and the versioned ready marker, rather than a mere PID.
flock -u 4
bridge_oauth_leave
if [ "$LISTENER_WAS_RUNNING" = true ] || {
    [ ! -d "$BACKUP/bridge" ] && jq -e '.enable_listener_adapter==true' "$DEST/config.json" >/dev/null 2>&1;
}; then
    XIAOAI_BRIDGE_DIR="$DEST" XIAOAI_BRIDGE_RUNTIME="$RUNTIME" \
    XIAOAI_BRIDGE_CONFIG="$DEST/config.json" "$DEST/lib/listener-service.sh" start 4>&- 8>&- 9>&-
fi
set_phase installed
printf '%s\n' 'Installed. Private identity/token retained; pair HA from local config.json.'
