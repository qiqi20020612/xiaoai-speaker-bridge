"""AirPlay ducking belongs to decoder playback, never provider synthesis."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.test_native_led import SEQUENCE
from tests.test_native_led import native_led as native_led
from tests.test_text_pipeline import enable_airplay_duck_fixture, script
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import text_device as text_device
from tests.test_tts_cancellation import install_providers


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "synthetic TTS did not reach the expected boundary"
        time.sleep(0.02)


def music_writes(path):
    output = path / "mixer-writes"
    return (
        [line for line in output.read_text().splitlines() if line.startswith("mysoftvol ")]
        if output.exists()
        else []
    )


def setup_provider(path, provider, ha_url):
    enable_airplay_duck_fixture(path)
    decoder = path / "synthetic-decoder"
    script(
        decoder,
        f"""#!{sys.executable}
import json,os,sys,time
from pathlib import Path
root=Path(os.environ['STATE_DIR'])
index=len(list(root.glob('decoded-*.json')))+1
data=Path(sys.argv[-1]).read_bytes()
(root/f'decoded-{{index}}.bin').write_bytes(data)
level=(root/'mixer-mysoftvol').read_text().strip() if (root/'mixer-mysoftvol').exists() else '60'
(root/f'decoded-{{index}}.json').write_text(json.dumps({{'level':level,'args':sys.argv[1:]}}))
while not (root/f'playback-released-{{index}}').exists():time.sleep(0.02)
sys.exit(1 if (root/'fail-first-playback').exists() and index==1 else 0)
""",
    )
    tts = install_providers(path, provider, decoder, ha_url)
    originals = {file: file.read_bytes() for file in tts.parent.glob("tts_*")}
    return originals


def start(path, env, speaker_shell, mode, **request):
    return subprocess.Popen(
        [
            *speaker_shell,
            str(path / "bridge/lib/text.sh"),
            mode,
            json.dumps({"text": "synthetic speech", **request}),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def finish(proc, path):
    (path / "synthesis-released").touch()
    (path / "fallback-released").touch()
    for index in (1, 2, 3):
        (path / f"playback-released-{index}").touch()
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    proc.communicate(timeout=15)


@pytest.mark.parametrize("mode", ["announce", "send_text"])
@pytest.mark.parametrize("provider", ["doubao", "microsoft", "google"])
def test_synthesis_keeps_music_and_loading_led_then_ducks_before_first_byte(
    native_led, ha_http, speaker_shell, mode, provider
):
    _, path, env = native_led
    originals = setup_provider(path, provider, ha_http.url)
    proc = start(path, env, speaker_shell, mode, volume=20)
    try:
        wait_for(lambda: (path / "synthesis.pid").exists())
        assert music_writes(path) == []
        assert not (path / "xiaoai_bridge.duck.json").exists()
        assert not (path / "decoded-1.json").exists()
        assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE[:2]
        (path / "synthesis-released").touch()
        wait_for(lambda: (path / "decoded-1.json").exists())
        first = json.loads((path / "decoded-1.json").read_text())
        assert first["level"] == "10"
        assert first["args"][:3] == ["-q", "-a", "notify"]
        assert (path / "decoded-1.bin").read_bytes() == (
            b"synthetic" if provider == "doubao" else b"synthetic-mp3"
        )
        assert music_writes(path) == ["mysoftvol 10%"]
        assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE[:2]
        (path / "playback-released-1").touch()
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0 and not stderr
        assert "reply_text" in json.loads(stdout.strip().splitlines()[-1])
        assert music_writes(path) == ["mysoftvol 10%", "mysoftvol 60%"]
        assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
        assert not (path / "xiaoai_bridge.duck.json").exists()
        assert not (path / "xiaoai_bridge.tts-volume.json").exists()
        assert not list(path.glob("xiaoai_text.*"))
        assert all(file.read_bytes() == contents for file, contents in originals.items())
        assert len(ha_http.conversations) == (1 if mode == "send_text" else 0)
    finally:
        finish(proc, path)


@pytest.mark.parametrize("mode", ["announce", "send_text"])
def test_ha_volume_during_synthesis_is_immediate_and_becomes_playback_baseline(
    text_device, ha_http, speaker_shell, mode
):
    _, path, env = text_device
    setup_provider(path, "doubao", ha_http.url)
    proc = start(path, env, speaker_shell, mode)
    try:
        wait_for(lambda: (path / "synthesis.pid").exists())
        update = subprocess.run(
            [*speaker_shell, "-c", f'. "{path / "bridge/lib/common.sh"}";volume_write master 45'],
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert update.returncode == 0 and not json.loads(update.stdout)["deferred"]
        assert music_writes(path) == ["mysoftvol 45%"]
        (path / "synthesis-released").touch()
        wait_for(lambda: (path / "decoded-1.json").exists())
        assert json.loads((path / "decoded-1.json").read_text())["level"] == "10"
        assert json.loads((path / "xiaoai_bridge.duck.json").read_text())["volumes"] == {
            "master": 45
        }
        (path / "playback-released-1").touch()
        proc.communicate(timeout=10)
        assert proc.returncode == 0
        assert music_writes(path) == ["mysoftvol 45%", "mysoftvol 10%", "mysoftvol 45%"]
    finally:
        finish(proc, path)


@pytest.mark.parametrize("phase", ["synthesis", "playback"])
def test_sender_volume_observed_at_playback_and_later_changes_are_not_overwritten(
    text_device, ha_http, speaker_shell, phase
):
    _, path, env = text_device
    setup_provider(path, "doubao", ha_http.url)
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
case "$*" in
 *org.bluez*) printf 'string "State"\\nvariant string "idle"\\n' ;;
 *) value=-18;[ ! -r "$STATE_DIR/sender-volume" ] || read value < "$STATE_DIR/sender-volume"
    printf 'string "Active"\\nvariant boolean true\\nstring "Volume"\\nvariant double %s\\n' "$value" ;;
esac
""",
    )
    proc = start(path, env, speaker_shell, "announce")
    try:
        wait_for(lambda: (path / "synthesis.pid").exists())
        (path / "sender-volume").write_text("-12\n")
        (path / "mixer-mysoftvol").write_text("45\n")
        (path / "synthesis-released").touch()
        wait_for(lambda: (path / "decoded-1.json").exists())
        saved = json.loads((path / "xiaoai_bridge.duck.json").read_text())
        assert saved["airplay_raw"] == "-12" and saved["volumes"] == {"master": 45}
        if phase == "playback":
            (path / "sender-volume").write_text("-6\n")
            (path / "mixer-mysoftvol").write_text("35\n")
        (path / "playback-released-1").touch()
        proc.communicate(timeout=10)
        assert proc.returncode == 0
        assert (path / "mixer-mysoftvol").read_text().strip() == (
            "35" if phase == "playback" else "45"
        )
        assert not (path / "xiaoai_bridge.duck.json").exists()
    finally:
        finish(proc, path)


def test_music_recovers_between_failed_playback_and_fallback_synthesis(
    native_led, ha_http, speaker_shell
):
    _, path, env = native_led
    setup_provider(path, "doubao", ha_http.url)
    (path / "fail-first-playback").touch()
    curl = path / "tools/curl"
    curl.write_text(
        curl.read_text().replace(
            "(root/'synthesis.pid').write_text(str(os.getpid()))",
            "if '/api/tts_proxy/' in url:\n"
            "    (root/'fallback-synthesis').touch()\n"
            "    while not (root/'fallback-released').exists():time.sleep(0.02)\n"
            "(root/'synthesis.pid').write_text(str(os.getpid()))",
        )
    )
    proc = start(path, env, speaker_shell, "announce")
    try:
        wait_for(lambda: (path / "synthesis.pid").exists())
        (path / "synthesis-released").touch()
        wait_for(lambda: (path / "decoded-1.json").exists())
        (path / "playback-released-1").touch()
        wait_for(lambda: (path / "fallback-synthesis").exists())
        assert music_writes(path) == ["mysoftvol 10%", "mysoftvol 60%"]
        assert not (path / "xiaoai_bridge.duck.json").exists()
        assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE[:2]
        (path / "fallback-released").touch()
        wait_for(lambda: (path / "decoded-2.json").exists())
        assert json.loads((path / "decoded-2.json").read_text())["level"] == "10"
        (path / "playback-released-2").touch()
        proc.communicate(timeout=10)
        assert proc.returncode == 0
        assert music_writes(path) == [
            "mysoftvol 10%",
            "mysoftvol 60%",
            "mysoftvol 10%",
            "mysoftvol 60%",
        ]
        assert (path / "native-led-calls").read_text().splitlines() == SEQUENCE
    finally:
        finish(proc, path)


@pytest.mark.parametrize("failure", ["journal", "volume", "lock"])
def test_preplay_safety_failure_never_starts_decoder_or_private_fallback(
    text_device, ha_http, speaker_shell, failure
):
    _, path, env = text_device
    setup_provider(path, "doubao", ha_http.url)
    if failure == "journal":
        recovery = path / "bridge/lib/volume-recovery.sh"
        recovery.write_text(
            recovery.read_text().replace(
                '    atomic_json "$journal_path" "$journal_json"',
                '    [ "$journal_path" != "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" ] || return 1\n'
                '    atomic_json "$journal_path" "$journal_json"',
            )
        )
    elif failure == "volume":
        mixer = path / "tools/amixer"
        mixer.write_text(
            mixer.read_text().replace(
                'if [ "$1" = get ]; then',
                'if [ "$1" = get ] && [ "$2" = mysoftvol ];then exit 1;fi\nif [ "$1" = get ]; then',
            )
        )
    proc = start(path, env, speaker_shell, "announce")
    lock = (path / "xiaoai_bridge.volume.lock").open("a+")
    try:
        wait_for(lambda: (path / "synthesis.pid").exists())
        if failure == "lock":
            fcntl.flock(lock, fcntl.LOCK_EX)
        (path / "synthesis-released").touch()
        stdout, _ = proc.communicate(timeout=15)
        assert proc.returncode != 0
        assert json.loads(stdout.strip().splitlines()[-1]) == {
            "error": {"journal": "state_write_failed", "volume": "volume_unknown", "lock": "busy"}[
                failure
            ]
        }
        assert not (path / "decoded-1.json").exists()
        assert (path / "provider-requests").read_text().splitlines() == ["response.jsonl"]
        assert music_writes(path) == []
    finally:
        lock.close()
        finish(proc, path)


def test_native_voice_keeps_original_tts_path_and_never_installs_playback_gate(
    text_device, speaker_shell
):
    call, path, env = text_device
    enable_airplay_duck_fixture(path)
    script(
        path / "existing-tts",
        '#!/bin/sh\nprintf \'%s\\n\' "$PATH" > "$STATE_DIR/tts-path"\n'
        'printf \'%s\\n\' "${XIAOAI_TTS_GATE:-}" > "$STATE_DIR/tts-gate"\nmpg123\n',
    )
    result, _ = call("voice", "hello")
    assert result.returncode == 0
    observed = (path / "tts-path").read_text().strip().split(os.pathsep)
    # Native Conversation already prepends its existing curl transport shim.
    assert observed[0].endswith("/transport") and "/xiaoai_text." in observed[0]
    assert os.pathsep.join(observed[1:]) == env["PATH"]
    assert (path / "tts-gate").read_text().strip() == ""
    assert music_writes(path) == []
    assert not list(path.glob("xiaoai_text.*"))


def test_decoder_stdin_is_byte_exact_and_nested_decoder_does_not_duck_twice(text_device):
    call, path, _ = text_device
    enable_airplay_duck_fixture(path)
    data = bytes(range(256)) * 64
    (path / "synthetic-audio").write_bytes(data)
    script(
        path / "existing-tts",
        '#!/bin/sh\ncat "$STATE_DIR/synthetic-audio" | mpg123 -\n',
    )
    script(path / "tools/mpg123", '#!/bin/sh\nexec ogg123 "$@"\n')
    script(
        path / "tools/ogg123",
        f"""#!{sys.executable}
import os,sys
from pathlib import Path
root=Path(os.environ['STATE_DIR'])
assert (root/'mixer-mysoftvol').read_text().strip()=='10'
(root/'decoded-stdin').write_bytes(sys.stdin.buffer.read())
""",
    )
    result, _ = call("announce", "synthetic speech")
    assert result.returncode == 0
    assert (path / "decoded-stdin").read_bytes() == data
    assert music_writes(path) == ["mysoftvol 10%", "mysoftvol 60%"]


def test_closed_task_gate_cannot_restart_playback_even_while_owner_is_alive(
    text_device, speaker_shell
):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    body = f'''. "{path / "bridge/lib/common.sh"}"
. "$BRIDGE_DIR/lib/tts-playback.sh"
WORK=$(mktemp -d "$BRIDGE_RUNTIME/xiaoai_text.XXXXXX")
bridge_tts_gate_prepare || exit 70
gate=$TTS_PLAYBACK_DIR
bridge_tts_gate_close || exit 71
XIAOAI_TTS_GATE="$gate" XIAOAI_TTS_MODE=announce "$gate/mpg123"
status=$?
[ -f "$gate/failed" ] || exit 72
rm -rf "$WORK"
exit "$status"
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=8
    )
    assert result.returncode == 0
    assert not (path / "player-started").exists() and music_writes(path) == []


def test_owned_decoder_reports_playing_without_proc_children_interface(text_device):
    call, path, _ = text_device
    script(path / "existing-tts", "#!/bin/sh\nexec mpg123\n")
    script(path / "tools/mpg123", "#!/bin/sh\nsleep 0.6\n")
    pipeline = path / "bridge/lib/text.sh"
    pipeline.write_text(
        pipeline.read_text()
        .replace('"/proc/$child/comm"', '"$STATE_DIR/missing-comm"')
        .replace('"/proc/$child/task/$child/children"', '"$STATE_DIR/missing-children"')
    )
    result, _ = call("announce", "synthetic speech")
    assert result.returncode == 0
    assert [
        json.loads(line)["stage"] for line in result.stdout.splitlines() if '"stage"' in line
    ] == [
        "synthesizing",
        "playing",
    ]


@pytest.mark.parametrize("mode", ["announce", "send_text"])
def test_voice_preparation_during_synthesis_cannot_receive_a_late_remote_duck(
    text_device, ha_http, speaker_shell, mode
):
    _, path, env = text_device
    setup_provider(path, "doubao", ha_http.url)
    remote = start(path, env, speaker_shell, mode)
    voice = None
    try:
        wait_for(lambda: (path / "synthesis.pid").exists())
        # Execute the unchanged native hooks. They capture volume before waiting
        # on the remote task's Audio Lock, with no daemon event receiver here.
        voice = subprocess.Popen(
            [
                *speaker_shell,
                "-c",
                f'. "{path / "bridge/lib/listener-hooks.sh"}"\n'
                "bridge_voice_begin || exit 70\n"
                'printf \'%s\\n\' "$BRIDGE_MASTER_SAVED" > "$STATE_DIR/voice-baseline"\n'
                "bridge_restore_volume\n",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        wait_for(lambda: (path / "xiaoai_bridge.voice-volume.json").exists())
        assert not (path / "voice-baseline").exists()
        (path / "synthesis-released").touch()
        wait_for(lambda: (path / "decoded-1.json").exists())
        assert json.loads((path / "decoded-1.json").read_text())["level"] == "60"
        assert music_writes(path) == []
        assert not (path / "xiaoai_bridge.duck.json").exists()
        (path / "playback-released-1").touch()
        remote.communicate(timeout=10)
        assert remote.returncode == 0
        _, stderr = voice.communicate(timeout=10)
        assert voice.returncode == 0, stderr
        assert (path / "voice-baseline").read_text().strip() == "60"
        assert (path / "mixer-mysoftvol").read_text().strip() == "60"
        assert not (path / "xiaoai_bridge.voice-volume.json").exists()
        for name in ("audio", "volume", "session"):
            with (path / f"xiaoai_bridge.{name}.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        finish(remote, path)
        if voice is not None:
            finish(voice, path)
