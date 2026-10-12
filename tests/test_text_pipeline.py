import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.http_conversation import ConversationHTTP

ROOT = Path(__file__).resolve().parents[1]


def script(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def ha_http(socket_enabled):
    server = ConversationHTTP()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def text_device(tmp_path, install_flock, speaker_shell, ha_http, busybox_binary):
    tools = tmp_path / "tools"
    tools.mkdir()
    install_flock(tools)
    if busybox_binary:
        for applet in ("awk", "sed"):
            script(tools / applet, f'#!/bin/sh\nexec "{busybox_binary}" {applet} "$@"\n')
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
    assert shutil.which("curl"), "The HTTP integration tests require real curl"
    # Keep every production script intact except the fixture's local-intent URL:
    # an ephemeral HTTP port replaces the device's fixed port 80. No curl mock.
    bridge = tmp_path / "bridge"
    shutil.copytree(ROOT / "speaker/lib", bridge / "lib")
    pipeline = bridge / "lib/text.sh"
    pipeline.write_text(
        pipeline.read_text().replace("http://127.0.0.1/intent", ha_http.url + "/intent")
    )
    agent = tmp_path / "existing-eoai-agent.sh"
    shutil.copy(ROOT / "tests/synthetic_eoai_agent.sh", agent)
    auth = tmp_path / "existing-launcher.sh"
    shutil.copy(ROOT / "tests/synthetic_auth_launcher.sh", auth)
    ha_http.state_dir = tmp_path
    script(tools / "ip", "#!/bin/sh\nprintf 'inet 192.0.2.42/24\\n'\n")
    if sys.platform == "darwin":
        script(
            tools / "sed",
            '#!/bin/sh\nif [ "$1" = -i ];then shift;exec /usr/bin/sed -i \'\' "$@";fi\nexec /usr/bin/sed "$@"\n',
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
    listener.write_text(f"""HA_URL={ha_http.url}
HA_TOKEN=synthetic-token
HA_REFRESH_TOKEN=synthetic-refresh-token
STT_LANGUAGE=zh-CN
. '{agent}'
""")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "device_id": "synthetic",
                "listener_config": str(listener),
                "tts_path": str(tts),
                "eoai_agent_script": str(agent),
                "ha_auth_source": str(auth),
            }
        )
    )
    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "STATE_DIR": str(tmp_path),
        "XIAOAI_BRIDGE_DIR": str(bridge),
        "XIAOAI_BRIDGE_BIN": str(tools / "fake-bridge"),
        "XIAOAI_BRIDGE_RUNTIME": str(tmp_path),
        "XIAOAI_BRIDGE_CONFIG": str(config),
    }

    def call(mode, text, **extra):
        payload = {"text": text, **extra}
        result = subprocess.run(
            [*speaker_shell, str(pipeline), mode, json.dumps(payload)],
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


def test_voice_text_voice_share_original_sliding_context(text_device, ha_http):
    call, path, env = text_device
    assert call("voice", "my name is Alice")[0].returncode == 0
    cid = json.loads((path / "eoai_conversation.json").read_text())["id"]
    state = json.loads((path / "eoai_conversation.json").read_text())
    state["ts"] -= 60
    (path / "eoai_conversation.json").write_text(json.dumps(state))
    before = int(time.time())
    _, reply = call("send_text", "what is my name", speak_response=False)
    assert reply["reply_text"] == "Alice" and reply["source"] == "mpd"
    assert json.loads((path / "eoai_conversation.json").read_text())["id"] == cid
    assert json.loads((path / "eoai_conversation.json").read_text())["ts"] >= before
    _, reply = call("voice", "what is my name")
    assert reply["reply_text"] == "Alice"
    assert (path / "tts-calls").read_text().splitlines() == ["reply: my name is Alice", "Alice"]
    wire = ha_http.conversations
    assert len(wire) == 3
    assert [r["body"].get("conversation_id") for r in wire] == [None, cid, cid]
    for request in wire:
        assert request["method"] == "POST"
        assert request["body"]["agent_id"] == "conversation.synthetic"
        assert request["body"]["language"] == "zh-CN"
        assert request["headers"]["Content-Type"] == "application/json"
        assert request["headers"]["Authorization"] == "Bearer synthetic-token"


def test_no_context_and_agent_override_do_not_mix_the_default_context(text_device, ha_http):
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
    assert ha_http.conversations[-1]["body"]["agent_id"] == "conversation.synthetic"
    assert "conversation_id" not in ha_http.conversations[-1]["body"]
    assert (
        call("send_text", "what is my name", speak_response=False, agent_id="conversation.other")[
            1
        ]["reply_text"]
        == "unknown"
    )
    assert (path / "eoai_conversation.json").read_text() == saved
    assert ha_http.conversations[-1]["body"]["agent_id"] == "conversation.other"
    assert "conversation_id" not in ha_http.conversations[-1]["body"]


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


@pytest.mark.parametrize("age", [300, 301, -30])
def test_real_http_excludes_expired_or_future_conversation_id(text_device, ha_http, age):
    call, path, _ = text_device
    cached = {
        "agent": "conversation.synthetic",
        "id": "expired-synthetic-id",
        "ts": int(time.time()) - age,
    }
    (path / "eoai_conversation.json").write_text(json.dumps(cached))
    result, _ = call("send_text", "hello", speak_response=False)
    assert result.returncode == 0
    assert "conversation_id" not in ha_http.conversations[-1]["body"]
    assert json.loads((path / "eoai_conversation.json").read_text())["id"] != cached["id"]


def test_real_http_override_cache_is_shared_without_changing_default_or_selection(
    text_device, ha_http
):
    call, path, _ = text_device
    call("voice", "my name is Alice", speak_response=False)
    default = (path / "eoai_conversation.json").read_text()
    (path / "xiaoai_bridge.agent.json").write_text('{"agent_id":"conversation.other"}')
    call("voice", "my name is Bob", speak_response=False)
    override = path / "xiaoai_bridge.context.conversation.other.json"
    cid = json.loads(override.read_text())["id"]
    result, reply = call("send_text", "what is my name", speak_response=False)
    assert result.returncode == 0 and reply["reply_text"] == "Bob"
    assert ha_http.conversations[-1]["body"]["agent_id"] == "conversation.other"
    assert ha_http.conversations[-1]["body"]["conversation_id"] == cid
    saved = override.read_text()
    result, reply = call(
        "send_text",
        "what is my name",
        speak_response=False,
        agent_id="conversation.third",
        use_context=False,
    )
    assert result.returncode == 0 and reply["reply_text"] == "unknown"
    assert ha_http.conversations[-1]["body"]["agent_id"] == "conversation.third"
    assert "conversation_id" not in ha_http.conversations[-1]["body"]
    assert (
        override.read_text() == saved and (path / "eoai_conversation.json").read_text() == default
    )
    assert not (path / "xiaoai_bridge.context.conversation.third.json").exists()
    assert (
        json.loads((path / "xiaoai_bridge.agent.json").read_text())["agent_id"]
        == "conversation.other"
    )
    assert not list(path.glob("xiaoai_text.*"))


def test_missing_original_wrapper_fails_without_a_raw_http_fallback(text_device, ha_http):
    call, path, _ = text_device
    agent = path / "existing-eoai-agent.sh"
    agent.write_text("EOAIC2_AGENT_ID=conversation.synthetic\nEOAIC2_STATE=unused\n")
    result, reply = call("send_text", "hello", speak_response=False)
    assert result.returncode != 0 and reply["error"] == "conversation_adapter_missing"
    assert not ha_http.conversations


def test_original_agent_is_explicitly_loaded_after_a_listener_curl_function(text_device, ha_http):
    call, path, _ = text_device
    with (path / "existing-listener").open("a") as listener:
        listener.write("curl() { printf 'This unrelated wrapper must not execute';return 99; }\n")
    result, _ = call("send_text", "hello", speak_response=False)
    assert result.returncode == 0
    assert ha_http.conversations[-1]["body"]["agent_id"] == "conversation.synthetic"


def test_reloading_original_wrapper_preserves_listener_agent_cache_and_ttl(text_device, ha_http):
    call, path, _ = text_device
    agent = path / "existing-eoai-agent.sh"
    agent.write_text(agent.read_text().replace("EOAIC2_KEEP_SECONDS=300", "EOAIC2_KEEP_SECONDS=1"))
    cache = path / "existing-custom-cache.json"
    cache.write_text(
        json.dumps(
            {
                "agent": "conversation.configured",
                "id": "configured-existing-id",
                "ts": int(time.time()) - 60,
            }
        )
    )
    with (path / "existing-listener").open("a") as listener:
        listener.write(
            f'EOAIC2_AGENT_ID=conversation.configured\nEOAIC2_STATE="{cache}"\nEOAIC2_KEEP_SECONDS=300\n'
        )
    result, _ = call("send_text", "hello", speak_response=False)
    assert result.returncode == 0
    body = ha_http.conversations[-1]["body"]
    assert (
        body["agent_id"] == "conversation.configured"
        and body["conversation_id"] == "configured-existing-id"
    )
    assert not (path / "eoai_conversation.json").exists()
    assert json.loads(cache.read_text())["id"] == "configured-existing-id"


@pytest.mark.parametrize("initial", [None, "not JSON"])
def test_error_response_restores_absent_or_malformed_original_cache(text_device, initial):
    call, path, _ = text_device
    cache = path / "eoai_conversation.json"
    if initial is not None:
        cache.write_text(initial)
    result, reply = call("send_text", "FAIL", speak_response=False)
    assert result.returncode != 0 and reply["error"] == "conversation_failed"
    assert (cache.read_text() if cache.exists() else None) == initial


def test_real_http_401_reuses_original_refresh_then_same_agent_and_conversation(
    text_device, ha_http
):
    call, path, _ = text_device
    call("voice", "my name is Alice", speak_response=False)
    state = json.loads((path / "eoai_conversation.json").read_text())
    state["ts"] -= 60
    (path / "eoai_conversation.json").write_text(json.dumps(state))
    original = (path / "existing-listener").read_text()
    ha_http.valid_token = "expired-on-server"
    start = len(ha_http.conversations)
    before = int(time.time())
    result, reply = call("send_text", "what is my name", speak_response=False)
    assert result.returncode == 0 and reply["reply_text"] == "Alice"
    attempts = ha_http.conversations[start:]
    assert len(attempts) == 2
    assert [r["headers"]["Authorization"] for r in attempts] == [
        "Bearer synthetic-token",
        "Bearer synthetic-refreshed",
    ]
    assert all(
        r["body"]["agent_id"] == state["agent"] and r["body"]["conversation_id"] == state["id"]
        for r in attempts
    )
    assert ha_http.refreshes == [
        {
            "grant_type": ["refresh_token"],
            "client_id": ["http://192.0.2.42"],
            "refresh_token": ["synthetic-refresh-token"],
        }
    ]
    assert (path / "existing-listener").read_text() == original.replace(
        "HA_TOKEN=synthetic-token", "HA_TOKEN=synthetic-refreshed"
    )
    saved = json.loads((path / "eoai_conversation.json").read_text())
    assert saved["id"] == state["id"] and saved["ts"] >= before
    assert not (path / "fatal-refresh").exists()
    assert not list(path.glob("xiaoai_text.*"))


def test_real_http_refresh_keeps_no_context_agent_override_isolated(text_device, ha_http):
    call, path, _ = text_device
    call("voice", "my name is Alice", speak_response=False)
    original = (path / "eoai_conversation.json").read_text()
    ha_http.valid_token = "expired-on-server"
    result, reply = call(
        "send_text",
        "what is my name",
        speak_response=False,
        agent_id="conversation.other",
        use_context=False,
    )
    assert result.returncode == 0 and reply["reply_text"] == "unknown"
    assert all(
        r["body"]["agent_id"] == "conversation.other" and "conversation_id" not in r["body"]
        for r in ha_http.conversations[-2:]
    )
    assert (path / "eoai_conversation.json").read_text() == original
    assert not (path / "xiaoai_bridge.context.conversation.other.json").exists()
    assert len(ha_http.refreshes) == 1


@pytest.mark.parametrize(
    "failure", ["refresh_error", "missing_token", "invalid_token", "second_401", "unknown_launcher"]
)
def test_real_http_refresh_failure_does_not_update_cache_or_stop_original_voice(
    text_device, ha_http, failure
):
    call, path, _ = text_device
    call("voice", "my name is Alice", speak_response=False)
    cached = (path / "eoai_conversation.json").read_text()
    listener = (path / "existing-listener").read_text()
    ha_http.valid_token = "expired-on-server"
    if failure == "refresh_error":
        ha_http.refresh_error = True
    elif failure == "missing_token":
        ha_http.refresh_body = {}
    elif failure == "invalid_token":
        ha_http.refresh_body = {"access_token": "invalid/token"}
    elif failure == "second_401":
        ha_http.reject_conversation = True
    else:
        (path / "existing-launcher.sh").write_text("# Unknown auth implementation: do not guess\n")
    start = len(ha_http.conversations)
    result, reply = call("send_text", "hello", speak_response=False)
    assert result.returncode != 0 and reply["error"] == "conversation_failed"
    assert len(ha_http.conversations[start:]) == (2 if failure == "second_401" else 1)
    assert len(ha_http.refreshes) == (0 if failure == "unknown_launcher" else 1)
    assert (path / "eoai_conversation.json").read_text() == cached
    if failure != "second_401":
        assert (path / "existing-listener").read_text() == listener
    assert not (path / "fatal-refresh").exists()
    assert not list(path.glob("xiaoai_text.*"))


@pytest.mark.parametrize("text", ["HTTP_FAIL", "DROP", "MALFORMED", "NO_ID", "FAIL"])
def test_real_http_non_auth_failure_is_never_retried_or_refreshed(text_device, ha_http, text):
    call, path, _ = text_device
    call("voice", "my name is Alice", speak_response=False)
    saved = (path / "eoai_conversation.json").read_text()
    start = len(ha_http.conversations)
    result, reply = call("send_text", text, speak_response=False)
    assert result.returncode != 0 and reply["error"] == "conversation_failed"
    assert len(ha_http.conversations[start:]) == 1 and not ha_http.refreshes
    assert (path / "eoai_conversation.json").read_text() == saved


def test_remote_text_cannot_forge_the_source_snapshot(text_device):
    call, path, env = text_device
    _, reply = call(
        "send_text",
        "hello",
        speak_response=False,
        _snapshot={"source": "airplay", "uncertain": False},
    )
    assert reply["source"] == "mpd"


def test_native_volume_intent_does_not_fall_through_to_master_or_conversation(text_device, ha_http):
    call, path, env = text_device
    ha_http.intent = "VolumeUp"
    result, payload = call("voice", "大声点", speak_response=False)
    assert result.returncode != 0 and payload["error"] == "unsupported_volume_target"
    assert not (path / "mixer-writes").exists()
    assert not (path / "legacy-calls").exists()


def test_temporary_tts_volume_restores_after_cancellation(text_device, speaker_shell):
    call, path, env = text_device
    payload = {"text": "long speech", "volume": 25}
    proc = subprocess.Popen(
        [
            *speaker_shell,
            str(Path(env["XIAOAI_BRIDGE_DIR"]) / "lib/text.sh"),
            "announce",
            json.dumps(payload),
        ],
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
                str(Path(env["XIAOAI_BRIDGE_DIR"]) / "lib/text.sh"),
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


@pytest.mark.parametrize(
    "ap,bt,mpd",
    [(a, b, m) for a in (False, True) for b in (False, True) for m in ("play", "stop", "unknown")],
)
def test_snapshot_single_jq_keeps_all_source_combinations(text_device, speaker_shell, ap, bt, mpd):
    _, path, env = text_device
    tools = path / "tools"
    real_jq = shutil.which("jq")
    script(tools / "jq", f'#!/bin/sh\nprintf x >> "$STATE_DIR/jq-count"\nexec "{real_jq}" "$@"\n')
    script(
        tools / "dbus-send",
        f'''#!/bin/sh
case "$*" in *org.bluez*)
 printf 'string "State"\\nvariant string "{"active" if bt else "idle"}"\\n' ;;
 *) printf 'string "Active"\\nvariant boolean {str(ap).lower()}\\n' ;;
esac
''',
    )
    bridge = tools / "fake-bridge"
    script(
        bridge,
        f"""#!/bin/sh
case "$1" in
 --snapshot) printf '{{"source":"mpd","uncertain":false}}\\n' ;;
 --mpd-state) printf '{mpd}\\n' ;;
esac
""",
    )
    result = subprocess.run(
        [*speaker_shell, str(path / "bridge/lib/source-snapshot.sh"), "42", "60", "50"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)
    sources = (
        (["airplay"] if ap else [])
        + (["bluetooth"] if bt else [])
        + (["mpd"] if mpd == "play" else [])
    )
    assert snapshot["active_sources"] == sources
    assert snapshot["source"] == (
        "mpd" if "mpd" in sources else sources[0] if len(sources) == 1 else None
    )
    assert snapshot["uncertain"] == ((not sources and mpd == "unknown") or len(sources) > 1)
    assert snapshot["volumes"] == {"master": 60, "bluetooth": 50}
    assert snapshot["pid"] == 42
    assert (path / "jq-count").read_text() == "x"


def test_wake_led_precedes_slow_snapshot_and_attenuation(text_device, speaker_shell):
    _, path, env = text_device
    led = path / "tools/show-led"
    script(led, '#!/bin/sh\nprintf "%s\\n" led >> "$STATE_DIR/order"\n')
    snapshot = path / "bridge/lib/source-snapshot.sh"
    snapshot.write_text(
        snapshot.read_text().replace(
            'base=$("$BRIDGE_BIN"',
            'printf "%s\\n" snapshot >> "$STATE_DIR/order"\nsleep 0.1\nbase=$("$BRIDGE_BIN"',
        )
    )
    hooks = path / "bridge/lib/listener-hooks.sh"
    hooks.write_text(hooks.read_text().replace("/bin/show_led", str(led)))
    body = f'''. "{hooks}"
bridge_voice_begin || exit 1
printf '%s\\n' attenuation >> "$STATE_DIR/order"
SAVED_VOL=60
bridge_restore_volume
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body],
        env={**env, "XIAOAI_PROFILE": "1"},
        capture_output=True,
        text=True,
        timeout=6,
    )
    assert result.returncode == 0, result.stderr
    assert (path / "order").read_text().splitlines() == ["led", "snapshot", "attenuation"]
    profile = (path / "xiaoai_bridge.profile").read_text()
    assert "phase=wake_led" in profile and "phase=recording_ready" in profile
    assert "synthetic-token" not in profile


def enable_airplay_duck_fixture(path):
    script(
        path / "tools/amixer",
        """#!/bin/sh
if [ "$1" = get ]; then
 value=60
 [ ! -f "$STATE_DIR/mixer-$2" ] || read value < "$STATE_DIR/mixer-$2"
 printf 'Front Left: 153 [%s%%]\\n' "$value"
else
 printf '%s %s\\n' "$2" "$3" >> "$STATE_DIR/mixer-writes"
 printf '%s\\n' "${3%?}" > "$STATE_DIR/mixer-$2"
fi
""",
    )
    config = path / "config.json"
    values = json.loads(config.read_text())
    values["verified"] = {"announcement_ducking": True}
    config.write_text(json.dumps(values))
    script(
        path / "tools/fake-bridge",
        """#!/bin/sh
case "$1" in
 --snapshot) printf '{"source":"airplay","uncertain":false}\\n' ;;
 --mpd-state) printf 'stop\\n' ;;
 --emit) exit 0 ;;
esac
""",
    )
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
case "$*" in
 *org.bluez*) printf 'string "State"\\nvariant string "idle"\\n' ;;
 *) printf 'string "Active"\\nvariant boolean true\\n' ;;
esac
""",
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_announce_duck_restores_master_independently_of_tts(text_device, enabled):
    call, path, _ = text_device
    enable_airplay_duck_fixture(path)
    result, _ = call("announce", "test", volume=45, **({"duck_volume": 10} if enabled else {}))
    assert result.returncode == 0
    writes = (path / "mixer-writes").read_text().splitlines()
    assert writes == (
        ["notifyvol 45%", "mysoftvol 10%", "notifyvol 60%", "mysoftvol 60%"]
        if enabled
        else ["notifyvol 45%", "notifyvol 60%"]
    )
    assert not (path / "xiaoai_bridge.duck").exists()
    assert not (path / "xiaoai_bridge.duck.json").exists()


@pytest.mark.parametrize("fail", [False, True])
def test_duck_failure_or_finish_uses_new_ha_target(text_device, fail):
    call, path, _ = text_device
    enable_airplay_duck_fixture(path)
    script(
        path / "existing-tts",
        f"""#!/bin/sh
printf '{{"revision":2,"versions":{{"master":2}},"volumes":{{"master":45}}}}' > "$STATE_DIR/xiaoai_bridge.desired.json"
exit {1 if fail else 0}
""",
    )
    result, _ = call("announce", "test", duck_volume=10)
    assert (result.returncode != 0) == fail
    assert (path / "mixer-writes").read_text().splitlines() == ["mysoftvol 10%", "mysoftvol 45%"]
    assert not (path / "xiaoai_bridge.duck.json").exists()


def test_duck_cancel_restores_and_deferred_ha_volume_is_not_applied_early(
    text_device, speaker_shell
):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    proc = subprocess.Popen(
        [
            *speaker_shell,
            str(path / "bridge/lib/text.sh"),
            "announce",
            json.dumps({"text": "test", "duck_volume": 10}),
        ],
        env={**env, "TTS_SLEEP": "2"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert "synthesizing" in proc.stdout.readline()
    update = subprocess.run(
        [*speaker_shell, "-c", f'. "{path / "bridge/lib/common.sh"}";volume_write master 45'],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert update.returncode == 0, update.stderr
    assert json.loads(update.stdout)["deferred"]
    assert (path / "mixer-writes").read_text().splitlines() == ["mysoftvol 10%"]
    proc.terminate()
    proc.communicate(timeout=6)
    assert (path / "mixer-writes").read_text().splitlines()[-1] == "mysoftvol 45%"
    assert not (path / "xiaoai_bridge.duck.json").exists()


@pytest.mark.parametrize("cleanup_first", [False, True])
@pytest.mark.parametrize("new_target", [False, True])
def test_duck_voice_handoff_preserves_pre_duck_value_in_both_orders(
    text_device, speaker_shell, cleanup_first, new_target
):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    hooks = path / "bridge/lib/listener-hooks.sh"
    # Model the two possible volume-lock orders, using the production functions.
    body = f'''. "{path / "bridge/lib/common.sh"}"
. "{path / "bridge/lib/ducking.sh"}"
MODE=announce
DUCK_VOLUME=10
DUCK_VERIFIED=true
bridge_duck_begin
{"volume_write master 45 >/dev/null" if new_target else ":"}
printf '%s\\n' "$$" > "$BRIDGE_RUNTIME/xiaoai_bridge.voice"
{"bridge_duck_restore" if cleanup_first else ":"}
. "{hooks}"
bridge_voice_begin || exit 1
[ "$BRIDGE_MASTER_SAVED" = {45 if new_target else 60} ] || exit 77
{":" if cleanup_first else "bridge_duck_restore"}
SAVED_VOL=$(percent mysoftvol)
bridge_restore_volume
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=8
    )
    assert result.returncode == 0, result.stderr
    assert (path / "mixer-writes").read_text().splitlines() == [
        "mysoftvol 10%",
        f"mysoftvol {45 if new_target else 60}%",
        "bluetooth 60%",
    ]
    assert not (path / "xiaoai_bridge.duck.json").exists()


def test_feedback_yields_to_voice_and_restores_prior_manual_led(text_device, speaker_shell):
    _, path, env = text_device
    led = path / "led_rgb"
    led.write_text("")
    feedback = path / "bridge/lib/task-feedback.sh"
    feedback.write_text(feedback.read_text().replace("/sys/devices/i2c-0/0-003a/led_rgb", str(led)))
    (path / "xiaoai_bridge.led.json").write_text('{"on":true,"rgb":[1,2,3],"brightness":255}')
    body = f'''. "{path / "bridge/lib/common.sh"}"
. "{feedback}"
MODE=send_text
TASK_LED_VERIFIED=true
bridge_feedback_setup
bridge_feedback_stage processing
bridge_led_busy || exit 78
cp "{led}" "$STATE_DIR/processing-color"
bridge_feedback_end
cp "{led}" "$STATE_DIR/restored-color"
bridge_feedback_stage synthesizing
printf '%s\\n' "$$" > "$BRIDGE_RUNTIME/xiaoai_bridge.voice"
printf sentinel > "{led}"
bridge_feedback_stage playing
bridge_feedback_end
'''
    result = subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert (path / "processing-color").read_text().strip() == "17 9457664"
    assert (path / "restored-color").read_text().strip() == "17 66051"
    assert led.read_text() == "sentinel"
    assert not (path / "xiaoai_bridge.feedback").exists()


@pytest.mark.parametrize("task_led", [None, False, True])
def test_silent_remote_text_has_opt_in_processing_but_no_speech_phases(text_device, task_led):
    call, path, env = text_device
    led = path / "led_rgb"
    led.write_text("")
    feedback = path / "bridge/lib/task-feedback.sh"
    feedback.write_text(feedback.read_text().replace("/sys/devices/i2c-0/0-003a/led_rgb", str(led)))
    config = path / "config.json"
    values = json.loads(config.read_text())
    values["verified"] = {"led": True}
    if task_led is not None:
        values["verified"]["task_led"] = task_led
    config.write_text(json.dumps(values))
    result, _ = call("send_text", "test", speak_response=False)
    assert result.returncode == 0
    assert "synthesizing" not in result.stdout and "playing" not in result.stdout
    assert led.read_text().strip() == ("17 0" if task_led else "")
    assert not (path / "xiaoai_bridge.feedback").exists()


def test_multiline_request_remains_literal_and_profile_has_no_text(text_device, speaker_shell):
    call, path, env = text_device
    text = "line one\nline two with $(no_command) and `no_command`"
    pipeline = path / "bridge/lib/text.sh"
    result = subprocess.run(
        [*speaker_shell, str(pipeline), "announce", json.dumps({"text": text})],
        env={**env, "XIAOAI_PROFILE": "1"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert (path / "tts-calls").read_text().strip() == text
    trace = (path / "xiaoai_bridge.profile").read_text()
    assert "phase=task_parsed" in trace and "phase=tts_launch" in trace
    assert "line one" not in trace and "synthetic-token" not in trace
