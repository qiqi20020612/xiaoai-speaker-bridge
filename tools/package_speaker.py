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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = args.binary.read_bytes()
    if (
        data[:4] != b"\x7fELF"
        or data[4] != 1
        or data[5] != 1
        or struct.unpack("<H", data[18:20])[0] != 40
    ):
        raise SystemExit(
            "Release binary must be little-endian 32-bit ARM ELF, not a workstation executable"
        )
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
                shutil.copytree(
                    source, package / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                )
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
