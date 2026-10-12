#!/usr/bin/env python3
"""Prepare reviewable sibling scripts on a workstation. Never writes to a speaker.

Private source files and generated files belong in .private/, never in public Git.
The exact input SHA256 and unified diffs make every transformation auditable.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import subprocess
from pathlib import Path


def exactly_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"Expected exactly one verified anchor: {old[:70]!r}")
    return source.replace(old, new, 1)


def prepare_listener(source: str) -> str:
    if "xiaoai_bridge/lib/listener-hooks.sh" in source:
        raise ValueError("Source is already adapted")
    if "kws_guard" not in source:
        raise ValueError("This adapter requires the reviewed KWS launcher variant")
    source = exactly_once(source, "ha_refresh_token() {", "bridge_original_ha_refresh_token() {")
    source = exactly_once(
        source,
        '  curl -s -f -d "grant_type=refresh_token',
        '  curl --connect-timeout 5 --max-time 30 -s -f -d "grant_type=refresh_token',
    )
    source = exactly_once(
        source,
        "# Set lower rate if cannot convert later",
        """. /data/xiaoai_bridge/lib/listener-hooks.sh

ha_refresh_token() {
    bridge_oauth_enter "$BRIDGE_RUNTIME" 35 || return 1
    exec 4> "$BRIDGE_RUNTIME/xiaoai_bridge.config.lock"
    bridge_lock_for 35 4 || { bridge_oauth_leave;return 1; }
    bridge_original_ha_refresh_token
    refresh_result=$?
    flock -u 4
    bridge_oauth_leave
    return "$refresh_result"
}

# Set lower rate if cannot convert later""",
    )
    led_commands = [
        line.strip()
        for line in source.splitlines()
        if "/bin/show_led" in line and not line.lstrip().startswith("#")
    ]
    if led_commands != ["/bin/show_led 1", "/bin/show_led 2", "/bin/show_led 11"]:
        raise ValueError("Expected verified standalone wake/loading/end LED commands 1/2/11")
    source, count = re.subn(
        r"(?m)^[ \t]*/bin/show_led[ \t]+1[ \t]*$",
        "# Wake LED is handled before snapshot in bridge_voice_begin",
        source,
    )
    if count != 1:
        raise ValueError("Expected one verified standalone wake LED command")
    source = exactly_once(
        source,
        ". /data/kws_guard.sh\nwhile true; do",
        ". /data/xiaoai_bridge/adapter/kws-guard.sh\nbridge_listener_initialized || exit 1\nwhile true; do",
    )
    source, count = re.subn(r"(?m)^(SAVED_VOL=.*)$", r"bridge_voice_begin || exit 1\n\1", source)
    if count != 1:
        raise ValueError("Expected one volume snapshot before attenuation")
    start = '    if [ "${API_AVAILABLE}" = "1" ]; then'
    end = "    fi # INTENT_SUCCESS"
    if source.count(start) != 1 or source.count(end) != 1:
        raise ValueError("Unified text entry needs manual review for this launcher")
    left, right = source.index(start), source.index(end) + len(end)
    source = (
        source[:left]
        + '    bridge_process_text || log "bridge text handler failed"'
        + source[right:]
    )
    source = exactly_once(source, "set_volume $SAVED_VOL", "bridge_restore_volume")
    source = exactly_once(
        source,
        "set_volume ${VOLUME_DURING_STT}",
        'bridge_voice_attenuate "${VOLUME_DURING_STT}" || exit 1',
    )
    # Error TTS participates in the same local audio mutex.
    source = source.replace("${SPEAK} error", "bridge_error_tts")
    return source


def prepare_kws_guard(source: str) -> str:
    """Keep the private supervisor, but serialize ownership and accept its sibling."""
    source = exactly_once(
        source,
        '            *"/data/porcupine_launcher_kws"*)',
        '            *"/data/porcupine_launcher_kws"*|*"/data/xiaoai_bridge/adapter/listener.sh"*)',
    )
    source = exactly_once(
        source,
        "kws_guard() {",
        """kws_guard() {
    # FD 5 lives for this KWS iteration; competing supervisors cannot remove its directory.
    exec 5> "$KWS_GUARD_LOCK.serial"
    bridge_lock_for 0 5 || exit 125""",
    )
    source = exactly_once(
        source,
        '    return "$result"',
        '    if [ "$result" -ne 0 ]; then rm -f "$BRIDGE_RUNTIME/xiaoai_bridge.voice_ready";fi\n    flock -u 5\n    return "$result"',
    )
    source = source.replace(
        '    rm -rf "$KWS_GUARD_LOCK"',
        "    kws_guard_unlock",
    )
    source = exactly_once(
        source,
        '    echo "$$" > "$KWS_GUARD_LOCK/owner"',
        '    bridge_mark_owner "$KWS_GUARD_LOCK/owner"',
    )
    source = source.replace(
        'owner=$(cat "$KWS_GUARD_LOCK/owner" 2>/dev/null)',
        'read owner owner_start < "$KWS_GUARD_LOCK/owner" || owner=0',
    )
    source = source.replace("    kws_guard_cleanup\n    exit 143", "    bridge_voice_exit")
    source = exactly_once(
        source,
        "    KWS_GUARD_CHILD=$!",
        "    KWS_GUARD_CHILD=$!\n    bridge_listener_ready || { kws_guard_cleanup;return 125; }",
    )
    source += """
# Remove only our directory or a dead owner while the serial FD is held.
kws_guard_unlock() {
    guard_owner=0;guard_start=''
    [ ! -r "$KWS_GUARD_LOCK/owner" ] || read guard_owner guard_start < "$KWS_GUARD_LOCK/owner"
    case "$guard_owner" in ''|*[!0-9]*) guard_owner=0 ;; esac
    if [ "$guard_owner" = "$$" ] || [ "$guard_owner" -le 1 ] ||
       ! bridge_owner_alive "$guard_owner" "$guard_start"; then
        if ! flock -x -n 5 2>/dev/null; then
            exec 5> "$KWS_GUARD_LOCK.serial"
            bridge_lock_for 0 5 || return 1
        fi
        rm -rf "$KWS_GUARD_LOCK"
    fi
}
"""
    return source


def prepare_airplay_led(source: str) -> str:
    if "xiaoai_bridge/lib/common.sh" in source:
        raise ValueError("Use the saved unmodified LED source, not an already adapted file")
    source = exactly_once(source, "#!/bin/sh", "#!/bin/sh\n. /data/xiaoai_bridge/lib/common.sh")
    source = exactly_once(
        source,
        "draw() {",
        """draw() {
    exec 6> "$BRIDGE_RUNTIME/xiaoai_bridge.led.lock"
    bridge_lock_for 0 6 || return 0
    if bridge_led_busy; then flock -u 6; return 0; fi
    bridge_original_draw "$@"
    bridge_draw_result=$?
    flock -u 6
    return "$bridge_draw_result"
}
bridge_original_draw() {""",
    )
    source = exactly_once(
        source,
        "restore_led() {",
        """restore_led() {
    exec 6> "$BRIDGE_RUNTIME/xiaoai_bridge.led.lock"
    bridge_lock_for 0 6 || return 0
    if bridge_led_busy; then flock -u 6; return 0; fi
    # Original restore may call draw(), which must use this already-held FD.
    BRIDGE_AIRPLAY_RESTORE_LOCK=1
    bridge_original_restore
    bridge_restore_result=$?
    BRIDGE_AIRPLAY_RESTORE_LOCK=0
    flock -u 6
    return "$bridge_restore_result"
}
bridge_original_restore() {""",
    )
    source = source.replace(
        "draw() {\n    exec 6>",
        'draw() {\n    if [ "${BRIDGE_AIRPLAY_RESTORE_LOCK:-0}" = 1 ]; then bridge_original_draw "$@"; return $?; fi\n    exec 6>',
        1,
    )
    source = exactly_once(
        source,
        "# 只运行一个LED刷新进程",
        """# Recover only a stale worker directory, serialized independently of LED drawing.
exec 5> "$LOCK.serial"
bridge_lock_for 1 5 || exit 0
if [ -d "$LOCK" ]; then
    owner=0;owner_start=''
    [ ! -r "$LOCK/owner" ] || read owner owner_start < "$LOCK/owner"
    case "$owner" in ''|*[!0-9]*) owner=0 ;; esac
    if [ "$owner" -le 1 ] || ! bridge_owner_alive "$owner" "$owner_start"; then rm -rf "$LOCK"; fi
fi
# 只运行一个LED刷新进程""",
    )
    source = exactly_once(
        source,
        "    trap 'rmdir \"$LOCK\" 2>/dev/null' 0",
        '    bridge_mark_owner "$LOCK/owner"\n    trap \'rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null\' 0',
    )
    source = exactly_once(
        source,
        "</dev/null >/dev/null 2>&1 &",
        '5>&- </dev/null >/dev/null 2>&1 &\n    [ ! -d "$LOCK" ] || printf \'%s %s\\n\' "$!" "$(bridge_pid_start "$!" 2>/dev/null || true)" > "$LOCK/owner"',
    )
    source = exactly_once(
        source,
        '    while [ -f "$STATE" ]; do',
        '    while [ -f "$STATE" ]; do\n        if bridge_led_busy; then rm -f "$STATE"; break; fi',
    )
    source = exactly_once(
        source,
        'printf \'%s %s\\n\' "$1" "$pct" > /tmp/airplay_led.last',
        'printf \'%s %s\\n\' "$1" "$pct" > "$BRIDGE_RUNTIME/airplay_led.last.$$"\n'
        'mv -f "$BRIDGE_RUNTIME/airplay_led.last.$$" "$BRIDGE_RUNTIME/airplay_led.last"',
    )
    # The original hook's self-fork must point to its sibling, not to the untouched file.
    source = source.replace(
        "/data/airplay_volume_led --worker", "/data/xiaoai_bridge/adapter/airplay-led.sh --worker"
    )
    return source


def prepare_shairport(source: str) -> str:
    pattern = r'(?m)^(\s*run_this_when_volume_is_set\s*=\s*)"[^"\n]+"\s*;'
    result, count = re.subn(pattern, r'\1"/data/xiaoai_bridge/lib/airplay-volume.sh";', source)
    if count != 1:
        raise ValueError("Expected one existing active AirPlay volume hook")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listener", type=Path, required=True)
    parser.add_argument("--kws-guard", type=Path, required=True)
    parser.add_argument("--airplay-led", type=Path)
    parser.add_argument("--shairport", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    pairs = [
        (args.listener, "listener.sh", prepare_listener),
        (args.kws_guard, "kws-guard.sh", prepare_kws_guard),
    ]
    if args.airplay_led:
        pairs.append((args.airplay_led, "airplay-led.sh", prepare_airplay_led))
    if args.shairport:
        pairs.append((args.shairport, "shairport-sync.conf", prepare_shairport))
    metadata = {"format": 3, "compatibility": {"listener_hooks": 3, "kws_guard": 1}, "files": {}}
    for original, name, transform in pairs:
        raw = original.read_bytes()
        before = raw.decode("utf-8").replace("\r\n", "\n")
        after = transform(before)
        target = args.output / name
        target.write_text(after)
        target.chmod(0o700 if name.endswith(".sh") else 0o600)
        if name.endswith(".sh"):
            subprocess.run(["sh", "-n", str(target)], check=True)
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(True),
                after.splitlines(True),
                fromfile="original",
                tofile="reviewed_sibling",
            )
        )
        (args.output / (name + ".diff")).write_text(diff)
        (args.output / (name + ".diff")).chmod(0o600)
        metadata["files"][name] = {
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "output_sha256": hashlib.sha256(after.encode()).hexdigest(),
        }
        if name == "airplay-led.sh":
            metadata["compatibility"]["airplay_led_guard"] = 3
    path = args.output / "review.json"
    path.write_text(json.dumps(metadata, indent=2) + "\n")
    path.chmod(0o600)
    print("Prepared private sibling files and review diffs. No device was modified.")


if __name__ == "__main__":
    main()
