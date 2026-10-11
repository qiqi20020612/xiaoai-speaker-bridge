import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def text_device(tmp_path, install_flock, speaker_shell):
    tools = tmp_path / "tools"
    tools.mkdir()
    install_flock(tools)
    script(
        tools / "amixer",
        """#!/bin/sh
if [ "$1" = get ];then
 printf 'Front Left: 153 [60%%]\\n'
else
 printf '%s %s\\n' "$2" "$3" >> "$STATE_DIR/mixer-writes"
fi
""",
    )
    script(
        tools / "curl",
        """#!/bin/sh
for arg in "$@";do
 case "$arg" in
  */volume_plan) printf '{"handled":false}';exit 0 ;;
  */intent) exit 22 ;;
 esac
done
exit 1
""",
    )
    script(
        tools / "dbus-send",
        """#!/bin/sh
printf 'string "Active"\nvariant boolean false\n'
""",
    )
    script(
        tools / "fake-bridge",
        """#!/bin/sh
case "$1" in
 --snapshot) printf '{"source":"mpd","uncertain":false}\n' ;;
 --mpd-state) printf 'play\n' ;;
 --emit) exit 0 ;;
esac
""",
    )
    tts = tmp_path / "existing-tts"
    script(
        tts,
        """#!/bin/sh
printf '%s\n' "$1" >> "$STATE_DIR/tts-calls"
sleep "${TTS_SLEEP:-0.02}"
""",
    )
    listener = tmp_path / "existing-listener"
    listener.write_text(f"""HA_URL=http://homeassistant.example
HA_TOKEN=synthetic-token
EOAIC2_AGENT_ID=conversation.synthetic
EOAIC2_KEEP_SECONDS=300
EOAIC2_STATE="$STATE_DIR/eoai_conversation.json"
STT_LANGUAGE=zh-CN
curl() {{
    previous='';body=''
    for part in "$@";do
        if [ "$previous" = -d ];then body=$part;break;fi
        previous=$part
    done
    '{sys.executable}' '{ROOT / "tests/legacy_agent_stub.py"}' "$EOAIC2_STATE" "$EOAIC2_AGENT_ID" "$body"
}}
""")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {"device_id": "synthetic", "listener_config": str(listener), "tts_path": str(tts)}
        )
    )
    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "STATE_DIR": str(tmp_path),
        "XIAOAI_BRIDGE_DIR": str(ROOT / "speaker"),
        "XIAOAI_BRIDGE_BIN": str(tools / "fake-bridge"),
        "XIAOAI_BRIDGE_RUNTIME": str(tmp_path),
        "XIAOAI_BRIDGE_CONFIG": str(config),
    }

    def call(mode, text, **extra):
        payload = {"text": text, **extra}
        result = subprocess.run(
            [*speaker_shell, str(ROOT / "speaker/lib/text.sh"), mode, json.dumps(payload)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert not result.stderr, result.stderr
        return result, json.loads(result.stdout.strip().splitlines()[-1])

    return call, tmp_path, env


def test_announce_reads_open_lamp_without_executing_or_updating_context(text_device):
    call, path, env = text_device
    result, payload = call("announce", "open lamp")
    assert result.returncode == 0 and payload["reply_text"] == ""
    assert (path / "tts-calls").read_text().strip() == "open lamp"
    assert not (path / "legacy-calls").exists() and not (path / "eoai_conversation.json").exists()


def test_voice_text_voice_share_original_sliding_context(text_device):
    call, path, env = text_device
    assert call("voice", "my name is Alice")[0].returncode == 0
    cid = json.loads((path / "eoai_conversation.json").read_text())["id"]
    _, reply = call("send_text", "what is my name", speak_response=False)
    assert reply["reply_text"] == "Alice" and reply["source"] == "mpd"
    assert json.loads((path / "eoai_conversation.json").read_text())["id"] == cid
    _, reply = call("voice", "what is my name")
    assert reply["reply_text"] == "Alice"
    assert (path / "tts-calls").read_text().splitlines() == ["reply: my name is Alice", "Alice"]


def test_no_context_and_agent_override_do_not_mix_the_default_context(text_device):
    call, path, env = text_device
    call("voice", "my name is Alice")
    saved = (path / "eoai_conversation.json").read_text()
    assert (
        call("send_text", "what is my name", speak_response=False, use_context=False)[1][
            "reply_text"
        ]
        == "unknown"
    )
    assert (path / "eoai_conversation.json").read_text() == saved
    assert (
        call("send_text", "what is my name", speak_response=False, agent_id="conversation.other")[
            1
        ]["reply_text"]
        == "unknown"
    )
    assert (path / "eoai_conversation.json").read_text() == saved


def test_failed_conversation_does_not_refresh_context(text_device):
    call, path, env = text_device
    call("voice", "my name is Alice")
    state = json.loads((path / "eoai_conversation.json").read_text())
    state["ts"] -= 60
    (path / "eoai_conversation.json").write_text(json.dumps(state))
    result, reply = call("send_text", "FAIL", speak_response=False)
    assert result.returncode != 0 and reply["error"] == "conversation_failed"
    assert json.loads((path / "eoai_conversation.json").read_text()) == state


def test_original_five_minute_expiry_creates_a_fresh_conversation(text_device):
    call, path, env = text_device
    call("voice", "my name is Alice")
    state = json.loads((path / "eoai_conversation.json").read_text())
    state["ts"] -= 301
    (path / "eoai_conversation.json").write_text(json.dumps(state))
    assert call("send_text", "what is my name", speak_response=False)[1]["reply_text"] == "unknown"
    assert json.loads((path / "eoai_conversation.json").read_text())["id"] != state["id"]


def test_remote_text_cannot_forge_the_source_snapshot(text_device):
    call, path, env = text_device
    _, reply = call(
        "send_text",
        "hello",
        speak_response=False,
        _snapshot={"source": "airplay", "uncertain": False},
    )
    assert reply["source"] == "mpd"


def test_native_volume_intent_does_not_fall_through_to_master_or_conversation(text_device):
    call, path, env = text_device
    script(
        path / "tools/curl",
        """#!/bin/sh
body='';previous=''
for arg in "$@";do
 [ "$previous" != --data-binary ] || body=$arg
 previous=$arg
 case "$arg" in
  */volume_plan)
   if printf '%s' "$body" | jq -e '.local_intent=="VolumeUp"' >/dev/null;then
    printf '{"error":"unsupported_volume_target"}'
   else
    printf '{"handled":false}'
   fi
   exit 0 ;;
  */intent) printf 'VolumeUp';exit 0 ;;
 esac
done
exit 1
""",
    )
    result, payload = call("voice", "大声点", speak_response=False)
    assert result.returncode != 0 and payload["error"] == "unsupported_volume_target"
    assert not (path / "mixer-writes").exists()
    assert not (path / "legacy-calls").exists()


def test_temporary_tts_volume_restores_after_cancellation(text_device, speaker_shell):
    call, path, env = text_device
    payload = {"text": "long speech", "volume": 25}
    proc = subprocess.Popen(
        [*speaker_shell, str(ROOT / "speaker/lib/text.sh"), "announce", json.dumps(payload)],
        env={**env, "TTS_SLEEP": "5"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    line = proc.stdout.readline()
    assert "synthesizing" in line
    proc.terminate()
    proc.communicate(timeout=8)
    writes = (path / "mixer-writes").read_text().splitlines()
    assert writes[0] == "notifyvol 25%" and writes[-1] == "notifyvol 60%"


def test_voice_restore_keeps_new_master_and_does_not_link_other_channels(
    text_device, speaker_shell
):
    call, path, env = text_device
    (path / "xiaoai_bridge.desired.json").write_text(
        json.dumps(
            {
                "revision": 2,
                "versions": {"master": 2, "tts": 2},
                "volumes": {"master": 30, "tts": 65},
            }
        )
    )
    body = f""". '{ROOT / "speaker/lib/listener-hooks.sh"}'
SAVED_VOL=60
BRIDGE_BT_SAVED=50
BRIDGE_VOICE_REVISION=1
printf '%s\\n' "$$" > "$BRIDGE_RUNTIME/xiaoai_bridge.voice"
bridge_restore_volume
"""
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert (path / "mixer-writes").read_text().splitlines() == ["mysoftvol 30%", "bluetooth 50%"]
    assert not (path / "xiaoai_bridge.voice").exists()


def test_voice_inherits_audio_reservation_without_locking_itself_out(text_device, speaker_shell):
    call, path, env = text_device
    body = f""". '{ROOT / "speaker/lib/listener-hooks.sh"}'
bridge_voice_begin
SAVED_VOL=60
STT_TEXT=hello
bridge_process_text
bridge_restore_volume
"""
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=8
    )
    assert result.returncode == 0, result.stderr
    assert (path / "tts-calls").read_text().strip() == "reply: hello"
    assert not (path / "xiaoai_bridge.voice").exists()


def test_remote_conversation_releases_context_lock_before_waiting_for_voice_audio(
    text_device, speaker_shell
):
    call, path, env = text_device
    with (path / "xiaoai_bridge.audio.lock").open("w") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        proc = subprocess.Popen(
            [
                *speaker_shell,
                str(ROOT / "speaker/lib/text.sh"),
                "send_text",
                json.dumps({"text": "hello"}),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not (path / "legacy-calls").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (path / "legacy-calls").exists()
            # The voice text stage must be able to take this lock while remote TTS waits.
            with (path / "xiaoai_bridge.session.lock").open("w") as session:
                deadline = time.monotonic() + 2
                while True:
                    try:
                        fcntl.flock(session, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.02)
                fcntl.flock(session, fcntl.LOCK_UN)
        finally:
            fcntl.flock(gate, fcntl.LOCK_UN)
            stdout, stderr = proc.communicate(timeout=8)
        assert proc.returncode == 0 and not stderr, stdout + stderr


def test_error_prompt_waits_for_audio_with_busybox_fd_lock(text_device, speaker_shell):
    call, path, env = text_device
    with (path / "xiaoai_bridge.audio.lock").open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        body = f""". '{ROOT / "speaker/lib/listener-hooks.sh"}'
SPEAK='{path / "existing-tts"}'
bridge_error_tts
"""
        proc = subprocess.Popen(
            [*speaker_shell, "-c", body],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.15)
        assert not (path / "tts-calls").exists() and proc.poll() is None
        fcntl.flock(owner, fcntl.LOCK_UN)
        _, stderr = proc.communicate(timeout=5)
        assert proc.returncode == 0 and not stderr
        assert (path / "tts-calls").read_text().strip() == "error"
