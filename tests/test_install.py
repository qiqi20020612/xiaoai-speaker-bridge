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


def private_adapter(data, output, led=False):
    original = data / "porcupine_launcher_kws"
    shutil.copy2(ROOT / "tests/fixtures/lx06_launcher.sh", original)
    guard = data / "kws_guard.sh"
    shutil.copy2(ROOT / "tests/fixtures/lx06_kws_guard.sh", guard)
    command = [
        sys.executable,
        str(ROOT / "speaker/tools/prepare_adapters.py"),
        "--listener",
        str(original),
        "--kws-guard",
        str(guard),
        "--output",
        str(output),
    ]
    if led:
        original_led = data / "airplay_volume_led"
        shutil.copy2(ROOT / "tests/fixtures/lx06_airplay_led.sh", original_led)
        command.extend(["--airplay-led", str(original_led)])
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return output / "listener.sh"


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
    shutil.copy2(ROOT / "speaker/lib/adapter-check.sh", package / "lib/adapter-check.sh")
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
    listener_service = tmp_path / "listener-service"
    executable(listener_service, '[ "$1" != status ]')
    source = (
        (ROOT / "speaker/install.sh")
        .read_text()
        .replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={tools}:{os.environ['PATH']}")
        .replace("/data/", str(data) + "/")
        .replace("df -k /data", f"df -k '{data}'")
        .replace("RUNTIME=/tmp", f"RUNTIME={runtime}")
        .replace("/etc/init.d/cron", str(cron))
        .replace("/etc/init.d/listener", str(listener_service))
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
            timeout=15,
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
    shutil.copy2(ROOT / "speaker/lib/volume-recovery.sh", bridge / "lib/volume-recovery.sh")
    shutil.copy2(ROOT / "speaker/lib/adapter-check.sh", bridge / "lib/adapter-check.sh")
    executable(bridge / "lib/listener-service.sh", ":")
    private_adapter(data, bridge / "adapter")
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
    private_adapter(data, package / "adapter")
    helper = package / "lib/listener-service.sh"
    executable(helper, 'printf "listener-%s\\n" "$1" >> "$INSTALL_CALLS"')
    hashes = package / "SHA256SUMS"
    with hashes.open("a") as stream:
        stream.write(
            hashlib.sha256(helper.read_bytes()).hexdigest() + "  lib/listener-service.sh\n"
        )
    result = call("--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().count("listener-start") == 1
    assert call("--apply").returncode == 0
    assert calls.read_text().count("listener-start") == 1


@pytest.mark.parametrize("location", ["package", "installed", "retry"])
@pytest.mark.parametrize("mode", ["--dry-run", "--apply"])
def test_upgrade_rejects_legacy_adapter_before_mutating_installation(installation, location, mode):
    call, data, cron, original_cron, original_boot, calls = installation
    dirs = {
        "package": data.parent / "package/adapter",
        "installed": data / "xiaoai_bridge/adapter",
        "retry": data / "xiaoai_bridge.backup/retry/adapter",
    }
    adapter = dirs[location]
    private_adapter(data, adapter)
    review = adapter / "review.json"
    values = json.loads(review.read_text())
    values["format"] = 1
    review.write_text(json.dumps(values))
    if location == "retry":
        (data / "xiaoai_bridge.backup/status").write_text("failed_restored\n")
    result = call(mode)
    assert result.returncode != 0 and "Regenerate and review" in result.stderr
    assert cron.read_text() == original_cron
    assert (data / "xiaoai_boot.sh").read_text() == original_boot
    assert not calls.exists() and not (data / "xiaoai_bridge.new").exists()
    assert review.read_text() == json.dumps(values)
    assert not (data / "xiaoai_bridge.backup.retained").exists()


@pytest.mark.parametrize("damage", ["hash", "duplicate_led", "order", "source"])
def test_same_package_cannot_bypass_adapter_compatibility(installation, damage):
    call, data, cron, original_cron, original_boot, calls = installation
    private_adapter(data, data.parent / "package/adapter")
    assert call("--apply").returncode == 0
    # No package adapter: the installed private copy must be checked even on a no-op upgrade.
    shutil.rmtree(data.parent / "package/adapter")
    adapter = data / "xiaoai_bridge/adapter"
    listener = adapter / "listener.sh"
    if damage == "source":
        (data / "porcupine_launcher_kws").write_text("# changed original\n")
    else:
        listener.write_text(
            listener.read_text()
            + ("/bin/show_led 1\n" if damage == "duplicate_led" else "# change\n")
        )
        if damage == "order":
            listener.write_text(listener.read_text().replace("bridge_voice_begin || exit 1\n", ""))
        if damage != "hash":
            review = adapter / "review.json"
            values = json.loads(review.read_text())
            values["files"]["listener.sh"]["output_sha256"] = hashlib.sha256(
                listener.read_bytes()
            ).hexdigest()
            review.write_text(json.dumps(values))
    before = calls.read_text()
    result = call("--apply")
    assert result.returncode != 0 and "Regenerate and review" in result.stderr
    assert "Same checked package" not in result.stdout and calls.read_text() == before
    assert cron.read_text() == original_cron


def test_upgrade_reuses_compatible_adapter_or_explicit_reviewed_replacement(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    old = data / "xiaoai_bridge"
    private_adapter(data, old / "adapter")
    (old / "config.json").write_text('{"token":"synthetic-stable","enable_listener_adapter":false}')
    assert call("--dry-run").returncode == 0
    original = (old / "adapter/listener.sh").read_bytes()
    assert call("--apply").returncode == 0
    assert (old / "adapter/listener.sh").read_bytes() == original
    # An explicit new reviewed package adapter wins over an incompatible retained adapter.
    (old / "adapter/review.json").write_text('{"format":1}')
    private_adapter(data, data.parent / "package/adapter")
    # Even an identical public package must apply a newly reviewed private replacement.
    assert call("--apply").returncode == 0
    assert json.loads((old / "adapter/review.json").read_text())["format"] == 3
    assert json.loads((old / "config.json").read_text())["token"] == "synthetic-stable"


def test_enabled_missing_adapter_is_not_silently_installed(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    config = data.parent / "package/config.json"
    config.write_text('{"enable_listener_adapter":true}')
    for mode in ("--dry-run", "--apply"):
        result = call(mode)
        assert result.returncode != 0 and "Enabled Listener adapter is missing" in result.stderr
    assert not calls.exists() and not (data / "xiaoai_bridge.backup").exists()


def test_upgrade_requires_removing_an_incompatible_active_airplay_led_guard(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    private_adapter(data, data.parent / "package/adapter")
    (data / "airplay_volume_led.bak.xiaoai_bridge").write_text("# saved original LED\n")
    (data / "airplay_volume_led").write_text("# older active LED guard\n")
    for mode in ("--dry-run", "--apply"):
        result = call(mode)
        assert result.returncode != 0 and "--remove before upgrading" in result.stderr
    assert not calls.exists() and not (data / "xiaoai_bridge.backup").exists()


def test_task_led_requires_reviewed_active_airplay_guard_during_upgrade(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    config = data.parent / "package/config.json"
    config.write_text('{"verified":{"task_led":true}}')
    result = call("--apply")
    assert result.returncode != 0 and "verified.task_led=false" in result.stderr
    assert not calls.exists() and not (data / "xiaoai_bridge.backup").exists()


@pytest.mark.parametrize("damage", [None, "hash", "lock", "review"])
def test_airplay_led_review_and_active_guard_are_verified_before_upgrade(installation, damage):
    call, data, cron, original_cron, original_boot, calls = installation
    adapter = data.parent / "package/adapter"
    private_adapter(data, adapter, led=True)
    candidate = adapter / "airplay-led.sh"
    review = adapter / "review.json"
    values = json.loads(review.read_text())
    if damage == "hash":
        candidate.write_text(candidate.read_text() + "# changed\n")
    elif damage == "lock":
        candidate.write_text(
            candidate.read_text().replace("bridge_lock_for 0 6", "bridge_lock_for 0 7")
        )
        values["files"]["airplay-led.sh"]["output_sha256"] = hashlib.sha256(
            candidate.read_bytes()
        ).hexdigest()
    elif damage == "review":
        values["compatibility"]["airplay_led_guard"] = 1
    review.write_text(json.dumps(values))
    if damage:
        result = call("--apply")
        assert result.returncode != 0 and "Regenerate and review" in result.stderr
        assert not calls.exists() and not (data / "xiaoai_bridge.backup").exists()
    else:
        shutil.copy2(data / "airplay_volume_led", data / "airplay_volume_led.bak.xiaoai_bridge")
        shutil.copy2(candidate, data / "airplay_volume_led")
        config = data.parent / "package/config.json"
        config.write_text(
            '{"enable_listener_adapter":false,"verified":{"led":true,"task_led":true}}'
        )
        assert call("--dry-run").returncode == 0
        assert call("--apply").returncode == 0
        assert call("--apply").returncode == 0


def test_enabled_v013_snapshot_is_restored_after_new_listener_readiness_failure(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    old = data / "xiaoai_bridge"
    (old / "bin").mkdir(parents=True)
    (old / "adapter").mkdir()
    binary = b"exact-installed-v013-binary-snapshot"
    (old / "bin/xiaoai_bridge").write_bytes(binary)
    config = {
        "device_id": "synthetic-stable",
        "token": "synthetic-stable-token",
        "enable_listener_adapter": True,
        "verified": {"airplay_volume": False},
    }
    (old / "config.json").write_text(json.dumps(config))
    (old / "adapter/listener.sh").write_text("#!/bin/sh\n# private format-1 listener\n")
    (old / "adapter/review.json").write_text('{"format":1}')
    original_adapter = {p.name: p.read_bytes() for p in (old / "adapter").iterdir()}
    listener = data / "listener"
    original_listener = f'HA_TOKEN="synthetic-current"\nHA_REFRESH_TOKEN="synthetic-rotated"\n. {old}/lib/bootstrap.sh\n. /data/kws_bootstrap.sh\n'
    listener.write_text(original_listener)
    kws = data / "kws_bootstrap.sh"
    kws.write_text("#!/bin/sh\n# Existing KWS include remains verbatim\n")
    package = data.parent / "package"
    private_adapter(data, package / "adapter")
    service = data.parent / "listener-service"
    running = data.parent / "listener-running"
    running.touch()
    executable(
        service,
        f"""case "$1" in
 status) [ -f '{running}' ] ;;
 stop) rm -f '{running}'; printf 'listener-stop\\n' >> "$INSTALL_CALLS" ;;
 start) touch '{running}'; printf 'listener-old-start\\n' >> "$INSTALL_CALLS" ;;
esac""",
    )
    helper = package / "lib/listener-service.sh"
    executable(
        helper, f'touch "{running}"\nprintf \'listener-not-ready\\n\' >> "$INSTALL_CALLS"\nexit 1'
    )
    with (package / "SHA256SUMS").open("a") as stream:
        stream.write(
            hashlib.sha256(helper.read_bytes()).hexdigest() + "  lib/listener-service.sh\n"
        )
    result = call("--apply")
    assert result.returncode != 0, result.stdout + result.stderr
    assert (old / "bin/xiaoai_bridge").read_bytes() == binary
    assert json.loads((old / "config.json").read_text()) == config
    assert {p.name: p.read_bytes() for p in (old / "adapter").iterdir()} == original_adapter
    assert listener.read_text() == original_listener
    assert kws.read_text().endswith("verbatim\n")
    assert running.exists()
    assert (data / "xiaoai_boot.sh").read_text() == original_boot
    assert cron.read_text() == original_cron
    assert (data / "xiaoai_bridge.backup/status").read_text().strip() == "failed_restored"
    assert calls.read_text().count("listener-stop") == 2
    assert calls.read_text().count("listener-old-start") == 1
    assert call("--rollback").returncode == 0
    assert json.loads((old / "config.json").read_text())["enable_listener_adapter"]


def test_existing_oauth_directory_blocks_install_without_breaking_owner(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    installer = data.parent / "package/install.sh"
    installer.write_text(
        installer.read_text().replace("${XIAOAI_OAUTH_WAIT:-35}", "${XIAOAI_OAUTH_WAIT:-0}")
    )
    lock = data.parent / "runtime/ha_oauth_refresh.lock"
    lock.mkdir()
    (lock / "existing-writer").write_text("original-oauth-cron")
    result = call("--apply")
    assert result.returncode != 0 and "OAuth refresh is active" in result.stderr
    assert (lock / "existing-writer").read_text() == "original-oauth-cron"
    assert not (data / "xiaoai_bridge.backup").exists() and not calls.exists()
    assert cron.read_text() == original_cron


def test_storage_preflight_does_not_retain_or_replace_existing_backup(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    backup = data / "xiaoai_bridge.backup"
    backup.mkdir()
    (backup / "status").write_text("installed\n")
    executable(
        data.parent / "tools/df",
        "printf 'Filesystem blocks used available mount\\nsynthetic 30000 29999 1 local\\n'",
    )
    result = call("--apply")
    assert result.returncode != 0 and "Insufficient /data space" in result.stderr
    assert (backup / "status").read_text() == "installed\n"
    assert not (data / "xiaoai_bridge.backup.retained").exists()
    assert not calls.exists() and cron.read_text() == original_cron


def test_snapshot_copy_failure_resumes_old_bridge_and_listener(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    old = data / "xiaoai_bridge"
    old.mkdir()
    (old / "config.json").write_text('{"enable_listener_adapter":false,"token":"synthetic"}')
    (old / "original-binary").write_text("v013")
    running = data.parent / "listener-running"
    running.touch()
    executable(
        data.parent / "listener-service",
        f"""case "$1" in
 status) [ -f '{running}' ] ;;
 stop) rm -f '{running}' ;;
 start) touch '{running}' ;;
esac""",
    )
    real_cp = shutil.which("cp")
    executable(
        data.parent / "tools/cp",
        f'''case "$*" in
 *xiaoai_bridge.backup/bridge*) exit 1 ;;
esac
exec '{real_cp}' "$@"''',
    )
    result = call("--apply")
    assert result.returncode != 0
    assert (old / "original-binary").read_text() == "v013" and running.exists()
    assert calls.read_text().splitlines() == ["stop", "start"]
    assert (data / "xiaoai_bridge.backup/status").read_text().strip() == "failed_restored"
    assert not (data.parent / "runtime/ha_oauth_refresh.lock").exists()


def test_active_bridge_task_blocks_upgrade_before_services_stop(installation):
    call, data, cron, original_cron, original_boot, calls = installation
    old = data / "xiaoai_bridge"
    old.mkdir()
    (old / "config.json").write_text('{"enable_listener_adapter":false,"token":"synthetic"}')
    (data.parent / "runtime/xiaoai_bridge.pid").write_text(str(os.getpid()))
    executable(
        data.parent / "tools/curl",
        'printf \'%s\' \'{"audio_active":true,"voice":{"active":false}}\'',
    )
    result = call("--apply")
    assert result.returncode != 0 and "Bridge tasks are active" in result.stderr
    assert not calls.exists() and not (data / "xiaoai_bridge.backup").exists()
