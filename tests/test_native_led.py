"""Native task animations, exact registry migration and real worker cleanup."""

import copy
import fcntl
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaoai_speaker.const import DOMAIN, PLATFORMS
from custom_components.xiaoai_speaker.diagnostics import async_get_config_entry_diagnostics
from speaker.tools.prepare_adapters import prepare_airplay_led
from tests.test_bridge import native_bridge as native_bridge
from tests.test_homeassistant import DATA, INFO
from tests.test_homeassistant import mock_clients as mock_clients
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import script
from tests.test_text_pipeline import text_device as text_device

ROOT = Path(__file__).resolve().parents[1]
SEQUENCE = ["show 1", "show 2", "show 11", "shut 1", "shut 2", "shut 3"]


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    pytest.fail("isolated native LED fixture did not reach expected state")


@pytest.fixture
def native_led(text_device):
    call, path, env = text_device
    for verb in ("show", "shut"):
        script(
            path / "tools" / f"{verb}-led",
            f"""#!{sys.executable}
import os,sys
from pathlib import Path
state=Path(os.environ["STATE_DIR"])
for fd in (4,6,7,8,9):
    try: os.fstat(fd)
    except OSError: continue
    (state/"native-inherited-lock").touch()
with (state/"native-led-calls").open("a") as stream:
    stream.write({verb!r}+" "+sys.argv[1]+"\\n")
sys.exit(1 if (state/"fail-native").exists() else 0)
""",
        )
    for name in ("task-feedback.sh", "voice-led.sh", "listener-hooks.sh"):
        file = path / "bridge/lib" / name
        file.write_text(
            file.read_text()
            .replace("/bin/show_led", str(path / "tools/show-led"))
            .replace("/bin/shut_led", str(path / "tools/shut-led"))
        )
    return call, path, env


@pytest.mark.parametrize(
    "mode,speak", [("announce", True), ("send_text", True), ("send_text", False)]
)
@pytest.mark.parametrize("legacy_flag", [None, False, True])
def test_native_task_sequence_is_default_and_ignores_old_flags(
    native_led, ha_http, mode, speak, legacy_flag
):
    call, path, _ = native_led
    config = path / "config.json"
    values = json.loads(config.read_text())
    if legacy_flag is not None:
        values["verified"] = {"led": legacy_flag, "task_led": legacy_flag}
    config.write_text(json.dumps(values))
    original = config.read_bytes()
    result, _ = call(mode, "synthetic speech", speak_response=speak)
    assert result.returncode == 0
    assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
    assert not (path / "xiaoai_bridge.feedback").exists()
    assert not (path / "native-inherited-lock").exists()
    assert config.read_bytes() == original
    assert not any("/api/stt/" in request["path"] for request in ha_http.requests)
    assert (path / "tts-calls").exists() is speak


def test_native_empty_reply_finishes_without_tts(native_led):
    call, path, _ = native_led
    result, payload = call("send_text", "EMPTY_REPLY")
    assert result.returncode == 0 and payload["reply_text"] == ""
    assert not (path / "tts-calls").exists()
    assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
    assert not (path / "xiaoai_bridge.feedback").exists()


@pytest.mark.parametrize("failure", ["tts", "conversation"])
def test_failed_task_finishes_native_animation(native_led, failure):
    call, path, _ = native_led
    if failure == "tts":
        script(path / "existing-tts", "#!/bin/sh\nexit 1\n")
    result, payload = call("announce" if failure == "tts" else "send_text", "FAIL", volume=20)
    assert result.returncode != 0
    assert payload["error"] == ("tts_failed" if failure == "tts" else "conversation_failed")
    assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
    assert not (path / "xiaoai_bridge.feedback").exists()
    assert not (path / "xiaoai_bridge.tts-volume.json").exists()


@pytest.mark.parametrize("missing", ["show", "shut", "both", "failed"])
def test_missing_or_failed_native_commands_never_fail_task(native_led, speaker_shell, missing):
    call, path, env = native_led
    if missing == "failed":
        (path / "fail-native").touch()
    else:
        for verb in ("show", "shut"):
            if missing in (verb, "both"):
                (path / "tools" / f"{verb}-led").unlink()
    result, payload = call("announce", "synthetic speech")
    assert result.returncode == 0 and payload == {"reply_text": ""}
    assert (path / "tts-calls").read_text().strip() == "synthetic speech"
    if missing != "failed":
        assert not (path / "native-led-calls").exists()
        assert not (path / "xiaoai_bridge.feedback").exists()
    else:
        assert (path / "xiaoai_bridge.feedback").exists()
        (path / "fail-native").unlink()
        subprocess.run(
            [*speaker_shell, str(path / "bridge/lib/task-feedback.sh"), "recover"],
            env=env,
            check=True,
            timeout=5,
        )
        assert not (path / "xiaoai_bridge.feedback").exists()
        assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE + SEQUENCE[2:]


def test_old_owner_cannot_end_or_reclaim_new_animation(native_led, speaker_shell):
    _, path, env = native_led
    body = f'''. "{path / "bridge/lib/common.sh"}"
. "{path / "bridge/lib/task-feedback.sh"}"
MODE=announce
bridge_feedback_setup
bridge_feedback_stage processing
printf '%s different-start\\n' "$$" > "$BRIDGE_RUNTIME/xiaoai_bridge.feedback"
bridge_feedback_stage playing
bridge_feedback_end
bridge_feedback_recover after-voice "$$" "$BRIDGE_FEEDBACK_START"
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE[:2]
    assert (path / "xiaoai_bridge.feedback").read_text().endswith("different-start\n")


def test_killed_owner_is_recovered_without_a_resident_worker(native_led, speaker_shell):
    _, path, env = native_led
    body = f'''. "{path / "bridge/lib/common.sh"}"
. "{path / "bridge/lib/task-feedback.sh"}"
MODE=announce
bridge_feedback_setup
bridge_feedback_stage processing
printf 'ready\\n'
read hold
'''
    proc = subprocess.Popen(
        [*speaker_shell, "-c", body],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert proc.stdout.readline() == "ready\n"
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=3)
        subprocess.run(
            [*speaker_shell, str(path / "bridge/lib/task-feedback.sh"), "recover"],
            env=env,
            check=True,
            timeout=5,
        )
        assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
        assert not (path / "xiaoai_bridge.feedback").exists()
        subprocess.run(
            [*speaker_shell, str(path / "bridge/lib/task-feedback.sh"), "recover"],
            env=env,
            check=True,
            timeout=5,
        )
        assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
        proc.stdin.close()
        proc.stdout.close()


def test_original_airplay_guard_yields_and_replays_after_native_task(native_led, speaker_shell):
    _, path, env = native_led
    script(
        path / "tools/dbus-send",
        "#!/bin/sh\nprintf 'string \"Active\"\\nvariant boolean true\\n'\n",
    )
    adapter = path / "bridge/adapter/airplay-led.sh"
    adapter.parent.mkdir()
    led = path / "airplay-rgb"
    led.write_text("sentinel")
    script(path / "tools/listener-status", "#!/bin/sh\nexit 0\n")
    original = (ROOT / "tests/fixtures/lx06_airplay_led.sh").read_text()
    source = prepare_airplay_led(original)
    source = (
        source.replace("/data/xiaoai_bridge", str(path / "bridge"))
        .replace("/tmp/airplay_led", str(path / "airplay_led"))
        .replace("/sys/devices/i2c-0/0-003a/led_rgb", str(led))
        .replace("/etc/init.d/listener", str(path / "tools/listener-status"))
    )
    script(adapter, source)
    body = f'''. "{path / "bridge/lib/common.sh"}"
. "{path / "bridge/lib/task-feedback.sh"}"
MODE=announce
bridge_feedback_setup
bridge_feedback_stage processing
printf '100 0\\n' > "$STATE_DIR/airplay_led.state"
"{adapter}" --worker
printf '%s\\n' 0 > "$STATE_DIR/airplay_led.last"
# Both draw and original restore must yield to this task.
[ "$(cat '{led}')" = sentinel ] || exit 77
bridge_feedback_end
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    # The replay uses the original software volume mapping and bounded worker.
    wait_for(lambda: led.read_text().strip() == "9 0x903000")
    wait_for(lambda: not (path / "airplay_led.lock").exists())
    assert led.read_text().strip() == "9 0x0"
    assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE


def test_led_lock_failure_keeps_owner_for_later_recovery(native_led, speaker_shell):
    _, path, env = native_led
    body = f'''. "{path / "bridge/lib/common.sh"}"
. "{path / "bridge/lib/task-feedback.sh"}"
MODE=announce
bridge_feedback_setup
bridge_feedback_stage processing
printf 'ready\\n'
read finish
bridge_feedback_end
'''
    proc = subprocess.Popen(
        [*speaker_shell, "-c", body],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline() == "ready\n"
        with (path / "xiaoai_bridge.led.lock").open("w") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX)
            proc.communicate("finish\n", timeout=5)
            assert proc.returncode == 0
            assert (path / "xiaoai_bridge.feedback").exists()
            assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE[:2]
        subprocess.run(
            [*speaker_shell, str(path / "bridge/lib/task-feedback.sh"), "recover"],
            env=env,
            check=True,
            timeout=5,
        )
        assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
        assert not (path / "xiaoai_bridge.feedback").exists()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.communicate(timeout=5)


def test_voice_reservation_before_led_lock_keeps_deferred_cleanup(native_led, speaker_shell):
    _, path, env = native_led
    body = f'''. "{path / "bridge/lib/common.sh"}"
. "{path / "bridge/lib/task-feedback.sh"}"
MODE=announce
bridge_feedback_setup
bridge_feedback_stage processing
# A live voice owner has reserved priority but has not acquired the LED lock.
printf '%s %s\\n' '{os.getpid()}' "$(bridge_pid_start '{os.getpid()}' 2>/dev/null || true)" > "$BRIDGE_RUNTIME/xiaoai_bridge.voice"
bridge_feedback_end
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE[:2]
    assert (path / "xiaoai_bridge.feedback").exists()
    (path / "xiaoai_bridge.voice").unlink()
    subprocess.run(
        [*speaker_shell, str(path / "bridge/lib/task-feedback.sh"), "recover"],
        env=env,
        check=True,
        timeout=5,
    )
    assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
    assert not (path / "xiaoai_bridge.feedback").exists()


def test_probe_omits_manual_led_even_with_old_flags_and_corrupt_state(native_led, speaker_shell):
    _, path, env = native_led
    config = path / "config.json"
    values = json.loads(config.read_text())
    values["verified"] = {"led": True, "task_led": True}
    config.write_text(json.dumps(values))
    (path / "xiaoai_bridge.led.json").write_text("not valid JSON")
    (path / "uptime").write_text("100.00 50.00\n")
    (path / "meminfo").write_text("MemAvailable: 50000 kB\nMemTotal: 100000 kB\n")
    (path / "loadavg").write_text("0.2 0.1 0.0 1/10 100\n")
    script(path / "tools/listener-status", "#!/bin/sh\nexit 1\n")
    device = path / "bridge/lib/device.sh"
    source = device.read_text()
    for original, target in {
        "/proc/uptime": path / "uptime",
        "/proc/meminfo": path / "meminfo",
        "/proc/loadavg": path / "loadavg",
        "/etc/init.d/listener": path / "tools/listener-status",
        "/data/eoai_agent.sh": path / "existing-eoai-agent.sh",
    }.items():
        source = source.replace(original, str(target))
    device.write_text(source)
    result = subprocess.run(
        [*speaker_shell, str(device), "probe"],
        input="{}",
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert "led" not in response["capabilities"] and "led" not in response["state"]
    result = subprocess.run(
        [*speaker_shell, str(device), "led"],
        input='{"on":false}',
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0 and json.loads(result.stdout) == {"error": "unsupported_action"}
    assert not (path / "native-led-calls").exists()


@pytest.mark.parametrize("task_alive", [False, True])
@pytest.mark.parametrize("led_failure", [False, True])
def test_voice_preemption_defers_airplay_replay_until_voice_ends(
    native_led, speaker_shell, task_alive, led_failure
):
    _, path, env = native_led
    script(
        path / "tools/dbus-send",
        "#!/bin/sh\nprintf 'string \"Active\"\\nvariant boolean true\\n'\n",
    )
    adapter = path / "bridge/adapter/airplay-led.sh"
    adapter.parent.mkdir()
    script(
        adapter,
        '#!/bin/sh\n[ ! -r "$XIAOAI_BRIDGE_RUNTIME/xiaoai_bridge.voice" ] || exit 77\nprintf "%s\\n" "$1" >> "$STATE_DIR/airplay-replayed"\n',
    )
    (path / "airplay_led.last").write_text("-18 40\n")
    task_body = f'''. "{path / "bridge/lib/common.sh"}"
. "{path / "bridge/lib/task-feedback.sh"}"
MODE=announce
bridge_feedback_setup
bridge_feedback_stage processing
printf 'ready\\n'
read finish
bridge_feedback_end
'''
    identity = "" if Path("/proc/self/stat").exists() else "bridge_listener_pid() { return 0; }\n"
    voice_body = f'''. "{path / "bridge/lib/listener-hooks.sh"}"
{identity}
bridge_voice_begin || exit 71
printf 'ready\\n'
read finish
bridge_voice_led_finish || true
bridge_restore_volume
'''
    voice_path = path / "bridge/adapter/listener.sh"
    script(voice_path, "#!/bin/sh\n" + voice_body)
    task = subprocess.Popen(
        [*speaker_shell, "-c", task_body],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    voice = None
    try:
        assert task.stdout.readline() == "ready\n"
        voice = subprocess.Popen(
            [*speaker_shell, str(voice_path)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert voice.stdout.readline() == "ready\n"
        if not task_alive:
            _, error = task.communicate("finish\n", timeout=5)
            assert task.returncode == 0, error
        assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE[:2] + ["show 1"]
        assert not (path / "airplay-replayed").exists()
        if led_failure:
            (path / "fail-native").touch()
        _, error = voice.communicate("finish\n", timeout=5)
        assert voice.returncode == 0, error
        if led_failure:
            assert (path / "xiaoai_bridge.feedback").exists()
            assert not (path / "airplay-replayed").exists()
            (path / "fail-native").unlink()
            if task_alive:
                _, error = task.communicate("finish\n", timeout=5)
                assert task.returncode == 0, error
            else:
                subprocess.run(
                    [*speaker_shell, str(path / "bridge/lib/task-feedback.sh"), "recover"],
                    env=env,
                    check=True,
                    timeout=5,
                )
            assert (path / "native-led-calls").read_text().splitlines()[-4:] == SEQUENCE[2:]
            assert (path / "airplay-replayed").read_text().strip() == "-18"
            assert not (path / "xiaoai_bridge.feedback").exists()
            return
        expected = SEQUENCE[:2] + ["show 1"] + SEQUENCE[2:]
        if task_alive:
            # An uncancelled Conversation can wait for voice audio, then resume.
            expected += ["show 2"]
            assert (path / "native-led-calls").read_text().splitlines() == expected
            assert not (path / "airplay-replayed").exists()
            _, error = task.communicate("finish\n", timeout=5)
            assert task.returncode == 0, error
            expected += SEQUENCE[2:]
        assert (path / "native-led-calls").read_text().splitlines() == expected
        assert (path / "airplay-replayed").read_text().strip() == "-18"
        assert not (path / "xiaoai_bridge.feedback").exists()
        assert not (path / "xiaoai_bridge.voice").exists()
    finally:
        for proc in (task, voice):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=6)


@pytest.mark.parametrize("activity", ["true", "false", "unavailable"])
def test_replay_requires_current_airplay_activity_not_just_saved_volume(native_led, activity):
    call, path, _ = native_led
    adapter = path / "bridge/adapter/airplay-led.sh"
    adapter.parent.mkdir()
    script(adapter, '#!/bin/sh\nprintf "%s\\n" "$1" >> "$STATE_DIR/airplay-replayed"\n')
    (path / "airplay_led.last").write_text("-18 40\n")
    script(
        path / "tools/dbus-send",
        "#!/bin/sh\n"
        + (
            "exit 1\n"
            if activity == "unavailable"
            else f"printf 'string \"Active\"\\nvariant boolean {activity}\\n'\n"
        ),
    )
    result, _ = call("announce", "synthetic speech")
    assert result.returncode == 0
    assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
    assert (path / "airplay-replayed").exists() is (activity == "true")
    assert not (path / "xiaoai_bridge.feedback").exists()


@pytest.fixture
def task_bridge(native_led, native_json_binary, socket_enabled, speaker_shell, request):
    _, path, env = native_led
    # Unix event sockets need a short path on macOS too; all LED/mixer logs stay
    # in STATE_DIR while the real C server owns this isolated runtime/config.
    with tempfile.TemporaryDirectory(prefix="native-led-", dir="/tmp") as directory:
        runtime = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        adapter = runtime / "adapter.py"
        script(
            adapter,
            f"""#!{sys.executable}
import json,os,sys
payload=json.load(sys.stdin)
if sys.argv[1]=="probe":
    print(json.dumps({{"capabilities":{{"announce":True,"conversation":True}},"state":{{}}}}))
else:
    command={speaker_shell!r}+[str({str(path / "bridge/lib/text.sh")!r}),sys.argv[1],json.dumps(payload)]
    os.execvp(command[0],command)
""",
        )
        token = "synthetic-native-led-token-for-test-only-00000000"
        values = json.loads((path / "config.json").read_text())
        values.update(
            token=token,
            device_id="synthetic-speaker",
            bind="127.0.0.1",
            port=port,
            runtime_dir=str(runtime),
            adapter=str(adapter),
            task_timeout=request.param,
        )
        config = runtime / "config.json"
        config.write_text(json.dumps(values))
        config.chmod(0o600)
        proc = subprocess.Popen(
            [str(native_json_binary), "--config", str(config)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        def call(endpoint, data=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request(
                "GET" if data is None else "POST",
                endpoint,
                body=None if data is None else json.dumps(data),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result

        def ready():
            try:
                return call("/api/health")[0] == 200
            except OSError:
                return False

        try:
            wait_for(ready)
            yield call, runtime
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=25)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            assert token.encode() not in proc.stderr.read()
            proc.stderr.close()


@pytest.mark.parametrize(
    "task_bridge,reason", [(15, "cancel"), (8, "timeout")], indirect=["task_bridge"]
)
def test_real_c_cancellation_and_timeout_clean_native_led_and_volume(
    task_bridge, native_led, reason
):
    call, runtime = task_bridge
    _, path, _ = native_led
    # Leave time for the real shell/jq/mixer setup before timing out the long
    # TTS. A two-second limit can expire before playback on a loaded test host.
    script(path / "existing-tts", '#!/bin/sh\n: > "$STATE_DIR/tts-active"\nsleep 30\n')
    assert (
        call(
            "/api/tts/announce", {"request_id": "native-cleanup", "text": "synthetic", "volume": 20}
        )[0]
        == 202
    )
    wait_for(lambda: (path / "tts-active").exists())
    if reason == "cancel":
        assert call("/api/jobs/native-cleanup/cancel", {})[0] == 200
    # task_timeout starts termination; it is not the cleanup deadline. The fast
    # Linux fixture reaches TTS almost immediately, so waiting exactly eight
    # seconds can expire before the next event-loop tick and TERM cleanup.
    wait_for(lambda: not (runtime / "xiaoai_bridge.feedback").exists(), timeout=15)
    wait_for(lambda: call("/api/jobs/native-cleanup")[1]["status"] in ("cancelled", "failed"))
    job = call("/api/jobs/native-cleanup")[1]
    assert job["status"] == ("cancelled" if reason == "cancel" else "failed")
    if reason == "timeout":
        assert job["error"] == "task_timeout"
    assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
    assert (path / "mixer-writes").read_text().splitlines() == ["notifyvol 20%", "notifyvol 60%"]
    assert not (runtime / "xiaoai_bridge.tts-volume.json").exists()
    # cancelled is acknowledged before worker cleanup/reaping completes.
    wait_for(lambda: not call("/api/state")[1]["audio_active"])
    for name in ("audio", "led", "volume"):
        with (runtime / f"xiaoai_bridge.{name}.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_retired_led_http_endpoint_is_not_dispatched(native_bridge):
    call, *_ = native_bridge
    assert call("/api/led")[0] == 404
    assert call(
        "/api/led", {"request_id": "retired-led", "on": True, "rgb": [1, 2, 3], "brightness": 255}
    ) == (404, {"error": "not_found"})
    assert call("/api/jobs/retired-led")[0] == 404


def test_retired_rgb_helper_is_unavailable(native_json_binary, tmp_path):
    state = tmp_path / "old-led.json"
    state.write_text('{"on":true,"rgb":[1,2,3],"brightness":255}')
    result = subprocess.run(
        [str(native_json_binary), "--json-op", "led-color", str(state)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2 and result.stdout == ""


@pytest.mark.parametrize("disabled", [None, er.RegistryEntryDisabler.USER])
async def test_upgrade_removes_only_own_manual_light_and_never_registers_it(
    hass, mock_clients, disabled
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Synthetic speaker",
        data=DATA,
        unique_id="synthetic-speaker",
        version=1,
        options={"agent_id": "conversation.existing"},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    obsolete = registry.async_get_or_create(
        "light",
        DOMAIN,
        "synthetic-speaker_led",
        config_entry=entry,
        suggested_object_id="custom_led",
        disabled_by=disabled,
    )
    registry.async_update_entity(obsolete.entity_id, name="Custom old light")
    other_entry = MockConfigEntry(
        domain=DOMAIN, title="Other", data=DATA, unique_id="other-speaker"
    )
    other_entry.add_to_hass(hass)
    keep = [
        registry.async_get_or_create("light", DOMAIN, "other_led", config_entry=entry),
        registry.async_get_or_create(
            "light", "other_integration", "synthetic-speaker_led", config_entry=entry
        ),
        registry.async_get_or_create(
            "light", DOMAIN, "other-speaker_led", config_entry=other_entry
        ),
        registry.async_get_or_create("number", DOMAIN, "synthetic-speaker_led", config_entry=entry),
        registry.async_get_or_create(
            "number",
            DOMAIN,
            "synthetic-speaker_volume_master",
            config_entry=entry,
            suggested_object_id="custom_volume",
            disabled_by=None,
        ),
        registry.async_get_or_create(
            "text",
            DOMAIN,
            "synthetic-speaker_announce_text",
            config_entry=entry,
            suggested_object_id="custom_text",
            disabled_by=None,
        ),
    ]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.unique_id)}, name="My speaker"
    )
    registry.async_update_entity(obsolete.entity_id, device_id=device.id)
    info = copy.deepcopy(INFO)
    info["version"] = "0.1.6"
    info["capabilities"]["led"] = True
    with patch(
        "custom_components.xiaoai_speaker.client.BridgeClient.info", AsyncMock(return_value=info)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(obsolete.entity_id) is None
    assert all(registry.async_get(e.entity_id) is not None for e in keep)
    assert dr.async_get(hass).async_get(device.id) is not None
    assert entry.data == DATA and entry.unique_id == "synthetic-speaker"
    assert entry.options == {"agent_id": "conversation.existing"}
    assert Platform.LIGHT not in PLATFORMS
    assert "xiaoai_speaker.light" not in hass.config.components
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert "led" not in diagnostics["capabilities"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(obsolete.entity_id) is None
    assert await hass.config_entries.async_unload(entry.entry_id)
