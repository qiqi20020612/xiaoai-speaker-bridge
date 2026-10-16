"""Public synthetic Shairport 4.3.5 replies; no device media or private data."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_lx06_v016 import json_binary as json_binary
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import script
from tests.test_text_pipeline import text_device as text_device
from tests.test_volume_probe import volume_probe as volume_probe

ROOT = Path(__file__).resolve().parents[1]
ART = "cover-" + "a" * 32 + ".jpg"
ART_DIRECTORY = "/tmp/shairport-sync/.cache/coverart"


def dbus_value(kind, value, indent=0):
    pad = " " * indent
    if kind == "dict":
        return (
            "array [\n"
            + "".join(
                f'{pad} dict entry(\n{pad}  string "{key}"\n{pad}  variant '
                + dbus_value(typ, item, indent + 3)
                + f"\n{pad} )\n"
                for key, typ, item in value
            )
            + pad
            + "]"
        )
    if kind == "array":
        return "array [\n" + "".join(pad + ' string "' + item + '"\n' for item in value) + pad + "]"
    if kind in ("string", "object path"):
        return f'{kind} "{value}"'
    return f"{kind} {value}"


def remote_reply(*, state="Playing", progress="0/441000/7938000", metadata=None):
    if metadata is None:
        metadata = [
            ("xesam:title", "string", 'Synthetic "歌曲" \\ path'),
            ("xesam:artist", "array", ["Synthetic Artist", "Another Artist"]),
            ("xesam:album", "string", "Synthetic Album"),
            ("mpris:trackid", "object path", "/org/gnome/ShairportSync/123"),
            ("mpris:length", "int64", 180000000),
            ("mpris:artUrl", "string", f"file://{ART_DIRECTORY}/{ART}"),
        ]
    return (
        "method return time=1 sender=:1.1 serial=1 reply_serial=2\n"
        + dbus_value(
            "dict",
            [
                ("PlayerState", "string", state),
                ("ProgressString", "string", progress),
                ("Available", "boolean", "true"),
                ("Metadata", "dict", metadata),
            ],
        )
        + "\n"
    )


def parse(binary, raw):
    result = subprocess.run(
        [str(binary), "--airplay-metadata"],
        input=raw,
        text=isinstance(raw, str),
        capture_output=True,
        timeout=5,
    )
    assert not result.stderr, result.stderr
    return result


def test_metadata_types_units_unicode_and_literal_quotes(json_binary):
    result = parse(json_binary, remote_reply())
    assert result.returncode == 0
    value = json.loads(result.stdout)
    assert value["song"] == {
        "title": 'Synthetic "歌曲" \\ path',
        "artist": ["Synthetic Artist", "Another Artist"],
        "album": "Synthetic Album",
        "file": "/org/gnome/ShairportSync/123",
    }
    assert value["status"] == {"duration": 180, "elapsed": 10}
    assert value["progress"] == "0/441000/7938000"
    assert value["artwork_id"] == ART
    assert "file://" not in result.stdout and ART_DIRECTORY not in result.stdout


@pytest.mark.parametrize("character", ['"', "\\"])
@pytest.mark.parametrize(
    "key,kind,output,limit",
    [
        ("xesam:artist", "array", "artist", 256),
        ("xesam:title", "string", "title", 512),
        ("xesam:album", "string", "album", 512),
        ("mpris:trackid", "string", "file", 256),
    ],
)
def test_maximum_escaped_metadata_strings_remain_complete_json(
    json_binary, character, key, kind, output, limit
):
    text = character * limit
    expected = [text] if kind == "array" else text
    result = parse(json_binary, remote_reply(metadata=[(key, kind, expected)]))
    assert result.returncode == 0
    value = json.loads(result.stdout)
    assert value["song"][output] == expected
    assert value["status"] == {"duration": 180, "elapsed": 10}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a reply",
        "array [",
        remote_reply()[:-6],
        remote_reply().replace("int64 180000000", "int64 NaN"),
        remote_reply().replace('string "Synthetic Album"', 'string "first\nsecond"'),
        remote_reply().replace('("unused")', "") + "trailing",
        b"array [\0]",
        b"\xff",
        "x" * 65536,
        "array [" + "array [" * 18 + "]" * 19,
    ],
)
def test_malformed_or_excessive_reply_fails_without_partial_json(json_binary, raw):
    result = parse(json_binary, raw)
    assert result.returncode != 0 and not result.stdout


@pytest.mark.parametrize(
    "progress",
    [
        "",
        "-1/0/10",
        "0/1/2/3",
        "0/4294967296/7938000",
        "0/7938001/7938000",
        "0/441000/100",
        "0/0/0",
    ],
)
def test_invalid_or_different_track_progress_is_unknown(json_binary, progress):
    result = parse(json_binary, remote_reply(progress=progress))
    value = json.loads(result.stdout)
    assert result.returncode == 0 and value["status"] == {"duration": 180}
    assert "progress" not in value


def test_rtp_wraparound_keeps_unsigned_frame_arithmetic(json_binary):
    start = 2**32 - 10000
    raw = f"{start}/{(start + 441000) % 2**32}/{(start + 7938000) % 2**32}"
    value = json.loads(parse(json_binary, remote_reply(progress=raw)).stdout)
    assert value["status"] == {"duration": 180, "elapsed": 10}


def test_untimed_stream_does_not_reuse_previous_track_progress(json_binary):
    value = json.loads(
        parse(
            json_binary,
            remote_reply(
                metadata=[
                    ("xesam:title", "string", "Synthetic Radio"),
                    ("sps:songdatakind", "uint32", 1),
                    ("mpris:length", "int64", 180000000),
                ]
            ),
        ).stdout
    )
    assert value["song"]["title"] == "Synthetic Radio" and value["status"] == {}
    assert "progress" not in value


def test_oversized_optional_fields_are_omitted_without_partial_values(json_binary):
    value = json.loads(
        parse(
            json_binary,
            remote_reply(
                metadata=[
                    ("xesam:title", "string", "t" * 513),
                    ("xesam:album", "string", "a" * 513),
                    ("xesam:artist", "array", ["artist"] * 9),
                    ("mpris:trackid", "object path", "/" + "i" * 256),
                ]
            ),
        ).stdout
    )
    assert value["song"] == {"file": None}
    assert value["status"] == {"duration": 180, "elapsed": 10}


@pytest.mark.parametrize(
    "uri",
    [
        f"file://{ART_DIRECTORY}/{ART}",
        f"{ART_DIRECTORY}/{ART}",
    ],
)
def test_local_artwork_uri_is_reduced_to_bounded_id(json_binary, uri):
    value = json.loads(
        parse(
            json_binary,
            remote_reply(
                metadata=[
                    ("mpris:artUrl", "string", uri),
                ]
            ),
        ).stdout
    )
    assert value["artwork_id"] == ART


@pytest.mark.parametrize(
    "uri",
    [
        f"http://example.invalid/{ART}",
        f"file://localhost{ART_DIRECTORY}/{ART}",
        f"{ART_DIRECTORY}/../{ART}",
        f"{ART_DIRECTORY}-other/{ART}",
        f"{ART_DIRECTORY}/{ART}?token=hidden",
        f"{ART_DIRECTORY}/{ART}#fragment",
        f"{ART_DIRECTORY}/%2e%2e/{ART}",
        f"{ART_DIRECTORY}/cover-other.jpg",
        "/etc/passwd",
        f"/tmp/{ART}",
    ],
)
def test_artwork_path_policy_rejects_remote_and_traversal(json_binary, uri):
    value = json.loads(
        parse(
            json_binary,
            remote_reply(
                metadata=[
                    ("mpris:artUrl", "string", uri),
                ]
            ),
        ).stdout
    )
    assert "artwork_id" not in value


@pytest.fixture(scope="module")
def artwork_cli(tmp_path_factory):
    configured = os.environ.get("XIAOAI_TEST_JSON_SANITIZER")
    if configured:
        return Path(configured).resolve()
    binary = tmp_path_factory.mktemp("airplay-cli") / "helper"
    subprocess.run(
        [
            "cc",
            "-D_DEFAULT_SOURCE",
            "-D_DARWIN_C_SOURCE",
            "-DXIAOAI_TEST_AIRPLAY",
            "-std=c11",
            "-I",
            str(ROOT / "speaker/src"),
            str(ROOT / "speaker/src/json.c"),
            str(ROOT / "speaker/src/json_ops.c"),
            str(ROOT / "speaker/src/airplay.c"),
            str(ROOT / "tests/json_ops_cli.c"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
    )
    return binary


@pytest.mark.parametrize(
    "kind",
    [
        "valid-jpeg",
        "valid-png",
        "file-symlink",
        "directory-symlink",
        "parent-symlink",
        "hardlink",
        "fifo",
        "directory",
        "empty",
        "oversized",
        "wrong-magic",
        "wrong-extension",
    ],
)
def test_artwork_open_restricts_paths_types_links_size_and_magic(artwork_cli, tmp_path, kind):
    directory = tmp_path / "parent/covers"
    directory.mkdir(parents=True)
    image = directory / ART
    image.write_bytes(b"\xff\xd8\xff" + b"synthetic" * 4)
    if kind == "valid-png":
        image = directory / ART.replace(".jpg", ".png")
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic" * 4)
    elif kind in ("file-symlink", "hardlink"):
        outside = tmp_path / "outside"
        image.rename(outside)
        image.symlink_to(outside) if kind == "file-symlink" else os.link(outside, image)
    elif kind in ("directory-symlink", "parent-symlink"):
        target = directory if kind == "directory-symlink" else directory.parent
        original = target.with_name("original")
        target.rename(original)
        target.symlink_to(original, target_is_directory=True)
    elif kind in ("fifo", "directory"):
        image.unlink()
        os.mkfifo(image) if kind == "fifo" else image.mkdir()
    elif kind == "empty":
        image.write_bytes(b"")
    elif kind == "oversized":
        with image.open("wb") as stream:
            stream.truncate(2 * 1024 * 1024 + 1)
    elif kind in ("wrong-magic", "wrong-extension"):
        image.write_bytes(b"not an image" if kind == "wrong-magic" else b"\x89PNG\r\n\x1a\n")
    result = subprocess.run(
        [str(artwork_cli), "--test-artwork", str(directory), image.name],
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert not result.stderr
    assert (result.returncode == 0) == kind.startswith("valid-")
    if result.returncode == 0:
        assert int(result.stdout.split()[0]) == image.stat().st_size


@pytest.mark.parametrize(
    "state,active,failed,parse_failed,escaped_artist",
    [
        ("Playing", True, False, False, False),
        ("Paused", True, False, False, False),
        ("Stopped", False, False, False, False),
        ("Stopped", True, False, False, False),
        ("Playing", True, True, False, False),
        ("Playing", True, False, True, False),
        ("Playing", True, False, False, True),
    ],
)
def test_probe_reuses_remote_reply_and_clears_stale_or_failed_media(
    volume_probe, native_json_binary, state, active, failed, parse_failed, escaped_artist
):
    run, path, env = volume_probe
    raw = remote_reply(state=state)
    if escaped_artist:
        raw = raw.replace("Synthetic Artist", '"' * 256)
    (path / "synthetic-remote").write_text(raw)
    script(
        path / "tools/dbus-send",
        f"""#!/bin/sh
printf '%s\\n' "$*" >> "$STATE_DIR/dbus-calls"
case "$*" in
 *org.bluez*) printf 'string "State"\\nvariant string "idle"\\n';;
 *org.gnome.ShairportSync.RemoteControl*) cat "$STATE_DIR/synthetic-remote";exit {int(failed)};;
 *) printf 'string "Active"\\nvariant boolean {str(active).lower()}\\nstring "Volume"\\nvariant double -18\\n';;
esac
""",
    )
    wrapper = path / "tools/metadata-helper"
    parse_error = "printf '{\"song\":';exit 1" if parse_failed else ":"
    script(
        wrapper,
        f"""#!/bin/sh
if [ "$1" = --airplay-metadata ]; then
 printf 'parse\\n' >> "$STATE_DIR/metadata-calls"
 {parse_error}
fi
exec "{native_json_binary}" "$@"
""",
    )
    env["XIAOAI_BRIDGE_JSON_BIN"] = str(wrapper)
    result = run()
    airplay = result["state"]["airplay"]
    attempted = active and state in ("Playing", "Paused") and not failed
    expected = attempted and not parse_failed
    assert ("song" in airplay) == expected
    if expected:
        assert airplay["song"]["title"] == 'Synthetic "歌曲" \\ path'
        assert airplay["song"]["artist"] == [
            '"' * 256 if escaped_artist else "Synthetic Artist",
            "Another Artist",
        ]
    if attempted:
        assert (path / "metadata-calls").read_text().splitlines() == ["parse"]
    else:
        assert not (path / "metadata-calls").exists()
    calls = (path / "dbus-calls").read_text().splitlines()
    assert len(calls) == 3 and sum("RemoteControl" in c for c in calls) == 1
    assert not (path / "mixer-writes").exists()
