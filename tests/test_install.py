"""Run the actual installer against a synthetic filesystem, never a speaker."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + text + "\n")
    path.chmod(0o755)


@pytest.fixture
def installation(tmp_path, install_flock, speaker_shell):
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
    install_flock(tools)
    executable(
        tools / "df",
        "printf 'Filesystem blocks used available mount\\nsynthetic 30000 1000 29000 local\\n'",
    )
    executable(tools / "amixer", ":")
    if shutil.which("sha256sum") is None:
        executable(tools / "sha256sum", 'exec shasum -a 256 "$@"')
    cron = tmp_path / "cron-service"
    executable(cron, 'printf "cron\\n" >> "$INSTALL_CALLS"\n[ ! -f "$INSTALL_FAIL_CRON" ]')
    executable(package / "bin/xiaoai_bridge", "exit 0")
    executable(
        package / "start.sh",
        """printf "%s\\n" "${1:-start}" >> "$INSTALL_CALLS"
if [ "${1:-start}" = start ] && [ -f "$INSTALL_FAIL_START" ];then
    [ ! -f "$INSTALL_ADD_TASK" ] || printf '0 * * * * added-during-failure\\n' >> "$INSTALL_CRON"
    exit 1
fi""",
    )
    shutil.copy2(ROOT / "speaker/lib/locks.sh", package / "lib/locks.sh")
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
        .replace("RUNTIME=/tmp", f"RUNTIME={runtime}")
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
    env = {
        **os.environ,
        "INSTALL_CALLS": str(calls),
        "INSTALL_FAIL_START": str(tmp_path / "fail-start"),
        "INSTALL_FAIL_CRON": str(tmp_path / "fail-cron"),
        "INSTALL_ADD_TASK": str(tmp_path / "add-task"),
        "INSTALL_CRON": str(cron_file),
    }

    def call(mode):
        return subprocess.run(
            [*speaker_shell, str(installer), mode],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
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
    assert call("--uninstall").returncode == 0


def test_activation_changes_only_one_include_and_keeps_refreshed_values(
    tmp_path, install_flock, speaker_shell
):
    data = tmp_path / "data"
    bridge = data / "xiaoai_bridge"
    (bridge / "adapter").mkdir(parents=True)
    (bridge / "lib").mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    executable(tools / "id", "printf 0")
    install_flock(tools)
    if shutil.which("sha256sum") is None:
        executable(tools / "sha256sum", 'exec shasum -a 256 "$@"')
    shutil.copy2(ROOT / "speaker/lib/common.sh", bridge / "lib/common.sh")
    shutil.copy2(ROOT / "speaker/lib/locks.sh", bridge / "lib/locks.sh")
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
            [*speaker_shell, str(activation), mode],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
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


def test_failed_start_restores_boot_cron_then_retries_with_same_identity(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    original_without_boot = original_cron.splitlines()[0:2]
    cron.write_text("\n".join(original_without_boot) + "\n")
    (data.parent / "fail-start").touch()
    (data.parent / "add-task").touch()
    result = call("--apply")
    assert result.returncode != 0 and "Startup failed" in result.stderr
    assert not (data / "xiaoai_bridge").exists()
    assert (data / "xiaoai_boot.sh").read_text() == original_boot
    assert "/xiaoai_boot.sh" not in cron.read_text()
    assert "synthetic-oauth-refresh" in cron.read_text()
    assert "added-during-failure" in cron.read_text()
    backup = data / "xiaoai_bridge.backup"
    assert (backup / "status").read_text().strip() == "failed_restored"
    saved = json.loads((backup / "retry/config.json").read_text())
    assert call("--rollback").returncode == 0
    (data.parent / "fail-start").unlink()
    assert call("--apply").returncode == 0
    assert json.loads((data / "xiaoai_bridge/config.json").read_text()) == saved
    assert (data / "xiaoai_bridge.backup.retained").is_dir()
    assert cron.read_text().count("/xiaoai_boot.sh") == 1
    assert not any(
        word in calls.read_text() for word in ("squeezelite", "snapclient", "upmpdcli", "listener")
    )


@pytest.mark.parametrize("missing", ["config.json", "bin/xiaoai_bridge", None])
def test_legacy_failed_install_rollback_uses_new_package_not_broken_start(installation, missing):
    call, data, cron, original_cron, original_boot, calls = installation
    assert call("--apply").returncode == 0
    backup = data / "xiaoai_bridge.backup"
    (backup / "status").unlink()  # Version 0.1.0 has no transaction marker.
    bridge = data / "xiaoai_bridge"
    executable(bridge / "start.sh", 'printf broken-old-start >> "$INSTALL_CALLS"\nflock -w 5 9')
    if missing:
        (bridge / missing).unlink()
    with cron.open("a") as stream:
        stream.write("0 * * * * new-oauth-task\n")
    result = call("--rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "broken-old-start" not in calls.read_text()
    assert (data / "xiaoai_boot.sh").read_text() == original_boot
    assert "new-oauth-task" in cron.read_text()
    assert call("--rollback").returncode == 0
    assert call("--apply").returncode == 0


def test_legacy_partial_directory_swap_is_recoverable(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    backup = data / "xiaoai_bridge.backup"
    (backup / "bridge/bin").mkdir(parents=True)
    (backup / "xiaoai_boot.sh").write_text(original_boot)
    (backup / "crontab.root").write_text(original_cron)
    (backup / "bridge/config.json").write_text(
        '{"token":"synthetic-old","enable_listener_adapter":false}'
    )
    (data / "xiaoai_bridge.previous").mkdir()
    (data / "xiaoai_bridge.new").mkdir()
    (data / "xiaoai_bridge.new/partial-file").write_text("incomplete")
    result = call("--rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (data / "xiaoai_bridge/config.json").exists()
    assert (data / "xiaoai_bridge.incomplete/partial-file").read_text() == "incomplete"
    assert (backup / "legacy_previous").is_dir()
    assert not (data / "xiaoai_bridge.previous").exists()


def test_failed_install_and_partial_uninstall_are_idempotent(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    assert call("--apply").returncode == 0
    (data / "xiaoai_bridge/config.json").unlink()
    (data / "xiaoai_bridge/bin/xiaoai_bridge").unlink()
    (data / "xiaoai_bridge.new").mkdir()
    (data / "xiaoai_bridge.uninstalled").mkdir()
    assert call("--uninstall").returncode == 0
    assert (data / "xiaoai_bridge.uninstalled.1").is_dir()
    assert not (data / "xiaoai_bridge").exists()
    assert (data / "xiaoai_bridge.incomplete").is_dir()
    assert cron.read_text() == original_cron
    assert call("--uninstall").returncode == 0


def test_cron_reload_failure_leaves_explicit_recoverable_state(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    (data.parent / "fail-cron").touch()
    result = call("--apply")
    assert result.returncode != 0
    backup = data / "xiaoai_bridge.backup"
    assert (backup / "status").read_text().strip() == "recovery_required"
    (data.parent / "fail-cron").unlink()
    assert call("--rollback").returncode == 0
    assert (data / "xiaoai_boot.sh").read_text() == original_boot
    assert cron.read_text() == original_cron
    assert call("--apply").returncode == 0


def test_pending_legacy_transaction_cannot_be_mistaken_for_success(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    assert call("--apply").returncode == 0
    (data / "xiaoai_bridge.backup/status").unlink()
    result = call("--apply")
    assert result.returncode != 0 and "--rollback" in result.stdout
    assert "Same checked package" not in result.stdout


def test_upgrade_failure_restores_previous_bridge_and_retains_retry_data(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    old = data / "xiaoai_bridge"
    (old / "bin").mkdir(parents=True)
    (old / "config.json").write_text(
        '{"device_id":"synthetic-stable","token":"synthetic-stable-token","enable_listener_adapter":false}'
    )
    (old / "old-version-marker").write_text("healthy-previous-code")
    executable(old / "start.sh", "exit 77")
    package_start = data.parent / "package/start.sh"
    source = package_start.read_text().replace(
        "exit 1\nfi", 'rm "$INSTALL_FAIL_START"\n    exit 1\nfi'
    )
    package_start.write_text(source)
    hashes = data.parent / "package/SHA256SUMS"
    lines = [row for row in hashes.read_text().splitlines() if not row.endswith("  start.sh")]
    lines.append(hashlib.sha256(package_start.read_bytes()).hexdigest() + "  start.sh")
    hashes.write_text("\n".join(lines) + "\n")
    (data.parent / "fail-start").touch()
    result = call("--apply")
    assert result.returncode != 0, result.stdout + result.stderr
    assert (old / "old-version-marker").read_text() == "healthy-previous-code"
    saved = json.loads((old / "config.json").read_text())
    assert saved["device_id"] == "synthetic-stable" and saved["token"] == "synthetic-stable-token"
    assert (data / "xiaoai_boot.sh").read_text() == original_boot
    assert cron.read_text() == original_cron
    assert (data / "xiaoai_bridge.backup/status").read_text().strip() == "failed_restored"
    assert call("--apply").returncode == 0
    assert json.loads((old / "config.json").read_text())["device_id"] == "synthetic-stable"


def test_partial_listener_detachment_preserves_values_and_resumes_failed_reload(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    assert call("--apply").returncode == 0
    installer = data.parent / "package/install.sh"
    listener_service = data.parent / "listener-service"
    installer.write_text(
        installer.read_text().replace("/etc/init.d/listener", str(listener_service))
    )
    executable(
        listener_service,
        """case "$1" in
status) [ -f "$INSTALL_FAIL_START" ] ;;
stop) rm -f "$INSTALL_FAIL_START" ;;
start) [ ! -f "$INSTALL_FAIL_CRON" ] ;;
esac""",
    )
    original = "KWS=existing\nSTT=existing\nEOAIC2_AGENT_ID=conversation.synthetic\nHA_TOKEN=synthetic-refreshed\nSPEAK=existing_doubao\n. /data/kws_bootstrap.sh\n"
    line = f". {data}/xiaoai_bridge/lib/bootstrap.sh\n"
    (data / "listener").write_text(
        original.replace(". /data/kws_bootstrap.sh", line + ". /data/kws_bootstrap.sh")
    )
    (data.parent / "fail-cron").touch()
    result = call("--rollback")
    assert result.returncode != 0
    assert (data / "listener").read_text() == original
    assert (data.parent / "runtime/xiaoai_bridge.listener_restore.pending").exists()
    (data.parent / "fail-cron").unlink()
    result = call("--rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (data / "listener").read_text() == original
    assert not (data.parent / "runtime/xiaoai_bridge.listener_restore.pending").exists()


def test_installer_cannot_race_the_existing_boot_lock(installation):
    import fcntl

    call, data, cron, original_cron, original_boot, calls = installation
    path = data.parent / "runtime/xiaoai_boot.lock"
    with path.open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        result = call("--apply")
        assert result.returncode != 0 and "Boot tasks are active" in result.stdout
        assert not (data / "xiaoai_bridge.backup").exists()
        assert (data / "xiaoai_boot.sh").read_text() == original_boot
        assert cron.read_text() == original_cron


def test_failed_uninstall_cannot_be_reported_as_installed_and_can_resume(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    assert call("--apply").returncode == 0
    (data.parent / "fail-cron").touch()
    assert call("--uninstall").returncode != 0
    assert (data / "xiaoai_bridge.backup/status").read_text().strip() == "recovery_required"
    assert call("--apply").returncode != 0
    (data.parent / "fail-cron").unlink()
    assert call("--uninstall").returncode == 0
    assert call("--apply").returncode == 0


def test_interrupted_legacy_recovery_imports_a_resumable_snapshot(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    assert call("--apply").returncode == 0
    backup = data / "xiaoai_bridge.backup"
    (backup / "status").unlink()
    (backup / "ready").unlink()
    (backup / "installed_boot").unlink()
    (data.parent / "fail-cron").touch()
    assert call("--rollback").returncode != 0
    assert (backup / "status").read_text().strip() == "recovery_required"
    assert (backup / "ready").exists()
    (data.parent / "fail-cron").unlink()
    assert call("--rollback").returncode == 0
    assert call("--apply").returncode == 0


def test_only_enabled_adapter_is_reloaded_to_receive_fixed_shell_functions(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    package = data.parent / "package"
    config = package / "config.json"
    value = json.loads(config.read_text())
    value["enable_listener_adapter"] = True
    config.write_text(json.dumps(value))
    helper = package / "lib/listener-service.sh"
    executable(helper, 'printf "listener-%s\\n" "$1" >> "$INSTALL_CALLS"')
    hashes = package / "SHA256SUMS"
    with hashes.open("a") as stream:
        stream.write(
            hashlib.sha256(helper.read_bytes()).hexdigest() + "  lib/listener-service.sh\n"
        )
    result = call("--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().count("listener-restart") == 1
    assert call("--apply").returncode == 0
    assert calls.read_text().count("listener-restart") == 1
