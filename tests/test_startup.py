"""Run production startup/installer/C workers and real authenticated HTTP against faults."""

import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


@pytest.fixture(scope="session")
def startup_binary():
    subprocess.run(["make", "-s", "-C", str(ROOT / "speaker")], check=True)
    return ROOT / "build/native/xiaoai_bridge"


class Endpoint:
    def __init__(self, root, binary, shell, install_flock, steps, installer):
        self.root, self.shell, self.installer = root, shell, installer
        self.runtime = root / "runtime"
        self.runtime.mkdir()
        self.data = root / "data"
        self.data.mkdir()
        self.bridge = self.data / "xiaoai_bridge"
        self.package = root / "package" if installer else self.bridge
        self.package.mkdir()
        for name in ("lib", "tools"):
            shutil.copytree(ROOT / "speaker" / name, self.package / name)
        for name in ("config.example.json", "xiaoai_boot.sh"):
            shutil.copy2(ROOT / "speaker" / name, self.package / name)
        self.binary = self.package / "bin/xiaoai_bridge"
        self.binary.parent.mkdir()
        shutil.copy2(binary, self.binary)
        tools = root / "tools"
        tools.mkdir()
        install_flock(tools)
        executable(tools / "id", "printf 0")
        executable(tools / "amixer", "exit 99")  # An invocation would fail the installation.
        executable(
            tools / "df",
            "printf 'Filesystem blocks used available mount\\nfixture 30000 1 29999 fixture\\n'",
        )
        if shutil.which("sha256sum") is None:
            executable(tools / "sha256sum", 'exec shasum -a 256 "$@"')
        self.env = {
            **os.environ,
            "XIAOAI_BRIDGE_DIR": str(self.bridge),
            "XIAOAI_BRIDGE_RUNTIME": str(self.runtime),
            "XIAOAI_BRIDGE_CONFIG": str(self.bridge / "config.json"),
        }
        self.start = self.package / "start.sh"
        self.start.write_text(
            (ROOT / "speaker/start.sh")
            .read_text()
            .replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={tools}:{os.environ['PATH']}")
        )
        self.start.chmod(0o755)
        self.port = free_port()
        self.token = "synthetic-startup-token-for-local-testing-00000000"
        self.config = self.package / "config.json"
        adapter = ROOT / "tests/startup_adapter.py"
        adapter.chmod(0o755)
        self.values = {
            "token": self.token,
            "device_id": "synthetic-startup-speaker",
            "bind": "127.0.0.1",
            "port": self.port,
            "runtime_dir": str(self.runtime),
            "adapter": str(adapter),
            "enable_listener_adapter": False,
        }
        self.config.write_text(json.dumps(self.values))
        self.config.chmod(0o600)
        self.set_steps(steps)
        self.proc = None
        if installer:
            cron_service = root / "cron-service"
            executable(cron_service, "exit 0")
            self.install_script = self.package / "install.sh"
            self.install_script.write_text(
                (ROOT / "speaker/install.sh")
                .read_text()
                .replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={tools}:{os.environ['PATH']}")
                .replace("/data/", str(self.data) + "/")
                .replace("df -k /data", f"df -k '{self.data}'")
                .replace("RUNTIME=/tmp", f"RUNTIME={self.runtime}")
                .replace("/etc/init.d/cron", str(cron_service))
            )
            self.cron = self.data / "etc/crontabs/root"
            self.cron.parent.mkdir(parents=True)
            self.original_cron = "# Existing tasks\n*/5 * * * * synthetic-oauth-refresh\n"
            self.cron.write_text(self.original_cron)
            self.boot = self.data / "xiaoai_boot.sh"
            self.original_boot = "#!/bin/sh\n# Existing Squeezelite boot behavior\n"
            self.boot.write_text(self.original_boot)
            self.checksums()

    def set_steps(self, steps):
        (self.runtime / "probe-fixture.json").write_text(json.dumps(steps))

    def checksums(self):
        (self.package / "SHA256SUMS").write_text(
            "".join(
                hashlib.sha256(file.read_bytes()).hexdigest()
                + "  "
                + str(file.relative_to(self.package))
                + "\n"
                for file in sorted(self.package.rglob("*"))
                if file.is_file() and file.name not in {"config.json", "SHA256SUMS"}
            )
        )

    def launch(self, stderr=subprocess.DEVNULL):
        self.proc = subprocess.Popen(
            [str(self.binary), "--config", str(self.config)],
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            env=self.env,
        )
        return self.proc

    def command(self, action="start"):
        path = self.install_script if self.installer else self.start
        return subprocess.run(
            [*self.shell, str(path), action],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=95,
        )

    def health(self):
        binary = self.bridge / "bin/xiaoai_bridge" if self.installer else self.binary
        return subprocess.run(
            [str(binary), "--health"], env=self.env, capture_output=True, text=True, timeout=4
        )

    def request(self, path="/api/health"):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            client.request("GET", path, headers={"Authorization": "Bearer " + self.token})
            result = client.getresponse()
            return result.status, json.loads(result.read())
        finally:
            client.close()

    def wait_for(self, predicate, budget=5):
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except OSError, http.client.HTTPException:
                pass
            time.sleep(0.05)
        pytest.fail("synthetic endpoint condition timed out: " + self.logs())

    def attempts(self):
        path = self.runtime / "probe-attempts.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def logs(self):
        return "".join(file.read_text() for file in sorted(self.runtime.glob("xiaoai_bridge.log*")))

    def shorten_wait(self):
        self.start.write_text(
            self.start.read_text().replace("STARTUP_BUDGET=75", "STARTUP_BUDGET=2")
        )
        if self.installer:
            self.checksums()

    def close(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
            self.proc.wait(timeout=8)
        subprocess.run(
            [*self.shell, str(self.start), "stop"], env=self.env, capture_output=True, timeout=18
        )


@pytest.fixture
def endpoint_factory(startup_binary, install_flock, speaker_shell, socket_enabled):
    instances = []
    with tempfile.TemporaryDirectory(prefix="xiaoai-startup-", dir="/tmp") as folder:

        def create(steps=None, installer=False):
            root = Path(folder) / str(len(instances))
            root.mkdir()
            endpoint = Endpoint(
                root, startup_binary, speaker_shell, install_flock, steps or [{}], installer
            )
            instances.append(endpoint)
            return endpoint

        yield create
        for endpoint in reversed(instances):
            endpoint.close()


@pytest.mark.parametrize("delay", [5.6, 11])
def test_probe_longer_than_old_time_budgets_becomes_ready(endpoint_factory, delay):
    endpoint = endpoint_factory([{"delay": delay}, {}])
    started = time.monotonic()
    result = endpoint.command()
    assert result.returncode == 0, result.stdout + result.stderr + endpoint.logs()
    assert delay <= time.monotonic() - started < 20
    assert endpoint.request()[0] == 200 and len(endpoint.attempts()) == 1
    assert "probe_timeout" not in endpoint.logs()


@pytest.mark.parametrize(
    "kind,event",
    [
        ("exit", "probe_exit_nonzero"),
        ("invalid_json", "probe_invalid_json"),
        ("invalid_response", "probe_invalid_response"),
        ("overflow", "probe_output_limit"),
    ],
)
def test_initial_probe_failure_retries_real_worker_without_restart(endpoint_factory, kind, event):
    endpoint = endpoint_factory([{"kind": kind}, {}])
    proc = endpoint.launch()
    endpoint.wait_for(lambda: event in endpoint.logs())
    status, health = endpoint.request()
    assert status == 503 and health["probe_status"] == event and not health["startup_failed"]
    assert endpoint.health().returncode == 4
    endpoint.wait_for(lambda: endpoint.request()[0] == 200, 6)
    assert proc.poll() is None and len(endpoint.attempts()) == 2
    assert "probe_retry_scheduled" in endpoint.logs()
    assert "synthetic-sensitive-fixture-marker" not in endpoint.logs()
    assert endpoint.token not in endpoint.logs()


def test_initial_probe_timeout_and_term_grace_retry_fit_startup_budget(endpoint_factory):
    endpoint = endpoint_factory([{"delay": 30, "ignore_term": True}, {}])
    started = time.monotonic()
    result = endpoint.command()
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stdout + result.stderr + endpoint.logs()
    assert 20 <= elapsed < 35 and endpoint.request()[0] == 200
    assert len(endpoint.attempts()) == 2
    assert "probe_deadline_exceeded" in endpoint.logs() and "probe_timeout" in endpoint.logs()
    assert "probe_failed_retrying" in endpoint.logs()


def test_three_invalid_probes_fail_with_diagnostics_and_rollback_then_retry(endpoint_factory):
    endpoint = endpoint_factory([{"kind": "invalid_json"}], installer=True)
    result = endpoint.command("--apply")
    assert result.returncode != 0 and "Startup diagnostics:" in result.stderr
    assert len(endpoint.attempts()) == 3
    assert endpoint.boot.read_text() == endpoint.original_boot
    assert endpoint.cron.read_text() == endpoint.original_cron and not endpoint.bridge.exists()
    backup = endpoint.data / "xiaoai_bridge.backup"
    assert (backup / "status").read_text().strip() == "failed_restored"
    identity = json.loads((backup / "retry/config.json").read_text())
    logs = endpoint.logs()
    assert all(
        event in logs
        for event in (
            "probe_invalid_json",
            "startup_probe_retries_exhausted",
            "start event=probe_failed",
            "installation event=startup_failed",
            "installation event=failed_restored",
        )
    )
    assert endpoint.token not in logs and "synthetic-sensitive-fixture-marker" not in logs
    assert endpoint.command("--rollback").returncode == 0
    endpoint.set_steps([{}])
    result = endpoint.command("--apply")
    assert result.returncode == 0, result.stdout + result.stderr + endpoint.logs()
    assert (backup / "status").read_text().strip() == "installed"
    assert json.loads((endpoint.bridge / "config.json").read_text()) == identity
    assert endpoint.request()[0] == 200
    assert endpoint.command("--uninstall").returncode == 0


def test_single_failed_first_probe_does_not_roll_back_installation(endpoint_factory):
    endpoint = endpoint_factory([{"kind": "exit"}, {}], installer=True)
    result = endpoint.command("--apply")
    assert result.returncode == 0, result.stdout + result.stderr + endpoint.logs()
    assert (endpoint.data / "xiaoai_bridge.backup/status").read_text().strip() == "installed"
    assert len(endpoint.attempts()) == 2 and endpoint.request()[0] == 200
    assert "installation event=failed_restored" not in endpoint.logs()


def test_process_initialization_failure_records_stage_and_exit_code(endpoint_factory):
    endpoint = endpoint_factory()
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", endpoint.port))
        occupied.listen()
        result = endpoint.command()
    assert result.returncode != 0 and "process_exited (code=2)" in result.stderr
    assert "init_http_bind_failed" in endpoint.logs()
    assert "start event=process_exited code=2" in endpoint.logs()
    assert endpoint.token not in endpoint.logs()


def test_pending_probe_deadline_has_specific_diagnostic(endpoint_factory):
    endpoint = endpoint_factory([{"delay": 30}])
    endpoint.shorten_wait()
    result = endpoint.command()
    assert result.returncode != 0 and "probe_pending_deadline" in result.stderr
    assert "start event=probe_pending" in endpoint.logs()


def test_existing_process_is_waited_for_without_starting_a_duplicate(endpoint_factory):
    endpoint = endpoint_factory([{"delay": 2}])
    proc = endpoint.launch()
    endpoint.wait_for(lambda: len(endpoint.attempts()) == 1)
    result = endpoint.command()
    assert result.returncode == 0 and proc.poll() is None
    assert len(endpoint.attempts()) == 1 and "waiting_existing_process" in endpoint.logs()


def test_service_not_ready_deadline_retains_existing_process(endpoint_factory):
    endpoint = endpoint_factory()
    proc = endpoint.launch()
    endpoint.wait_for(lambda: endpoint.request()[0] == 200)
    endpoint.values["port"] = free_port()
    endpoint.config.write_text(json.dumps(endpoint.values))
    endpoint.shorten_wait()
    result = endpoint.command()
    assert result.returncode != 0 and "service_not_ready_deadline" in result.stderr
    assert proc.poll() is None and "start event=service_not_ready" in endpoint.logs()


def test_daemon_rotates_log_while_running_and_never_copies_probe_payload(endpoint_factory):
    endpoint = endpoint_factory([{"kind": "invalid_json"}, {}])
    log = endpoint.runtime / "xiaoai_bridge.log"
    with log.open("ab") as sink:
        endpoint.launch(stderr=sink)  # The same inherited regular-file stderr as start.sh.
    endpoint.wait_for(lambda: "probe_invalid_json" in endpoint.logs())
    with log.open("a") as stream:
        stream.write("x" * (65530 - log.stat().st_size))
    endpoint.wait_for(lambda: endpoint.request()[0] == 200, 6)
    assert log.with_name(log.name + ".1").exists()
    assert all(file.stat().st_size <= 65536 for file in endpoint.runtime.glob("xiaoai_bridge.log*"))
    assert "synthetic-sensitive-fixture-marker" not in endpoint.logs()
    assert endpoint.logs().count("bridge event=probe_ready") == 1


def test_health_cli_uses_one_deadline_for_a_stalled_real_http_connection(endpoint_factory):
    endpoint = endpoint_factory()
    with socket.socket() as server:
        server.bind(("127.0.0.1", endpoint.port))
        server.listen()

        def stall():
            client, _ = server.accept()
            with client:
                client.recv(4096)
                time.sleep(2.5)

        thread = threading.Thread(target=stall)
        thread.start()
        started = time.monotonic()
        result = endpoint.health()
        elapsed = time.monotonic() - started
        thread.join(timeout=4)
    assert result.returncode == 1 and not result.stdout and not result.stderr
    assert 1.8 <= elapsed < 2.5


def test_native_initialization_error_is_visible_on_stderr_without_log_directory(endpoint_factory):
    endpoint = endpoint_factory()
    endpoint.values["runtime_dir"] = str(endpoint.root / "missing")
    endpoint.config.write_text(json.dumps(endpoint.values))
    result = subprocess.run(
        [str(endpoint.binary), "--config", str(endpoint.config)],
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 2 and "init_pid_open_failed" in result.stderr
    assert endpoint.token not in result.stderr
