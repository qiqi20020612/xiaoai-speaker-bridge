"""jq 1.6, bounded native schemas and observable LX06 lifecycle regressions.

Every file, service, mixer and recording command is an isolated fixture. Counts
and order are assertions; workstation/QEMU timings never certify LX06 latency.
"""

import csv
import fcntl
import itertools
import json
import os
import random
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_text_pipeline import enable_airplay_duck_fixture, script
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import text_device as text_device

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_FILTER = """[if $ap then "airplay" else empty end, if $bt then "bluetooth" else empty end,
 if $mpd=="play" then "mpd" else empty end] as $sources |
.source as $old | .active_sources=$sources |
.source=(if ($sources|index($old)) then $old elif ($sources|length)==1 then $sources[0] else null end) |
.observed_sources={airplay:$ap_observed,bluetooth:$bt_observed,
 mpd:(if $mpd=="unknown" then null else $mpd=="play" end)} |
(.priority // ["airplay","bluetooth","mpd"]) as $priority | .observed_sources as $observed |
.source as $selected |
.uncertain=(($sources|length)>1 or any($priority[]; . as $s | $observed[$s]==null and
 ($selected==null or ($priority|index($s))<($priority|index($selected))))) |
.+{type:"voice_begin",pid:42,volumes:{master:60,bluetooth:50}}
"""
JOURNAL_FILTER = """{owner:77,start:"88",revision:$desired.revision,volumes:$values,
 airplay_raw:$raw,airplay_active:$active} |
if $duck!=null then .revision=$duck.revision |
 .volumes.master=(if ($desired.versions.master // 0)>$duck.revision
 then $desired.volumes.master else $duck.volumes.master end) |
 if ($active=="" or ($current==$duck.applied_master)) then
 .airplay_raw=$duck.airplay_raw | .airplay_active=$duck.airplay_active | .applied_master=$duck.applied_master
 elif ($raw!=$duck.airplay_raw or $active!=$duck.airplay_active) and
 (($desired.versions.master // 0)<=$duck.revision) then .volumes.master=$current
 else .applied_master=$duck.applied_master end
else . end
"""


def op(binary, name, *args, data=None, **kwargs):
    return subprocess.run(
        [str(binary), "--json-op", name, *map(str, args)],
        input=None
        if data is None
        else json.dumps(data)
        if not isinstance(data, str | bytes)
        else data,
        capture_output=True,
        text=not isinstance(data, bytes),
        timeout=5,
        **kwargs,
    )


@pytest.fixture(params=["native", "sanitized"])
def json_binary(request, native_json_binary):
    if request.param == "native":
        return native_json_binary
    configured = os.environ.get("XIAOAI_TEST_JSON_SANITIZER")
    if not configured:
        pytest.skip("Configure XIAOAI_TEST_JSON_SANITIZER; required in Linux CI")
    return Path(configured).resolve()


def test_real_jq16_reproduces_nul_bug_and_codepoint_check(jq16_binary):
    for text, expected in [("hello", True), ("你好", True), ("", True), ("a\0b", False)]:
        result = subprocess.run(
            [str(jq16_binary), "-c", '{old:contains("\\u0000"),valid:(explode|index(0)|not)}'],
            input=json.dumps(text),
            text=True,
            capture_output=True,
            check=True,
        )
        assert json.loads(result.stdout) == {"old": True, "valid": expected}


@pytest.mark.parametrize("text", ["hello", "打开灯", '引号 " 与\\路径', "第一行\n第二行", "😀"])
def test_normal_text_executes_under_real_jq16(text_device, jq16_binary, text):
    call, path, _ = text_device
    assert Path(os.environ["XIAOAI_TEST_JQ16"]).resolve() == jq16_binary
    result, response = call("announce", text)
    assert result.returncode == 0 and response == {"reply_text": ""}
    assert (path / "tts-calls").read_text().strip() == text


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"text": ""}, "invalid_text"),
        ({"text": "a\0b"}, "invalid_request"),
        ({"text": None}, "invalid_request"),
        ({"text": 5}, "invalid_request"),
        ({"text": "hello", "speak_response": 1}, "invalid_request"),
        ({"text": "hello", "use_context": "true"}, "invalid_request"),
        ({"text": "hello", "volume": -1}, "invalid_request"),
        ({"text": "hello", "volume": 101}, "invalid_request"),
        ({"text": "hello", "duck_volume": "10"}, "invalid_request"),
        ({"text": "hello", "agent_id": "conversation.good\n"}, "invalid_request"),
        ({"text": "hello", "agent_id": "conversation.good\n../bad"}, "invalid_request"),
        ({"text": "hello", "_snapshot": []}, "invalid_request"),
        ({"text": "hello", "_snapshot": {"source": []}}, "invalid_request"),
        ({"text": "hello", "_snapshot": {"uncertain": "false"}}, "invalid_request"),
    ],
)
def test_jq16_invalid_fields_never_start_tts_and_diagnostic_has_only_code(
    text_device, jq16_binary, speaker_shell, payload, error
):
    _, path, env = text_device
    result = subprocess.run(
        [*speaker_shell, str(path / "bridge/lib/text.sh"), "voice", json.dumps(payload)],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1 and json.loads(result.stdout) == {"error": error}
    assert not (path / "tts-calls").exists() and not (path / "legacy-calls").exists()
    diagnostic = (path / "xiaoai_bridge.text-errors").read_text()
    assert diagnostic.strip().split()[1] == f"error={error}"
    assert "hello" not in diagnostic and "synthetic-token" not in diagnostic


def test_snapshot_matches_jq16_observations_and_priority_matrix(json_binary, jq16_binary):
    for ap, bt, mpd, old, priority in itertools.product(
        (True, False, None),
        (True, False, None),
        ("play", "stop", "unknown"),
        (None, "airplay", "bluetooth", "mpd"),
        (["airplay", "bluetooth", "mpd"], ["mpd", "bluetooth", "airplay"]),
    ):
        base = {"source": old, "priority": priority, "extra": {"literal": '你好 "\n'}}
        expected = subprocess.run(
            [
                str(jq16_binary),
                "-c",
                "--argjson",
                "ap",
                json.dumps(ap is True),
                "--argjson",
                "bt",
                json.dumps(bt is True),
                "--arg",
                "mpd",
                mpd,
                "--argjson",
                "ap_observed",
                json.dumps(ap),
                "--argjson",
                "bt_observed",
                json.dumps(bt),
                SNAPSHOT_FILTER,
            ],
            input=json.dumps(base),
            text=True,
            capture_output=True,
            check=True,
        )
        result = op(
            json_binary,
            "snapshot",
            json.dumps(ap is True),
            json.dumps(bt is True),
            mpd,
            json.dumps(ap),
            json.dumps(bt),
            42,
            60,
            50,
            data=base,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == json.loads(expected.stdout)


def test_duck_journal_matches_jq16_handoff_and_sender_rules(json_binary, jq16_binary, tmp_path):
    desired_file = tmp_path / "desired.json"
    duck_file = tmp_path / "duck.json"
    values = {"master": 10, "bluetooth": 70}
    for newer, raw, active, current in itertools.product(
        (False, True), ("-18", "-6"), ("true", "false", ""), (10, 40)
    ):
        desired = {
            "revision": 4,
            "versions": {"master": 4 if newer else 2},
            "volumes": {"master": 75},
        }
        duck = {
            "revision": 2,
            "volumes": {"master": 60},
            "applied_master": 10,
            "airplay_raw": "-18",
            "airplay_active": "true",
        }
        desired_file.write_text(json.dumps(desired))
        duck_file.write_text(json.dumps(duck))
        expected = subprocess.run(
            [
                str(jq16_binary),
                "-nc",
                "--argjson",
                "desired",
                json.dumps(desired),
                "--argjson",
                "duck",
                json.dumps(duck),
                "--argjson",
                "values",
                json.dumps(values),
                "--arg",
                "raw",
                raw,
                "--arg",
                "active",
                active,
                "--argjson",
                "current",
                str(current),
                JOURNAL_FILTER,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        result = op(
            json_binary,
            "journal",
            desired_file,
            duck_file,
            json.dumps(values),
            77,
            88,
            raw,
            active,
            current,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == json.loads(expected.stdout)


@pytest.mark.parametrize(
    "data",
    [
        b"{}\0{}",
        b'{"x":"a\\u0000b"}',
        b'{"x":1,"x":2}',
        b'{"x":"\xff"}',
        b"[" * 26 + b"]" * 26,
        b" " * 65537,
    ],
)
def test_native_validation_rejects_nul_duplicates_utf8_depth_and_overflow(json_binary, data):
    result = op(json_binary, "validate", data=data)
    assert result.returncode == 2 and result.stderr == b"invalid_json_operation\n"


def test_native_helpers_escape_reply_preserve_metadata_and_recovery_channels(json_binary, tmp_path):
    literal = '你好 "\\\n\t 😀 $(literal)'
    result = op(json_binary, "reply", literal, '{"source":"mpd","uncertain":false}')
    assert result.returncode == 0 and json.loads(result.stdout)["reply_text"] == literal
    old = {
        "revision": 3,
        "volumes": {"bluetooth": 35},
        "versions": {"bluetooth": 2},
        "extra": literal,
    }
    updated = op(json_binary, "desired-update", "master", "10.5", data=old)
    assert json.loads(updated.stdout) == {
        **old,
        "revision": 4,
        "volumes": {"master": 10.5, "bluetooth": 35},
        "versions": {"master": 4, "bluetooth": 2},
    }
    saved = {
        "owner": 77,
        "start": "88",
        "revision": 3,
        "volumes": {"master": 60, "tts": 55},
        "airplay_raw": "-18",
        "airplay_active": "true",
        "applied_master": 10,
        "extra": literal,
    }
    save = tmp_path / "saved.json"
    desired = tmp_path / "desired.json"
    save.write_text(json.dumps(saved))
    desired.write_text("{}")
    pending = op(json_binary, "recovery-plan", save, desired, "-6", "true", "10")
    assert pending.stdout.splitlines() == ["master\tpending", "tts\t55"]
    desired.write_text('{"versions":{"master":4},"volumes":{"master":75}}')
    explicit = op(json_binary, "recovery-plan", save, desired, "-6", "true", "10")
    assert explicit.stdout.splitlines() == ["master\t75", "tts\t55"]
    dropped = op(json_binary, "drop-channel", "tts", data=saved)
    assert json.loads(dropped.stdout) == {**saved, "volumes": {"master": 60}}


def test_native_invalid_corpus_is_bounded_and_never_writes_files(json_binary, tmp_path):
    randomizer = random.Random(16)
    marker = tmp_path / "keep"
    marker.write_text("original")
    for _ in range(64):
        data = bytes(randomizer.randrange(256) for _ in range(randomizer.randrange(1, 128)))
        assert op(json_binary, "validate", data=data).returncode == 2
    for channel, level in [
        ("invalid", "50"),
        ("master", "-1"),
        ("master", "101"),
        ("master", "NaN"),
        ("master", "0x10"),
    ]:
        assert (
            op(json_binary, "desired-update", channel, level, data={"revision": 3}).returncode == 2
        )
    assert marker.read_text() == "original" and list(tmp_path.iterdir()) == [marker]


def test_fractional_cap_compares_numbers_and_rejects_negative_cap(json_binary, tmp_path):
    saved = tmp_path / "saved.json"
    desired = tmp_path / "desired.json"
    desired.write_text("{}")
    journal = {
        "revision": 1,
        "volumes": {"master": 60, "tts": 55},
        "applied_master": 10.1,
        "airplay_raw": "-18",
        "airplay_active": "true",
    }
    saved.write_text(json.dumps(journal))
    for current in ("10.1", "10.100", "1.01e1"):
        result = op(json_binary, "recovery-plan", saved, desired, "-6", "true", current)
        assert result.returncode == 0 and result.stdout.splitlines() == [
            "master\tpending",
            "tts\t55",
        ]
    journal["applied_master"] = -1
    saved.write_text(json.dumps(journal))
    assert op(json_binary, "recovery-plan", saved, desired, "-6", "true", "10.1").returncode == 2


def test_record_trace_is_byte_exact_bounded_and_never_stores_audio(json_binary, tmp_path):
    trace = tmp_path / "phase.log"
    trace.write_text("")
    audio = b"RIFF\x00\xfffixture-private-audio" * 700
    result = subprocess.run(
        [str(json_binary), "--trace-pcm", str(trace), "42.99.1"],
        input=audio,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stdout == audio and not result.stderr
    lines = trace.read_text().splitlines()
    if sys.platform == "linux":
        assert len(lines) == 1 and "phase=recording_first_output" in lines[0]
        assert lines[0].endswith("cycle=42.99.1")
    else:
        assert lines == []
    assert b"fixture-private-audio" not in trace.read_bytes()
    trace.write_bytes(b"x" * 65536)
    link = tmp_path / "unsafe.log"
    link.symlink_to(trace)
    for log in (trace, link, tmp_path / "missing.log"):
        result = subprocess.run(
            [str(json_binary), "--trace-pcm", str(log), "42.99.2"],
            input=audio,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0 and result.stdout == audio
    assert trace.read_bytes() == b"x" * 65536 and not (tmp_path / "missing.log").exists()


def test_profile_report_correlates_voice_cycle_across_pids_and_accepts_legacy(tmp_path):
    trace = tmp_path / "phase.log"
    trace.write_text(
        "pid=42 phase=wake uptime=1.00 cycle=42.99.1\n"
        "pid=43 phase=tts_finished uptime=3.00 cycle=42.99.1\n"
        "pid=43 phase=voice_led_off_sent uptime=3.03 cycle=42.99.1\n"
        "pid=42 phase=voice_restore_done uptime=3.20 cycle=42.99.1\n"
        "pid=42 phase=wake uptime=4.00 cycle=42.99.2\n"
        "pid=50 phase=task_begin uptime=4.01\n"
        "pid=50 phase=task_finished uptime=5.01\n"
        "untrusted unrelated line\n"
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/profile_report.py"), str(trace)],
        text=True,
        capture_output=True,
        check=True,
    )
    rows = list(csv.DictReader(result.stdout.splitlines()))
    assert len(rows) == 4
    assert rows[0] == {
        "pid": "43",
        "from_phase": "wake",
        "to_phase": "tts_finished",
        "elapsed_seconds": "2.000",
        "cycle": "42.99.1",
        "from_pid": "42",
    }
    assert rows[1]["elapsed_seconds"] == "0.030"
    assert rows[3]["cycle"] == "" and rows[3]["elapsed_seconds"] == "1.000"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux native memory bound; no hardware claim")
def test_native_helpers_work_inside_36_mib_address_space(native_json_binary):
    import resource

    def constrain():
        resource.setrlimit(resource.RLIMIT_AS, (36 * 1024 * 1024, 36 * 1024 * 1024))

    result = op(
        native_json_binary,
        "desired-update",
        "master",
        45,
        data={"revision": 1},
        preexec_fn=constrain,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["volumes"]["master"] == 45


@pytest.mark.parametrize("warm_duck", [False, True])
def test_wake_through_recorder_and_restore_uses_zero_jq_even_with_duck(
    text_device, speaker_shell, warm_duck
):
    _, path, env = text_device
    if warm_duck:
        enable_airplay_duck_fixture(path)
    script(
        path / "tools/jq-forbidden",
        '#!/bin/sh\nprintf called > "$STATE_DIR/forbidden-jq"\nexit 99\n',
    )
    script(
        path / "tools/rec",
        '#!/bin/sh\nprintf recorded > "$STATE_DIR/captured"\nprintf fixture-pcm\n',
    )
    body = f'''. "{path / "bridge/lib/listener-hooks.sh"}"
. "{path / "bridge/lib/ducking.sh"}"
{"""MODE=announce;DUCK_VOLUME=10;DUCK_VERIFIED=true;bridge_duck_begin || exit 70""" if warm_duck else ":"}
cp "$STATE_DIR/tools/jq-forbidden" "$STATE_DIR/tools/jq"
bridge_voice_begin || exit 71
bridge_voice_attenuate 5 || exit 72
RECORD_COMMAND=rec
bridge_record_response
printf '%s' '{{"revision":9,"versions":{{"master":9}},"volumes":{{"master":75}}}}' > "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json"
bridge_restore_volume || exit 73
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=8
    )
    assert result.returncode == 0, result.stderr
    assert (path / "captured").read_text() == "recorded" and not (path / "forbidden-jq").exists()
    assert (path / "mixer-writes").read_text().splitlines()[-2:] == [
        "mysoftvol 75%",
        "bluetooth 60%",
    ]
    assert not (path / "xiaoai_bridge.voice-volume.json").exists()


def test_successful_json_output_followed_by_failure_cannot_commit_attenuation(
    text_device, speaker_shell
):
    _, path, env = text_device
    helper = path / "tools/failing-json"
    script(
        helper,
        f'''#!/bin/sh
if [ "$2" = applied ]; then "{env["XIAOAI_BRIDGE_JSON_BIN"]}" "$@";exit 1;fi
exec "{env["XIAOAI_BRIDGE_JSON_BIN"]}" "$@"
''',
    )
    body = f'''. "{path / "bridge/lib/listener-hooks.sh"}"
bridge_voice_begin || exit 70
cp "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" "$STATE_DIR/before-journal"
BRIDGE_JSON_BIN='{helper}'
if bridge_voice_attenuate 10; then exit 71;fi
cp "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" "$STATE_DIR/after-journal"
[ ! -f "$STATE_DIR/mixer-writes" ] || exit 72
bridge_restore_volume
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=8
    )
    assert result.returncode == 0, result.stderr
    assert (path / "before-journal").read_bytes() == (path / "after-journal").read_bytes()


@pytest.mark.parametrize("failure", ["profile", "owner"])
def test_profile_failure_is_passive_but_voice_owner_failure_stops_recording(
    text_device, speaker_shell, failure
):
    _, path, env = text_device
    (path / f"xiaoai_bridge.{'profile' if failure == 'profile' else 'voice'}").mkdir()
    script(path / "tools/rec", '#!/bin/sh\nprintf captured > "$STATE_DIR/captured"\n')
    body = f'''. "{path / "bridge/lib/listener-hooks.sh"}"
bridge_voice_begin || exit 70
bridge_voice_attenuate 5 || exit 71
RECORD_COMMAND=rec
bridge_record_response
bridge_restore_volume
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body],
        env={**env, "XIAOAI_PROFILE": "1"},
        capture_output=True,
        text=True,
        timeout=8,
    )
    if failure == "profile":
        assert result.returncode == 0 and (path / "captured").exists()
        assert not (path / "xiaoai_bridge.voice-volume.json").exists()
    else:
        assert result.returncode == 70 and not (path / "captured").exists()
        assert not (path / "mixer-writes").exists()


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(),
    reason="Linux private Listener identity and inherited locks",
)
@pytest.mark.parametrize("cancel", [False, True])
def test_led_finishes_before_guard_cleanup_but_keeps_audio_and_journal(
    text_device, speaker_shell, ha_http, cancel
):
    _, path, env = text_device
    bridge = path / "bridge"
    (bridge / "adapter").mkdir()
    for command in ("show-led", "shut-led"):
        script(
            path / "tools" / command,
            f'#!/bin/sh\nprintf "{command} %s\\n" "$1" >> "$STATE_DIR/led-flow"\n',
        )
    for name in ("listener-hooks.sh", "voice-led.sh"):
        p = bridge / "lib" / name
        p.write_text(
            p.read_text()
            .replace("/bin/show_led", str(path / "tools/show-led"))
            .replace("/bin/shut_led", str(path / "tools/shut-led"))
        )
    listener = bridge / "adapter/listener.sh"
    script(
        listener,
        f'''. "{bridge / "lib/listener-hooks.sh"}"
bridge_voice_begin || exit 70
bridge_voice_attenuate 10 || exit 71
STT_TEXT=hello
bridge_process_text || exit 72
bridge_voice_led_finish
bridge_restore_volume
''',
    )
    ha_http.hold_guard_end = True
    proc = subprocess.Popen(
        [*speaker_shell, str(listener)],
        env={**env, "XIAOAI_PROFILE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert ha_http.guard_end_entered.wait(timeout=5)
        lines = (path / "led-flow").read_text().splitlines()
        assert lines == ["show-led 1", "show-led 11", "shut-led 1", "shut-led 2", "shut-led 3"]
        assert proc.poll() is None and (path / "xiaoai_bridge.voice").exists()
        assert (path / "xiaoai_bridge.voice-volume.json").exists()
        for lock in ("audio", "led"):
            with (path / f"xiaoai_bridge.{lock}.lock").open("w") as gate:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if cancel:
            os.killpg(proc.pid, signal.SIGTERM)
        ha_http.guard_end_release.set()
        stdout, stderr = proc.communicate(timeout=8)
        assert proc.returncode == (143 if cancel else 0), stdout + stderr
    finally:
        ha_http.guard_end_release.set()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
    assert not (path / "xiaoai_bridge.voice").exists()
    assert not (path / "xiaoai_bridge.voice-volume.json").exists()
    assert (path / "mixer-writes").read_text().splitlines()[-2:] == [
        "mysoftvol 60%",
        "bluetooth 60%",
    ]
    assert (path / "led-flow").read_text().splitlines().count("show-led 11") == 1
    trace = (path / "xiaoai_bridge.profile").read_text()
    assert trace.index("phase=voice_led_off_sent") < trace.index("phase=guard_end_begin")
    assert "hello" not in trace and "synthetic-token" not in trace
