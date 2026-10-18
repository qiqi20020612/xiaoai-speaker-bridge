#!/usr/bin/env python3
"""Exercise the release ARM endpoint under Linux QEMU, with no hardware APIs."""

import argparse
import http.client
import json
import os
import socket
import struct
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
        helper = [args.qemu, str(args.binary.resolve()), "--json-op"]
        # Exact production parser on 32-bit ARM; values are wholly synthetic.
        metadata = subprocess.check_output(
            [args.qemu, str(args.binary.resolve()), "--airplay-metadata"],
            input=b"""array [
dict entry(
 string "ProgressString"
 variant string "4294957296/431000/7928000"
)
dict entry(
 string "Metadata"
 variant array [
  dict entry(
   string "xesam:title"
   variant string "Synthetic ARM Song"
  )
  dict entry(
   string "mpris:length"
   variant int64 180000000
  )
 ]
)
]
""",
        )
        metadata = json.loads(metadata)
        assert metadata["song"]["title"] == "Synthetic ARM Song"
        assert metadata["status"] == {"duration": 180, "elapsed": 10}
        quoted = subprocess.check_output(
            [*helper, "reply", 'synthetic "\\\n中文', '{"source":"mpd","uncertain":false}'],
            text=True,
        )
        assert json.loads(quoted)["reply_text"] == 'synthetic "\\\n中文'
        invalid = subprocess.run(
            [*helper, "validate"], input=b'{"text":"a\\u0000b"}', capture_output=True
        )
        assert invalid.returncode == 2 and invalid.stderr == b"invalid_json_operation\n"
        journal = runtime / "journal.json"
        data = subprocess.check_output(
            [
                *helper,
                "journal",
                str(runtime / "missing.json"),
                "",
                '{"master":60,"bluetooth":50}',
                "77",
                "88",
                "-18",
                "true",
                "null",
            ],
            text=True,
        )
        journal.write_text(data)
        assert json.loads(data)["volumes"] == {"master": 60, "bluetooth": 50}
        assert subprocess.check_output(
            [
                *helper,
                "recovery-plan",
                str(journal),
                str(runtime / "missing.json"),
                "-18",
                "true",
                "10",
            ],
            text=True,
        ).splitlines() == ["master\t60", "bluetooth\t50"]
        # Same shared-mixer gain model as native/sanitized tests, with no ALSA
        # or D-Bus access. The real firmware's audio writes remain untested.
        sender_config = runtime / "shairport.conf"
        sender_config.write_text(
            "general={volume_range_db=30;volume_max_db=-4.0;};"
            'alsa={mixer_control_name="mysoftvol";};'
        )
        os.utime(sender_config, (1, 1))
        sender_target = subprocess.check_output(
            [
                args.qemu,
                str(args.binary.resolve()),
                "--airplay-volume-target",
                str(sender_config),
                "-6",
                "standard",
                "60",
                "1",
            ],
            input="numid=1,iface=MIXER,name='mysoftvol'\n"
            " ; type=INTEGER,access=rw---RW-,values=2,min=0,max=255,step=0\n"
            " : values=26,26\n"
            " | dBscale-min=-51.00dB,step=0.20dB,mute=0\n",
            text=True,
        )
        assert sender_target == "220,220\n"
        for pair, expected in (("177,177", 0), ("177,176", 2), ("26,26", 2)):
            checked = subprocess.run(
                [
                    args.qemu,
                    str(args.binary.resolve()),
                    "--airplay-volume-target",
                    str(sender_config),
                    "-18",
                    "standard",
                    "60",
                    "1",
                    "--verify",
                ],
                input="numid=1,iface=MIXER,name='mysoftvol'\n"
                " ; type=INTEGER,access=rw---RW-,values=2,min=0,max=255,step=0\n"
                f" : values={pair}\n"
                " | dBscale-min=-51.00dB,step=0.20dB,mute=0\n",
                text=True,
                capture_output=True,
            )
            assert checked.returncode == expected and not checked.stderr
            assert checked.stdout == ("177,177\n" if expected == 0 else "")
        profile = runtime / "profile"
        profile.write_text("")
        pcm = bytes(range(256)) * 20
        traced = subprocess.run(
            [args.qemu, str(args.binary.resolve()), "--trace-pcm", str(profile), "77.88.1"],
            input=pcm,
            capture_output=True,
        )
        assert traced.returncode == 0 and traced.stdout == pcm
        assert "phase=recording_first_output" in profile.read_text()
        # Exercise fixed-width WAV chunk/frame accounting on 32-bit ARM too.
        fmt = struct.pack("<HHIIHH", 1, 8, 16000, 256000, 16, 16)
        chunks = (
            b"JUNK"
            + struct.pack("<I", 3)
            + b"abc\0"
            + b"fmt "
            + struct.pack("<I", len(fmt))
            + fmt
            + b"data"
            + struct.pack("<I", len(pcm))
            + pcm
        )
        wav = b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks
        for enabled in ("0", "1"):
            profile.write_text("")
            traced = subprocess.run(
                [args.qemu, str(args.binary.resolve()), "--trace-pcm", str(profile), "77.88.2"],
                input=wav,
                capture_output=True,
                env={**os.environ, "XIAOAI_PROFILE_PCM": enabled},
            )
            assert traced.returncode == 0 and traced.stdout == wav and not traced.stderr
            assert ("phase=recording_first_pcm" in profile.read_text()) == (enabled == "1")
        print(
            "QEMU ARM JSON/journal/recovery/UTF-8/NUL and byte-exact opt-in trace passed; no audio hardware."
        )
        recovery = runtime / "recovery"
        (recovery / "lib").mkdir(parents=True)
        recovery_script = recovery / "lib/volume-recovery.sh"
        recovery_marker = runtime / "recovery-started"
        recovery_script.write_text(
            "#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n"
            "assert os.getsid(0)==os.getpid()==os.getpgrp()\n"
            f"Path({str(recovery_marker)!r}).write_text('detached')\n"
        )
        recovery_script.chmod(0o700)
        subprocess.run(
            [args.qemu, str(args.binary.resolve()), "--schedule-recovery", str(recovery)],
            check=True,
            timeout=5,
        )
        deadline = time.monotonic() + 5
        while not recovery_marker.exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert recovery_marker.read_text() == "detached"
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
            assert (
                call("/api/tts/announce", {"request_id": "qemu-cancel", "text": "synthetic"})[0]
                == 202
            )
            assert call("/api/jobs/qemu-cancel/cancel", {})[1]["status"] == "cancelled"
            deadline = time.monotonic() + 5
            while call("/api/state")[1]["audio_active"]:
                assert time.monotonic() < deadline
                time.sleep(0.12)
            assert call("/api/jobs/qemu-cancel")[1]["status"] == "cancelled"
            print(
                f"QEMU ARM {info['version']}: config, HTTP, state, worker, idempotency, cancellation and detached recovery passed; hardware remains untested."
            )
        finally:
            proc.terminate()
            proc.wait(timeout=25)
            diagnostics = proc.stderr.read().decode()
            proc.stderr.close()
            assert proc.returncode == 0 and token not in diagnostics


if __name__ == "__main__":
    main()
