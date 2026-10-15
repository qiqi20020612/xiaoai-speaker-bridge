"""Real daemon + start.sh restart faults in isolated filesystems only."""

import fcntl
import http.client
import json
import os
import shlex
import subprocess
import time

import pytest

from tests.test_bridge import native_bridge as native_bridge
from tests.test_startup import endpoint_factory as endpoint_factory
from tests.test_startup import startup_binary as startup_binary


def post(endpoint, path="/api/bridge/restart", body=None):
    connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=2)
    try:
        connection.request(
            "POST",
            path,
            json.dumps(body or {}),
            {
                "Authorization": "Bearer " + endpoint.token,
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def new_instance(endpoint, previous, *, ready=True):
    status, state = endpoint.request("/api/state")
    return (
        status == 200
        and state.get("instance_id") not in (None, "", previous)
        and (not ready or not state.get("state_stale", True))
    )


def test_repeated_restart_acknowledges_once_and_preserves_listener(endpoint_factory):
    endpoint = endpoint_factory([{}, {"delay": 2}, {}])
    endpoint.values["enable_listener_adapter"] = True
    endpoint.config.write_text(json.dumps(endpoint.values))
    listener_calls = endpoint.root / "listener-calls"
    service = endpoint.bridge / "lib/listener-service.sh"
    service.write_text(f"#!/bin/sh\nprintf 'called\\n' >> '{listener_calls}'\nexit 99\n")
    service.chmod(0o755)
    proc = endpoint.launch()
    endpoint.wait_for(lambda: endpoint.request()[0] == 200)
    original = endpoint.request("/api/state")[1]["instance_id"]
    started = time.monotonic()
    for _ in range(4):
        assert post(endpoint) == (202, {"accepted": True})
    assert time.monotonic() - started < 0.8
    assert endpoint.request()[0] == 200
    assert post(
        endpoint, "/api/tts/announce", {"request_id": "pending-restart", "text": "hello"}
    ) == (503, {"error": "restart_in_progress"})
    endpoint.wait_for(lambda: new_instance(endpoint, original, ready=False), 10)
    restarted = endpoint.request("/api/state")[1]["instance_id"]
    # The new daemon has no parent restart_pid; the script still holds its guard
    # until the first delayed probe completes, covering clicks across instances.
    for _ in range(3):
        assert post(endpoint) == (202, {"accepted": True})
    endpoint.wait_for(lambda: endpoint.request()[0] == 200, 5)
    assert endpoint.request("/api/state")[1]["instance_id"] == restarted
    proc.wait(timeout=3)
    assert not listener_calls.exists()
    assert endpoint.logs().count("start event=launching") == 1
    assert endpoint.request("/api/jobs/pending-restart")[0] == 404

    # Neither short-lived guard may be inherited by the new daemon.
    def released():
        for name in ("restart", "install", "audio", "session", "volume"):
            with (endpoint.runtime / f"xiaoai_bridge.{name}.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
        return True

    endpoint.wait_for(released, 5)


@pytest.mark.parametrize("name", ["audio", "session"])
def test_restart_rejects_local_voice_lock_before_event_arrives(endpoint_factory, name):
    endpoint = endpoint_factory()
    proc = endpoint.launch()
    endpoint.wait_for(lambda: endpoint.request()[0] == 200)
    with (endpoint.runtime / f"xiaoai_bridge.{name}.lock").open("a+") as voice:
        fcntl.flock(voice, fcntl.LOCK_EX)
        assert post(endpoint) == (409, {"error": "busy"})
    assert proc.poll() is None
    assert "start event=launching" not in endpoint.logs()


def test_installation_starting_after_ack_prevents_restart_mutation(endpoint_factory):
    endpoint = endpoint_factory()
    proc = endpoint.launch()
    endpoint.wait_for(lambda: endpoint.request()[0] == 200)
    original = endpoint.request("/api/state")[1]["instance_id"]
    assert post(endpoint)[0] == 202
    with (endpoint.runtime / "xiaoai_bridge.install.lock").open("a+") as installer:
        fcntl.flock(installer, fcntl.LOCK_EX)
        endpoint.wait_for(lambda: "start event=installation_in_progress" in endpoint.logs(), 4)
    assert proc.poll() is None and endpoint.request("/api/state")[1]["instance_id"] == original
    assert "start event=launching" not in endpoint.logs()


@pytest.mark.parametrize(
    "late_owner", ["marker", "audio", "session", "volume", "waiting_for_start_lock"]
)
def test_wake_after_restart_ack_aborts_handoff_and_keeps_source_tracking(
    endpoint_factory, late_owner
):
    endpoint = endpoint_factory()
    waiting = endpoint.root / "waiting-for-start-lock"
    if late_owner == "waiting_for_start_lock":
        # Instrument only the isolated copy to signal the exact wait boundary.
        endpoint.start.write_text(
            endpoint.start.read_text().replace(
                "bridge_lock_for 5 9 ||", f"printf waiting > '{waiting}'\nbridge_lock_for 5 9 ||"
            )
        )
    proc = endpoint.launch()
    endpoint.wait_for(lambda: endpoint.request()[0] == 200)
    original = endpoint.request("/api/state")[1]["instance_id"]
    held = None
    try:
        if late_owner == "waiting_for_start_lock":
            held = (endpoint.runtime / "xiaoai_bridge.start.lock").open("a+")
            fcntl.flock(held, fcntl.LOCK_EX)
        assert post(endpoint)[0] == 202
        if late_owner == "waiting_for_start_lock":
            # The caller has passed preflight and is waiting inside start.sh.
            endpoint.wait_for(waiting.exists, 4)
        if late_owner in ("marker", "waiting_for_start_lock"):
            start = subprocess.check_output(
                [
                    *endpoint.shell,
                    "-c",
                    f'. "{endpoint.bridge}/lib/locks.sh"; bridge_pid_start {os.getpid()} || true',
                ],
                env=endpoint.env,
                text=True,
            ).strip()
            (endpoint.runtime / "xiaoai_bridge.voice").write_text(f"{os.getpid()} {start}\n")
            event = {
                "type": "voice_begin",
                "pid": os.getpid(),
                "source": "airplay",
                "active_sources": ["airplay"],
                "uncertain": False,
                "volumes": {"master": 60, "bluetooth": 40},
                "observed_sources": {"airplay": True, "bluetooth": False, "mpd": False},
            }
            subprocess.run(
                [str(endpoint.binary), "--emit", json.dumps(event)],
                env=endpoint.env,
                check=True,
                timeout=2,
            )
            endpoint.wait_for(lambda: endpoint.request("/api/state")[1]["voice"]["active"])
        else:
            held = (endpoint.runtime / f"xiaoai_bridge.{late_owner}.lock").open("a+")
            fcntl.flock(held, fcntl.LOCK_EX)
        if late_owner == "waiting_for_start_lock":
            fcntl.flock(held, fcntl.LOCK_UN)
        endpoint.wait_for(lambda: "start event=restart_voice_busy" in endpoint.logs(), 4)
        current = endpoint.request("/api/state")[1]
        assert proc.poll() is None and current["instance_id"] == original
        assert "start event=launching" not in endpoint.logs()
        if late_owner in ("marker", "waiting_for_start_lock"):
            assert current["voice"]["active"] and current["voice"]["source"] == "airplay"
            assert current["voice"]["volumes"] == {"master": 60, "bluetooth": 40}
        if late_owner == "marker":
            (endpoint.runtime / "xiaoai_bridge.voice").unlink()
            subprocess.run(
                [str(endpoint.binary), "--emit", '{"type":"voice_end"}'],
                env=endpoint.env,
                check=True,
                timeout=2,
            )
            endpoint.wait_for(lambda: post(endpoint, "/api/policy", {"source": None})[0] == 200)
            assert post(endpoint)[0] == 202
            endpoint.wait_for(lambda: new_instance(endpoint, original), 10)
    finally:
        if held is not None:
            held.close()


def test_wake_during_daemon_gap_reaches_replacement_before_probe_finishes(endpoint_factory):
    endpoint = endpoint_factory([{}, {"delay": 4}, {}])
    boundary, resume, capture = (endpoint.root / name for name in ("gap", "resume", "capture"))
    endpoint.start.write_text(
        endpoint.start.read_text().replace(
            '    start_diagnostic launching "$STARTUP_BUDGET"',
            f"    touch '{boundary}'\n"
            "    waited=0\n"
            f"    while [ ! -r '{resume}' ]; do\n"
            '      [ "$waited" -lt 40 ] || exit 1\n'
            "      sleep 0.05; waited=$((waited+1))\n    done\n"
            '    start_diagnostic launching "$STARTUP_BUDGET"',
        )
    )
    # Use the actual voice hook and native journal/event helpers. Only hardware
    # reads/writes and source observation are replaced in the isolated copy.
    (endpoint.bridge / "lib/source-snapshot.sh").write_text(
        '#!/bin/sh\nprintf \'{"type":"voice_begin","pid":%s,"source":"airplay",'
        '"active_sources":["airplay"],"uncertain":false,'
        '"volumes":{"master":60,"bluetooth":60}}\\n\' "$1"\n'
    )
    voice_body = f""". {shlex.quote(str(endpoint.bridge / "lib/listener-hooks.sh"))}
percent() {{ printf 60; }}
dbus_get_all() {{ return 0; }}
amixer() {{ return 0; }}
bridge_voice_begin || exit 71
touch {shlex.quote(str(capture))}
read -r finish
bridge_restore_volume
"""
    proc = endpoint.launch()
    endpoint.wait_for(lambda: endpoint.request()[0] == 200)
    original = endpoint.request("/api/state")[1]["instance_id"]
    assert post(endpoint)[0] == 202
    endpoint.wait_for(boundary.exists, 5)
    proc.wait(timeout=3)
    # The hook emits its snapshot before taking audio/session. Its existing
    # volume gate must therefore cover this interval without an event receiver.
    with (endpoint.runtime / "xiaoai_bridge.volume.lock").open("a+") as gate:
        with pytest.raises(BlockingIOError):
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
    voice = subprocess.Popen(
        [*endpoint.shell, "-c", voice_body],
        env={
            **endpoint.env,
            "PATH": str(endpoint.root / "tools") + os.pathsep + os.environ["PATH"],
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        endpoint.wait_for((endpoint.runtime / "xiaoai_bridge.voice").exists, 2)
        resume.touch()
        endpoint.wait_for(capture.exists, 5)
        endpoint.wait_for(lambda: new_instance(endpoint, original, ready=False), 3)
        state = endpoint.request("/api/state")[1]
        assert state["voice"]["active"] and state["voice"]["source"] == "airplay"
        assert state["voice"]["volumes"] == {"master": 60, "bluetooth": 60}
        assert state["state_stale"]  # Gates release at event readiness, not slow probe readiness.
        assert (
            post(endpoint, "/api/tts/announce", {"text": "hello", "request_id": "gap-voice"})[0]
            == 409
        )
    finally:
        resume.touch()
        _, error = voice.communicate("finish\n", timeout=8)
    assert voice.returncode == 0, error
    endpoint.wait_for(lambda: not endpoint.request("/api/state")[1]["voice"]["active"], 3)


@pytest.mark.parametrize("marker", ["99999999 1\n", f"{os.getpid()} 0\n"])
def test_stale_or_reused_voice_marker_does_not_block_idle_restart(endpoint_factory, marker):
    endpoint = endpoint_factory()
    endpoint.launch()
    endpoint.wait_for(lambda: endpoint.request()[0] == 200)
    original = endpoint.request("/api/state")[1]["instance_id"]
    assert post(endpoint)[0] == 202
    (endpoint.runtime / "xiaoai_bridge.voice").write_text(marker)
    endpoint.wait_for(lambda: new_instance(endpoint, original), 10)
    assert "start event=restart_voice_busy" not in endpoint.logs()


def test_missing_restart_script_reports_immediate_failure(endpoint_factory):
    endpoint = endpoint_factory()
    proc = endpoint.launch()
    endpoint.wait_for(lambda: endpoint.request()[0] == 200)
    source = endpoint.start.read_bytes()
    endpoint.start.unlink()
    try:
        assert post(endpoint) == (503, {"error": "restart_unavailable"})
        assert proc.poll() is None and endpoint.request()[0] == 200
    finally:
        endpoint.start.write_bytes(source)
        endpoint.start.chmod(0o755)


def test_replacement_exec_failure_releases_all_gates_and_can_start_again(endpoint_factory):
    endpoint = endpoint_factory()
    proc = endpoint.launch()
    endpoint.wait_for(lambda: endpoint.request()[0] == 200)
    source = endpoint.start.read_text()
    endpoint.start.write_text(
        source.replace('      exec "$BIN"', '      exit 77\n      exec "$BIN"')
    )
    assert post(endpoint)[0] == 202
    endpoint.wait_for(lambda: "start event=process_exited code=77" in endpoint.logs(), 5)
    proc.wait(timeout=3)
    for name in ("restart", "install", "start", "audio", "session", "volume"):
        with (endpoint.runtime / f"xiaoai_bridge.{name}.lock").open("a+") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
    endpoint.start.write_text(source)
    assert endpoint.command().returncode == 0
    assert endpoint.request()[0] == 200


def test_failed_config_check_leaves_old_daemon_running_and_allows_retry(endpoint_factory):
    endpoint = endpoint_factory()
    proc = endpoint.launch()
    endpoint.wait_for(lambda: endpoint.request()[0] == 200)
    original = endpoint.request("/api/state")[1]["instance_id"]
    source = endpoint.config.read_bytes()
    endpoint.config.write_text("invalid JSON")
    assert post(endpoint)[0] == 202
    endpoint.wait_for(lambda: "start event=invalid_config" in endpoint.logs(), 4)
    assert proc.poll() is None and endpoint.request("/api/state")[1]["instance_id"] == original
    endpoint.config.write_bytes(source)
    # Reap the failed short-lived caller before the next acknowledged request.
    endpoint.wait_for(lambda: post(endpoint, "/api/policy", {"source": None})[0] == 200, 4)
    assert post(endpoint)[0] == 202
    endpoint.wait_for(lambda: new_instance(endpoint, original), 10)


def test_restart_refuses_active_audio_job_and_retired_agent_api_is_absent(native_bridge):
    call, proc, *_ = native_bridge
    assert call(
        "/api/conversation/agent", {"agent_id": "conversation.other", "request_id": "old-agent"}
    ) == (404, {"error": "not_found"})
    assert call(
        "/api/conversation/text",
        {"text": "hello", "agent_id": "conversation.other", "request_id": "override"},
    ) == (400, {"error": "unsupported_agent_override"})
    assert call("/api/tts/announce", {"text": "sleep", "request_id": "active-job"})[0] == 202
    assert call("/api/bridge/restart", {}) == (409, {"error": "busy"})
    assert proc.poll() is None
    assert "worker_peak_rss_kb" not in call("/api/state")[1]
    call("/api/jobs/active-job/cancel", {})
