"""Run the actual installer against a synthetic filesystem, never a speaker."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + text + "\n")
    path.chmod(0o755)


@pytest.fixture
def installation(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    package = tmp_path / "package"
    package.mkdir()
    for directory in ("bin", "lib", "tools"):
        (package / directory).mkdir()
    executable(tools / "id", "printf 0")
    executable(
        tools / "flock",
        f"exec '{sys.executable}' -c 'import fcntl;fcntl.flock(9,fcntl.LOCK_EX|fcntl.LOCK_NB)'",
    )
    executable(
        tools / "df",
        "printf 'Filesystem blocks used available mount\\nsynthetic 30000 1000 29000 local\\n'",
    )
    executable(tools / "amixer", ":")
    if shutil.which("sha256sum") is None:
        executable(tools / "sha256sum", 'exec shasum -a 256 "$@"')
    cron = tmp_path / "cron-service"
    executable(cron, ":")
    executable(package / "bin/xiaoai_bridge", "exit 0")
    executable(package / "start.sh", 'printf "%s\\n" "${1:-start}" >> "$INSTALL_CALLS"')
    executable(package / "lib/device.sh", ":")
    executable(package / "tools/activate.sh", ":")
    (package / "config.example.json").write_text("{}")
    (package / "config.json").write_text(
        json.dumps({"token": "synthetic-only", "enable_listener_adapter": False})
    )
    executable(package / "xiaoai_boot.sh", "# Updated synthetic boot entry")
    hashes = [
        hashlib.sha256(f.read_bytes()).hexdigest() + "  " + str(f.relative_to(package))
        for f in package.rglob("*")
        if f.is_file() and f.name != "config.json"
    ]
    (package / "SHA256SUMS").write_text("\n".join(hashes) + "\n")
    source = (
        (ROOT / "speaker/install.sh")
        .read_text()
        .replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={tools}:{os.environ['PATH']}")
        .replace("/data/", str(data) + "/")
        .replace("df -k /data", f"df -k '{data}'")
        .replace("/tmp/xiaoai_bridge.install.lock", str(runtime / "install.lock"))
        .replace("/etc/init.d/cron", str(cron))
    )
    installer = package / "install.sh"
    installer.write_text(source)
    cron_file = data / "etc/crontabs/root"
    cron_file.parent.mkdir(parents=True)
    original_cron = f"# Keep existing refresh\n*/5 * * * * synthetic-oauth-refresh\n* * * * * /bin/sh {data}/xiaoai_boot.sh\n"
    cron_file.write_text(original_cron)
    original_boot = "#!/bin/sh\n# Already disables Squeezelite via Cron\n"
    (data / "xiaoai_boot.sh").write_text(original_boot)
    calls = tmp_path / "calls"
    env = {**os.environ, "INSTALL_CALLS": str(calls)}

    def call(mode):
        return subprocess.run(
            ["sh", str(installer), mode], env=env, capture_output=True, text=True, timeout=5
        )

    return call, data, cron_file, original_cron, original_boot, calls


def test_installer_preserves_cron_is_idempotent_and_first_install_rolls_back(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    assert call("--dry-run").returncode == 0
    assert not (data / "xiaoai_bridge.backup").exists()
    result = call("--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert cron.read_text() == original_cron
    assert (data / "xiaoai_bridge/config.json").stat().st_mode & 0o077 == 0
    assert (data / "xiaoai_bridge/tools/activate.sh").stat().st_mode & 0o100
    assert call("--apply").returncode == 0
    assert cron.read_text().count("/xiaoai_boot.sh") == 1
    with cron.open("a") as stream:
        stream.write("0 * * * * newly-added-task\n")
    result = call("--rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (data / "xiaoai_bridge").exists()
    assert (data / "xiaoai_bridge.failed").exists()
    assert (data / "xiaoai_boot.sh").read_text() == original_boot
    assert "newly-added-task" in cron.read_text() and "synthetic-oauth-refresh" in cron.read_text()


def test_uninstall_keeps_the_squeezelite_boot_mechanism(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    assert call("--apply").returncode == 0
    result = call("--uninstall")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (data / "xiaoai_bridge").exists()
    assert (data / "xiaoai_boot.sh").exists() and cron.read_text() == original_cron
    assert call("--uninstall").returncode != 0


def test_activation_changes_only_one_include_and_keeps_refreshed_values(tmp_path):
    data = tmp_path / "data"
    bridge = data / "xiaoai_bridge"
    (bridge / "adapter").mkdir(parents=True)
    (bridge / "lib").mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    executable(tools / "id", "printf 0")
    executable(
        tools / "flock",
        f"exec '{sys.executable}' -c 'import fcntl;fcntl.flock(9,fcntl.LOCK_EX|fcntl.LOCK_NB)'",
    )
    if shutil.which("sha256sum") is None:
        executable(tools / "sha256sum", 'exec shasum -a 256 "$@"')
    shutil.copy2(ROOT / "speaker/lib/common.sh", bridge / "lib/common.sh")
    executable(bridge / "lib/listener-service.sh", ":")
    original = data / "porcupine_launcher_kws"
    original.write_text("# Synthetic original launcher\n")
    candidate = bridge / "adapter/listener.sh"
    executable(candidate, ":")
    (bridge / "adapter/review.json").write_text(
        json.dumps(
            {
                "files": {
                    "listener.sh": {
                        "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                        "output_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    }
                }
            }
        )
    )
    config = bridge / "config.json"
    config.write_text('{"enable_listener_adapter":false}')
    listener = data / "listener"
    original_values = 'KWS="existing"\nSTT="existing"\nHA_TOKEN="synthetic"\nSPEAK=/data/tts_doubao\n. /data/kws_bootstrap.sh\n'
    listener.write_text(original_values)
    source = (
        (ROOT / "speaker/tools/activate.sh")
        .read_text()
        .replace("/data/", str(data) + "/")
        .replace("/tmp/xiaoai_bridge.activate.lock", str(runtime / "activate.lock"))
    )
    activation = tmp_path / "activate.sh"
    activation.write_text(source)
    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "XIAOAI_BRIDGE_DIR": str(bridge),
        "XIAOAI_BRIDGE_CONFIG": str(config),
        "XIAOAI_BRIDGE_RUNTIME": str(runtime),
    }

    def call(mode):
        return subprocess.run(
            ["sh", str(activation), mode], env=env, capture_output=True, text=True, timeout=5
        )

    assert call("--dry-run").returncode == 0 and listener.read_text() == original_values
    (runtime / "xiaoai_bridge.voice").write_text(str(os.getpid()) + "\n")
    assert call("--apply").returncode != 0 and listener.read_text() == original_values
    (runtime / "xiaoai_bridge.voice").unlink()
    result = call("--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    line = f". {bridge}/lib/bootstrap.sh\n"
    assert listener.read_text().replace(line, "") == original_values
    assert json.loads(config.read_text())["enable_listener_adapter"]
    assert call("--apply").returncode == 0 and listener.read_text().count(line) == 1
    listener.write_text(
        listener.read_text().replace('HA_TOKEN="synthetic"', 'HA_TOKEN="synthetic-refreshed"')
    )
    assert call("--remove").returncode == 0
    assert listener.read_text() == original_values.replace(
        'HA_TOKEN="synthetic"', 'HA_TOKEN="synthetic-refreshed"'
    )
