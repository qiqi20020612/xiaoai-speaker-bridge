import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def stub(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def test_boot_waits_for_asynchronous_exit_and_does_not_stop_retained_services(
    tmp_path, install_flock, speaker_shell
):
    tools = tmp_path / "tools"
    tools.mkdir()
    checks = tmp_path / "checks"
    stops = tmp_path / "stops"
    starts = tmp_path / "starts"
    install_flock(tools)
    stub(
        tools / "pidof",
        f"""[ "$1" = squeezelite ] || exit 99
n=0;[ -f '{checks}' ] && read n < '{checks}'
n=$((n+1));printf '%s' "$n" > '{checks}'
[ "$n" -lt 4 ]""",
    )
    stub(tools / "sleep", ":")
    stub(tmp_path / "squeezelite", f"printf '%s' \"$1\" >> '{stops}'")
    stub(
        tmp_path / "bridge-start",
        f"[ \"${{1:-}}\" != status ] || exit 0\nprintf '%s' started >> '{starts}'",
    )
    (tmp_path / "uptime").write_text("200.01 100.0\n")
    source = (ROOT / "speaker/xiaoai_boot.sh").read_text()
    source = source.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={tools}:/usr/bin:/bin")
    for old, new in [
        ("/tmp/xiaoai_boot", str(tmp_path / "xiaoai_boot")),
        ("/proc/uptime", str(tmp_path / "uptime")),
        ("/etc/init.d/squeezelite", str(tmp_path / "squeezelite")),
        ("/data/xiaoai_bridge/start.sh", str(tmp_path / "bridge-start")),
    ]:
        source = source.replace(old, new)
    script = tmp_path / "boot.sh"
    script.write_text(source)
    subprocess.run([*speaker_shell, str(script)], check=True)
    assert checks.read_text() == "4" and stops.read_text() == "stop"
    assert starts.read_text() == "started"
    subprocess.run([*speaker_shell, str(script)], check=True)
    assert checks.read_text() == "4" and starts.read_text() == "started"
    assert "snapclient" not in source and "upmpdcli" not in source


def test_boot_failure_leaves_flag_unset_for_cron_retry(tmp_path, install_flock, speaker_shell):
    # Derive only a fixture copy; production paths remain fixed.
    source = (ROOT / "speaker/xiaoai_boot.sh").read_text()
    (tmp_path / "uptime").write_text("200.1 100\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    install_flock(tools)
    stub(tools / "pidof", "exit 1")
    stub(tmp_path / "bridge-start", "exit 1")
    source = (
        source.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={tools}:/usr/bin:/bin")
        .replace("/tmp/xiaoai_boot", str(tmp_path / "xiaoai_boot"))
        .replace("/proc/uptime", str(tmp_path / "uptime"))
        .replace("/data/xiaoai_bridge/start.sh", str(tmp_path / "bridge-start"))
    )
    script = tmp_path / "boot.sh"
    script.write_text(source)
    result = subprocess.run([*speaker_shell, str(script)])
    assert result.returncode != 0 and not (tmp_path / "xiaoai_boot.done").exists()


def test_completed_boot_recovers_only_bridge_daemon_without_restarting_microphone(
    tmp_path, install_flock, speaker_shell
):
    tools = tmp_path / "tools"
    tools.mkdir()
    install_flock(tools)
    calls = tmp_path / "calls"
    stub(
        tmp_path / "bridge-start",
        f"""printf '%s %s\\n' "$1" "${{XIAOAI_SKIP_LISTENER_START:-0}}" >> '{calls}'
[ "$1" != status ]""",
    )
    stub(tools / "pidof", "exit 99")
    (tmp_path / "xiaoai_boot.done").touch()
    (tmp_path / "uptime").write_text("200.1 100\n")
    source = (
        (ROOT / "speaker/xiaoai_boot.sh")
        .read_text()
        .replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={tools}:/usr/bin:/bin")
        .replace("/tmp/xiaoai_boot", str(tmp_path / "xiaoai_boot"))
        .replace("/proc/uptime", str(tmp_path / "uptime"))
        .replace("/data/xiaoai_bridge/start.sh", str(tmp_path / "bridge-start"))
    )
    script_file = tmp_path / "boot.sh"
    script_file.write_text(source)
    subprocess.run([*speaker_shell, str(script_file)], check=True)
    assert calls.read_text().splitlines() == ["status 0", "start 1"]
    assert (tmp_path / "xiaoai_boot.done").exists()


@pytest.mark.parametrize("healthy", [False, True])
def test_existing_cron_recovers_orphaned_feedback_without_audio_boot_tasks(
    tmp_path, install_flock, speaker_shell, healthy
):
    tools = tmp_path / "tools"
    tools.mkdir()
    install_flock(tools)
    calls = tmp_path / "calls"
    marker = tmp_path / "xiaoai_bridge.feedback"
    marker.write_text("999999 0\n")
    recovery = tmp_path / "task-feedback.sh"
    stub(recovery, f"""printf 'feedback %s\\n' "$1" >> '{calls}'\nrm -f '{marker}' """)
    stub(
        tmp_path / "bridge-start",
        f"""printf 'bridge %s %s\\n' "$1" "${{XIAOAI_SKIP_LISTENER_START:-0}}" >> '{calls}'
[ "$1" != status ] || {"exit 0" if healthy else "exit 1"}""",
    )
    stub(tools / "pidof", f"printf 'unexpected-audio-boot\\n' >> '{calls}'; exit 99")
    (tmp_path / "xiaoai_boot.done").touch()
    (tmp_path / "uptime").write_text("200.1 100\n")
    source = (
        (ROOT / "speaker/xiaoai_boot.sh")
        .read_text()
        .replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={tools}:/usr/bin:/bin")
        .replace("/tmp/xiaoai_boot", str(tmp_path / "xiaoai_boot"))
        .replace("/proc/uptime", str(tmp_path / "uptime"))
        .replace("/data/xiaoai_bridge/start.sh", str(tmp_path / "bridge-start"))
        .replace("/tmp/xiaoai_bridge.feedback", str(marker))
        .replace("/data/xiaoai_bridge/lib/task-feedback.sh", str(recovery))
    )
    script = tmp_path / "boot.sh"
    script.write_text(source)
    subprocess.run([*speaker_shell, str(script)], check=True)
    expected = ["feedback recover", "bridge status 0"]
    if not healthy:
        expected.append("bridge start 1")
    assert calls.read_text().splitlines() == expected
    assert not marker.exists()
    subprocess.run([*speaker_shell, str(script)], check=True)
    assert calls.read_text().count("feedback recover") == 1
