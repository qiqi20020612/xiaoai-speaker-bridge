#!/usr/bin/env python3
"""Exercise the release ARM endpoint under Linux QEMU, with no hardware APIs."""

import argparse
import http.client
import json
import socket
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--qemu", default="qemu-arm")
    args = parser.parse_args()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = "synthetic-qemu-only-token-000000000000000000000000"
    with tempfile.TemporaryDirectory(prefix="xiaoai-qemu-") as directory:
        runtime = Path(directory)
        config = runtime / "config.json"
        config.write_text(
            json.dumps(
                {
                    "token": token,
                    "device_id": "synthetic-qemu",
                    "bind": "127.0.0.1",
                    "port": port,
                    "runtime_dir": str(runtime),
                    "adapter": str(ROOT / "tests/mock_adapter.py"),
                }
            )
        )
        config.chmod(0o600)
        command = [args.qemu, str(args.binary.resolve()), "--config", str(config)]
        subprocess.run([*command, "--check-config"], check=True)
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def call(path, body=None, credential=token):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request(
                "GET" if body is None else "POST",
                path,
                body=None if body is None else json.dumps(body),
                headers={
                    "Authorization": "Bearer " + credential,
                    "Content-Type": "application/json",
                },
            )
            response = conn.getresponse()
            result = response.status, json.loads(response.read())
            conn.close()
            return result

        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError("QEMU endpoint exited during startup")
                try:
                    if call("/api/health")[0] == 200:
                        break
                except OSError, http.client.HTTPException:
                    pass
                time.sleep(0.05)
            else:
                raise RuntimeError("QEMU endpoint did not become ready")
            assert call("/api/state", credential="wrong")[0] == 401
            info = call("/api/info")[1]
            expected_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
                "version"
            ]
            assert info["version"] == expected_version
            assert info["protocol"] == 1 and info["device_id"] == "synthetic-qemu"
            assert call("/api/state")[0] == 200
            assert (
                call("/api/tts/announce", {"request_id": "qemu-announce", "text": "synthetic"})[0]
                == 202
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = call("/api/jobs/qemu-announce")[1]
                if job["status"] == "completed":
                    break
                assert job["status"] in ("accepted", "running", "synthesizing", "playing")
                time.sleep(0.05)
            else:
                raise RuntimeError("QEMU task did not complete")
            assert (
                call("/api/tts/announce", {"request_id": "qemu-announce", "text": "synthetic"})[1][
                    "status"
                ]
                == "completed"
            )
            print(
                f"QEMU ARM {info['version']}: config, authenticated HTTP, state, worker and idempotency passed; hardware remains untested."
            )
        finally:
            proc.terminate()
            proc.wait(timeout=25)
            diagnostics = proc.stderr.read().decode()
            proc.stderr.close()
            assert proc.returncode == 0 and token not in diagnostics


if __name__ == "__main__":
    main()
