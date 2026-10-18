"""Keep cached mixer observations consistent across real Journal restoration."""

import fcntl
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_capabilities import setup
from tests.test_homeassistant import STATE
from tests.test_homeassistant import mock_clients as mock_clients
from tests.test_text_pipeline import enable_airplay_duck_fixture, script
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import text_device as text_device


@pytest.fixture
def volume_probe(text_device, speaker_shell):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    mixer = path / "tools/amixer"
    mixer.write_text(
        mixer.read_text().replace(
            'if [ "$1" = get ]; then',
            """if [ "$1" = scontrols ]; then
 printf "Simple mixer control '%s',0\\n" mysoftvol notifyvol bluetooth 'headphone volume'
 exit 0
fi
if [ "$1" = get ]; then
 printf '%s\\n' "$2" >> "$STATE_DIR/mixer-reads"
""",
        )
    )
    script(path / "tools/bluetoothctl", "#!/bin/sh\nprintf 'Status: stopped\\n'\n")
    script(path / "tools/listener-status", "#!/bin/sh\nexit 1\n")
    device = path / "bridge/lib/device.sh"
    source = device.read_text()
    for original, target, contents in (
        ("/proc/meminfo", path / "meminfo", "MemAvailable: 50000 kB\n"),
        ("/proc/loadavg", path / "loadavg", "0.2 0.1 0.0 1/10 100\n"),
    ):
        target.write_text(contents)
        source = source.replace(original, str(target))
    source = source.replace("/etc/init.d/listener", str(path / "tools/listener-status"))
    device.write_text(source)
    dbus = path / "tools/dbus-send"
    dbus.rename(path / "tools/dbus-original")
    script(
        dbus,
        """#!/bin/sh
if [ "${PROBE_BARRIER:-0}" = 1 ] && [ ! -f "$STATE_DIR/probe-observed" ]; then
 touch "$STATE_DIR/probe-observed"
 while [ ! -f "$STATE_DIR/probe-release" ]; do sleep 0.01;done
fi
exec "$STATE_DIR/tools/dbus-original" "$@"
""",
    )

    def run(**extra):
        result = subprocess.run(
            [*speaker_shell, str(device), "probe"],
            input="{}",
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return run, path, env


def wait_for(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, f"synthetic probe barrier missing: {path.name}"
        time.sleep(0.01)


def restore_during_probe(volume_probe, speaker_shell, kind, channel, mixer):
    run, path, env = volume_probe
    worker = subprocess.Popen(
        [
            *speaker_shell,
            "-c",
            f'''. "$XIAOAI_BRIDGE_DIR/lib/common.sh"
exec 7> "$BRIDGE_RUNTIME/xiaoai_bridge.volume.lock"
flock -n 7 || exit 70
if [ "{kind}" = voice-volume ]; then bridge_mark_owner "$BRIDGE_RUNTIME/xiaoai_bridge.voice";fi
bridge_volume_journal "$BRIDGE_RUNTIME/xiaoai_bridge.{kind}.json" '{{"{channel}":60}}' || exit 71
amixer sset '{mixer}' 10% || exit 72
flock -u 7
touch "$STATE_DIR/temporary-ready"
while [ ! -f "$STATE_DIR/restore-now" ]; do sleep 0.01;done
# The probe must release the lock before entering its slow DBus phase.
flock -n 7 || exit 73
bridge_volume_recover_locked "$BRIDGE_RUNTIME/xiaoai_bridge.{kind}.json" || exit 74
flock -u 7
if [ "{kind}" = voice-volume ]; then rm "$BRIDGE_RUNTIME/xiaoai_bridge.voice";fi
''',
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        wait_for(path / "temporary-ready")
        probe = executor.submit(run, PROBE_BARRIER="1")
        wait_for(path / "probe-observed")
        (path / "restore-now").touch()
        stdout, stderr = worker.communicate(timeout=5)
        assert worker.returncode == 0, stdout + stderr
        assert (path / f"mixer-{mixer}").read_text().strip() == "60"
        assert not (path / f"xiaoai_bridge.{kind}.json").exists()
    finally:
        (path / "restore-now").touch()
        (path / "probe-release").touch()
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=5)
        executor.shutdown(wait=True)
    return probe.result()["state"]


@pytest.mark.parametrize(
    "kind,channel,mixer",
    [
        ("duck", "master", "mysoftvol"),
        ("tts-volume", "tts", "notifyvol"),
        ("voice-volume", "master", "mysoftvol"),
    ],
)
async def test_restoration_during_slow_probe_cannot_persist_temporary_volume(
    hass, mock_clients, volume_probe, speaker_shell, kind, channel, mixer
):
    run, path, _ = volume_probe
    observed = await hass.async_add_executor_job(
        restore_during_probe, volume_probe, speaker_shell, kind, channel, mixer
    )
    assert observed["volumes"][channel] == 10
    # C combines the completed cached probe with current (now idle) workers.
    state = {**STATE, **observed, "instance_id": "same-instance", "audio_active": False}
    entry, c = await setup(hass)
    c._synced_instance = "same-instance"
    c.volume.desired = {"master": 60, "tts": 60, "bluetooth": 60}
    try:
        with (
            patch.object(c.client, "state", AsyncMock(return_value=state)),
            patch.object(c.volume, "commit", wraps=c.volume.commit) as commit,
        ):
            await c.async_refresh()
            commit.assert_not_called()
            assert c.volume.desired[channel] == 60
            assert state["volume_recovery_pending"] is True
            # A later stable user setting must still become the desired value.
            (path / f"mixer-{mixer}").write_text("45")
            state.update((await hass.async_add_executor_job(run))["state"])
            await c.async_refresh()
            commit.assert_awaited_once_with(
                [{"target": channel, "level": 45}], generation=c.volume.generation
            )
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


def test_busy_volume_lock_skips_mixer_observation_without_waiting(volume_probe):
    run, path, _ = volume_probe
    with (path / "xiaoai_bridge.volume.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        response = run()
    assert response["state"]["volume_recovery_pending"] is True
    assert all(
        response["state"]["volumes"][key] is None
        for key in ("master", "tts", "bluetooth", "headphone")
    )
    assert not (path / "mixer-reads").exists()
    assert response["capabilities"]["volume"]["master"] is True
    assert run()["state"]["volume_recovery_pending"] is False


def test_mixer_observation_excludes_concurrent_volume_writes(volume_probe):
    run, path, _ = volume_probe
    mixer = path / "tools/amixer"
    mixer.write_text(
        mixer.read_text().replace(
            'if [ "$1" = get ]; then',
            """if [ "$1" = get ] && [ ! -f "$STATE_DIR/mixer-observed" ]; then
 touch "$STATE_DIR/mixer-observed"
 while [ ! -f "$STATE_DIR/mixer-release" ]; do sleep 0.01;done
fi
if [ "$1" = get ]; then""",
        )
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        probe = executor.submit(run)
        try:
            wait_for(path / "mixer-observed")
            with (path / "xiaoai_bridge.volume.lock").open("w") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            (path / "mixer-release").touch()
        assert probe.result()["state"]["volume_recovery_pending"] is False


@pytest.mark.parametrize("kind", ["duck", "tts-volume", "voice-volume"])
def test_invalid_journal_keeps_volume_observation_untrusted(volume_probe, kind):
    run, path, _ = volume_probe
    (path / f"xiaoai_bridge.{kind}.json").write_text("not JSON")
    response = run()
    assert response["state"]["volume_recovery_pending"] is True
    assert (path / f"xiaoai_bridge.{kind}.json").read_text() == "not JSON"
    assert not (path / "mixer-writes").exists()
