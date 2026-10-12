#!/usr/bin/env python3
"""Validate versions/assets and publish only a new, fully tested existing tag."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

from tools.package_speaker import validate_arm_elf

ROOT = Path(__file__).resolve().parents[1]


def version(root=ROOT):
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    manifest = json.loads((root / "custom_components/xiaoai_speaker/manifest.json").read_text())[
        "version"
    ]
    speaker = re.search(
        r'^#define VERSION "([^"]+)"', (root / "speaker/src/bridge.c").read_text(), re.M
    )[1]
    if project != manifest or project != speaker:
        raise ValueError("HA, project and speaker versions must agree")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?", project):
        raise ValueError("Invalid release version")
    return project


def validate_tag(tag, root=ROOT):
    expected = version(root)
    if tag != f"v{expected}":
        raise ValueError("Tag does not match checked-out source versions")
    return "-" in expected


def package(binary, output, root=ROOT):
    release_version = version(root)
    output.mkdir(parents=True, exist_ok=True)
    speaker = output / f"xiaoai-speaker-lx06-arm-eabi5-hf-{release_version}.tar.gz"
    subprocess.run(
        [
            sys.executable,
            str(root / "tools/package_speaker.py"),
            "--binary",
            str(binary),
            "--output",
            str(speaker),
        ],
        check=True,
    )
    component = root / "custom_components/xiaoai_speaker"
    ha = output / f"xiaoai-speaker-ha-{release_version}.zip"
    modules = (
        "__init__ binary_sensor button client config_flow const coordinator diagnostics entity jobs light media_player mpd number quick select sensor services switch text views volume"
    ).split()
    files = [
        *(component / f"{module}.py" for module in modules),
        component / "manifest.json",
        component / "strings.json",
        component / "services.yaml",
        component / "translations/en.json",
        component / "translations/zh-Hans.json",
        component / "brand/icon.png",
        component / "brand/icon@2x.png",
    ]
    with zipfile.ZipFile(ha, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            info = zipfile.ZipInfo(str(file.relative_to(root)), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, file.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    notes = output / "RELEASE_NOTES.md"
    notes.write_text(
        f"# XiaoAI Speaker Bridge {release_version}\n\n"
        + (root / "docs/release-notes.md").read_text()
    )
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}\n"
            for file in (speaker, ha, notes)
        )
    )
    verify_assets(output, release_version)


def verify_assets(output, release_version):
    expected = {
        f"xiaoai-speaker-lx06-arm-eabi5-hf-{release_version}.tar.gz",
        f"xiaoai-speaker-ha-{release_version}.zip",
        "RELEASE_NOTES.md",
    }
    entries = {}
    for line in (output / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        if name not in expected or name in entries or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid release checksum manifest")
        entries[name] = digest
    if set(entries) != expected:
        raise ValueError("Missing release assets")
    for name, digest in entries.items():
        if hashlib.sha256((output / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Release checksum mismatch")
    with tarfile.open(
        output / f"xiaoai-speaker-lx06-arm-eabi5-hf-{release_version}.tar.gz"
    ) as archive:
        validate_arm_elf(archive.extractfile("xiaoai_bridge/bin/xiaoai_bridge").read())
        manifest = archive.extractfile("xiaoai_bridge/SHA256SUMS").read().decode()
        for line in manifest.splitlines():
            digest, name = line.split("  ", 1)
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("Invalid package path")
            if (
                hashlib.sha256(archive.extractfile(f"xiaoai_bridge/{name}").read()).hexdigest()
                != digest
            ):
                raise ValueError("Speaker package checksum mismatch")
    with zipfile.ZipFile(output / f"xiaoai-speaker-ha-{release_version}.zip") as archive:
        metadata = json.loads(archive.read("custom_components/xiaoai_speaker/manifest.json"))
        if metadata["version"] != release_version or archive.testzip():
            raise ValueError("HA package validation failed")
    if (
        not (output / "RELEASE_NOTES.md")
        .read_text()
        .startswith(f"# XiaoAI Speaker Bridge {release_version}\n")
    ):
        raise ValueError("Release notes version mismatch")
    return [output / name for name in sorted(expected)]


def publish(tag, output, repository, channel="auto"):
    prerelease = validate_tag(tag)
    if channel != "auto" and (channel == "prerelease") != prerelease:
        raise ValueError("Stable/prerelease channel disagrees with version")
    assets = verify_assets(output, tag[1:])
    # Any API/network failure aborts. Refuse drafts too; never overwrite a release.
    pages = json.loads(
        subprocess.check_output(
            ["gh", "api", "--paginate", "--slurp", f"repos/{repository}/releases"], text=True
        )
    )
    if any(release["tag_name"] == tag for page in pages for release in page):
        raise ValueError("Release already exists; no overwrite permitted")
    subprocess.run(
        [
            "gh",
            "release",
            "create",
            tag,
            *map(str, assets),
            str(output / "SHA256SUMS"),
            "--repo",
            repository,
            "--verify-tag",
            "--draft",
            "--title",
            tag,
            "--notes-file",
            str(output / "RELEASE_NOTES.md"),
            *(["--prerelease"] if prerelease else []),
        ],
        check=True,
    )
    subprocess.run(
        [
            "gh",
            "release",
            "edit",
            tag,
            "--repo",
            repository,
            "--draft=false",
            f"--prerelease={str(prerelease).lower()}",
            f"--latest={str(not prerelease).lower()}",
        ],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "package", "publish"))
    parser.add_argument("--tag")
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--repository")
    parser.add_argument("--channel", choices=("auto", "stable", "prerelease"), default="auto")
    args = parser.parse_args()
    if args.action == "validate":
        validate_tag(args.tag)
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        tagged = subprocess.check_output(
            ["git", "rev-parse", f"refs/tags/{args.tag}^{{commit}}"], text=True
        ).strip()
        if actual != tagged:
            raise ValueError("Checked-out commit is not the requested tag")
    elif args.action == "package":
        package(args.binary, args.output)
    else:
        if not args.repository or not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repository):
            raise ValueError("Invalid repository")
        publish(args.tag, args.output, args.repository, args.channel)


if __name__ == "__main__":
    main()
