import fcntl
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def native_bridge(tmp_path, socket_enabled, request):
    runtime = tempfile.TemporaryDirectory(
        prefix="xiaoai-test-", dir="/private/tmp" if os.path.isdir("/private/tmp") else "/tmp"
    )
    tmp_path = Path(runtime.name)
    subprocess.run(["make", "-s", "-C", str(ROOT / "speaker")], check=True)
    binary = tmp_path / "bin/xiaoai_bridge"
    binary.parent.mkdir()
    shutil.copy2(ROOT / "build/native/xiaoai_bridge", binary)
    adapter = ROOT / "tests/mock_adapter.py"
    if getattr(request, "param", None):
        adapter = tmp_path / "cleanup-adapter.py"
        adapter.write_text(request.param)
    adapter.chmod(0o755)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = tmp_path / "config.json"
    token = "synthetic-token-for-local-test-00000000000000000000"
    config.write_text(
        json.dumps(
            {
                "token": token,
                "device_id": "synthetic-speaker",
                "bind": "127.0.0.1",
                "port": port,
                "runtime_dir": str(tmp_path),
                "adapter": str(adapter),
            }
        )
    )
    config.chmod(0o600)
    proc = subprocess.Popen(
        [str(binary), "--config", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )

    def call(path, data=None, auth=token):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Authorization": "Bearer " + auth}
        body = json.dumps(data) if data is not None else None
        if body:
            headers["Content-Type"] = "application/json"
        c.request("POST" if data is not None else "GET", path, body, headers)
        response = c.getresponse()
        status = response.status
        result = json.loads(response.read())
        c.close()
        return status, result

    for _ in range(50):
        try:
            if call("/api/info")[0] == 200:
                break
        except OSError, http.client.HTTPException:
            pass
        time.sleep(0.05)
    else:
        pytest.fail("native endpoint did not become ready")
    yield call, proc, tmp_path, port, config, token
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    stderr = proc.stderr.read().decode()
    proc.stderr.close()
    runtime.cleanup()
    assert all(
        re.fullmatch(
            r"time=\d+ elapsed=[\d.]+ bridge event="
            r"(initializing|http_listening|probe_started|probe_ready|stopped) attempt=\d+ code=\d+",
            line,
        )
        for line in stderr.splitlines()
    ), stderr
    assert token not in stderr


def test_authentication_and_no_arbitrary_shell(native_bridge):
    call, *_ = native_bridge
    assert call("/api/state", auth="wrong")[0] == 401
    assert call("/api/shell", {"request_id": "r1", "command": "touch /tmp/forbidden"})[0] == 404
    assert call("/api/volume", {"target": "master", "level": 30})[0] == 400


def test_installation_lock_blocks_new_work_but_keeps_health_and_state_readable(native_bridge):
    call, _, runtime, *_ = native_bridge
    with (runtime / "xiaoai_bridge.install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert call("/api/health")[0] == 200
        assert call("/api/state")[0] == 200
        status, result = call("/api/tts/announce", {"request_id": "during-install", "text": "test"})
        assert status == 503 and result["error"] == "installation_in_progress"
        assert call("/api/jobs/during-install")[0] == 404
        fcntl.flock(lock, fcntl.LOCK_UN)
    assert call("/api/tts/announce", {"request_id": "after-install", "text": "test"})[0] == 202


@pytest.mark.parametrize(
    "native_bridge",
    [
        """#!/usr/bin/env python3
import json,os,signal,sys,time
from pathlib import Path
json.load(sys.stdin)
if sys.argv[1]=='probe':
    print('{"capabilities":{"announce":true},"state":{}}')
else:
    def cleanup(*args):
        time.sleep(6)
        Path(os.environ['XIAOAI_BRIDGE_RUNTIME'],'cleanup-finished').touch()
        sys.exit(0)
    signal.signal(signal.SIGTERM,cleanup)
    print('{"stage":"synthesizing"}',flush=True)
    while True:time.sleep(0.05)
"""
    ],
    indirect=True,
)
def test_cancel_allows_cleanup_longer_than_two_volume_lock_budgets(native_bridge):
    call, _, runtime, *_ = native_bridge
    assert call("/api/tts/announce", {"request_id": "slow-cleanup", "text": "test"})[0] == 202
    deadline = time.monotonic() + 3
    while call("/api/jobs/slow-cleanup")[1]["status"] != "synthesizing":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert call("/api/jobs/slow-cleanup/cancel", {})[1]["status"] == "cancelled"
    deadline = time.monotonic() + 9
    while not (runtime / "cleanup-finished").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (runtime / "cleanup-finished").exists()


def test_source_policy_priority_roundtrip_and_invalid_permutations(native_bridge):
    call, _, runtime, _, _, _ = native_bridge
    policy = {"source": "airplay", "uncertain": False, "priority": ["bluetooth", "airplay", "mpd"]}
    assert call("/api/policy", policy)[0] == 200
    saved = json.loads((runtime / "xiaoai_bridge.source.json").read_text())
    assert saved["priority"] == policy["priority"]
    for priority in [
        ["airplay", "airplay", "mpd"],
        ["airplay", "mpd"],
        ["airplay", "other", "mpd"],
        "mpd",
        [None, "mpd", "airplay"],
    ]:
        assert call("/api/policy", {**policy, "priority": priority})[0] == 400
        assert json.loads((runtime / "xiaoai_bridge.source.json").read_text()) == saved


def test_text_idempotency_busy_and_real_status(native_bridge):
    call, proc, path, *_ = native_bridge
    payload = {"request_id": "text-1", "text": "打开灯", "speak_response": False}
    assert call("/api/conversation/text", payload)[0] == 202
    assert call("/api/conversation/text", payload)[0] == 200
    assert call("/api/conversation/text", {**payload, "text": "other"})[0] == 409
    assert call("/api/tts/announce", payload)[1]["error"] == "request_id_conflict"
    assert (
        call("/api/tts/announce", {"request_id": "other-2", "text": "hello"})[1]["error"] == "busy"
    )
    time.sleep(0.7)
    status, result = call("/api/jobs/text-1")
    assert status == 200 and result["status"] == "completed" and result["reply_text"] == "打开灯"
    assert (path / "executions.txt").read_text().splitlines() == ["text-1"]


def test_announce_failure_is_not_reported_as_success(native_bridge):
    call, *_ = native_bridge
    call("/api/tts/announce", {"request_id": "failure", "text": "FAIL"})
    time.sleep(0.7)
    assert call("/api/jobs/failure")[1]["status"] == "failed"
    assert call("/api/jobs/failure")[1]["error"] == "tts_failed"


def test_local_voice_snapshot_and_no_preemption(native_bridge):
    call, proc, path, port, config, token = native_bridge
    subprocess.run(
        [
            str(ROOT / "build/native/xiaoai_bridge"),
            "--config",
            str(config),
            "--emit",
            json.dumps(
                {"type": "voice_begin", "pid": os.getpid(), "source": "airplay", "uncertain": False}
            ),
        ],
        check=True,
    )
    time.sleep(0.1)
    state = call("/api/state")[1]
    assert state["voice"]["active"] and state["voice"]["source"] == "airplay"
    assert (
        call("/api/conversation/text", {"request_id": "blocked", "text": "hello"})[1]["error"]
        == "busy"
    )
    subprocess.run(
        [
            str(ROOT / "build/native/xiaoai_bridge"),
            "--config",
            str(config),
            "--emit",
            '{"type":"voice_end"}',
        ],
        check=True,
    )
    time.sleep(0.1)
    assert not call("/api/state")[1]["voice"]["active"]


@pytest.mark.parametrize(
    "headers",
    [
        "Content-Length: 2\r\nContent-Length: 2\r\n",
        "Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
        "Content-Length: -1\r\n",
        "Content-Length: 20000\r\n",
    ],
)
def test_request_smuggling_and_length_limits(native_bridge, headers):
    call, proc, path, port, config, token = native_bridge
    with socket.create_connection(("127.0.0.1", port), 2) as s:
        s.sendall(
            (
                "POST /api/volume HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                + token
                + "\r\n"
                + headers
                + "\r\n{}"
            ).encode()
        )
        assert b"HTTP/1.1 400" in s.recv(4096)


def test_duplicate_json_keys_and_unicode_nul_are_rejected(native_bridge):
    call, proc, path, port, config, token = native_bridge
    for body in ['{"request_id":"a","request_id":"b"}', '{"text":"\\u0000"}']:
        with socket.create_connection(("127.0.0.1", port), 2) as s:
            s.sendall(
                (
                    f"POST /api/volume HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(body)}\r\n\r\n"
                    + body
                ).encode()
            )
            assert b"HTTP/1.1 400" in s.recv(4096)


def test_single_instance_kernel_lock(native_bridge):
    call, proc, path, port, config, token = native_bridge
    second = subprocess.run(
        [str(ROOT / "build/native/xiaoai_bridge"), "--config", str(config)],
        capture_output=True,
        timeout=2,
    )
    assert second.returncode != 0 and proc.poll() is None
    assert call("/api/health")[0] == 200


def test_running_announcement_cancels_only_its_own_worker(native_bridge):
    call, proc, path, *_ = native_bridge
    assert call("/api/tts/announce", {"request_id": "cancel-me", "text": "hello"})[0] == 202
    assert call("/api/jobs/cancel-me/cancel", {})[1]["status"] == "cancelled"
    time.sleep(0.2)
    assert call("/api/jobs/cancel-me")[1]["status"] == "cancelled"
    assert proc.poll() is None and call("/api/health")[0] == 200


def test_wake_event_yields_own_announcement_without_killing_conversation_or_server(native_bridge):
    call, proc, path, port, config, token = native_bridge
    assert call("/api/tts/announce", {"request_id": "voice-yield", "text": "hello"})[0] == 202
    subprocess.run(
        [
            str(ROOT / "build/native/xiaoai_bridge"),
            "--config",
            str(config),
            "--emit",
            json.dumps(
                {
                    "type": "voice_begin",
                    "pid": os.getpid(),
                    "source": "mpd",
                    "volumes": {"master": 60},
                }
            ),
        ],
        check=True,
    )
    time.sleep(0.2)
    result = call("/api/jobs/voice-yield")[1]
    assert result["status"] == "cancelled" and result["error"] == "voice_preempted"
    assert call("/api/state")[1]["voice"]["volumes"]["master"] == 60
    assert proc.poll() is None


@pytest.mark.parametrize("broken", ["missing_config", "malformed_config", "missing_binary"])
def test_recovery_stop_works_with_incomplete_files(
    native_bridge, tmp_path, install_flock, speaker_shell, broken
):
    call, proc, runtime, port, config, token = native_bridge
    package = tmp_path / "recovery-package"
    (package / "lib").mkdir(parents=True)
    shutil.copy2(ROOT / "speaker/lib/locks.sh", package / "lib/locks.sh")
    tools = tmp_path / "tools"
    tools.mkdir()
    install_flock(tools)
    source = (
        (ROOT / "speaker/start.sh")
        .read_text()
        .replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={tools}:{os.environ['PATH']}")
    )
    start = package / "start.sh"
    start.write_text(source)
    if broken == "missing_config":
        config.unlink()
    elif broken == "malformed_config":
        config.write_text("broken")
    else:
        (runtime / "bin/xiaoai_bridge").unlink()
    result = subprocess.run(
        [*speaker_shell, str(start), "stop"],
        env={
            **os.environ,
            "XIAOAI_BRIDGE_DIR": str(runtime),
            "XIAOAI_BRIDGE_RUNTIME": str(runtime),
            "XIAOAI_BRIDGE_CONFIG": str(config),
        },
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0 and not result.stderr, result.stdout + result.stderr
    assert proc.wait(timeout=3) == 0
