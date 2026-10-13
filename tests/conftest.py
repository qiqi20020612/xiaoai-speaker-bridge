import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def native_json_binary():
    subprocess.run(["make", "-s", "-C", str(ROOT / "speaker")], check=True)
    return ROOT / "build/native/xiaoai_bridge"


@pytest.fixture(scope="session")
def jq16_binary():
    configured = os.environ.get("XIAOAI_TEST_JQ16")
    if not configured:
        pytest.skip("Real jq 1.6 is required by Linux CI; configure XIAOAI_TEST_JQ16 locally")
    path = Path(configured).resolve()
    assert subprocess.check_output([str(path), "--version"], text=True).strip() == "jq-1.6"
    return path


@pytest.fixture(autouse=True)
def enable_custom_integrations_fixture(enable_custom_integrations):
    yield


@pytest.fixture
def busybox_binary():
    configured = os.environ.get("XIAOAI_TEST_BUSYBOX")
    if not configured:
        return None
    path = Path(configured).resolve()
    result = subprocess.run([str(path)], capture_output=True, text=True)
    assert "BusyBox v1.36.1" in result.stdout + result.stderr
    applets = subprocess.run(
        [str(path), "--list"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    assert {"ash", "flock", "awk", "sed"} <= set(applets), (
        "BusyBox must include ash, flock, awk and sed"
    )
    return str(path)


@pytest.fixture
def speaker_shell(busybox_binary):
    return [busybox_binary, "ash"] if busybox_binary else ["sh"]


@pytest.fixture
def install_flock(busybox_binary):
    """CI uses real BusyBox; the macOS fallback rejects every GNU-only option."""

    def install(directory):
        path = directory / "flock"
        if busybox_binary:
            path.write_text(f'#!/bin/sh\nexec "{busybox_binary}" flock "$@"\n')
        else:
            path.write_text(f"""#!{sys.executable}
import fcntl,sys
args=sys.argv[1:]
flags=[]
while args and args[0].startswith('-'):
    option=args.pop(0)
    if option not in ('-s','-x','-u','-n'):
        print('flock: invalid option',file=sys.stderr);sys.exit(2)
    flags.append(option)
if len(args)!=1 or not args[0].isdigit():sys.exit(2)
mode=fcntl.LOCK_UN if '-u' in flags else fcntl.LOCK_SH if '-s' in flags else fcntl.LOCK_EX
if '-n' in flags:mode|=fcntl.LOCK_NB
try:fcntl.flock(int(args[0]),mode)
except (OSError,ValueError):sys.exit(1)
""")
        path.chmod(0o755)
        return path

    return install
