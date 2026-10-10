#!/usr/bin/env python3
"""Synthetic device, with no network or real credentials."""

import json
import os
import sys
import time
from pathlib import Path

request = json.load(sys.stdin)
operation = sys.argv[1]
if operation == "probe":
    print(
        json.dumps(
            {
                "capabilities": {
                    "announce": True,
                    "conversation": True,
                    "voice_hooks": True,
                    "listener": True,
                    "volume": {"master": True, "tts": True, "bluetooth": True, "airplay": False},
                    "playback": {"airplay": {"actions": []}, "bluetooth": {"actions": []}},
                },
                "state": {
                    "volumes": {"master": 60, "tts": 55, "bluetooth": 50},
                    "mpd": {"state": "stop"},
                    "airplay": {
                        "active": False,
                        "state": "stopped",
                        "raw_volume": -18,
                        "volume": 40,
                    },
                    "system": {"uptime": 100, "memory_available": 50000},
                },
            }
        )
    )
elif operation in ("announce", "send_text"):
    root = Path(os.environ["XIAOAI_BRIDGE_RUNTIME"])
    with (root / "executions.txt").open("a") as f:
        f.write(request["request_id"] + "\n")
    print('{"stage":"synthesizing"}', flush=True)
    time.sleep(0.15)
    print('{"stage":"playing"}', flush=True)
    time.sleep(0.3)
    if request["text"] == "FAIL":
        print('{"error":"tts_failed"}')
        sys.exit(1)
    print(json.dumps({"reply_text": request["text"] if operation == "send_text" else ""}))
else:
    if request.get("action") == "INVALID":
        print('{"error":"unsupported_action"}')
        sys.exit(1)
    print('{"completed":true}')
