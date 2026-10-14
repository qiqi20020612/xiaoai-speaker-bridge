"""Real daemon + start.sh restart faults in isolated filesystems only."""

import fcntl
import http.client
import json
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


def test_repeated_restart_acknowledges_once_and_preserves_listener(endpoint_factory):
    endpoint = endpoint_factory()
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
    endpoint.wait_for(lambda: endpoint.request("/api/state")[1].get("instance_id") != original, 10)
    endpoint.wait_for(lambda: endpoint.request()[0] == 200, 5)
    proc.wait(timeout=3)
    assert not listener_calls.exists()
    assert endpoint.logs().count("start event=launching") == 1
    assert endpoint.request("/api/jobs/pending-restart")[0] == 404

    # Neither short-lived guard may be inherited by the new daemon.
    def released():
        for name in ("restart", "install"):
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
    endpoint.wait_for(
        lambda: (
            endpoint.request()[0] == 200
            and endpoint.request("/api/state")[1].get("instance_id") != original
        ),
        10,
    )


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
