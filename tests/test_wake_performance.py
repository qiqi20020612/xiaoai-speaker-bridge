"""Offline diagnostics and critical-path invariants; never hardware acceptance."""

import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
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
        proc.stdin.close()
        proc.wait(timeout=3)
        lines = trace.read_text().splitlines()
        assert len(lines) == 2
        assert "phase=recording_first_output" in lines[0]
        assert "phase=recording_first_pcm" in lines[1]
        assert all(line.endswith("cycle=42.99.1") for line in lines)
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
            assert run["stages"]["wake->recording_launch"]["samples"] == 1
        before = scenario["baseline"]["before_record_commands"]
        after = scenario["candidate"]["before_record_commands"]
        assert before["sed"] == 3 and after["sed"] == 1
        assert before["awk"] == 10 and after["awk"] == 7
        assert before["cat"] == 2 and after.get("cat", 0) == 0
        for command in ("json:validate", "chmod", "mv", "amixer:get", "amixer:sset"):
            assert before[command] == after[command]


@pytest.mark.parametrize(
    "data,expected",
    [
        (
            'string "Volume"\nvariant double -18\nstring "Active"\nvariant boolean true\n',
            ["-18", "true"],
        ),
        (
            'string "Active"\nvariant boolean false\nstring "Volume"\nvariant double -6\n',
            ["-6", "false"],
        ),
        ('string "Active"\nvariant boolean true\n', ["", "true"]),
        ('string "Volume"\nvariant double -18\n', ["-18", ""]),
        ('string "Volume"\nvariant string ""\n', ["", ""]),
        ('string "Volume"\nvariant double -18\nstring "Volume"\nvariant double -6\n', ["-18", ""]),
        ('string "Volume"\nstring "Active"\nvariant boolean true\n', ["true", "true"]),
        ("unrecognized output\n", ["", ""]),
    ],
)
def test_pair_parser_preserves_independent_first_matches(
    text_device, speaker_shell, data, expected
):
    _, path, env = text_device
    command = f'. "{path}/bridge/lib/common.sh"\ndbus_property Volume Active\n'
    pair = subprocess.run(
        [*speaker_shell, "-c", command],
        input=data,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert pair.stdout.splitlines() == expected
    for name, value in zip(("Volume", "Active"), expected, strict=True):
        single = subprocess.run(
            [*speaker_shell, "-c", f'. "{path}/bridge/lib/common.sh"\ndbus_property {name}\n'],
            input=data,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert single.stdout.rstrip("\n") == value


@pytest.mark.parametrize("stdin", [False, True])
def test_atomic_json_keeps_validation_literal_bytes_and_original_on_failure(
    text_device, speaker_shell, stdin
):
    _, path, env = text_device
    target = path / "atomic.json"
    content = json.dumps({"literal": "$(must_not_run) `must_not_run`\n你好"}, ensure_ascii=False)
    command = f'. "{path}/bridge/lib/common.sh"\n' + (
        'printf "%s" "$CONTENT" | atomic_json "$STATE_DIR/atomic.json"'
        if stdin
        else 'atomic_json "$STATE_DIR/atomic.json" "$CONTENT"'
    )
    for data, valid in ((content, True), ('{"duplicate":1,"duplicate":2}', False)):
        result = subprocess.run(
            [*speaker_shell, "-c", command],
            env={**env, "CONTENT": data},
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert (result.returncode == 0) is valid
        assert target.read_text() == content and target.stat().st_mode & 0o777 == 0o600
        assert list(path.glob("atomic.json.*")) == []


def test_live_voice_reuses_only_its_cycle_identity_and_standalone_callers_read_fresh(
    text_device, speaker_shell
):
    _, path, env = text_device
    body = f'''. "{path}/bridge/lib/listener-hooks.sh"
bridge_pid_start() {{ printf 'read\\n' >> "$STATE_DIR/identity-reads";printf 999; }}
bridge_voice_begin || exit 71
cp "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" "$STATE_DIR/saved-journal"
cp "$BRIDGE_RUNTIME/xiaoai_bridge.voice" "$STATE_DIR/saved-owner"
[ "$(wc -l < "$STATE_DIR/identity-reads")" -eq 1 ] || exit 72
bridge_mark_owner "$STATE_DIR/independent-owner"
bridge_volume_journal "$STATE_DIR/independent-journal" '{{"tts":60}}' || exit 73
[ "$(wc -l < "$STATE_DIR/identity-reads")" -eq 3 ] || exit 74
bridge_restore_volume
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=8
    )
    assert result.returncode == 0, result.stderr
    journal = json.loads((path / "saved-journal").read_text())
    assert (path / "saved-owner").read_text().split() == [str(journal["owner"]), journal["start"]]
    assert journal["start"] == "999"


def test_airplay_transition_between_journal_and_snapshot_remains_observable(
    text_device, speaker_shell
):
    _, path, env = text_device
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
case "$*" in *org.bluez*) printf 'string "State"\\nvariant string "idle"\\n';exit 0;; esac
n=0;[ ! -f "$STATE_DIR/ap-count" ] || read n < "$STATE_DIR/ap-count"
n=$((n+1));printf '%s\\n' "$n" > "$STATE_DIR/ap-count"
if [ "$n" = 1 ];then active=false;raw=-18;else active=true;raw=-6;fi
printf 'string "Active"\\nvariant boolean %s\\nstring "Volume"\\nvariant double %s\\n' "$active" "$raw"
""",
    )
    script(
        path / "tools/fake-bridge",
        """#!/bin/sh
case "$1" in
 --snapshot) printf '{"source":null,"uncertain":false}\\n';;
 --mpd-state) printf stop;;
 --emit) exit 0;;
esac
""",
    )
    body = f'''. "{path}/bridge/lib/listener-hooks.sh"
bridge_voice_begin || exit 71
cp "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" "$STATE_DIR/saved-journal"
printf '%s' "$BRIDGE_VOICE_SNAPSHOT" > "$STATE_DIR/saved-snapshot"
cp "$STATE_DIR/ap-count" "$STATE_DIR/pre-record-count"
bridge_restore_volume
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=8
    )
    assert result.returncode == 0, result.stderr
    journal = json.loads((path / "saved-journal").read_text())
    snapshot = json.loads((path / "saved-snapshot").read_text())
    assert journal["airplay_active"] == "false" and journal["airplay_raw"] == "-18"
    assert snapshot["observed_sources"]["airplay"] is True
    assert snapshot["active_sources"] == ["airplay"]
    assert (path / "pre-record-count").read_text().strip() == "2"


@pytest.mark.parametrize("failure", ["volume", "journal"])
def test_unsafe_preparation_failure_never_attenuates_or_records(
    text_device, speaker_shell, failure
):
    _, path, env = text_device
    if failure == "volume":
        script(path / "tools/amixer", "#!/bin/sh\nexit 1\n")
    setup = (
        'mkdir "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json.$$"'
        if failure == "journal"
        else ":"
    )
    body = f'''. "{path}/bridge/lib/listener-hooks.sh"
{setup}
if bridge_voice_begin; then printf unsafe > "$STATE_DIR/recorded";exit 71;fi
[ ! -f "$BRIDGE_RUNTIME/xiaoai_bridge.voice" ] || exit 72
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=8
    )
    assert result.returncode == 0, result.stderr
    assert not (path / "recorded").exists() and not (path / "mixer-writes").exists()


@pytest.mark.parametrize("failure", ["policy", "airplay", "bluez", "mpd"])
def test_failed_query_partial_output_is_unknown_not_confirmed_activity(
    text_device, speaker_shell, failure
):
    _, path, env = text_device
    script(
        path / "tools/fake-bridge",
        """#!/bin/sh
case "$1" in
 --snapshot)
  printf '{"source":null,"uncertain":false}\\n'
  [ "$FAIL_QUERY" != policy ];;
 --mpd-state)
  if [ "$FAIL_QUERY" = mpd ];then printf play;exit 1;fi
  printf stop;;
esac
""",
    )
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
case "$*" in *--reply-timeout=600*) :;; *) exit 99;; esac
case "$*" in *org.bluez*)
 if [ "$FAIL_QUERY" = bluez ];then printf 'string "State"\\nvariant string "active"\\n';exit 1;fi
 printf 'string "State"\\nvariant string "idle"\\n';;
 *)
 if [ "$FAIL_QUERY" = airplay ];then printf 'string "Active"\\nvariant boolean true\\n';exit 1;fi
 printf 'string "Active"\\nvariant boolean false\\n';;
esac
""",
    )
    result = subprocess.run(
        [*speaker_shell, str(path / "bridge/lib/source-snapshot.sh"), "42", "60", "60"],
        env={**env, "FAIL_QUERY": failure},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)
    expected = {
        name: None if name == failure else False for name in ("airplay", "bluetooth", "mpd")
    }
    if failure == "bluez":
        expected["bluetooth"] = None
    assert snapshot["observed_sources"] == expected
    assert snapshot["active_sources"] == [] and snapshot["source"] is None
    assert snapshot["uncertain"] is (failure != "policy")


def test_failed_airplay_reply_cannot_become_journal_baseline(text_device, speaker_shell):
    _, path, env = text_device
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
printf 'string "Volume"\\nvariant double -6\\nstring "Active"\\nvariant boolean true\\n'
exit 1
""",
    )
    body = f'''. "{path}/bridge/lib/common.sh"
bridge_volume_journal "$STATE_DIR/journal" '{{"master":60,"bluetooth":60}}'
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    journal = json.loads((path / "journal").read_text())
    assert journal["airplay_active"] == "" and journal["airplay_raw"] == ""


@pytest.fixture(scope="module")
def mpd_cli():
    # Compile a public source copy against an isolated Unix socket. No access to
    # /var/run/mpd.sock or any media action is possible through this fixture.
    with tempfile.TemporaryDirectory(prefix="xsb-mpd-", dir="/tmp") as directory:
        root = Path(directory)
        path = root / "mpd.sock"
        source = root / "bridge.c"
        source.write_text(
            (ROOT / "speaker/src/bridge.c").read_text().replace("/var/run/mpd.sock", str(path))
        )
        binary = root / "bridge"
        subprocess.run(
            [
                "cc",
                "-D_DEFAULT_SOURCE",
                "-D_DARWIN_C_SOURCE",
                "-std=c11",
                "-Os",
                "-I",
                str(ROOT / "speaker/src"),
                str(source),
                str(ROOT / "speaker/src/json.c"),
                str(ROOT / "speaker/src/json_ops.c"),
                "-o",
                str(binary),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run([str(binary), "--version"], check=True, capture_output=True)
        config = root / "config.json"
        config.write_text(json.dumps({"device_id": "synthetic", "token": "s" * 32}))
        config.chmod(0o600)
        yield binary, config, path


@pytest.mark.parametrize(
    "mode,expected", [("slow", "play"), ("timeout", "unknown"), ("ack", "unknown")]
)
def test_native_mpd_slow_response_and_read_timeout_are_bounded(mpd_cli, mode, expected):
    binary, config, path = mpd_cli
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        server.settimeout(4)
        errors = []

        def respond():
            try:
                connection, _ = server.accept()
                with connection:
                    if mode == "timeout":
                        time.sleep(1.2)
                        return
                    time.sleep(0.03)
                    connection.sendall(b"OK MPD 0.23.0\n")
                    assert connection.recv(64) == b"status\n"
                    time.sleep(0.03)
                    connection.sendall(
                        b"state: play\nOK\n" if mode == "slow" else b"ACK [5@0] {status} denied\n"
                    )
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            result = subprocess.run(
                [str(binary), "--config", str(config), "--mpd-state"],
                capture_output=True,
                text=True,
                timeout=4,
            )
        finally:
            worker.join(timeout=4)
        assert not worker.is_alive() and not errors
        assert result.returncode == 0 and result.stdout.strip() == expected
