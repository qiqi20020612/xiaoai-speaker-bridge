#!/usr/bin/env python3
"""Synthetic probe faults for the real C worker, without speaker commands or secrets."""

import json
import os
import signal
import sys
import time
from pathlib import Path

json.load(sys.stdin)
assert sys.argv[1] == "probe"
runtime = Path(os.environ["XIAOAI_BRIDGE_RUNTIME"])
steps = json.loads((runtime / "probe-fixture.json").read_text())
attempts = runtime / "probe-attempts.jsonl"
number = len(attempts.read_text().splitlines()) if attempts.exists() else 0
with attempts.open("a") as stream:
    stream.write(json.dumps({"time": time.monotonic(), "pid": os.getpid()}) + "\n")
step = steps[min(number, len(steps) - 1)]
if step.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(step.get("delay", 0))
kind = step.get("kind", "success")
marker = "synthetic-sensitive-fixture-marker"
if kind == "exit":
    print(marker, file=sys.stderr)
    print(json.dumps({"error": marker}))
    sys.exit(7)
if kind == "invalid_json":
    print(marker)
elif kind == "invalid_response":
    print(json.dumps({"state": {}, "private": marker}))
elif kind == "overflow":
    print(marker * 2000)
else:
    print(json.dumps({"capabilities": {"listener": True}, "state": {"system": {}}}))
