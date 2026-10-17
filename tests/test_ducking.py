"""Automatic AirPlay ducking with real journals/locks and synthetic hardware."""

import fcntl
import json
import os
import signal
import subprocess
import time

import pytest

from tests.test_text_pipeline import enable_airplay_duck_fixture, script
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import text_device as text_device


@pytest.mark.parametrize("mode", ["announce", "send_text"])
@pytest.mark.parametrize("level", [0, 5, 10, 11, 60, 100])
def test_automatic_duck_is_an_absolute_ceiling_without_raising_quiet_audio(
    text_device, mode, level
):
    call, path, _ = text_device
    enable_airplay_duck_fixture(path)
    (path / "mixer-mysoftvol").write_text(str(level))
    result, _ = call(mode, "test")
    assert result.returncode == 0, result.stderr
    writes = path / "mixer-writes"
    assert (writes.read_text().splitlines() if writes.exists() else []) == (
        ["mysoftvol 10%", f"mysoftvol {level}%"] if level > 10 else []
    )
    assert (path / "mixer-mysoftvol").read_text().strip() == str(level)
    assert not (path / "xiaoai_bridge.duck.json").exists()


@pytest.mark.parametrize(
    "airplay,bluetooth,mpd",
    [
        ("false", "idle", "stop"),
        ("true", "active", "stop"),
        ("true", "idle", "play"),
        ("true", "active", "play"),
        ("unknown", "idle", "stop"),
        ("true", "unknown", "stop"),
        ("true", "idle", "unknown"),
    ],
)
def test_inactive_conflicting_or_unobserved_sources_skip_duck_but_keep_tts(
    text_device, speaker_shell, airplay, bluetooth, mpd
):
    call, path, env = text_device
    enable_airplay_duck_fixture(path)
    env.update(AP_STATE=airplay, BT_STATE=bluetooth, MPD_STATE=mpd)
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
case "$*" in *org.bluez*)
 [ "$BT_STATE" != unknown ] || exit 1
 printf 'string "State"\\nvariant string "%s"\\n' "$BT_STATE";;
 *)
 [ "$AP_STATE" != unknown ] || exit 1
 printf 'string "Active"\\nvariant boolean %s\\n' "$AP_STATE";;
esac
""",
    )
    script(
        path / "tools/fake-bridge",
        """#!/bin/sh
case "$1" in
 --snapshot) printf '{"source":"airplay","uncertain":false}\\n';;
 --mpd-state) printf '%s' "$MPD_STATE";;
 --emit) exit 0;;
esac
""",
    )
    if airplay == "true" and (bluetooth == "unknown" or mpd == "unknown"):
        snapshot = subprocess.run(
            [*speaker_shell, str(path / "bridge/lib/source-snapshot.sh")],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        observed = json.loads(snapshot.stdout)
        assert observed["active_sources"] == ["airplay"] and not observed["uncertain"]
        assert None in observed["observed_sources"].values()
    result, _ = call("announce", "test")
    assert result.returncode == 0, result.stderr
    assert (path / "tts-calls").read_text().strip() == "test"
    assert not (path / "mixer-writes").exists()


@pytest.mark.parametrize(
    "text,speak,reply", [("test", False, "reply: test"), ("EMPTY_REPLY", True, "")]
)
def test_remote_without_spoken_reply_does_not_attenuate_or_play_tts(
    text_device, text, speak, reply
):
    call, path, _ = text_device
    enable_airplay_duck_fixture(path)
    result, payload = call("send_text", text, speak_response=speak)
    assert result.returncode == 0 and payload["reply_text"] == reply
    assert not (path / "mixer-writes").exists() and not (path / "tts-calls").exists()


@pytest.mark.parametrize(
    "journal_reply", ["failed", "stopped", "missing", "NaN", "Infinity", "oops"]
)
def test_later_airplay_journal_requires_current_sender_baseline(text_device, journal_reply):
    call, path, env = text_device
    enable_airplay_duck_fixture(path)
    env["JOURNAL_REPLY"] = journal_reply
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
case "$*" in *org.bluez*) printf 'string "State"\\nvariant string "idle"\\n';exit 0;; esac
n=0;[ ! -r "$STATE_DIR/ap-count" ] || read n < "$STATE_DIR/ap-count"
n=$((n+1));printf '%s\\n' "$n" > "$STATE_DIR/ap-count"
active=true;raw=-18
if [ "$n" -gt 1 ];then
 case "$JOURNAL_REPLY" in failed) exit 1;; stopped) active=false;; *) raw=$JOURNAL_REPLY;; esac
fi
printf 'string "Active"\\nvariant boolean %s\\n' "$active"
[ "$raw" = missing ] || printf 'string "Volume"\\nvariant double %s\\n' "$raw"
""",
    )
    result, _ = call("announce", "test")
    assert result.returncode == 0, result.stderr
    assert (path / "tts-calls").read_text().strip() == "test"
    assert (path / "ap-count").read_text().strip() == "2"
    assert not (path / "mixer-writes").exists()
    assert not (path / "xiaoai_bridge.duck.json").exists()


@pytest.mark.parametrize("legacy_flag", [False, True])
def test_legacy_device_verification_flag_does_not_control_automatic_duck(text_device, legacy_flag):
    call, path, _ = text_device
    enable_airplay_duck_fixture(path)
    config = path / "config.json"
    config.write_text(
        json.dumps(
            {**json.loads(config.read_text()), "verified": {"announcement_ducking": legacy_flag}}
        )
    )
    result, _ = call("announce", "test")
    assert result.returncode == 0, result.stderr
    assert (path / "mixer-writes").read_text().splitlines() == ["mysoftvol 10%", "mysoftvol 60%"]


@pytest.mark.parametrize("failure", ["read", "journal", "owner", "attenuate"])
def test_duck_preparation_failure_never_starts_playback(text_device, speaker_shell, failure):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    mixer = path / "tools/amixer"
    if failure == "read":
        mixer.write_text(
            mixer.read_text().replace('if [ "$1" = get ]; then', 'if [ "$1" = get ]; then exit 1;')
        )
    elif failure == "attenuate":
        mixer.write_text(
            mixer.read_text().replace(
                'if [ "$1" = get ]; then',
                'if [ "$1" = sset ] && [ "$3" = 10% ];then exit 1;fi\nif [ "$1" = get ]; then',
            )
        )
    elif failure == "journal":
        helper = path / "tools/json-fail-journal"
        script(
            helper,
            '#!/bin/sh\nif [ "$2" = journal ];then mkdir "$STATE_DIR/xiaoai_bridge.duck.json.$6";fi\nexec "'
            + env["XIAOAI_BRIDGE_JSON_BIN"]
            + '" "$@"\n',
        )
        env["XIAOAI_BRIDGE_JSON_BIN"] = str(helper)
    else:
        (path / "xiaoai_bridge.duck").mkdir()
    # Real redirection failures write a shell diagnostic. Keep the common
    # success helper's no-stderr assertion and inspect this fault directly.
    result = subprocess.run(
        [*speaker_shell, str(path / "bridge/lib/text.sh"), "announce", '{"text":"test"}'],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert result.returncode != 0
    assert payload["error"] == {
        "read": "volume_unknown",
        "attenuate": "hardware_control_failed",
    }.get(failure, "state_write_failed")
    assert (path / "tts-calls").read_text().strip() == "test"
    assert not (path / "player-started").exists()
    writes = path / "mixer-writes"
    assert "mysoftvol 10%" not in (writes.read_text().splitlines() if writes.exists() else [])


def test_volume_lock_contention_keeps_audio_and_journal_safe(text_device, speaker_shell):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    with (path / "xiaoai_bridge.volume.lock").open("a+") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        result = subprocess.run(
            [*speaker_shell, str(path / "bridge/lib/text.sh"), "announce", '{"text":"test"}'],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert result.returncode != 0 and payload["error"] == "busy"
    assert (path / "tts-calls").read_text().strip() == "test"
    assert not (path / "player-started").exists() and not (path / "mixer-writes").exists()
    assert not (path / "xiaoai_bridge.duck.json").exists()
    with (path / "xiaoai_bridge.audio.lock").open("a+") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGKILL])
def test_interrupted_automatic_duck_recovers_and_releases_audio(text_device, speaker_shell, stop):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    proc = subprocess.Popen(
        [*speaker_shell, str(path / "bridge/lib/text.sh"), "announce", '{"text":"test"}'],
        env={**env, "PLAY_SLEEP": "30"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert "synthesizing" in proc.stdout.readline()
        started = time.monotonic() + 5
        while not (path / "player-started").exists():
            assert time.monotonic() < started
            time.sleep(0.01)
        assert (path / "mixer-mysoftvol").read_text().strip() == "10"
        os.killpg(proc.pid, stop)
        proc.communicate(timeout=8)
        result = subprocess.run(
            [*speaker_shell, "-c", f'. "{path}/bridge/lib/common.sh"\nbridge_recover_pending'],
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert result.returncode == 0, result.stderr
        assert (path / "mixer-mysoftvol").read_text().strip() == "60"
        assert not (path / "xiaoai_bridge.duck.json").exists()
        for name in ("audio", "volume"):
            with (path / f"xiaoai_bridge.{name}.lock").open("a+") as gate:
                # The existing one-shot recovery can finish just after the
                # caller. Bound that cleanup; a surviving TTS keeps audio busy.
                deadline = time.monotonic() + 2
                while True:
                    try:
                        fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()


@pytest.mark.parametrize("new_master", [10, 40])
@pytest.mark.parametrize("new_ha_target", [None, 75])
def test_automatic_duck_preserves_sender_or_newer_ha_target(
    text_device, speaker_shell, new_master, new_ha_target
):
    call, path, env = text_device
    enable_airplay_duck_fixture(path)
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
case "$*" in *org.bluez*) printf 'string "State"\\nvariant string "idle"\\n';exit 0;; esac
raw=-18
[ ! -f "$STATE_DIR/sender-raw" ] || read raw < "$STATE_DIR/sender-raw"
printf 'string "Active"\\nvariant boolean true\\nstring "Volume"\\nvariant double %s\\n' "$raw"
""",
    )
    desired = (
        ""
        if new_ha_target is None
        else 'printf \'%s\' \'{"revision":2,"versions":{"master":2},"volumes":{"master":75}}\' > "$STATE_DIR/xiaoai_bridge.desired.json"\n'
    )
    script(
        path / "tools/mpg123",
        f"""#!/bin/sh
printf '%s\\n' -6 > "$STATE_DIR/sender-raw"
printf '{new_master}\\n' > "$STATE_DIR/mixer-mysoftvol"
{desired}""",
    )
    result, _ = call("announce", "test")
    pending = new_master == 10 and new_ha_target is None
    assert (result.returncode != 0) == pending
    assert (path / "mixer-mysoftvol").read_text().strip() == str(new_ha_target or new_master)
    assert "mysoftvol 60%" not in (path / "mixer-writes").read_text().splitlines()
    assert (path / "xiaoai_bridge.duck.json").exists() == pending
    if pending:
        # The applied cap cannot reveal the sender's new hardware target.
        # Preserve the journal until an explicit newer HA target resolves it.
        result = subprocess.run(
            [
                *speaker_shell,
                "-c",
                f'. "{path}/bridge/lib/common.sh"\nvolume_write master 55 >/dev/null\nbridge_recover_pending',
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert result.returncode == 0, result.stderr
        assert (path / "mixer-mysoftvol").read_text().strip() == "55"
        assert not (path / "xiaoai_bridge.duck.json").exists()
