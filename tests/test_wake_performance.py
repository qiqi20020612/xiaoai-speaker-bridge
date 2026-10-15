"""Offline diagnostics and critical-path invariants; never hardware acceptance."""

import io
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_lx06_v016 import json_binary as json_binary
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import script
from tests.test_text_pipeline import text_device as text_device
from tools.profile_report import distribution, read_points, summarize

ROOT = Path(__file__).resolve().parents[1]


def test_summary_counts_complete_cycles_with_and_without_detail_and_no_legacy_totals():
    points = []
    for cycle in range(1, 21):
        points.extend(
            [
                ("42", "wake", 0.0, str(cycle)),
                ("43", "recording_launch", float(cycle), str(cycle)),
            ]
        )
        if cycle % 2:
            points.append(("44", "detail", float(cycle), str(cycle)))
        points.append(("44", "recording_first_pcm", float(cycle) + 0.25, str(cycle)))
    points += [("90", "wake", 0, None), ("90", "recording_launch", 900, None)]
    result = summarize(points)
    assert result["wake", "recording_launch"] == distribution(list(range(1, 21)))
    assert result["wake", "recording_launch"]["p95"] == 19
    assert result["recording_launch", "recording_first_pcm"] == {
        "samples": 20,
        "median": 0.25,
        "p95": 0.25,
        "max": 0.25,
    }
    assert not summarize([("42", "wake", 1, "1"), ("43", "recording_launch", 2, "2")])
    assert list(
        read_points(
            io.StringIO(
                "pid=1 phase=wake uptime=1.00 cycle=1.2.3\n"
                "pid=1 phase=wake uptime=2.00 secret=ignore\n"
            )
        )
    ) == [("1", "wake", 1.0, "1.2.3")]


@pytest.fixture
def trace_binary(json_binary, native_json_binary, tmp_path):
    if sys.platform == "linux":
        return json_binary
    # macOS lacks /proc: this fixed test clock verifies events, never latency.
    clock = tmp_path / "clock"
    clock.write_text("42.50 0\n")
    source = tmp_path / "json_ops.c"
    source.write_text(
        (ROOT / "speaker/src/json_ops.c").read_text().replace("/proc/uptime", str(clock))
    )
    binary = tmp_path / "trace"
    flags = [] if json_binary == native_json_binary else ["-fsanitize=address,undefined"]
    subprocess.run(
        [
            "cc",
            "-D_DEFAULT_SOURCE",
            "-D_DARWIN_C_SOURCE",
            "-std=c11",
            *flags,
            "-I",
            str(ROOT / "speaker/src"),
            str(ROOT / "speaker/src/json.c"),
            str(source),
            str(ROOT / "tests/json_ops_cli.c"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
    )
    return binary


def wav_header(channels=1, bits=16, extended=False):
    fmt = struct.pack(
        "<HHIIHH", 1, channels, 16000, 16000 * channels * bits // 8, channels * bits // 8, bits
    )
    if extended:
        fmt += b"\0\0"
    junk = b"JUNK" + struct.pack("<I", 3) + b"abc\0"
    return (
        b"RIFF"
        + struct.pack("<I", 1000)
        + b"WAVE"
        + junk
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", 640)
    )


@pytest.mark.parametrize("channels,bits,extended", [(1, 16, False), (8, 16, True), (2, 24, False)])
def test_first_pcm_requires_complete_frame_after_variable_split_headers(
    trace_binary, tmp_path, channels, bits, extended
):
    trace = tmp_path / "profile"
    trace.write_text("")
    header = wav_header(channels, bits, extended)
    frame = bytes(range(channels * bits // 8))
    proc = subprocess.Popen(
        [str(trace_binary), "--trace-pcm", str(trace), "42.99.1"],
        env={**os.environ, "XIAOAI_PROFILE_PCM": "1"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for part in (header[:3], header[3:17], header[17:], frame[:-1]):
            if not part:
                continue
            proc.stdin.write(part)
            proc.stdin.flush()
            assert proc.stdout.read(len(part)) == part
            assert "recording_first_pcm" not in trace.read_text()
        proc.stdin.write(frame[-1:])
        proc.stdin.flush()
        assert proc.stdout.read(1) == frame[-1:]
        lines = trace.read_text().splitlines()
        assert len(lines) == 2
        assert "phase=recording_first_output" in lines[0]
        assert "phase=recording_first_pcm" in lines[1]
        assert all(line.endswith("cycle=42.99.1") for line in lines)
        proc.stdin.close()
        proc.wait(timeout=3)
        assert proc.returncode == 0 and proc.stderr.read() == b""
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize("enabled", [False, True])
def test_pcm_diagnostic_is_optional_and_bytes_survive_invalid_or_failed_trace(
    trace_binary, tmp_path, enabled
):
    valid = wav_header() + b"\0" * 640
    # Unsupported/invalid/truncated formats must not invent PCM observations.
    invalid = [
        b"raw bytes",
        wav_header()[:-1],
        wav_header() + b"\0",
        b"RIFF\0\0\0\0WAVEdata" + struct.pack("<I", 64) + b"\0" * 64,
        b"RIFF\0\0\0\0WAVEJUNK" + struct.pack("<I", 0xFFFFFFFF) + b"short",
        valid.replace(b"fmt \x10\0\0\0\x01\0", b"fmt \x10\0\0\0\x03\0"),
    ]
    for index, data in enumerate([valid, *invalid]):
        trace = tmp_path / f"trace-{index}"
        trace.write_text("")
        result = subprocess.run(
            [str(trace_binary), "--trace-pcm", str(trace), "42.99.2"],
            input=data,
            env={**os.environ, "XIAOAI_PROFILE_PCM": "1" if enabled else "0"},
            capture_output=True,
            timeout=3,
        )
        assert result.returncode == 0 and result.stdout == data and not result.stderr
        assert ("recording_first_pcm" in trace.read_text()) == (enabled and index == 0)
    full = tmp_path / "full"
    full.write_bytes(b"x" * 65536)
    link = tmp_path / "link"
    link.symlink_to(full)
    for path in (full, link, tmp_path / "missing"):
        result = subprocess.run(
            [str(trace_binary), "--trace-pcm", str(path), "42.99.2"],
            input=valid,
            env={**os.environ, "XIAOAI_PROFILE_PCM": "1"},
            capture_output=True,
            timeout=3,
        )
        assert result.returncode == 0 and result.stdout == valid
    assert full.read_bytes() == b"x" * 65536 and not (tmp_path / "missing").exists()


def test_normal_recorder_never_starts_diagnostic_helper(text_device, speaker_shell):
    _, path, env = text_device
    script(path / "tools/rec", "#!/bin/sh\nprintf original-pcm\n")
    script(
        path / "tools/forbidden",
        '#!/bin/sh\nprintf invoked > "$STATE_DIR/trace-invoked"\nexit 99\n',
    )
    body = f'. "{path}/bridge/lib/listener-hooks.sh"\nRECORD_COMMAND=rec\nbridge_record_response\n'
    result = subprocess.run(
        [*speaker_shell, "-c", body],
        env={
            **env,
            "XIAOAI_PROFILE": "0",
            "XIAOAI_PROFILE_PCM": "1",
            "XIAOAI_BRIDGE_JSON_BIN": str(path / "tools/forbidden"),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stdout == "original-pcm"
    assert not (path / "trace-invoked").exists() and not (path / "xiaoai_bridge.profile").exists()


def test_detail_trace_is_opt_in_and_reports_slow_query_separately(text_device, speaker_shell):
    _, path, env = text_device
    bus = path / "tools/dbus-send"
    bus.write_text(bus.read_text().replace("#!/bin/sh", "#!/bin/sh\nsleep 0.03"))
    body = f'. "{path}/bridge/lib/listener-hooks.sh"\nbridge_voice_begin || exit 71\nbridge_restore_volume\n'
    for detail in ("0", "1"):
        trace = path / "xiaoai_bridge.profile"
        trace.write_text("")
        result = subprocess.run(
            [*speaker_shell, "-c", body],
            env={**env, "XIAOAI_PROFILE": "1", "XIAOAI_PROFILE_DETAIL": detail},
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert result.returncode == 0, result.stderr
        text = trace.read_text()
        assert ("phase=journal_airplay_begin" in text) == (detail == "1")
        assert ("phase=source_bluez_end" in text) == (detail == "1")
        if detail == "1" and sys.platform == "linux":
            stats = summarize(read_points(io.StringIO(text)))
            assert stats["source_bluez_begin", "source_bluez_end"]["median"] >= 0.02
        assert "synthetic-token" not in text


def test_offline_wake_benchmark_uses_equal_routes_and_counts_queries():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.benchmark_wake",
            "--samples",
            "1",
            "--scenarios",
            "idle",
            "multiple",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert "NOT LX06" in report["evidence"]
    for scenario in report["scenarios"].values():
        assert scenario["baseline"]["snapshot"] == scenario["candidate"]["snapshot"]
        for run in scenario.values():
            assert run["counts_stable"]
            assert run["before_record_commands"]["dbus:airplay"] == 2
            assert run["before_record_commands"]["dbus:bluez"] == 1
            assert run["before_record_commands"]["mpd"] == 1
            assert run["before_record_commands"]["sed"] == 3
            assert run["stages"]["wake->recording_launch"]["samples"] == 1
