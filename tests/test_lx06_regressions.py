"""LX06 structure and failure injection; all paths and hardware calls are local mocks."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from speaker.tools.prepare_adapters import prepare_airplay_led, prepare_kws_guard, prepare_listener
from tests.test_text_pipeline import enable_airplay_duck_fixture, script
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import text_device as text_device

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


def redirect_fixture(source, mapping):
    # One pass: Linux's /tmp targets must not be rewritten a second time.
    pattern = re.compile("|".join(re.escape(key) for key in sorted(mapping, key=len, reverse=True)))
    return pattern.sub(lambda match: str(mapping[match.group()]), source)


def run_shell(body, env, speaker_shell, timeout=10):
    return subprocess.run(
        [*speaker_shell, "-c", body], env=env, capture_output=True, text=True, timeout=timeout
    )


def test_real_launcher_retains_record_loading_end_and_initialization_order():
    source = (FIXTURES / "lx06_launcher.sh").read_text()
    prepared = prepare_listener(source)
    assert source.count("\n/bin/show_led 1\n") == 1
    assert "\n/bin/show_led 1\n" not in prepared
    assert prepared.count("\n/bin/show_led 2\n") == 1
    assert prepared.count("\n/bin/show_led 11\n") == 1
    assert prepared.index("\nget_stt_settings\n") < prepared.index(
        "\nbridge_listener_initialized ||"
    )
    assert prepared.index("\nbridge_listener_initialized ||") < prepared.index("\nkws_guard ||")
    assert prepared.index("\nbridge_voice_begin ||") < prepared.index("\nSAVED_VOL=")
    assert prepared.index("bridge_voice_attenuate") < prepared.index("${RECORD_COMMAND} | tee")
    assert prepared.index("${RECORD_COMMAND} | tee") < prepared.index("\n/bin/show_led 2\n")
    assert prepared.index("\n/bin/show_led 2\n") < prepared.index("bridge_process_text ||")
    assert prepared.index("\n/bin/show_led 11\n") < prepared.index("\nbridge_restore_volume\n")
    assert "for NUM in 1 2 3; do\n  /bin/shut_led ${NUM}" in prepared


def test_review_hashes_the_raw_crlf_source(tmp_path):
    launcher = tmp_path / "launcher"
    launcher.write_bytes((FIXTURES / "lx06_launcher.sh").read_bytes().replace(b"\n", b"\r\n"))
    output = tmp_path / "private-output"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "speaker/tools/prepare_adapters.py"),
            "--listener",
            str(launcher),
            "--kws-guard",
            str(FIXTURES / "lx06_kws_guard.sh"),
            "--airplay-led",
            str(FIXTURES / "lx06_airplay_led.sh"),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    review = json.loads((output / "review.json").read_text())
    assert review["format"] == 3
    assert (
        review["files"]["listener.sh"]["source_sha256"]
        == hashlib.sha256(launcher.read_bytes()).hexdigest()
    )
    assert b"\r" not in (output / "listener.sh").read_bytes()
    for name in ("listener.sh", "kws-guard.sh", "airplay-led.sh"):
        assert (
            review["files"][name]["output_sha256"]
            == hashlib.sha256((output / name).read_bytes()).hexdigest()
        )


def test_restore_lock_failure_releases_voice_audio_led_and_retries(text_device, speaker_shell):
    _, path, env = text_device
    hooks = path / "bridge/lib/listener-hooks.sh"
    body = f'''. "{hooks}"
bridge_voice_begin || exit 77
printf 'ready\\n'
read release
bridge_restore_volume; result=$?
printf '%s %s %s\\n' "$result" "$XIAOAI_VOICE_AUDIO_LOCK" "$XIAOAI_VOICE_LED_LOCK"
read retry
bridge_recover_pending || exit 78
'''
    proc = subprocess.Popen(
        [*speaker_shell, "-c", body],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline() == "ready\n"
        journal = path / "xiaoai_bridge.voice-volume.json"
        assert journal.exists()
        with (path / "xiaoai_bridge.volume.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            proc.stdin.write("release\n")
            proc.stdin.flush()
            assert proc.stdout.readline() == "1 0 0\n"
            assert journal.exists() and not (path / "xiaoai_bridge.voice").exists()
            for name in ("audio", "led"):
                with (path / f"xiaoai_bridge.{name}.lock").open("w") as released:
                    fcntl.flock(released, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (path / "xiaoai_bridge.desired.json").write_text(
                '{"revision":1,"versions":{"master":1},"volumes":{"master":45}}'
            )
            fcntl.flock(lock, fcntl.LOCK_UN)
        _, stderr = proc.communicate("retry\n", timeout=5)
        assert proc.returncode == 0, stderr
        assert not journal.exists()
        assert (path / "mixer-writes").read_text().splitlines()[-2:] == [
            "mysoftvol 45%",
            "bluetooth 60%",
        ]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_failed_audio_handoff_retains_and_recovers_original_duck(text_device, speaker_shell):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    body = f'''. "{path / "bridge/lib/listener-hooks.sh"}"
. "{path / "bridge/lib/ducking.sh"}"
MODE=announce; DUCK_VOLUME=10; DUCK_VERIFIED=true
bridge_duck_begin || exit 70
if bridge_voice_begin; then exit 71;fi
[ -r "$BRIDGE_RUNTIME/xiaoai_bridge.duck.json" ] || exit 72
[ "$XIAOAI_VOICE_AUDIO_LOCK" = 0 ] || exit 73
[ ! -f "$BRIDGE_RUNTIME/xiaoai_bridge.voice" ] || exit 74
bridge_duck_restore || exit 75
'''
    with (path / "xiaoai_bridge.audio.lock").open("w") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        result = run_shell(body, {**env, "XIAOAI_AUDIO_WAIT": "0"}, speaker_shell)
    assert result.returncode == 0, result.stderr
    assert (path / "mixer-mysoftvol").read_text().strip() == "60"
    assert not (path / "xiaoai_bridge.duck.json").exists()


@pytest.mark.parametrize("explicit_master", [False, True])
def test_sender_change_is_preserved_unless_new_ha_master_target(
    text_device, speaker_shell, explicit_master
):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
raw=-18
[ ! -f "$STATE_DIR/sender-raw" ] || read raw < "$STATE_DIR/sender-raw"
printf 'string "Active"\\nvariant boolean true\\nstring "Volume"\\nvariant double %s\\n' "$raw"
""",
    )
    body = f'''. "{path / "bridge/lib/common.sh"}"
bridge_volume_journal "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" '{{"master":60,"bluetooth":60}}'
amixer sset mysoftvol 10%
# Simulate Shairport's independent hardware write and changed DBus property.
printf '40\\n' > "$STATE_DIR/mixer-mysoftvol"
printf '%s\\n' -6 > "$STATE_DIR/sender-raw"
{'''printf '%s' '{"revision":1,"versions":{"master":1},"volumes":{"master":75}}' > "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json"''' if explicit_master else ":"}
bridge_volume_recover "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json"
'''
    result = run_shell(body, env, speaker_shell)
    assert result.returncode == 0, result.stderr
    assert (path / "mixer-mysoftvol").read_text().strip() == ("75" if explicit_master else "40")
    writes = (path / "mixer-writes").read_text().splitlines()
    assert "mysoftvol 60%" not in writes
    assert "bluetooth 60%" in writes


def test_mixer_failure_keeps_recovery_journal(text_device, speaker_shell):
    _, path, env = text_device
    mixer = path / "tools/amixer"
    mixer.write_text(
        mixer.read_text().replace(
            'if [ "$1" = get ];then',
            'if [ -f "$STATE_DIR/fail-mixer" ] && [ "$1" != get ]; then exit 1;fi\nif [ "$1" = get ];then',
        )
    )
    body = f'''. "{path / "bridge/lib/common.sh"}"
bridge_volume_journal "$BRIDGE_RUNTIME/xiaoai_bridge.tts-volume.json" '{{"tts":60}}'
: > "$STATE_DIR/fail-mixer"
if bridge_volume_recover "$BRIDGE_RUNTIME/xiaoai_bridge.tts-volume.json"; then exit 77;fi
[ -r "$BRIDGE_RUNTIME/xiaoai_bridge.tts-volume.json" ] || exit 78
rm "$STATE_DIR/fail-mixer"
bridge_volume_recover "$BRIDGE_RUNTIME/xiaoai_bridge.tts-volume.json"
'''
    result = run_shell(body, env, speaker_shell)
    assert result.returncode == 0, result.stderr
    assert not (path / "xiaoai_bridge.tts-volume.json").exists()


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="Linux process identity is validated in CI"
)
@pytest.mark.parametrize("player", ["miplay", "mpg123", "ogg123", "aplay"])
def test_tts_playing_observes_direct_provider_exec(text_device, player):
    call, path, env = text_device
    shutil.copy2(shutil.which("sleep"), path / "tools" / player)
    script(path / "existing-tts", f'#!/bin/sh\nsleep 0.3\nexec "$STATE_DIR/tools/{player}" 0.8\n')
    result, _ = call("announce", "synthetic")
    assert result.returncode == 0
    stages = [json.loads(line)["stage"] for line in result.stdout.splitlines() if '"stage"' in line]
    assert stages == ["synthesizing", "playing"]


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="Linux KWS identity is validated in CI"
)
def test_kws_reentry_cannot_remove_running_supervisor(text_device, speaker_shell):
    _, path, env = text_device
    bridge = path / "bridge"
    (bridge / "adapter").mkdir()
    guard = bridge / "adapter/kws-guard.sh"
    guard.write_text(
        prepare_kws_guard((FIXTURES / "lx06_kws_guard.sh").read_text())
        .replace("/tmp/kws_guard.lock", str(path / "kws_guard.lock"))
        .replace("/data/xiaoai_bridge/adapter/listener.sh", str(bridge / "adapter/listener.sh"))
        .replace("/data/kws_client", str(path / "kws_client"))
    )
    script(
        path / "kws_client",
        '#!/bin/sh\nprintf x >> "$STATE_DIR/kws-launches"\nwhile :; do sleep 1;done\n',
    )
    listener = bridge / "adapter/listener.sh"
    script(
        listener,
        f'. "{bridge}/lib/listener-hooks.sh"\n. "{guard}"\nbridge_listener_initialized || exit 1\nkws_guard\n',
    )
    owner = subprocess.Popen(
        [*speaker_shell, str(listener)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not (path / "kws-launches").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (path / "kws-launches").read_text() == "x"
        result = subprocess.run(
            [*speaker_shell, str(listener)], env=env, capture_output=True, text=True, timeout=3
        )
        assert result.returncode == 125
        assert int((path / "kws_guard.lock/owner").read_text().split()[0]) == owner.pid
        assert (path / "kws-launches").read_text() == "x"
    finally:
        owner.terminate()
        owner.communicate(timeout=6)
    assert not (path / "kws_guard.lock").exists()


def test_full_airplay_restore_is_gated_and_worker_recovers_stale_owner(text_device, speaker_shell):
    _, path, env = text_device
    bridge = path / "bridge"
    (bridge / "adapter").mkdir()
    led = path / "led-rgb"
    led.write_text("sentinel")
    script(path / "tools/listener-status", "exit 1\n")
    script(
        path / "tools/show_led", '#!/bin/sh\nprintf "show %s\\n" "$1" >> "$STATE_DIR/led-effects"\n'
    )
    script(
        path / "tools/shut_led", '#!/bin/sh\nprintf "shut %s\\n" "$1" >> "$STATE_DIR/led-effects"\n'
    )
    adapter = bridge / "adapter/airplay-led.sh"
    source = prepare_airplay_led((FIXTURES / "lx06_airplay_led.sh").read_text())
    source = (
        source.replace("/data/xiaoai_bridge", str(bridge))
        .replace("/tmp/airplay_led", str(path / "airplay_led"))
        .replace("/sys/devices/i2c-0/0-003a/led_rgb", str(led))
        .replace("/etc/init.d/listener", str(path / "tools/listener-status"))
    )
    script(adapter, source)
    lock = path / "airplay_led.lock"
    lock.mkdir()
    (lock / "owner").write_text("99999999\n")
    (path / "airplay_led.state").write_text("20 0\n")
    # A task owns LED: even the original restore's show/shut branch must yield.
    (path / "xiaoai_bridge.feedback").write_text(str(os.getpid()) + "\n")
    result = subprocess.run(
        [*speaker_shell, str(adapter), "--worker"],
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr
    assert led.read_text() == "sentinel" and not (path / "led-effects").exists()
    (path / "xiaoai_bridge.feedback").unlink()
    # A dead worker directory must not suppress future volume animations.
    lock.mkdir()
    (lock / "owner").write_text("99999999\n")
    result = subprocess.run(
        [*speaker_shell, str(adapter), "-18"], env=env, capture_output=True, text=True, timeout=3
    )
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 5
    while lock.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (path / "led-effects").read_text().splitlines() == ["shut 7", "show 7"]


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="Linux provider process trees are validated in CI"
)
@pytest.mark.parametrize("provider", ["doubao", "microsoft", "google"])
def test_full_real_tts_fallback_chain_reports_playing(text_device, provider):
    call, path, env = text_device
    data = path / "provider-data"
    data.mkdir()
    shutil.copy2(path / "existing-listener", data / "listener")
    (data / "doubao_tts.conf").write_text("DOUBAO_API_KEY=synthetic-only\n")
    for name in ("doubao", "microsoft", "google"):
        target = (
            data
            / {
                "doubao": "tts_doubao",
                "microsoft": "tts_microsoft_fallback",
                "google": "tts_google_zh",
            }[name]
        )
        source = (FIXTURES / f"lx06_tts_{name}.sh").read_text()
        script(
            target,
            redirect_fixture(
                source,
                {
                    "/data/": str(data) + "/",
                    "/tmp/": str(path) + "/",
                    "/bin/miplay": path / "tools/miplay",
                },
            ),
        )
    config = json.loads((path / "config.json").read_text())
    config["tts_path"] = str(data / "tts_doubao")
    (path / "config.json").write_text(json.dumps(config))
    script(path / "tools/miplay", '#!/bin/sh\nexec "$STATE_DIR/tools/mpg123" 0.8\n')
    # Retain direct mpg123 and miplay->mpg123 process structures. The local
    # decoder is named mpg123, while the PATH wrapper accepts original options.
    (path / "decoder-bin").mkdir()
    shutil.copy2(shutil.which("sleep"), path / "decoder-bin/mpg123")
    script(path / "tools/mpg123", '#!/bin/sh\nexec "$STATE_DIR/decoder-bin/mpg123" 0.8\n')
    script(
        path / "tools/ffmpeg",
        """#!/bin/sh
for output; do :;done
cat >/dev/null
printf synthetic-mp3 > "$output"
""",
    )
    curl = path / "tools/curl"
    curl.write_text(f"""#!{sys.executable}
import json,sys,time
from pathlib import Path
args=sys.argv[1:]
output=Path(args[args.index('-o')+1])
url=args[-1]
provider={provider!r}
time.sleep(0.15)
if url.endswith('/api/tts_get_url'):
    if provider=='google':sys.exit(1)
    output.write_text(json.dumps({{"path":"/api/tts_proxy/synthetic.wav"}}))
elif '/api/tts_proxy/' in url:
    output.write_bytes(b'RIFF'+b'x'*128)
elif output.name=='response.jsonl':
    if provider!='doubao':sys.exit(1)
    output.write_text('{{"code":0,"data":"c3ludGhldGlj"}}\\n{{"code":20000000}}\\n')
else:
    output.write_bytes(b'synthetic-mp3')
""")
    curl.chmod(0o755)
    result, _ = call("announce", "synthetic speech")
    assert result.returncode == 0
    stages = [json.loads(line)["stage"] for line in result.stdout.splitlines() if '"stage"' in line]
    assert stages == ["synthesizing", "playing"]


def test_empty_stt_early_restore_keeps_voice_ownership_and_new_targets(text_device, speaker_shell):
    _, path, env = text_device
    body = f'''. "{path / "bridge/lib/listener-hooks.sh"}"
bridge_voice_begin || exit 70
bridge_voice_attenuate 10 || exit 71
printf '%s' '{{"revision":1,"versions":{{"master":1}},"volumes":{{"master":45}}}}' > "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json"
# Exact call used by LX06's private miplay empty-STT branch.
SAVED_VOL=60
set_volume $SAVED_VOL || exit 72
[ "$XIAOAI_VOICE_AUDIO_LOCK" = 1 ] && [ "$XIAOAI_VOICE_LED_LOCK" = 1 ] || exit 73
[ -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" ] || exit 74
voice_busy || exit 75
printf '%s' '{{"revision":2,"versions":{{"master":2}},"volumes":{{"master":35}}}}' > "$BRIDGE_RUNTIME/xiaoai_bridge.desired.json"
bridge_restore_volume
'''
    result = run_shell(body, env, speaker_shell)
    assert result.returncode == 0, result.stderr
    assert (path / "mixer-writes").read_text().splitlines() == [
        "mysoftvol 10%",
        "bluetooth 10%",
        "mysoftvol 45%",
        "bluetooth 60%",
        "mysoftvol 35%",
        "bluetooth 60%",
    ]
    assert not (path / "xiaoai_bridge.voice-volume.json").exists()


def test_oauth_wait_observes_rotated_credentials_without_refreshing_again(
    text_device, speaker_shell, ha_http
):
    _, path, env = text_device
    lock = path / "ha_oauth_refresh.lock"
    lock.mkdir()
    ha_http.valid_token = "synthetic-rotated-token"
    pipeline = path / "bridge/lib/text.sh"
    proc = subprocess.Popen(
        [
            *speaker_shell,
            str(pipeline),
            "send_text",
            json.dumps({"text": "hello", "speak_response": False}),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not ha_http.conversations and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ha_http.conversations and proc.poll() is None
        listener = path / "existing-listener"
        listener.write_text(
            listener.read_text()
            .replace("HA_TOKEN=synthetic-token", "HA_TOKEN=synthetic-rotated-token")
            .replace(
                "HA_REFRESH_TOKEN=synthetic-refresh-token",
                "HA_REFRESH_TOKEN=synthetic-rotated-refresh",
            )
        )
        lock.rmdir()
        stdout, stderr = proc.communicate(timeout=5)
        assert proc.returncode == 0, stdout + stderr
        assert len(ha_http.conversations) == 2 and not ha_http.refreshes
        assert "synthetic-rotated-token" not in stdout + stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="Linux launcher/KWS identity is validated in CI"
)
def test_full_launcher_one_voice_cycle_keeps_original_led_and_record_order(
    text_device, speaker_shell
):
    _, path, env = text_device
    bridge = path / "bridge"
    (bridge / "adapter").mkdir()
    data = path / "launcher-data"
    data.mkdir()
    shutil.copy2(path / "existing-listener", data / "listener")
    guard = bridge / "adapter/kws-guard.sh"
    script(
        guard,
        prepare_kws_guard((FIXTURES / "lx06_kws_guard.sh").read_text())
        .replace("/data/kws_client", str(data / "kws_client"))
        .replace("/tmp/kws_guard.lock", str(path / "kws_guard.lock")),
    )
    script(data / "kws_client", "#!/bin/sh\nsleep 0.15\nexit 0\n")
    tools = path / "tools"
    script(tools / "api-service", "exit 0\n")
    script(tools / "uci", "printf LX06\n")
    script(tools / "which", 'case "$1" in sox|ffmpeg) exit 0 ;; *) exit 1 ;; esac\n')
    script(tools / "micocfg_build_time", "printf '2000-01-01'\n")
    real_date = shutil.which("date")
    script(
        tools / "date",
        f'#!/bin/sh\n[ "${{1:-}}" != -d ] || {{ printf 0;exit 0; }}\nexec "{real_date}" "$@"\n',
    )
    for name, body in {
        "show-led": 'printf "led %s\\n" "$1" >> "$STATE_DIR/flow"',
        "shut-led": 'printf "shut %s\\n" "$1" >> "$STATE_DIR/flow"',
        "miplay": 'printf "prompt\\n" >> "$STATE_DIR/flow"',
        "timeout": 'shift;exec "$@"',
        "rec": 'printf "record\\n" >> "$STATE_DIR/flow"\nprintf synthetic-pcm',
        "ffmpeg": "cat",
        "ubus": ":",
    }.items():
        script(tools / name, "#!/bin/sh\n" + body + "\n")
    real_curl = shutil.which("curl")
    script(
        tools / "curl",
        f"""#!{sys.executable}
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
if any('/api/stt/' in arg for arg in args):
    output=Path(args[args.index('-o')+1])
    recorded='-T' in args
    if recorded:sys.stdin.read()
    response={{"result":"success","text":"hello"}} if recorded else {{
      "sample_rates":[8000],"channels":[1],"bit_rates":[16],"languages":["en"],
      "formats":["wav"],"codecs":["pcm"]}}
    output.write_text(json.dumps(response))
    Path(args[args.index('-D')+1]).write_text('HTTP/1.1 200 OK\\n')
else:os.execv({real_curl!r},[{real_curl!r},*args])
""",
    )
    hooks = bridge / "lib/listener-hooks.sh"
    hooks.write_text(
        hooks.read_text()
        .replace("/bin/show_led", str(tools / "show-led"))
        .replace("/bin/shut_led", str(tools / "shut-led"))
    )
    listener = bridge / "adapter/listener.sh"
    source = prepare_listener((FIXTURES / "lx06_launcher.sh").read_text())
    source = redirect_fixture(
        source,
        {
            "/data/xiaoai_bridge": bridge,
            "/data/": str(data) + "/",
            "/tmp/": str(path) + "/",
            "/dev/kmsg": path / "kmsg",
            "/etc/init.d/api": tools / "api-service",
            "/bin/show_led": tools / "show-led",
            "/bin/shut_led": tools / "shut-led",
        },
    )
    # Stop after one complete original iteration; all init/function/pipeline
    # branches above it remain intact. No real device command is reachable.
    left, right = source.rsplit("\ndone\n", 1)
    script(listener, left + "\nbreak\ndone\n" + right)
    result = subprocess.run(
        [*speaker_shell, str(listener)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
    flow = (path / "flow").read_text().splitlines()
    assert [item for item in flow if item.startswith("led ")] == ["led 1", "led 2", "led 11"]
    assert (
        flow.index("led 1")
        < flow.index("prompt")
        < flow.index("record")
        < flow.index("led 2")
        < flow.index("led 11")
    )
    assert (path / "tts-calls").read_text().strip() == "reply: hello"
    assert not (path / "xiaoai_bridge.voice").exists()
    assert not (path / "xiaoai_bridge.voice_ready").exists()
    assert not (path / "kws_guard.lock").exists()


def test_ambiguous_sender_cap_keeps_master_journal_but_restores_other_channel(
    text_device, speaker_shell
):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
raw=-18
[ ! -f "$STATE_DIR/sender-raw" ] || read raw < "$STATE_DIR/sender-raw"
printf 'string "Active"\\nvariant boolean true\\nstring "Volume"\\nvariant double %s\\n' "$raw"
""",
    )
    body = f'''. "{path / "bridge/lib/listener-hooks.sh"}"
bridge_voice_begin || exit 70
bridge_voice_attenuate 10 || exit 71
# Sender property changed immediately before our hardware cap; the current
# master still equals our cap, so its intended hardware level is unknown.
printf '%s\\n' -6 > "$STATE_DIR/sender-raw"
if bridge_restore_volume; then exit 72;fi
bridge_has_pending_recovery || exit 73
jq -e '.volumes=={{"master":60}}' "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" >/dev/null || exit 74
[ "$(percent bluetooth)" = 60 ] || exit 75
volume_write master 55 >/dev/null || exit 76
bridge_recover_pending || exit 77
'''
    result = run_shell(body, env, speaker_shell, timeout=12)
    assert result.returncode == 0, result.stderr
    assert (path / "mixer-mysoftvol").read_text().strip() == "55"
    assert not (path / "xiaoai_bridge.voice-volume.json").exists()


def test_sender_changed_before_duck_handoff_uses_current_hardware_baseline(
    text_device, speaker_shell
):
    _, path, env = text_device
    enable_airplay_duck_fixture(path)
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
raw=-18
[ ! -f "$STATE_DIR/sender-raw" ] || read raw < "$STATE_DIR/sender-raw"
printf 'string "Active"\\nvariant boolean true\\nstring "Volume"\\nvariant double %s\\n' "$raw"
""",
    )
    body = f'''. "{path / "bridge/lib/listener-hooks.sh"}"
. "{path / "bridge/lib/ducking.sh"}"
MODE=announce;DUCK_VOLUME=10;DUCK_VERIFIED=true
bridge_duck_begin
printf '%s\\n' -6 > "$STATE_DIR/sender-raw"
printf '40\\n' > "$STATE_DIR/mixer-mysoftvol"
bridge_voice_begin || exit 70
[ "$BRIDGE_MASTER_SAVED" = 40 ] || exit 71
bridge_voice_attenuate 10
bridge_restore_volume
'''
    result = run_shell(body, env, speaker_shell)
    assert result.returncode == 0, result.stderr
    assert (path / "mixer-mysoftvol").read_text().strip() == "40"


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(),
    reason="Linux PID reuse and command identity are validated in CI",
)
def test_ready_rejects_alive_unrelated_pid_and_markers_reject_wrong_start(
    text_device, speaker_shell
):
    _, path, env = text_device
    unrelated = os.getpid()
    start = Path(f"/proc/{unrelated}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    (path / "xiaoai_bridge.voice_ready").write_text(f"{unrelated} {start} 3\n")
    (path / "xiaoai_bridge.voice").write_text(f"{unrelated} 0\n")
    body = f'''. "{path / "bridge/lib/common.sh"}"
if hooks_ready; then exit 70;fi
if voice_busy; then exit 71;fi
'''
    result = run_shell(body, env, speaker_shell)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("refresh_error", [False, True])
def test_generated_launcher_refresh_shares_config_lock_and_cleans_init_failure(
    text_device, speaker_shell, ha_http, refresh_error
):
    _, path, env = text_device
    ha_http.refresh_error = refresh_error
    prepared = prepare_listener((FIXTURES / "lx06_launcher.sh").read_text())
    begin = prepared.index("bridge_original_ha_refresh_token() {")
    original = prepared[begin : prepared.index("\n}\n", begin) + 2]
    begin = prepared.index("\nha_refresh_token() {")
    wrapper = prepared[begin : prepared.index("\n}\n", begin) + 2]
    service = path / "tools/listener-service"
    script(service, '#!/bin/sh\nprintf stopped > "$STATE_DIR/refresh-stop"\n')
    script(path / "tools/miplay", "#!/bin/sh\nexit 0\n")
    original = original.replace("/tmp/ha_token.json", str(path / "ha_token.json"))
    original = original.replace("/etc/init.d/listener", str(service))
    body = f'''. "{path / "bridge/lib/listener-hooks.sh"}"
. "{path / "existing-listener"}"
CONFIG_FILE='{path / "existing-listener"}'
log() {{ :; }}
{original}
{wrapper}
ha_refresh_token
'''
    with (path / "xiaoai_bridge.config.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        proc = subprocess.Popen(
            [*speaker_shell, "-c", body],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not (path / "ha_oauth_refresh.lock").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (path / "ha_oauth_refresh.lock").exists()
            assert not ha_http.refreshes and proc.poll() is None
            fcntl.flock(lock, fcntl.LOCK_UN)
            stdout, stderr = proc.communicate(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    assert (proc.returncode != 0) == refresh_error, stdout + stderr
    assert len(ha_http.refreshes) == 1
    assert not (path / "ha_oauth_refresh.lock").exists()
    assert not (path / "xiaoai_bridge.voice_ready").exists()
    assert (path / "refresh-stop").exists() == refresh_error
    if not refresh_error:
        assert "HA_TOKEN=synthetic-refreshed" in (path / "existing-listener").read_text()


def test_dead_tts_restore_is_independent_of_active_voice_master(text_device, speaker_shell):
    _, path, env = text_device
    body = f'''. "{path / "bridge/lib/listener-hooks.sh"}"
bridge_voice_begin || exit 70
bridge_voice_attenuate 10 || exit 71
printf '%s' '{{"owner":0,"start":"","revision":0,"volumes":{{"tts":60}},"airplay_raw":"","airplay_active":"false"}}' > "$BRIDGE_RUNTIME/xiaoai_bridge.tts-volume.json"
# Master remains voice-owned; recovery still services dead independent TTS.
if bridge_recover_pending; then exit 72;fi
[ ! -f "$BRIDGE_RUNTIME/xiaoai_bridge.tts-volume.json" ] || exit 73
voice_busy || exit 74
[ -r "$BRIDGE_RUNTIME/xiaoai_bridge.voice-volume.json" ] || exit 75
bridge_restore_volume
'''
    result = run_shell(body, env, speaker_shell)
    assert result.returncode == 0, result.stderr
    assert "notifyvol 60%" in (path / "mixer-writes").read_text().splitlines()
