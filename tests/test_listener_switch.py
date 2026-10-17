"""Native mute toggles, explicit HA targets, and the existing KWS bootstrap."""

import asyncio
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.xiaoai_speaker.client import BridgeError
from custom_components.xiaoai_speaker.const import DOMAIN
from speaker.tools.prepare_adapters import prepare_kws_guard
from tests.test_capabilities import setup
from tests.test_homeassistant import INFO, STATE
from tests.test_homeassistant import mock_clients as mock_clients
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import script
from tests.test_text_pipeline import text_device as text_device

ROOT = Path(__file__).resolve().parents[1]

# Structural rc.common/procd fixture, following the read-only LX06 init audit:
# toggle decides from the PID; start/stop own sound and LED 7; start uses the
# original porcupine_launcher, which imports /data/listener before KWS bootstrap.
# No real service, audio, LED, or private configuration is reachable here.
NATIVE = """#!/bin/sh
CONFIG="$STATE_DIR/data/listener"
SILENT=${SILENT:-0}
status() { "$STATE_DIR/tools/procd-fixture" status; }
start() {
    [ -f "$CONFIG" ] || return 0
    printf 'shut 7\\n' >> "$STATE_DIR/native-led"
    [ "$SILENT" = 1 ] || printf 'unmute\\n' >> "$STATE_DIR/native-sounds"
    "$STATE_DIR/tools/procd-fixture" start "$STATE_DIR/porcupine_launcher"
}
stop() {
    . "$CONFIG"
    [ "${MUTE_LED:-1}" = 0 ] || printf 'show 7\\n' >> "$STATE_DIR/native-led"
    [ "$SILENT" = 1 ] || printf 'mute\\n' >> "$STATE_DIR/native-sounds"
    "$STATE_DIR/tools/procd-fixture" stop
}
toggle() { if status; then stop;else start;fi; }
printf '%s\\n' "$1" >> "$STATE_DIR/native-calls"
if [ "$1" = status ]; then
    [ ! -f "$STATE_DIR/status-error" ] || exit 2
    status;exit $?
fi
if [ "$1" = toggle ]; then
    [ ! -f "$STATE_DIR/toggle-error" ] || exit 7
    [ ! -f "$STATE_DIR/toggle-noop" ] || exit 0
    if [ -f "$STATE_DIR/hold-toggle" ]; then
        : > "$STATE_DIR/toggle-entered"
        while [ ! -f "$STATE_DIR/release-toggle" ]; do sleep 0.01;done
    fi
fi
"$1"
"""

PROCD = """import json,os,signal,subprocess,sys,time
from pathlib import Path
root=Path(os.environ['STATE_DIR'])
pidfile=root/'listener.pid'
def running(pid):
    try:
        stat=Path(f'/proc/{pid}/stat')
        if stat.exists() and stat.read_text().rsplit(')',1)[1].split()[0] in ('Z','X'):
            return False
        os.kill(pid,0)
        return True
    except ProcessLookupError:return False
action=sys.argv[1]
if action=='status':
    sys.exit(0 if pidfile.exists() and running(int(pidfile.read_text())) else 1)
if action=='start':
    args=sys.argv[2:]
    if args[0]=='/bin/sh':args=json.loads(os.environ['FIXTURE_SHELL'])+args[1:]
    with (root/'listener-stderr').open('ab') as errors:
        child=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                               stderr=errors,start_new_session=True,close_fds=True)
    pidfile.write_text(str(child.pid))
    with (root/'owned-pids').open('a') as stream:stream.write(str(child.pid)+'\\n')
elif action=='stop' and pidfile.exists():
    pid=int(pidfile.read_text())
    assert str(pid) in (root/'owned-pids').read_text().splitlines()
    if running(pid):os.kill(pid,signal.SIGTERM)
    deadline=time.monotonic()+8
    while running(pid) and time.monotonic()<deadline:time.sleep(0.01)
    if running(pid):raise RuntimeError('fixture listener did not stop')
    pidfile.unlink()
"""


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "isolated Listener did not reach expected state"
        time.sleep(0.12)


def lines(path):
    return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def tmp_path():
    # Keep the real C event socket below sockaddr_un's path limit on macOS too.
    with tempfile.TemporaryDirectory(prefix="listener-", dir="/tmp") as folder:
        yield Path(folder)


@pytest.fixture
def listener_device(text_device, speaker_shell, busybox_binary):
    _, path, original_env = text_device
    env = {
        **original_env,
        "FIXTURE_SHELL": json.dumps(speaker_shell),
        "XIAOAI_LISTENER_WAIT": "300",
    }
    bridge = path / "bridge"
    data = path / "data"
    data.mkdir()
    config = Path(env["XIAOAI_BRIDGE_CONFIG"])
    config.write_text(
        json.dumps({**json.loads(config.read_text()), "enable_listener_adapter": True})
    )
    (bridge / "config.json").symlink_to(config)
    (bridge / "adapter").mkdir()
    script(path / "tools/procd-fixture", f"#!{sys.executable}\n{PROCD}")
    script(path / "native-listener", NATIVE)
    script(
        path / "tools/ubus",
        f"""#!{sys.executable}
import json,os,subprocess,sys
from pathlib import Path
assert sys.argv[1:4]==['call','service','set']
value=json.loads(sys.argv[4])
assert value['name']=='listener'
command=value['instances']['instance1']['command']
assert command==['/bin/sh',os.environ['XIAOAI_BRIDGE_DIR']+'/adapter/listener.sh']
with (Path(os.environ['STATE_DIR'])/'ubus-starts').open('a') as stream:stream.write('start\\n')
subprocess.run([os.environ['STATE_DIR']+'/tools/procd-fixture','start',*command],check=True)
""",
    )
    script(
        path / "porcupine_launcher",
        f"""#!/bin/sh
CONFIG_FILE='{data}/listener'
[ ! -f "$CONFIG_FILE" ] || . "$CONFIG_FILE"
: > "$STATE_DIR/unadapted-launcher"
exec sleep 300
""",
    )
    script(
        data / "kws_bootstrap.sh",
        '[ "${0##*/}" != porcupine_launcher ] || : > "$STATE_DIR/original-kws-bootstrap"\n',
    )
    listener_config = data / "listener"
    listener_config.write_text(f". {bridge}/lib/bootstrap.sh\n. {data}/kws_bootstrap.sh\n")
    bootstrap = bridge / "lib/bootstrap.sh"
    bootstrap.write_text(bootstrap.read_text().replace("/data/xiaoai_bridge", str(bridge)))
    script(
        bridge / "adapter/kws-guard.sh",
        prepare_kws_guard((ROOT / "tests/fixtures/lx06_kws_guard.sh").read_text())
        .replace("/data/kws_client", str(data / "kws_client"))
        .replace("/tmp/kws_guard.lock", str(path / "kws_guard.lock"))
        .replace("/data/xiaoai_bridge/adapter/listener.sh", str(bridge / "adapter/listener.sh")),
    )
    script(data / "kws_client", 'printf "started\\n" >> "$STATE_DIR/kws-starts"\nexec sleep 300\n')
    script(
        bridge / "adapter/listener.sh",
        f"""#!/bin/sh
. '{bridge}/lib/listener-hooks.sh'
. '{bridge}/adapter/kws-guard.sh'
bridge_listener_initialized || exit 1
kws_guard
""",
    )
    if sys.platform == "darwin":
        # Linux CI uses real /proc identity; macOS only substitutes that read.
        common = bridge / "lib/common.sh"
        common.write_text(
            common.read_text().replace('"/proc/$1/cmdline"', '"$STATE_DIR/listener-cmdline"')
        )
        (path / "listener-cmdline").write_bytes(os.fsencode(bridge / "adapter/listener.sh") + b"\0")
    for name in ("device.sh", "listener-service.sh"):
        file = bridge / "lib" / name
        source = file.read_text().replace("/etc/init.d/listener", str(path / "native-listener"))
        source = source.replace("/var/run/listener.pid", str(path / "listener.pid"))
        source = source.replace("/data/listener", str(listener_config))
        source = source.replace("/data/kws_bootstrap.sh", str(data / "kws_bootstrap.sh"))
        # Keep the production iteration bounds, shorten fixture sleep units.
        source = source.replace("sleep 1;", "sleep 0.02;")
        if name == "device.sh":
            source = source.replace("/proc/meminfo", str(path / "meminfo"))
            source = source.replace("/proc/loadavg", str(path / "loadavg"))
        script(file, source)
    (path / "meminfo").write_text("MemAvailable: 50000 kB\n")
    (path / "loadavg").write_text("0.1 0 0\n")
    script(path / "tools/bluetoothctl", "#!/bin/sh\nexit 1\n")
    if busybox_binary:
        for file in (
            path / "native-listener",
            path / "porcupine_launcher",
            bridge / "adapter/listener.sh",
            bridge / "lib/listener-service.sh",
            bridge / "lib/device.sh",
        ):
            file.write_text(file.read_text().replace("#!/bin/sh\n", f"#!{busybox_binary} ash\n", 1))

    def control(action, *, user=True, **extra_env):
        command = (
            [*speaker_shell, str(bridge / "lib/device.sh"), "listener"]
            if user
            else [*speaker_shell, str(bridge / "lib/listener-service.sh"), action]
        )
        return subprocess.run(
            command,
            input=json.dumps({"action": action}),
            env={**env, **extra_env},
            text=True,
            capture_output=True,
            timeout=15,
        )

    def physical():
        return subprocess.run(
            [*speaker_shell, str(path / "native-listener"), "toggle"],
            env=env,
            capture_output=True,
            text=True,
            timeout=12,
        )

    try:
        yield control, physical, path, env
    finally:
        (path / "release-toggle").touch()
        subprocess.run(
            [str(path / "tools/procd-fixture"), "stop"], env=env, capture_output=True, timeout=10
        )
        for value in lines(path / "owned-pids"):
            pid = int(value)
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def toggles(path):
    return lines(path / "native-calls").count("toggle")


def test_explicit_targets_reuse_native_toggle_and_preserve_ready_kws(listener_device):
    control, physical, path, _ = listener_device
    assert control("stop").returncode == 0 and toggles(path) == 0
    for _ in range(2):
        assert physical().returncode == 0
        assert control("start").returncode == 0
        assert control("start").returncode == 0
        assert control("stop").returncode == 0
        assert control("stop").returncode == 0
        assert not (path / "xiaoai_bridge.voice_ready").exists()
    assert toggles(path) == 4
    assert lines(path / "native-sounds") == ["unmute", "mute"] * 2
    assert lines(path / "native-led") == ["shut 7", "show 7"] * 2
    assert lines(path / "kws-starts") == ["started"] * 2
    assert not (path / "unadapted-launcher").exists()
    assert not (path / "original-kws-bootstrap").exists()
    assert not (path / "ubus-starts").exists()


def test_ha_on_itself_uses_bootstrap_not_direct_procd_replacement(listener_device):
    control, _, path, env = listener_device
    assert control("start", SILENT="1", MUTE_LED="0").returncode == 0
    ready = (path / "xiaoai_bridge.voice_ready").read_text().split()
    assert ready[0] == (path / "listener.pid").read_text() and ready[2] == "4"
    result = subprocess.run(
        [*json.loads(env["FIXTURE_SHELL"]), "-c", f'. "{path}/bridge/lib/common.sh"\nhooks_ready'],
        env=env,
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 0
    assert control("stop", SILENT="1", MUTE_LED="0").returncode == 0
    assert lines(path / "native-sounds") == ["unmute", "mute"]
    assert lines(path / "native-led") == ["shut 7", "show 7"]
    assert toggles(path) == 2 and not (path / "ubus-starts").exists()


@pytest.mark.parametrize("damage", ["missing-adapter", "missing-include", "late-include"])
def test_native_resume_cannot_fall_back_past_missing_adapter_activation(listener_device, damage):
    control, _, path, _ = listener_device
    if damage == "missing-adapter":
        (path / "bridge/adapter/listener.sh").unlink()
    else:
        config = path / "data/listener"
        config.write_text(
            "\n".join(reversed(config.read_text().splitlines())) + "\n"
            if damage == "late-include"
            else ""
        )
    result = control("start")
    assert (
        result.returncode != 0 and json.loads(result.stdout)["error"] == "listener_control_failed"
    )
    assert toggles(path) == 0 and not (path / "listener.pid").exists()
    assert control("stop").returncode == 0 and toggles(path) == 0


def test_running_pid_without_adapter_readiness_cannot_satisfy_on(listener_device):
    control, _, path, _ = listener_device
    script(path / "bridge/adapter/listener.sh", "#!/bin/sh\nexec sleep 300\n")
    for _ in range(2):
        result = control("start", XIAOAI_LISTENER_WAIT="0")
        assert result.returncode != 0 and json.loads(result.stdout) == {
            "error": "listener_control_failed"
        }
    assert toggles(path) == 1 and (path / "listener.pid").exists()
    assert not (path / "xiaoai_bridge.voice_ready").exists()


def test_activation_check_preserves_valid_trailing_whitespace(listener_device):
    control, _, path, _ = listener_device
    config = path / "data/listener"
    config.write_text("\n".join(line + " \t" for line in config.read_text().splitlines()) + "\n")
    assert control("start").returncode == 0 and toggles(path) == 1
    assert (path / "xiaoai_bridge.voice_ready").exists()
    assert not (path / "unadapted-launcher").exists()


def test_ready_adapter_must_belong_to_the_native_service_pid(listener_device):
    control, _, path, _ = listener_device
    assert control("start", user=False).returncode == 0
    pidfile = path / "listener.pid"
    original = pidfile.read_text()
    unrelated = subprocess.Popen(["sleep", "300"])
    try:
        pidfile.write_text(str(unrelated.pid))
        result = control("start", XIAOAI_LISTENER_WAIT="0")
        assert result.returncode != 0 and json.loads(result.stdout) == {
            "error": "listener_control_failed"
        }
        assert toggles(path) == 0
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)
        pidfile.write_text(original)


def test_post_toggle_status_failure_does_not_trigger_a_second_toggle(listener_device):
    control, _, path, _ = listener_device
    native = path / "native-listener"
    native.write_text(
        native.read_text().replace(
            '\n"$1"\n',
            '\n"$1"\nresult=$?\n[ "$1" != toggle ] || : > "$STATE_DIR/status-error"\nexit "$result"\n',
        )
    )
    result = control("start")
    assert result.returncode != 0 and json.loads(result.stdout) == {
        "error": "listener_control_failed"
    }
    assert toggles(path) == 1
    (path / "status-error").unlink()
    assert control("start").returncode == 0 and toggles(path) == 1


def test_physical_key_racing_the_ha_target_is_verified_without_blind_retoggle(listener_device):
    control, physical, path, _ = listener_device
    (path / "hold-toggle").touch()
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(control, "start", XIAOAI_LISTENER_WAIT="0")
        try:
            wait_for(lambda: (path / "toggle-entered").exists())
            (path / "hold-toggle").unlink()
            assert physical().returncode == 0
        finally:
            (path / "release-toggle").touch()
        result = running.result(timeout=10)
    assert result.returncode != 0 and json.loads(result.stdout) == {
        "error": "listener_control_failed"
    }
    assert toggles(path) == 2 and not (path / "listener.pid").exists()
    assert control("start").returncode == 0 and toggles(path) == 3


@pytest.mark.parametrize("action", ["start", "stop"])
@pytest.mark.parametrize("fault", ["status-error", "toggle-error", "toggle-noop"])
def test_unknown_failed_or_wrong_post_state_is_never_reported_completed(
    listener_device, action, fault
):
    control, _, path, _ = listener_device
    if action == "stop":
        assert control("start").returncode == 0
    previous = toggles(path)
    (path / fault).touch()
    result = control(action, XIAOAI_LISTENER_WAIT="0")
    assert result.returncode != 0
    assert json.loads(result.stdout) == {"error": "listener_control_failed"}
    assert toggles(path) == previous + (0 if fault == "status-error" else 1)


@pytest.mark.parametrize("adapted", [True, False])
def test_internal_start_stop_restart_remain_silent(listener_device, adapted):
    control, _, path, env = listener_device
    config = Path(env["XIAOAI_BRIDGE_CONFIG"])
    config.write_text(
        json.dumps({**json.loads(config.read_text()), "enable_listener_adapter": adapted})
    )
    for action in ("start", "start", "restart", "stop"):
        result = control(action, user=False)
        assert result.returncode == 0, result.stderr
    assert toggles(path) == 0 and not (path / "native-sounds").exists()
    if adapted:
        assert lines(path / "ubus-starts") == ["start", "start"]


def test_api_restart_keeps_the_existing_silent_maintenance_path(listener_device):
    control, _, path, _ = listener_device
    assert control("start", user=False).returncode == 0
    assert control("restart").returncode == 0
    assert toggles(path) == 0 and not (path / "native-sounds").exists()
    assert lines(path / "ubus-starts") == ["start", "start"]


def test_original_without_enabled_adapter_keeps_native_behavior(listener_device):
    control, _, path, env = listener_device
    config = Path(env["XIAOAI_BRIDGE_CONFIG"])
    config.write_text(
        json.dumps({**json.loads(config.read_text()), "enable_listener_adapter": False})
    )
    assert control("start").returncode == 0
    wait_for(lambda: (path / "unadapted-launcher").exists())
    assert control("start").returncode == 0
    assert control("stop").returncode == 0
    assert toggles(path) == 2
    assert lines(path / "native-sounds") == ["unmute", "mute"]


@pytest.mark.parametrize("action", ["start", "stop"])
def test_voice_busy_still_rejects_ha_control_without_native_effects(listener_device, action):
    control, _, path, env = listener_device
    result = subprocess.run(
        [
            *json.loads(env["FIXTURE_SHELL"]),
            "-c",
            f'. "{path}/bridge/lib/common.sh"\nbridge_pid_start {os.getpid()}',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
    )
    (path / "xiaoai_bridge.voice").write_text(f"{os.getpid()} {result.stdout.strip()}\n")
    result = control(action)
    assert result.returncode != 0 and json.loads(result.stdout) == {"error": "busy"}
    assert not (path / "native-calls").exists()


@pytest.fixture
def listener_bridge(listener_device, native_json_binary, socket_enabled):
    control, physical, path, env = listener_device
    config = Path(env["XIAOAI_BRIDGE_CONFIG"])
    token = "synthetic-listener-test-token-not-a-real-credential"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config.write_text(
        json.dumps(
            {
                **json.loads(config.read_text()),
                "device_id": "synthetic-speaker",
                "bind": "127.0.0.1",
                "port": port,
                "token": token,
                "runtime_dir": str(path),
                "adapter": str(path / "bridge/lib/device.sh"),
            }
        )
    )
    config.chmod(0o600)
    proc = subprocess.Popen(
        [str(native_json_binary), "--config", str(config)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    def call(endpoint, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(
                "GET" if payload is None else "POST",
                endpoint,
                body=None if payload is None else json.dumps(payload),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def ready():
        try:
            return call("/api/health")[0] == 200
        except OSError:
            return False

    try:
        wait_for(ready)
        yield call, physical, path
    finally:
        (path / "release-toggle").touch()
        proc.terminate()
        proc.wait(timeout=8)
        assert token.encode() not in proc.stderr.read()
        proc.stderr.close()


def test_listener_api_preserves_request_deduplication_across_physical_changes(listener_bridge):
    call, physical, path = listener_bridge
    payload = {"request_id": "listener-on", "action": "start"}
    assert call("/api/listener", payload) == (200, {"completed": True})
    assert toggles(path) == 1
    assert physical().returncode == 0
    assert call("/api/listener", payload) == (
        200,
        {"request_id": "listener-on", "status": "completed", "completed": True},
    )
    assert toggles(path) == 2 and not (path / "listener.pid").exists()
    assert call("/api/listener", {**payload, "request_id": "fresh-on"})[0] == 200
    assert toggles(path) == 3


def test_listener_api_keeps_install_and_control_worker_busy_guards(listener_bridge):
    call, _, path = listener_bridge
    with (path / "xiaoai_bridge.install.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert call("/api/listener", {"request_id": "install-busy", "action": "start"}) == (
            503,
            {"error": "installation_in_progress"},
        )
    assert toggles(path) == 0
    (path / "hold-toggle").touch()
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(call, "/api/listener", {"request_id": "first", "action": "start"})
        try:
            wait_for(lambda: (path / "toggle-entered").exists())
            assert call("/api/listener", {"request_id": "second", "action": "stop"}) == (
                409,
                {"error": "busy"},
            )
        finally:
            (path / "release-toggle").touch()
        assert running.result(timeout=10) == (200, {"completed": True})
    assert toggles(path) == 1


async def test_native_ha_switch_and_physical_key_alternate_with_real_probe_state(
    listener_bridge, hass, mock_clients
):
    call, physical, path = listener_bridge
    samples = copy.deepcopy((INFO, STATE))
    # The coordinator replaces info.capabilities after the real Probe. Do not
    # let this device's capabilities alter the shared samples of later tests.
    with (
        patch(
            "custom_components.xiaoai_speaker.client.BridgeClient.info",
            AsyncMock(return_value=copy.deepcopy(INFO)),
        ),
        patch(
            "custom_components.xiaoai_speaker.client.BridgeClient.state",
            AsyncMock(return_value=copy.deepcopy(STATE)),
        ),
    ):
        entry, c = await setup(hass)

    async def request(method, endpoint, payload=None, **kwargs):
        status, value = await asyncio.to_thread(call, endpoint, payload)
        if status >= 400:
            raise BridgeError(value["error"])
        return value

    async def state():
        return await request("GET", "/api/state")

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("switch", DOMAIN, "synthetic-speaker_listener")
    assert entity_id
    registry.async_update_entity(entity_id, new_entity_id="switch.custom_listener")
    entity_id = "switch.custom_listener"
    try:
        with (
            patch.object(c.client, "request", AsyncMock(side_effect=request)),
            patch.object(c.client, "state", AsyncMock(side_effect=state)),
        ):
            await c.async_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(entity_id).state == "off"

            async def set_target(running):
                await hass.services.async_call(
                    "switch",
                    "turn_on" if running else "turn_off",
                    {"entity_id": entity_id},
                    blocking=True,
                )
                await asyncio.to_thread(
                    wait_for, lambda: call("/api/state")[1]["listener"]["running"] is running
                )
                await c.async_refresh()
                await hass.async_block_till_done()
                assert hass.states.get(entity_id).state == ("on" if running else "off")

            await set_target(True)
            await set_target(True)
            assert toggles(path) == 1
            assert (await asyncio.to_thread(physical)).returncode == 0
            # No mutation or extra poller: the existing 15s device Probe must
            # observe the physical button, then the HA coordinator imports it.
            await asyncio.to_thread(
                wait_for, lambda: call("/api/state")[1]["listener"]["running"] is False, 20
            )
            await c.async_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(entity_id).state == "off" and toggles(path) == 2
            await set_target(False)
            assert toggles(path) == 2
            assert (await asyncio.to_thread(physical)).returncode == 0
            # HA still says off; its next explicit off must inspect the device,
            # rather than skip the action based on a stale coordinator value.
            assert hass.states.get(entity_id).state == "off"
            await set_target(False)
            assert toggles(path) == 4
            (path / "toggle-error").touch()
            with pytest.raises(HomeAssistantError, match="listener_control_failed"):
                await hass.services.async_call(
                    "switch", "turn_on", {"entity_id": entity_id}, blocking=True
                )
            assert hass.states.get(entity_id).state == "off"
            assert lines(path / "native-sounds") == ["unmute", "mute"] * 2
            assert not (path / "unadapted-launcher").exists()
        assert (
            registry.async_get_entity_id("switch", DOMAIN, "synthetic-speaker_listener")
            == entity_id
        )
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert (INFO, STATE) == samples
