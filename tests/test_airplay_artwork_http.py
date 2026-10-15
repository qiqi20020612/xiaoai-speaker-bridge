"""Authenticated streaming through the real C HTTP server, with synthetic art."""

import http.client
import json
import os
import socket
import subprocess
import time

import pytest

from tests.test_airplay_metadata import ART
from tests.test_bridge import native_bridge as native_bridge

ADAPTER = f"""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
json.load(sys.stdin)
state=Path(os.environ["XIAOAI_BRIDGE_RUNTIME"])/"artwork-state.json"
print(json.dumps({{
 "capabilities":{{}},
 "state":json.loads(state.read_text()) if state.exists() else {{
  "airplay":{{"active":True,"state":"Playing","artwork_id":"{ART}"}}}}
}}))
"""
pytestmark = pytest.mark.parametrize("native_bridge", [{"adapter": ADAPTER}], indirect=True)


def image_get(port, token, image_id=ART):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "GET", "/api/airplay/artwork/" + image_id, headers={"Authorization": "Bearer " + token}
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("size", [32, 40000, 2 * 1024 * 1024])
def test_current_artwork_streams_with_fixed_buffer_and_authentication(native_bridge, size):
    call, _, path, port, _, token = native_bridge
    data = b"\xff\xd8\xff" + b"x" * (size - 3)
    (path / "coverart" / ART).write_bytes(data)
    assert image_get(port, "wrong")[0] == 401
    status, headers, body = image_get(port, token)
    assert status == 200 and body == data
    assert headers["Content-Type"] == "image/jpeg"
    assert int(headers["Content-Length"]) == size
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert call("/api/health")[0] == 200


@pytest.mark.parametrize(
    "name",
    [
        "../config.json",
        "%2e%2e/config.json",
        ART + "?path=/etc/passwd",
        ART.replace("a", "b"),
        "cover-invalid.jpg",
    ],
)
def test_arbitrary_old_or_encoded_paths_cannot_be_downloaded(native_bridge, name):
    _, _, path, port, _, token = native_bridge
    (path / "coverart" / ART).write_bytes(b"\xff\xd8\xff" + b"synthetic")
    assert image_get(port, token, name)[0] == 404


def test_track_change_revokes_previous_image_and_serves_new_current_image(native_bridge):
    call, _, path, port, config, token = native_bridge
    second = "cover-" + "b" * 32 + ".png"
    (path / "coverart" / ART).write_bytes(b"\xff\xd8\xff" + b"synthetic")
    data = b"\x89PNG\r\n\x1a\n" + b"synthetic"
    (path / "coverart" / second).write_bytes(data)
    assert image_get(port, token)[0] == 200
    (path / "artwork-state.json").write_text(
        json.dumps({"airplay": {"active": True, "state": "Playing", "artwork_id": second}})
    )
    subprocess.run(
        [
            str(path / "bin/xiaoai_bridge"),
            "--config",
            str(config),
            "--emit",
            '{"type":"voice_end"}',
        ],
        check=True,
        capture_output=True,
    )
    deadline = time.monotonic() + 5
    while call("/api/state")[1]["airplay"]["artwork_id"] != second:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert image_get(port, token)[0] == 404
    status, headers, actual = image_get(port, token, second)
    assert status == 200 and headers["Content-Type"] == "image/png" and actual == data


def test_slow_image_client_does_not_block_voice_events_and_disconnect_releases_fd(native_bridge):
    call, proc, path, port, config, token = native_bridge
    (path / "coverart" / ART).write_bytes(b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024 - 3))
    descriptors = f"/proc/{proc.pid}/fd"
    before = len(os.listdir(descriptors)) if os.path.isdir(descriptors) else None
    with socket.socket() as client:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        client.connect(("127.0.0.1", port))
        client.sendall(
            f"GET /api/airplay/artwork/{ART} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n\r\n".encode()
        )
        assert client.recv(1024).startswith(b"HTTP/1.1 200")
        subprocess.run(
            [
                str(path / "bin/xiaoai_bridge"),
                "--config",
                str(config),
                "--emit",
                '{"type":"voice_begin","pid":0}',
            ],
            check=True,
            capture_output=True,
        )
        deadline = time.monotonic() + 3
        while not call("/api/state")[1]["voice"]["active"]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
    if before is not None:
        deadline = time.monotonic() + 3
        while len(os.listdir(descriptors)) > before:
            assert time.monotonic() < deadline
            time.sleep(0.02)
