#!/usr/bin/env python3
"""Offline wake comparison: synthetic devices and clocks, never LX06 timings."""

import argparse
import collections
import io
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

from tools.profile_report import distribution, read_points, summarize

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {
    "idle": ("false", "idle", "stop"),
    "airplay": ("true", "idle", "stop"),
    "bluetooth": ("false", "active", "stop"),
    "mpd": ("false", "idle", "play"),
    "multiple": ("true", "active", "play"),
    "unknown": ("", "", "unknown"),
}


def executable(path, source):
    path.write_text(source)
    path.chmod(0o700)


def checkout(target, revision):
    """Copy tracked public speaker files only; never private/ignored adapters."""
    names = subprocess.check_output(
        [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            revision if revision != "working-tree" else "HEAD",
            "speaker",
        ],
        cwd=ROOT,
        text=True,
    ).splitlines()
    for name in names:
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = (
            (ROOT / name).read_bytes()
            if revision == "working-tree"
            else subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=ROOT)
        )
        dest.write_bytes(data)
        if name.endswith(".sh"):
            dest.chmod(0o700)


def prepare(root, revision):
    checkout(root, revision)
    speaker = root / "speaker"
    proc = root / "proc"
    proc.mkdir()
    (proc / "uptime").write_text(f"{time.monotonic():.3f} 0\n")
    # The same workstation monotonic clock drives both shell and C trace points.
    source = speaker / "src/json_ops.c"
    source.write_text(source.read_text().replace("/proc/uptime", str(proc / "uptime")))
    subprocess.run(
        [
            "cc",
            "-D_DEFAULT_SOURCE",
            "-D_DARWIN_C_SOURCE",
            "-std=c11",
            "-Os",
            str(speaker / "src/json.c"),
            str(source),
            str(ROOT / "tests/json_ops_cli.c"),
            "-I",
            str(speaker / "src"),
            "-o",
            str(root / "json-helper"),
        ],
        check=True,
        capture_output=True,
    )
    tools = root / "bin"
    tools.mkdir()
    for name in ("awk", "sed", "cat", "chmod", "mv", "tr", "head"):
        executable(
            tools / name,
            f"""#!/bin/sh
printf '{name}\\n' >> "$BENCH_COUNT"
exec {shlex.quote(shutil.which(name))} "$@"
""",
        )
    executable(
        tools / "flock",
        f"""#!{sys.executable}
import fcntl, sys
args=sys.argv[1:]
mode=fcntl.LOCK_UN if '-u' in args else fcntl.LOCK_EX | fcntl.LOCK_NB
try: fcntl.flock(int(args[-1]),mode)
except BlockingIOError: sys.exit(1)
""",
    )
    executable(
        tools / "amixer",
        """#!/bin/sh
printf 'amixer:%s\\n' "$1" >> "$BENCH_COUNT"
if [ "$1" = get ]; then printf 'Front Left: 153 [60%%]\\n'; fi
""",
    )
    executable(
        tools / "dbus-send",
        """#!/bin/sh
case "$*" in *org.bluez*) kind=bluez;; *) kind=airplay;; esac
printf 'dbus:%s\\n' "$kind" >> "$BENCH_COUNT"
sleep "$BENCH_DELAY"
if [ "$kind" = bluez ]; then
 [ -n "$BENCH_BT" ] || exit 1
 printf 'string "State"\\nvariant string "%s"\\n' "$BENCH_BT"
else
 [ -n "$BENCH_AP" ] || exit 1
 printf 'string "Active"\\nvariant boolean %s\\nstring "Volume"\\nvariant double -18\\n' "$BENCH_AP"
fi
""",
    )
    executable(
        tools / "bridge",
        """#!/bin/sh
case "$1" in
 --snapshot) printf 'policy\\n' >> "$BENCH_COUNT"; printf '{"source":null,"uncertain":false}\\n';;
 --mpd-state) printf 'mpd\\n' >> "$BENCH_COUNT"; sleep "$BENCH_DELAY"; printf '%s\\n' "$BENCH_MPD";;
 --emit) printf 'emit\\n' >> "$BENCH_COUNT";;
esac
""",
    )
    executable(
        tools / "json",
        f"""#!/bin/sh
case "$1" in --json-op) printf 'json:%s\\n' "$2" >> "$BENCH_COUNT";; esac
exec {shlex.quote(str(root / "json-helper"))} "$@"
""",
    )
    executable(tools / "led", "#!/bin/sh\n:\n")
    for name in ("common.sh", "locks.sh", "listener-hooks.sh"):
        file = speaker / "lib" / name
        file.write_text(
            file.read_text()
            .replace("/proc/uptime", str(proc / "uptime"))
            .replace("/proc/$1/stat", str(proc) + "/$1/stat")
            .replace("/bin/show_led", str(tools / "led"))
            .replace("/bin/shut_led", str(tools / "led"))
        )
    audio = root / "synthetic.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0" * 640)
    executable(
        tools / "rec",
        f"""#!{sys.executable}
import sys,time
from pathlib import Path
data=Path({str(audio)!r}).read_bytes()
sys.stdout.buffer.write(data[:44]); sys.stdout.buffer.flush()
time.sleep(0.02)
sys.stdout.buffer.write(data[44:])
""",
    )
    return proc, audio


def trial(root, proc, audio, scenario, delay, detail):
    ap, bt, mpd = SCENARIOS[scenario]
    for name in ("counts", "xiaoai_bridge.profile"):
        (root / name).write_text("")
    env = {
        **os.environ,
        "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
        "XIAOAI_BRIDGE_DIR": str(root / "speaker"),
        "XIAOAI_BRIDGE_RUNTIME": str(root),
        "XIAOAI_BRIDGE_BIN": str(root / "bin/bridge"),
        "XIAOAI_BRIDGE_JSON_BIN": str(root / "bin/json"),
        "XIAOAI_PROFILE": "1",
        "XIAOAI_PROFILE_PCM": "1",
        "XIAOAI_PROFILE_DETAIL": "1" if detail else "0",
        "BENCH_COUNT": str(root / "counts"),
        "BENCH_AP": ap,
        "BENCH_BT": bt,
        "BENCH_MPD": mpd,
        "BENCH_DELAY": str(delay),
    }
    # Fake /proc starttime models the immutable identity; kernel flock still
    # protects real inherited FDs. No device, HA server or private script exists.
    body = f"""
mkdir -p "{proc}/$$"
printf '%s (synthetic listener) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 999\\n' "$$" > "{proc}/$$/stat"
. "{root}/speaker/lib/listener-hooks.sh"
bridge_voice_detected
bridge_voice_begin || exit 71
saved=$(amixer get mysoftvol | awk '$0~/%/{{print $4}}' | tr -d '[]%' | head -n1)
if [ "$saved" -gt 10 ]; then bridge_voice_attenuate 10 || exit 72; fi
sleep 0.15
cp "$BENCH_COUNT" "{root}/before-record"
printf '%s' "$BRIDGE_VOICE_SNAPSHOT" > "{root}/snapshot"
RECORD_COMMAND=rec
bridge_record_response > "{root}/captured"
bridge_restore_volume || exit 73
"""
    stopped = threading.Event()

    def clock():
        while not stopped.is_set():
            tmp = proc / "next"
            tmp.write_text(f"{time.monotonic():.3f} 0\n")
            tmp.replace(proc / "uptime")
            stopped.wait(0.005)

    updater = threading.Thread(target=clock)
    updater.start()
    at = time.monotonic()
    try:
        result = subprocess.run(["sh", "-c", body], env=env, capture_output=True, timeout=30)
        elapsed = time.monotonic() - at
    finally:
        stopped.set()
        updater.join()
    if result.returncode:
        raise RuntimeError(f"Synthetic wake failed: {result.returncode}: {result.stderr.decode()}")
    if (root / "captured").read_bytes() != audio.read_bytes():
        raise ValueError("Recorder bytes changed")
    snapshot = json.loads((root / "snapshot").read_text())
    snapshot.pop("pid")
    return {
        "wall": elapsed,
        "points": list(read_points(io.StringIO((root / "xiaoai_bridge.profile").read_text()))),
        "counts": dict(collections.Counter((root / "before-record").read_text().splitlines())),
        "snapshot": snapshot,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="v0.1.7")
    parser.add_argument("--candidate", default="working-tree")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--query-delay", type=float, default=0)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    args = parser.parse_args()
    if (
        not 1 <= args.samples <= 100
        or not 0 <= args.query_delay <= 0.6
        or not 0 <= args.warmup <= 3
    ):
        parser.error("Use 1..100 samples, 0..3 warmups and 0..0.6 seconds synthetic delay")
    report = {
        "evidence": "Controlled workstation simulation; NOT LX06 latency or CPU/RSS acceptance",
        "host": platform.platform(),
        "clock": "workstation monotonic file updated every 5ms",
        "baseline": args.baseline,
        "candidate": args.candidate,
        "query_delay_seconds": args.query_delay,
        "warmup_per_scenario_and_version": args.warmup,
        "excluded": "KWS/Guard, real LED/mixer/DBus/MPD, miplay, STT and providers; prompt is only the original 0.15s wait",
        "detail": args.detail,
        "scenarios": {},
    }
    with tempfile.TemporaryDirectory(prefix="xiaoai-wake-") as directory:
        roots = [Path(directory) / str(i) for i in range(2)]
        setup = [
            prepare(root, ref)
            for root, ref in zip(roots, (args.baseline, args.candidate), strict=True)
        ]
        for scenario in args.scenarios:
            runs = [[], []]
            for _ in range(args.warmup):
                for index in (0, 1):
                    trial(roots[index], *setup[index], scenario, args.query_delay, args.detail)
            for sample in range(args.samples):
                for index in (sample % 2, 1 - sample % 2):
                    runs[index].append(
                        trial(roots[index], *setup[index], scenario, args.query_delay, args.detail)
                    )
                if runs[0][-1]["snapshot"] != runs[1][-1]["snapshot"]:
                    raise ValueError(f"Routing differs: {scenario}")
            entries = []
            for group in runs:
                stages = summarize(point for run in group for point in run["points"])
                entries.append(
                    {
                        "samples": len(group),
                        "wall_including_restore": distribution([run["wall"] for run in group]),
                        "stages": {f"{a}->{b}": value for (a, b), value in stages.items()},
                        "before_record_commands": group[0]["counts"],
                        "counts_stable": all(run["counts"] == group[0]["counts"] for run in group),
                        "snapshot": group[0]["snapshot"],
                    }
                )
            report["scenarios"][scenario] = {"baseline": entries[0], "candidate": entries[1]}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
