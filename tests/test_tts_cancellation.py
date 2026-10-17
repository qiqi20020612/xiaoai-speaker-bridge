"""Cancellation must stop owned synthesis/player processes, not just job labels."""

import asyncio
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import pytest

from custom_components.xiaoai_speaker.client import BridgeClient
from custom_components.xiaoai_speaker.jobs import JobManager
from tests.test_bridge import native_bridge as native_bridge
from tests.test_lx06_regressions import redirect_fixture
from tests.test_native_led import native_led as native_led
from tests.test_native_led import task_bridge as task_bridge
from tests.test_text_pipeline import enable_airplay_duck_fixture, script
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import text_device as text_device

ROOT = Path(__file__).resolve().parents[1]

ORPHAN_ADAPTER = """#!/usr/bin/env python3
import json,os,shutil,signal,sys,time
from pathlib import Path
request=json.load(sys.stdin)
root=Path(os.environ['XIAOAI_BRIDGE_RUNTIME'])
if sys.argv[1]=='probe':
    print('{"capabilities":{"announce":true,"conversation":true},"state":{}}')
    sys.exit(0)
phase=request['text']
player=root/'mpg123'
player.symlink_to(shutil.which('sleep'))
with (root/'executions').open('a') as stream:stream.write(request['request_id']+'\\n')
if os.fork()==0:
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    (root/'tts-child').write_text(str(os.getpid())+' '+str(os.getpgrp()))
    while phase=='synthesizing' and not (root/'synthesis-released').exists():
        time.sleep(0.01)
    (root/'played').touch()
    os.execl(str(player),str(player),'30')
signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
while not (root/'tts-child').exists():time.sleep(0.01)
print(json.dumps({'stage':phase},separators=(',',':')),flush=True)
while True:time.sleep(0.05)
"""


def running(pid):
    try:
        if Path("/proc/self/stat").exists():
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state in ("Z", "X"):
                return False
        os.kill(pid, 0)
    except ProcessLookupError, FileNotFoundError:
        return False
    return True


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "owned TTS processes did not settle"
        time.sleep(0.12)


@pytest.mark.parametrize("native_bridge", [ORPHAN_ADAPTER], indirect=True, ids=["owned-provider"])
@pytest.mark.parametrize("phase", ["synthesizing", "playing"])
@pytest.mark.parametrize("kind", ["announce", "send_text"])
def test_cancel_reaps_owned_descendants_even_when_worker_exits_first(native_bridge, phase, kind):
    call, server, runtime, *_ = native_bridge
    endpoint = "/api/tts/announce" if kind == "announce" else "/api/conversation/text"
    payload = {"request_id": "tts-orphan", "text": phase}
    assert call(endpoint, payload)[0] == 202
    wait_for(lambda: call("/api/jobs/tts-orphan")[1]["status"] == phase)
    child, group = map(int, (runtime / "tts-child").read_text().split())
    unrelated = subprocess.Popen(
        [str(runtime / "mpg123"), "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert call("/api/jobs/tts-orphan/cancel", {})[1]["status"] == "cancelled"
        wait_for(lambda: not call("/api/state")[1]["audio_active"])
        (runtime / "synthesis-released").touch()
        wait_for(lambda: not running(child) or (runtime / "played").exists())
        if phase == "synthesizing":
            assert not (runtime / "played").exists(), "cancelled synthesis started a player"
        wait_for(lambda: not running(child))
        assert unrelated.poll() is None and server.poll() is None
        assert call(endpoint, payload)[1]["status"] == "cancelled"
        assert (runtime / "executions").read_text().splitlines() == ["tts-orphan"]
    finally:
        if running(child) and os.getpgid(child) == group:
            os.kill(child, signal.SIGKILL)
        unrelated.terminate()
        unrelated.wait(timeout=3)


async def wait_async(predicate, timeout=10):
    await asyncio.to_thread(wait_for, predicate, timeout)


@pytest.fixture(scope="session")
def silent_player(tmp_path_factory):
    directory = tmp_path_factory.mktemp("silent-player")
    source = directory / "player.c"
    source.write_text("#include <unistd.h>\nint main(void) { sleep(30); return 0; }\n")
    binary = directory / "mpg123"
    subprocess.run(["cc", str(source), "-o", str(binary)], check=True, capture_output=True)
    return binary


def install_providers(path, provider, player, ha_url):
    data = path / "provider-data"
    data.mkdir()
    shutil.copyfile(path / "existing-listener", data / "listener")
    (data / "doubao_tts.conf").write_text("DOUBAO_API_KEY=synthetic-only\n")
    names = {
        "doubao": "tts_doubao",
        "microsoft": "tts_microsoft_fallback",
        "google": "tts_google_zh",
    }
    for name, filename in names.items():
        source = (ROOT / f"tests/fixtures/lx06_tts_{name}.sh").read_text()
        source = redirect_fixture(
            source,
            {
                "/data/": str(data) + "/",
                "/tmp/": str(path) + "/",
                "/bin/miplay": path / "tools/miplay",
            },
        )
        source = source.replace(
            "#!/bin/sh\n",
            f'#!/bin/sh\nprintf \'%s\\n\' "$$" > "$STATE_DIR/provider-{name}.pid"\n',
            1,
        )
        script(data / filename, source)
    config = json.loads((path / "config.json").read_text())
    config["tts_path"] = str(data / "tts_doubao")
    (path / "config.json").write_text(json.dumps(config))
    script(path / "tools/miplay", '#!/bin/sh\nexec mpg123 -q -a notify "$1"\n')
    script(
        path / "tools/mpg123",
        f'#!/bin/sh\nprintf \'%s\\n\' "$$" > "$STATE_DIR/player.pid"\n'
        f': > "$STATE_DIR/played"\nexec "{player}" "$@"\n',
    )
    script(
        path / "tools/ffmpeg",
        '#!/bin/sh\nfor output; do :;done\ncat >/dev/null\nprintf synthetic-mp3 > "$output"\n',
    )
    script(
        path / "tools/curl",
        f"""#!{sys.executable}
import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
url=next((arg for arg in args if arg.startswith(('http://','https://'))),'')
if url.startswith({ha_url!r}) and not url.endswith('/api/tts_get_url'):
    os.execv({shutil.which("curl")!r},[{shutil.which("curl")!r},*args])
root=Path(os.environ['STATE_DIR'])
output=Path(args[args.index('-o')+1])
with (root/'provider-requests').open('a') as stream:stream.write(output.name+'\\n')
provider={provider!r}
if url.endswith('/api/tts_get_url'):
    if provider=='google':sys.exit(1)
    output.write_text(json.dumps({{'path':'/api/tts_proxy/synthetic.wav'}}))
    sys.exit(0)
if output.name=='response.jsonl' and provider!='doubao':sys.exit(1)
(root/'synthesis.pid').write_text(str(os.getpid()))
while not (root/'synthesis-released').exists():time.sleep(0.02)
if output.name=='response.jsonl':
    output.write_text('{{"code":0,"data":"c3ludGhldGlj"}}\\n{{"code":20000000}}\\n')
elif '/api/tts_proxy/' in url:
    output.write_bytes(b'RIFF'+b'x'*128)
else:output.write_bytes(b'synthetic-mp3')
""",
    )
    return data / "tts_doubao"


@pytest.mark.parametrize("task_bridge", [30], indirect=True)
@pytest.mark.parametrize("provider", ["doubao", "microsoft", "google"])
@pytest.mark.parametrize("phase", ["synthesizing", "playing"])
@pytest.mark.parametrize("kind", ["announce", "send_text"])
async def test_ha_cancel_stops_real_provider_chain_and_preserves_context(
    task_bridge, native_led, ha_http, silent_player, hass, provider, phase, kind
):
    call, runtime = task_bridge
    _, path, _ = native_led
    tts_path = install_providers(path, provider, silent_player, ha_http.url)
    values = json.loads((runtime / "config.json").read_text())
    values["tts_path"] = str(tts_path)
    (runtime / "config.json").write_text(json.dumps(values))
    if phase == "playing":
        (path / "synthesis-released").touch()
    ready = asyncio.Event()
    async with aiohttp.ClientSession() as session:
        client = BridgeClient(session, "127.0.0.1", values["token"], values["port"])
        original_submit = client.submit

        async def submit(action, payload):
            state = await original_submit(action, payload)
            # Return the actual HTTP acceptance only after TTS is active. This
            # deterministically tests a stale accepted status at cancellation.
            marker = path / ("synthesis.pid" if phase == "synthesizing" else "played")
            await wait_async(marker.exists)
            expected = {"synthesizing", "playing"}
            if phase == "playing" and sys.platform == "linux":
                expected = {"playing"}
            await wait_async(
                lambda: call("/api/jobs/" + payload["request_id"])[1]["status"] in expected
            )
            return state

        client.submit = submit
        manager = JobManager(hass, client)
        job = await manager.submit(kind, {"text": "synthetic speech", "volume": 20})
        manager.on_change = lambda: ready.set() if job.status == "accepted" else None
        manager.start()
        try:
            async with asyncio.timeout(15):
                await ready.wait()
            context = path / "eoai_conversation.json"
            saved_context = context.read_bytes() if kind == "send_text" else None
            before = (path / "provider-requests").read_bytes()
            assert (await client.job(job.request_id))["status"] in ("synthesizing", "playing")
            assert (await manager.cancel(job.request_id))["status"] == "cancelled"
            assert job.status == "cancelled"
            assert (await manager.wait(job, 8))["status"] == "cancelled"
            (path / "synthesis-released").touch()
            await wait_async(lambda: not call("/api/state")[1]["audio_active"])
            pids = [int(file.read_text()) for file in path.glob("provider-*.pid")]
            pids.append(int((path / "synthesis.pid").read_text()))
            if phase == "playing":
                pids.append(int((path / "player.pid").read_text()))
            await wait_async(lambda: not any(running(pid) for pid in pids))
            if phase == "synthesizing":
                assert not (path / "played").exists(), "cancelled synthesis started playback"
            assert (path / "provider-requests").read_bytes() == before
            assert (path / "mixer-writes").read_text().splitlines() == [
                "notifyvol 20%",
                "notifyvol 60%",
            ]
            assert not (runtime / "xiaoai_bridge.tts-volume.json").exists()
            for name in ("audio", "volume", "session", "led"):
                with (runtime / f"xiaoai_bridge.{name}.lock").open("w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert len(ha_http.conversations) == (1 if kind == "send_text" else 0)
            if kind == "send_text":
                assert context.read_bytes() == saved_context
            assert (
                await manager.submit(
                    kind, {"text": "synthetic speech", "volume": 20, "request_id": job.request_id}
                )
                is job
            )
        finally:
            await manager.close()
            await client.cancel(job.request_id)
            (path / "synthesis-released").touch()


@pytest.mark.parametrize("task_bridge", [30], indirect=True)
def test_conversation_still_executing_rejects_cancellation(task_bridge, native_led, ha_http):
    call, _ = task_bridge
    _, path, _ = native_led
    ha_http.hold_conversation = True
    payload = {"request_id": "active-conversation", "text": "synthetic instruction"}
    try:
        assert call("/api/conversation/text", payload)[0] == 202
        assert ha_http.conversation_entered.wait(timeout=5)
        assert call("/api/jobs/active-conversation/cancel", {}) == (
            409,
            {"error": "cannot_cancel_running_conversation"},
        )
        assert call("/api/jobs/active-conversation")[1]["status"] in ("accepted", "running")
        ha_http.conversation_release.set()
        wait_for(lambda: call("/api/jobs/active-conversation")[1]["status"] == "completed")
        assert call("/api/conversation/text", payload)[1]["status"] == "completed"
        assert len(ha_http.conversations) == 1 and (path / "eoai_conversation.json").exists()
    finally:
        ha_http.conversation_release.set()


@pytest.mark.parametrize("task_bridge", [30], indirect=True)
def test_cancel_keeps_deferred_volume_recovery_outside_stopped_group(task_bridge, native_led):
    call, runtime = task_bridge
    _, path, _ = native_led
    enable_airplay_duck_fixture(path)
    script(path / "existing-tts", "#!/bin/sh\nmpg123\n")
    script(path / "tools/mpg123", '#!/bin/sh\n: > "$STATE_DIR/tts-started"\nsleep 30\n')
    assert (
        call(
            "/api/tts/announce",
            {"request_id": "deferred-recovery", "text": "synthetic", "volume": 20},
        )[0]
        == 202
    )
    wait_for(lambda: (path / "tts-started").exists())
    with (runtime / "xiaoai_bridge.volume.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert call("/api/jobs/deferred-recovery/cancel", {})[1]["status"] == "cancelled"
        # Both normal restore attempts must fail before the worker exits; the
        # original bounded recovery then needs to survive group termination.
        wait_for(lambda: not call("/api/state")[1]["audio_active"], timeout=15)
        assert (runtime / "xiaoai_bridge.tts-volume.json").exists()
        assert (runtime / "xiaoai_bridge.duck.json").exists()
    wait_for(lambda: not (runtime / "xiaoai_bridge.tts-volume.json").exists(), timeout=8)
    wait_for(lambda: not (runtime / "xiaoai_bridge.duck.json").exists(), timeout=8)
    assert (path / "mixer-mysoftvol").read_text().strip() == "60"
    assert (path / "mixer-notifyvol").read_text().strip() == "60"


@pytest.mark.parametrize("task_bridge", [30], indirect=True)
@pytest.mark.parametrize("kind", ["announce", "send_text"])
@pytest.mark.parametrize("hold_recovery_unlock", [False, True])
def test_cancel_while_decoder_waits_for_volume_lock_cannot_start_late_playback(
    task_bridge, native_led, ha_http, silent_player, kind, hold_recovery_unlock, request
):
    call, runtime = task_bridge
    _, path, _ = native_led
    enable_airplay_duck_fixture(path)
    tts_path = install_providers(path, "doubao", silent_player, ha_http.url)
    values = json.loads((runtime / "config.json").read_text())
    values["tts_path"] = str(tts_path)
    (runtime / "config.json").write_text(json.dumps(values))
    dbus = path / "tools/dbus-send"
    dbus.write_text(
        dbus.read_text().replace(
            "#!/bin/sh\n",
            '#!/bin/sh\n[ -z "${XIAOAI_TTS_GATE:-}" ] || : > "$STATE_DIR/preplay-ready"\n',
            1,
        )
    )
    endpoint = "/api/tts/announce" if kind == "announce" else "/api/conversation/text"
    request_id = "cancel-preplay-gate"
    if hold_recovery_unlock:
        request.addfinalizer(lambda: (path / "recovery-unlock-released").touch())
        recovery = path / "bridge/lib/volume-recovery.sh"
        recovery.write_text(
            recovery.read_text().replace(
                '[ "${2:-}" = keep ] || rm -f "$recovery_file"',
                """[ "${2:-}" = keep ] || rm -f "$recovery_file"
    if [ "$recovery_file" = "$BRIDGE_RUNTIME/xiaoai_bridge.tts-volume.json" ]; then
        : > "$STATE_DIR/recovery-journal-removed"
        while [ ! -f "$STATE_DIR/recovery-unlock-released" ]; do sleep 0.01;done
    fi""",
            )
        )
    assert call(endpoint, {"request_id": request_id, "text": "synthetic", "volume": 20})[0] == 202
    wait_for(lambda: (path / "synthesis.pid").exists())
    with (runtime / "xiaoai_bridge.volume.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (path / "synthesis-released").touch()
        wait_for(lambda: (path / "preplay-ready").exists())
        assert not (path / "played").exists()
        assert call(f"/api/jobs/{request_id}/cancel", {})[1]["status"] == "cancelled"
        wait_for(lambda: not call("/api/state")[1]["audio_active"], timeout=15)
        assert not (runtime / "xiaoai_bridge.duck.json").exists()
    wait_for(lambda: not (runtime / "xiaoai_bridge.tts-volume.json").exists(), timeout=8)
    if hold_recovery_unlock:
        try:
            wait_for(lambda: (path / "recovery-journal-removed").exists())
            # Removing the Journal precedes the recovery subprocess's return
            # and FD close; it does not promise the Volume Lock is free yet.
            with (runtime / "xiaoai_bridge.volume.lock").open("a+") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            (path / "recovery-unlock-released").touch()

    def locks_released():
        for name in ("audio", "volume", "session", "led"):
            with (runtime / f"xiaoai_bridge.{name}.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
        return True

    # Verify actual lock availability within the same bounded cleanup window,
    # rather than using Journal absence as a proxy for a completed FD close.
    wait_for(locks_released, timeout=8)
    assert not (path / "played").exists()
    assert (path / "mixer-notifyvol").read_text().strip() == "60"
    assert not any(
        line.startswith("mysoftvol ") for line in (path / "mixer-writes").read_text().splitlines()
    )
    assert (
        call(endpoint, {"request_id": request_id, "text": "synthetic", "volume": 20})[1]["status"]
        == "cancelled"
    )


@pytest.mark.parametrize(
    "native_bridge",
    [
        """#!/usr/bin/env python3
import json,os,signal,sys,time
from pathlib import Path
json.load(sys.stdin)
root=Path(os.environ['XIAOAI_BRIDGE_RUNTIME'])
if sys.argv[1]=='probe':
    print('{"capabilities":{"announce":true},"state":{}}')
else:
    def cleanup(*args):
        with (root/'term-signals').open('a') as stream:stream.write('term\\n')
        while not (root/'finish-cleanup').exists():time.sleep(0.02)
        sys.exit(0)
    signal.signal(signal.SIGTERM,cleanup)
    print('{"stage":"synthesizing"}',flush=True)
    while True:time.sleep(0.05)
"""
    ],
    indirect=True,
    ids=["slow-cleanup"],
)
def test_repeated_cancellation_does_not_restart_cleanup(native_bridge):
    call, _, runtime, *_ = native_bridge
    assert call("/api/tts/announce", {"request_id": "repeat-cancel", "text": "synthetic"})[0] == 202
    wait_for(lambda: call("/api/jobs/repeat-cancel")[1]["status"] == "synthesizing")
    try:
        assert call("/api/jobs/repeat-cancel/cancel", {})[1]["status"] == "cancelled"
        wait_for(lambda: (runtime / "term-signals").exists())
        for _ in range(3):
            assert call("/api/jobs/repeat-cancel/cancel", {})[1]["status"] == "cancelled"
        assert (runtime / "term-signals").read_text().splitlines() == ["term"]
    finally:
        (runtime / "finish-cleanup").touch()
    wait_for(lambda: not call("/api/state")[1]["audio_active"])
