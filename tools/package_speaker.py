#!/usr/bin/env python3
"""Build a public generic speaker archive using an explicit allowlist."""

import argparse
import hashlib
import shutil
import struct
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate_arm_elf(data: bytes) -> None:
    """Release ABI is the tested static ARM EABI5 hard-float executable only."""
    if (
        len(data) < 52
        or data[:6] != b"\x7fELF\x01\x01"
        or struct.unpack_from("<HH", data, 16) != (2, 40)
    ):
        raise ValueError("Release binary must be a little-endian ARM32 executable")
    flags = struct.unpack_from("<I", data, 36)[0]
    if flags & 0xFF000000 != 0x05000000 or not flags & 0x400 or flags & 0x200:
        raise ValueError("Release binary must use ARM EABI5 hard-float")
    offset = struct.unpack_from("<I", data, 28)[0]
    size, count = struct.unpack_from("<HH", data, 42)
    if not count or size != 32 or offset + size * count > len(data):
        raise ValueError("Invalid ELF program headers")
    if any(struct.unpack_from("<I", data, offset + size * i)[0] == 3 for i in range(count)):
        raise ValueError("Release binary must be static (no ELF interpreter)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = args.binary.read_bytes()
    try:
        validate_arm_elf(data)
    except ValueError as err:
        raise SystemExit(str(err)) from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        package = Path(tmp) / "xiaoai_bridge"
        package.mkdir()
        for name in [
            "start.sh",
            "install.sh",
            "xiaoai_boot.sh",
            "config.example.json",
            "lib",
            "tools",
        ]:
            source = ROOT / "speaker" / name
            if source.is_dir():
                (package / name).mkdir()
                files = (
                    (
                        "bootstrap.sh common.sh volume-recovery.sh voice-led.sh locks.sh adapter-check.sh device.sh conversation.sh conversation-curl.sh listener-hooks.sh listener-service.sh source-snapshot.sh text.sh airplay-volume.sh ducking.sh task-feedback.sh"
                    ).split()
                    if name == "lib"
                    else (
                        "activate.sh activate_airplay_led.sh prepare_adapters.py resource_probe.sh"
                    ).split()
                )
                for file in files:
                    shutil.copy2(source / file, package / name / file)
            else:
                shutil.copy2(source, package / name)
        (package / "bin").mkdir()
        shutil.copy2(args.binary, package / "bin/xiaoai_bridge")
        (package / "bin/xiaoai_bridge").chmod(0o700)
        hashes = []
        for file in sorted(package.rglob("*")):
            if file.is_file():
                hashes.append(
                    hashlib.sha256(file.read_bytes()).hexdigest()
                    + "  "
                    + str(file.relative_to(package))
                )
        (package / "SHA256SUMS").write_text("\n".join(hashes) + "\n")

        def public_metadata(member):
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mtime = 0
            member.pax_headers = {}
            return member

        with tarfile.open(args.output, "w:gz") as tar:
            tar.add(package, arcname="xiaoai_bridge", filter=public_metadata)
    print(
        f"Generic speaker package created: {args.output}; private configs and adapters are excluded."
    )


if __name__ == "__main__":
    main()
