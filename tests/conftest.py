import os
import subprocess
import sys
from pathlib import Path

import pytest


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
