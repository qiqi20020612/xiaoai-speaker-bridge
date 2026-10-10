#!/usr/bin/env python3
"""Synthetic existing Agent contract. History lives in a simulated HA, not the cache."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

state_path = Path(sys.argv[1])
agent = sys.argv[2]
body = json.loads(sys.argv[3])
now = int(time.time())
state = {}
if state_path.exists():
    state = json.loads(state_path.read_text())
cid = (
    state.get("id") if state.get("agent") == agent and 0 <= now - state.get("ts", 0) < 300 else None
)
cid = cid or uuid.uuid4().hex
backend = Path(os.environ["STATE_DIR"]) / "simulated-ha-history.json"
history = json.loads(backend.read_text()) if backend.exists() else {}
conversation = history.setdefault(cid, {})
text = body["text"]
if text.startswith("my name is "):
    conversation["name"] = text[11:]
reply = conversation.get("name", "unknown") if text == "what is my name" else "reply: " + text
backend.write_text(json.dumps(history))
state_path.write_text(json.dumps({"agent": agent, "id": cid, "ts": now}))
with (Path(os.environ["STATE_DIR"]) / "legacy-calls").open("a") as f:
    f.write("conversation\n")
print(
    json.dumps(
        {
            "conversation_id": cid,
            "response": {
                "response_type": "error" if text == "FAIL" else "action_done",
                "speech": {"plain": {"speech": reply}},
            },
        }
    )
)
