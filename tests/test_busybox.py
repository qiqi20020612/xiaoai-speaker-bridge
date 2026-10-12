"""Real BusyBox in CI, strict option contract locally, and kernel FD-lock semantics."""

import fcntl
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_real_busybox_rejects_gnu_timeout_option(busybox_binary):
    if not busybox_binary:
        pytest.skip("Linux CI requires BusyBox 1.36.1; macOS has no Linux applets")
    result = subprocess.run(
        [busybox_binary, "flock", "-w", "1", "9"], capture_output=True, text=True
    )
    assert result.returncode != 0 and "invalid option" in result.stderr


def test_real_busybox_supports_readonly_audited_launcher_syntax(busybox_binary):
    if not busybox_binary:
        pytest.skip("Linux CI reproduces LX06 ash features verified in the read-only audit")
    result = subprocess.run(
        [
            busybox_binary,
            "ash",
            "-c",
            'language=zh-CN; [ "${language:0:2}" = zh ] || exit 1; '
            'let count=1+1; [ "$count" = 2 ] || exit 1; '
            "(printf hidden; printf hidden >&2) &>/dev/null",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and not result.stdout and not result.stderr


def test_every_speaker_flock_command_uses_busybox_options():
    import re

    found = 0
    for script in (ROOT / "speaker").rglob("*.sh"):
        for line in script.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in re.finditer(r"\bflock\s+((?:-[a-z]+\s+)+)", line):
                found += 1
                assert set(match.group(1).split()) <= {"-s", "-x", "-u", "-n"}, script
    assert found > 10


@pytest.fixture
def lock_runner(tmp_path, install_flock, speaker_shell):
    tools = tmp_path / "tools"
    tools.mkdir()
    install_flock(tools)
    lock = tmp_path / "mutex"
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]}

    def launch(body):
        source = f". '{ROOT / 'speaker/lib/locks.sh'}'\nexec 9> '{lock}'\n" + body
        return subprocess.Popen(
            [*speaker_shell, "-c", source],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    return launch, lock, tmp_path


def test_lock_stays_with_caller_after_helper_exits_and_releases_on_exit(lock_runner):
    launch, lock, _ = lock_runner
    proc = launch("bridge_lock_for 1 9 || exit 1\nprintf 'ready\\n'\nread release\n")
    try:
        assert proc.stdout.readline() == "ready\n"
        with lock.open("w") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _, stderr = proc.communicate("release\n", timeout=3)
            assert proc.returncode == 0 and not stderr
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_contended_lock_times_out_without_removing_the_lock(lock_runner):
    launch, lock, _ = lock_runner
    with lock.open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        before = time.monotonic()
        proc = launch("bridge_lock_for 1 9")
        _, stderr = proc.communicate(timeout=4)
        elapsed = time.monotonic() - before
        assert proc.returncode == 1 and not stderr
        assert 0.9 <= elapsed < 4 and lock.exists()
        fcntl.flock(owner, fcntl.LOCK_UN)
    retry = launch("bridge_lock_for 1 9")
    _, stderr = retry.communicate(timeout=3)
    assert retry.returncode == 0 and not stderr


def test_waiting_lock_acquires_after_release(lock_runner):
    launch, lock, _ = lock_runner
    with lock.open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        proc = launch("bridge_lock_for 3 9 || exit 1\nprintf 'acquired\\n'")
        time.sleep(0.15)
        assert proc.poll() is None
        fcntl.flock(owner, fcntl.LOCK_UN)
        stdout, stderr = proc.communicate(timeout=4)
        assert proc.returncode == 0 and stdout == "acquired\n" and not stderr


def test_parallel_operations_are_serialized(lock_runner):
    launch, _, root = lock_runner
    log = root / "critical-sections"
    body = f"""bridge_lock_for 5 9 || exit 1
printf 'enter %s\\n' "$$" >> '{log}'
sleep 0.1
printf 'exit %s\\n' "$$" >> '{log}'
"""
    processes = [launch(body) for _ in range(3)]
    for proc in processes:
        _, stderr = proc.communicate(timeout=7)
        assert proc.returncode == 0 and not stderr
    lines = log.read_text().splitlines()
    assert len(lines) == 6
    for index in range(0, len(lines), 2):
        assert lines[index].startswith("enter ")
        assert lines[index + 1] == lines[index].replace("enter ", "exit ", 1)
