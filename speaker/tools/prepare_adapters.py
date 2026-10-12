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
    source = exactly_once(
        source,
        "# Set lower rate if cannot convert later",
        ". /data/xiaoai_bridge/lib/listener-hooks.sh\n\n# Set lower rate if cannot convert later",
    )
    source = exactly_once(
        source, "/bin/show_led 1", "# Wake LED is handled before snapshot in bridge_voice_begin"
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
    # Error TTS participates in the same local audio mutex.
    source = source.replace("${SPEAK} error", "bridge_error_tts")
    return source


def prepare_airplay_led(source: str) -> str:
    if "xiaoai_bridge/lib/common.sh" in source:
        raise ValueError("Use the saved unmodified LED source, not an already adapted file")
    source = exactly_once(source, "#!/bin/sh", "#!/bin/sh\n. /data/xiaoai_bridge/lib/common.sh")
    source = exactly_once(source, "draw() {", "draw() {\n    voice_busy && return 0")
    source = exactly_once(
        source,
        '    while [ -f "$STATE" ]; do',
        '    while [ -f "$STATE" ]; do\n        if voice_busy; then rm -f "$STATE"; break; fi',
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
    parser.add_argument("--airplay-led", type=Path)
    parser.add_argument("--shairport", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    pairs = [(args.listener, "listener.sh", prepare_listener)]
    if args.airplay_led:
        pairs.append((args.airplay_led, "airplay-led.sh", prepare_airplay_led))
    if args.shairport:
        pairs.append((args.shairport, "shairport-sync.conf", prepare_shairport))
    metadata = {"format": 1, "files": {}}
    for original, name, transform in pairs:
        before = original.read_text()
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
            "source_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "output_sha256": hashlib.sha256(after.encode()).hexdigest(),
        }
    path = args.output / "review.json"
    path.write_text(json.dumps(metadata, indent=2) + "\n")
    path.chmod(0o600)
    print("Prepared private sibling files and review diffs. No device was modified.")


if __name__ == "__main__":
    main()
