"""Synthetic HA endpoints; requests arrive via real curl, never a curl() mock."""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


class ConversationHTTP:
    def __init__(self):
        self.requests = []
        self.history = {}
        self.valid_token = "synthetic-token"
        self.refresh_error = False
        self.reject_conversation = False
        self.refresh_body = {"access_token": "synthetic-refreshed"}
        self.intent = ""
        self.state_dir = None
        self.hold_guard_end = False
        self.guard_end_entered = threading.Event()
        self.guard_end_release = threading.Event()
        self.conversation_entered = threading.Event()
        self.hold_conversation = False
        self.conversation_release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
                body = (
                    json.loads(raw)
                    if "application/json" in self.headers.get("Content-Type", "")
                    else raw
                )
                owner.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": body,
                    }
                )
                if self.path == "/auth/token":
                    if owner.refresh_error:
                        self.reply(400, {"error": "invalid_grant"})
                    else:
                        if "access_token" in owner.refresh_body:
                            owner.valid_token = owner.refresh_body["access_token"]
                        self.reply(200, owner.refresh_body)
                    return
                if self.path.endswith("/volume_plan"):
                    self.reply(
                        200,
                        {"error": "unsupported_volume_target"}
                        if body.get("local_intent")
                        else {"handled": False},
                    )
                    return
                if self.path.endswith("/voice_guard"):
                    if "text" not in body and owner.hold_guard_end:
                        owner.guard_end_entered.set()
                        owner.guard_end_release.wait(timeout=2)
                    self.reply(200, {"accepted": True})
                    return
                if self.path == "/intent":
                    self.reply(200 if owner.intent else 404, owner.intent)
                    return
                if self.path != "/api/conversation/process":
                    self.reply(404, {})
                    return
                owner.conversation_entered.set()
                if owner.hold_conversation:
                    owner.conversation_release.wait(timeout=10)
                if owner.state_dir:
                    with (owner.state_dir / "legacy-calls").open("a") as calls:
                        calls.write("conversation\n")
                if (
                    owner.reject_conversation
                    or self.headers.get("Authorization") != "Bearer " + owner.valid_token
                ):
                    self.reply(401, {"message": "unauthorized"})
                    return
                if not isinstance(body.get("agent_id"), str):
                    self.reply(400, {"error": "missing_agent_id"})
                    return
                if body["text"] == "HTTP_FAIL":
                    self.reply(500, {})
                    return
                if body["text"] == "DROP":
                    self.close_connection = True
                    return
                if body["text"] == "MALFORMED":
                    self.reply(200, "not JSON")
                    return
                agent = body["agent_id"]
                cid = body.get("conversation_id") or uuid.uuid4().hex
                conversation = owner.history.setdefault((agent, cid), {})
                text = body["text"]
                if text.startswith("my name is "):
                    conversation["name"] = text[11:]
                reply = (
                    conversation.get("name", "unknown")
                    if text == "what is my name"
                    else "reply: " + text
                )
                if text == "EMPTY_REPLY":
                    reply = ""
                response = {
                    "conversation_id": cid,
                    "response": {
                        "response_type": "error" if text == "FAIL" else "action_done",
                        "speech": {"plain": {"speech": reply}},
                    },
                }
                if text == "NO_ID":
                    del response["conversation_id"]
                self.reply(200, response)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    @property
    def conversations(self):
        return [
            request for request in self.requests if request["path"] == "/api/conversation/process"
        ]

    @property
    def refreshes(self):
        return [
            parse_qs(request["body"])
            for request in self.requests
            if request["path"] == "/auth/token"
        ]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
