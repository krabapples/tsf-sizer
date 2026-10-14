"""A stand-in for the AI gateway: an OpenAI-style chat completions endpoint that plays scripted
answers, and records every request for the tests to inspect."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def tool_reply(*calls, content=None, request_id="req-1"):
    """An assistant message that calls tools: calls are (name, arguments) pairs."""
    return {
        "id": request_id,
        "model": "fake-model",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {"name": n, "arguments": json.dumps(a)},
                        }
                        for i, (n, a) in enumerate(calls)
                    ],
                },
            }
        ],
    }


def text_reply(text, request_id="req-1"):
    return {
        "id": request_id,
        "model": "fake-model",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
    }


def last_tool_result(body):
    """The JSON payload of the newest tool message in a request (the agent's last result)."""
    for m in reversed(body["messages"]):
        if m["role"] == "tool":
            return json.loads(m["content"])
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeGateway/1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        gw = self.server.gateway
        gw.requests.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        gw.calls += 1
        script = gw.script
        status, payload, headers = 200, None, {}
        if callable(script):
            out = script(body, gw)
        elif script:
            out = script.pop(0) if len(script) > 1 or not gw.repeat_last else script[0]
            if callable(out):
                out = out(body, gw)
        else:
            out = text_reply("OK")
        if isinstance(out, tuple):
            status, payload, *rest = out
            headers = rest[0] if rest else {}
        else:
            payload = out
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


class FakeGateway:
    def __init__(self):
        self.requests = []
        self.calls = 0
        self.script = None
        self.repeat_last = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.gateway = self
        threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        ).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
