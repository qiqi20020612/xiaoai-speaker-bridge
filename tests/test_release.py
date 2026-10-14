"""Asset/ABI/checksum and publication guards; never create a real release in tests."""

import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tools.package_speaker import validate_arm_elf
from tools.release import ROOT, package, publish, validate_tag, verify_assets, version


def test_metadata_generator_reproduces_checked_in_catalogs(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    generator = tools / "generate_metadata.py"
    shutil.copy2(ROOT / "tools/generate_metadata.py", generator)
    component = Path("custom_components/xiaoai_speaker")
    (tmp_path / component / "translations").mkdir(parents=True)
    files = [
        component / name
        for name in (
            "services.yaml",
            "strings.json",
            "translations/en.json",
            "translations/zh-Hans.json",
        )
    ]
    # Execute the real tool only in an isolated tree, including a second run.
    for _ in range(2):
        subprocess.run(
            [sys.executable, str(generator)], check=True, capture_output=True, timeout=10
        )
        for relative in files:
            assert (tmp_path / relative).read_bytes() == (ROOT / relative).read_bytes(), relative


def synthetic_arm():
    data = bytearray(512)
    data[:7] = b"\x7fELF\x01\x01\x01"
    struct.pack_into("<HHI", data, 16, 2, 40, 1)
    struct.pack_into("<I", data, 28, 52)
    struct.pack_into("<I", data, 36, 0x05000400)
    struct.pack_into("<HHH", data, 40, 52, 32, 1)
    struct.pack_into("<I", data, 52, 1)
    return bytes(data)


@pytest.fixture
def assets(tmp_path):
    binary = tmp_path / "synthetic-arm"
    binary.write_bytes(synthetic_arm())
    output = tmp_path / "dist"
    package(binary, output)
    return output


@pytest.mark.parametrize("kind", ["native", "soft_float", "eabi4", "dynamic", "truncated"])
def test_release_rejects_unverified_abi(kind):
    data = bytearray(synthetic_arm())
    if kind == "native":
        struct.pack_into("<H", data, 18, 183)
    elif kind == "soft_float":
        struct.pack_into("<I", data, 36, 0x05000200)
    elif kind == "eabi4":
        struct.pack_into("<I", data, 36, 0x04000400)
    elif kind == "dynamic":
        struct.pack_into("<I", data, 52, 3)
    else:
        data = data[:12]
    with pytest.raises(ValueError):
        validate_arm_elf(data)


def test_tag_semantics_and_version_consistency(tmp_path):
    (tmp_path / "custom_components/xiaoai_speaker").mkdir(parents=True)
    (tmp_path / "speaker/src").mkdir(parents=True)
    for candidate in ("0.2.0", "0.2.0-rc.1"):
        (tmp_path / "pyproject.toml").write_text(f'[project]\nversion="{candidate}"\n')
        (tmp_path / "custom_components/xiaoai_speaker/manifest.json").write_text(
            json.dumps({"version": candidate})
        )
        (tmp_path / "speaker/src/bridge.c").write_text(f'#define VERSION "{candidate}"\n')
        assert validate_tag(f"v{candidate}", tmp_path) == ("-" in candidate)
        with pytest.raises(ValueError):
            validate_tag("v0.1.3", tmp_path)
    (tmp_path / "speaker/src/bridge.c").write_text('#define VERSION "0.1.0"\n')
    with pytest.raises(ValueError):
        version(tmp_path)


def test_release_assets_have_architecture_versions_and_inner_outer_checksums(assets):
    files = verify_assets(assets, version())
    assert len(files) == 3
    archive = next(assets.glob("*.tar.gz"))
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert "xiaoai_bridge/lib/ducking.sh" in names
        assert "xiaoai_bridge/lib/task-feedback.sh" in names
        assert "xiaoai_bridge/lib/voice-led.sh" in names
        assert not any("adapter/" in name or name.endswith("config.json") for name in names)
        assert not any("__pycache__" in name or ".private" in name for name in names)
        assert tar.getmember("xiaoai_bridge/bin/xiaoai_bridge").mode & 0o100
    with zipfile.ZipFile(next(assets.glob("*.zip"))) as archive:
        assert "custom_components/xiaoai_speaker/text.py" in archive.namelist()
        assert not any("__pycache__" in name for name in archive.namelist())


@pytest.mark.parametrize("bad_manifest", ["corrupt", "duplicate", "traversal", "missing"])
def test_release_integrity_failures_are_rejected_before_publication(assets, bad_manifest):
    manifest = assets / "SHA256SUMS"
    lines = manifest.read_text().splitlines()
    if bad_manifest == "corrupt":
        next(assets.glob("*.zip")).write_bytes(b"corrupt")
    elif bad_manifest == "duplicate":
        manifest.write_text("\n".join([*lines, lines[0]]) + "\n")
    elif bad_manifest == "traversal":
        manifest.write_text("0" * 64 + "  ../secret\n")
    else:
        manifest.write_text("\n".join(lines[1:]) + "\n")
    with patch("tools.release.subprocess.run") as run:
        with pytest.raises(ValueError):
            publish(f"v{version()}", assets, "example/repo")
        run.assert_not_called()


def test_publish_refuses_existing_release_and_uses_verified_draft(assets):
    tag = f"v{version()}"
    with (
        patch(
            "tools.release.subprocess.check_output", return_value=json.dumps([[{"tag_name": tag}]])
        ),
        patch("tools.release.subprocess.run") as run,
    ):
        with pytest.raises(ValueError, match="already exists"):
            publish(tag, assets, "example/repo")
        run.assert_not_called()
    with (
        patch("tools.release.subprocess.check_output", return_value="[[]]"),
        patch("tools.release.subprocess.run", side_effect=download_draft(assets)) as run,
    ):
        publish(tag, assets, "example/repo")
        create, download, finish = [call.args[0] for call in run.call_args_list]
        assert "--draft" in create and "--verify-tag" in create
        assert "--notes-file" in create
        assert download[:3] == ["gh", "release", "download"]
        assert "--draft=false" in finish and "--prerelease=false" in finish
    with pytest.raises(ValueError, match="channel"):
        publish(tag, assets, "example/repo", "prerelease")


def download_draft(assets, failure=None):
    def run(command, **kwargs):
        action = command[2]
        if failure == action:
            raise subprocess.CalledProcessError(1, command)
        if action == "download":
            output = Path(command[command.index("--dir") + 1])
            for file in assets.iterdir():
                if failure != "missing" or file.name != "SHA256SUMS":
                    shutil.copy2(file, output / file.name)
            if failure == "corrupt":
                next(output.glob("*.zip")).write_bytes(b"incomplete upload")
        return subprocess.CompletedProcess(command, 0)

    return run


@pytest.mark.parametrize("failure", ["create", "download", "missing", "corrupt"])
def test_failed_draft_upload_or_download_is_never_published(assets, failure):
    with (
        patch("tools.release.subprocess.check_output", return_value="[[]]"),
        patch("tools.release.subprocess.run", side_effect=download_draft(assets, failure)) as run,
    ):
        with pytest.raises((subprocess.CalledProcessError, ValueError)):
            publish(f"v{version()}", assets, "example/repo")
        assert not any(call.args[0][2] == "edit" for call in run.call_args_list)
        assert not any("--clobber" in call.args[0] for call in run.call_args_list)


def test_release_lookup_failure_aborts_without_writes(assets):
    with (
        patch(
            "tools.release.subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "gh api"),
        ),
        patch("tools.release.subprocess.run") as run,
    ):
        with pytest.raises(subprocess.CalledProcessError):
            publish(f"v{version()}", assets, "example/repo")
        run.assert_not_called()


def test_ci_release_depends_on_all_checks_and_limits_write_token():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["release"]
    assert set(job["needs"]) == {"tests", "armhf", "hassfest", "hacs"}
    assert job["permissions"] == {"contents": "write"}
    assert "refs/tags/v" in job["if"] and "inputs.publish" in job["if"]
    for spec in workflow["jobs"].values():
        checkout = next(
            step for step in spec["steps"] if step.get("uses", "").startswith("actions/checkout@")
        )
        assert checkout["with"]["ref"] == "${{ inputs.release_tag || github.ref }}"
    assert "SHA256SUMS" in (ROOT / "docs/release-notes.md").read_text()


def test_checksum_manifest_is_portable(assets):
    for line in (assets / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert "/" not in name
        assert digest == hashlib.sha256((assets / name).read_bytes()).hexdigest()
