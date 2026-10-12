#!/usr/bin/env python3
"""Controlled local fork-cost comparison; synthetic sources, never hardware."""

import argparse
import csv
import json
import os
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def executable(path, source):
    path.write_text(source)
    path.chmod(0o700)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="origin/main")
    parser.add_argument("--jq-delay", type=float, default=0.03)
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    if not 0 <= args.jq_delay <= 1 or not 1 <= args.samples <= 10:
        parser.error("Use a bounded delay and sample count")
    baseline = subprocess.check_output(
        ["git", "show", f"{args.baseline}:speaker/lib/source-snapshot.sh"], cwd=ROOT, text=True
    )
    real_jq = shutil.which("jq")
    if not real_jq:
        parser.error("jq is required")
    writer = csv.writer(sys.stdout)
    writer.writerow(
        [
            "synthetic_source",
            "old_median_s",
            "new_median_s",
            "old_jq_runs",
            "new_jq_runs",
            "routing_matches",
            "injected_jq_delay_s",
        ]
    )
    with tempfile.TemporaryDirectory(prefix="xiaoai-snapshot-") as folder:
        root = Path(folder)
        tools = root / "bin"
        tools.mkdir()
        shutil.copytree(ROOT / "speaker/lib", root / "lib")
        (root / "old.sh").write_text(baseline)
        executable(
            tools / "jq",
            f'#!/bin/sh\nprintf x >> "$BENCH_COUNTER"\nsleep {args.jq_delay}\nexec {shlex.quote(real_jq)} "$@"\n',
        )
        executable(
            tools / "bridge",
            """#!/bin/sh
case "$1" in --snapshot) printf '{"source":null,"uncertain":false}\\n' ;; --mpd-state) printf '%s\\n' "$BENCH_MPD" ;; esac
""",
        )
        executable(
            tools / "dbus-send",
            """#!/bin/sh
case "$*" in *org.bluez*) printf 'string "State"\\nvariant string "%s"\\n' "$BENCH_BT" ;;
*) printf 'string "Active"\\nvariant boolean %s\\n' "$BENCH_AP" ;; esac
""",
        )
        for name, ap, bt, mpd in [
            ("idle", "false", "idle", "stop"),
            ("airplay", "true", "idle", "stop"),
            ("bluetooth", "false", "active", "stop"),
            ("mpd", "false", "idle", "play"),
            ("multiple", "true", "active", "play"),
        ]:
            env = {
                **os.environ,
                "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                "XIAOAI_BRIDGE_DIR": str(root),
                "XIAOAI_BRIDGE_BIN": str(tools / "bridge"),
                "XIAOAI_BRIDGE_RUNTIME": str(root),
                "BENCH_COUNTER": str(root / "count"),
                "BENCH_AP": ap,
                "BENCH_BT": bt,
                "BENCH_MPD": mpd,
            }
            durations, counts, results = [], [], []
            for script in [root / "old.sh", root / "lib/source-snapshot.sh"]:
                elapsed = []
                for _ in range(args.samples):
                    (root / "count").write_text("")
                    at = time.monotonic()
                    response = subprocess.check_output(["sh", str(script)], env=env, text=True)
                    elapsed.append(time.monotonic() - at)
                results.append(json.loads(response))
                counts.append(len((root / "count").read_text()))
                durations.append(statistics.median(elapsed))
            same = all(
                results[0][key] == results[1][key]
                for key in ("source", "uncertain", "active_sources")
            )
            if not same:
                raise ValueError("Synthetic routing mismatch")
            writer.writerow(
                [name, *[f"{value:.4f}" for value in durations], *counts, same, args.jq_delay]
            )


if __name__ == "__main__":
    main()
