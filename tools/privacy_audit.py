#!/usr/bin/env python3
"""Reject private device material from the public tree; never print matched secrets."""

from __future__ import annotations

import argparse
import re
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private IPv4 address": re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    ),
    "JWT credential": re.compile(
        r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"
    ),
    "GitHub credential": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"
    ),
    "device hostname or serial": re.compile(r"\bLX06-[0-9A-Fa-f]{4,}\b"),
    "real MAC address": re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"),
    "personal local path": re.compile(r"/Users/[A-Za-z0-9_.-]+/|/home/[A-Za-z0-9_.-]+/"),
    "embedded SSH password": re.compile(r"sshpass\s+-p\s+\S+"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE[ ]KEY-----"),
}


def audit(paths: list[Path]) -> list[tuple[str, str]]:
    findings = []
    for path in paths:
        relative = str(path.relative_to(ROOT))
        if relative in {
            "custom_components/xiaoai_speaker/brand/icon.png",
            "custom_components/xiaoai_speaker/brand/icon@2x.png",
        }:
            data = path.read_bytes()
            pos = 8
            safe = data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) < 65536
            while safe and pos < len(data):
                if pos + 12 > len(data):
                    safe = False
                    break
                size = struct.unpack(">I", data[pos : pos + 4])[0]
                if data[pos + 4 : pos + 8] not in {
                    b"IHDR",
                    b"IDAT",
                    b"IEND",
                } or pos + size + 12 > len(data):
                    safe = False
                    break
                pos += size + 12
            if not safe:
                findings.append((relative, "unexpected image metadata or invalid brand asset"))
            continue
        if (
            any(
                part in {".private", ".env", "known_hosts", "adapter"}
                for part in path.relative_to(ROOT).parts
            )
            or relative == "speaker/config.json"
        ):
            findings.append((relative, "private artifact path"))
            continue
        if path.suffix.lower() in {".bin", ".elf", ".tar", ".gz", ".xz", ".zip", ".pem", ".key"}:
            findings.append((relative, "unexpected binary or credential artifact"))
            continue
        try:
            text = path.read_text()
        except UnicodeError, OSError:
            findings.append((relative, "non-text public file"))
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append((relative, label))
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    command = ["git", "ls-files", "-z"]
    if not args.staged:
        command += ["--cached", "--others", "--exclude-standard"]
    names = subprocess.check_output(command, cwd=ROOT).decode().split("\0")
    paths = [ROOT / name for name in names if name and (ROOT / name).is_file()]
    findings = audit(paths)
    if findings:
        for name, label in findings:
            print(f"{name}: {label}")
        raise SystemExit(1)
    print(
        f"Privacy audit passed for {len(paths)} public files; no device data or credentials found."
    )


if __name__ == "__main__":
    main()
