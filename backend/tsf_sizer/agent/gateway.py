"""The one client through which the agent talks to an LLM.

It sends OpenAI-style chat completion requests (with tool definitions) to the configured
endpoint: the Prisma AIRS AI Gateway, or in direct mode any OpenAI-compatible server. Nothing
else in the application knows which model sits behind it.

Tool calls: `native` uses the API's tool-calling, `json` asks the model to answer with a JSON
object for every tool call (for servers or models without tool support), `auto` tries native
and falls back to json once if the server refuses tools.

Failures are typed (GatewayError.kind) so the agent can tell the engineer what happened:
  auth         401/403: the key or its permissions are wrong
  blocked      the gateway's security policy refused the prompt or the answer: final, no retry
  rate         429 after the retries
  unreachable  no connection, timeout, TLS problem
  bad_response the server answered, but not with a usable chat completion
  http         any other HTTP error
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .config import LLMConfig

RETRY_BASE_S = 1.0  # tests set this to 0
BLOCK_WORDS = (
    "blocked",
    "guardrail",
    "policy violation",
    "content policy",
    "content_filter",
    "prompt injection",
    "security profile",
    "dlp",
)
REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-correlation-id", "x-amzn-requestid")


class GatewayError(RuntimeError):
    def __init__(self, kind: str, message: str, request_id: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.request_id = request_id


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Reply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    seconds: float = 0.0
    request_id: str | None = None
    usage: dict = field(default_factory=dict)
    tool_mode: str = "native"


def _localhost_hint(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1") and os.path.exists("/.dockerenv"):
        return (
            ". 'localhost' is this container, not your computer: use the name or address the "
            "container can reach (for a service on the Docker host, host.docker.internal)"
        )
    return ""


def tool_definitions(specs: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["parameters"],
            },
        }
        for s in specs
    ]


JSON_PROTOCOL = """You have no native tool calling here. To use a tool, answer with ONE JSON
object and nothing else:
{"tool": "<tool name>", "arguments": { ... }}
You get the result in the next message. To talk to the engineer instead, answer in plain text
(no JSON). Tools:
"""


def _json_system(system: str, specs: list[dict]) -> str:
    lines = [JSON_PROTOCOL]
    for s in specs:
        props = ", ".join(
            f"{k}{'' if k in s['parameters'].get('required', []) else '?'}: "
            f"{(v.get('type') or 'any') if isinstance(v, dict) else 'any'}"
            for k, v in s["parameters"].get("properties", {}).items()
        )
        lines.append(f"- {s['name']}({props}): {s['description']}")
    return system + "\n\n" + "\n".join(lines)


def to_json_messages(messages: list[dict], specs: list[dict]) -> list[dict]:
    """Rewrite native tool-call history as plain text for json mode."""
    out: list[dict] = []
    names: dict[str, str] = {}
    for m in messages:
        role = m["role"]
        if role == "system":
            out.append({"role": "system", "content": _json_system(m["content"], specs)})
        elif role == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                names[tc["id"]] = tc["function"]["name"]
                args = tc["function"].get("arguments") or "{}"
                calls.append(
                    {
                        "tool": tc["function"]["name"],
                        "arguments": json.loads(args) if isinstance(args, str) else args,
                    }
                )
            out.append({"role": "assistant", "content": json.dumps(calls[0])})
        elif role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": f"Result of {names.get(m.get('tool_call_id'), 'the tool')} "
                    f"({m.get('tool_call_id')}):\n{m['content']}",
                }
            )
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def parse_json_call(text: str, known: set[str]) -> tuple[ToolCall | None, str]:
    """Find a {"tool": ..., "arguments": ...} object in the model's text."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start() :])
        except ValueError:
            continue
        if isinstance(obj, dict):
            name = obj.get("tool") or obj.get("name")
            args = obj.get("arguments", obj.get("args", {}))
            if isinstance(name, str) and name in known and isinstance(args, dict):
                rest = (text[: m.start()] + text[m.start() :].replace(json.dumps(obj), "")).strip()
                return ToolCall(f"call_{int(time.time() * 1000) % 10**9}", name, args), rest
    return None, text


class GatewayClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.tool_mode = "native" if cfg.tool_mode in ("native", "auto") else "json"
        self.fell_back = False  # auto mode switched to json: said once in the run log

    # -- HTTP

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.cfg.endpoint.startswith("https"):
            return None
        if not self.cfg.verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if self.cfg.ca_bundle:
            return ssl.create_default_context(cafile=self.cfg.ca_bundle)
        return None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.cfg.api_key:
            scheme = f"{self.cfg.auth_scheme} " if self.cfg.auth_scheme else ""
            h[self.cfg.auth_header] = f"{scheme}{self.cfg.api_key}"
        h.update(self.cfg.headers)
        return h

    def _post(self, body: dict) -> tuple[dict, str | None]:
        data = json.dumps(body).encode()
        attempt = 0
        while True:
            req = urllib.request.Request(
                self.cfg.endpoint, data=data, method="POST", headers=self._headers()
            )
            try:
                with urllib.request.urlopen(  # noqa: S310 - admin-configured URL
                    req, timeout=self.cfg.timeout_s, context=self._ssl_context()
                ) as resp:
                    rid = next(
                        (resp.headers[h] for h in REQUEST_ID_HEADERS if resp.headers[h]), None
                    )
                    try:
                        return json.loads(resp.read().decode() or "{}"), rid
                    except json.JSONDecodeError:
                        raise GatewayError(
                            "bad_response", f"{self.cfg.endpoint} did not return JSON", rid
                        ) from None
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:600]
                rid = next((e.headers[h] for h in REQUEST_ID_HEADERS if e.headers[h]), None)
                low = text.lower()
                if e.code in (400, 403, 406, 422, 451) and any(w in low for w in BLOCK_WORDS):
                    raise GatewayError(
                        "blocked", f"Blocked by the AI security policy: {text[:300]}", rid
                    ) from None
                if e.code in (401, 403):
                    raise GatewayError(
                        "auth",
                        f"The gateway refused the credentials (HTTP {e.code}). Check "
                        "TSF_SIZER_GATEWAY_API_KEY, the auth header and the key's permissions.",
                        rid,
                    ) from None
                if (e.code == 429 or e.code >= 500) and attempt < self.cfg.max_retries:
                    attempt += 1
                    time.sleep(RETRY_BASE_S * 2 ** (attempt - 1))
                    continue
                kind = "rate" if e.code == 429 else "http"
                raise GatewayError(
                    kind, f"HTTP {e.code} from the model endpoint: {text}", rid
                ) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as e:
                if attempt < self.cfg.max_retries:
                    attempt += 1
                    time.sleep(RETRY_BASE_S * 2 ** (attempt - 1))
                    continue
                reason = getattr(e, "reason", e)
                raise GatewayError(
                    "unreachable",
                    f"Cannot reach {self.cfg.endpoint}: {reason}{_localhost_hint(self.cfg.url)}",
                ) from None

    # -- chat

    def chat(self, messages: list[dict], specs: list[dict] | None = None) -> Reply:
        """One model call. `messages` use the OpenAI shape (assistant tool_calls, tool
        results). `specs` are the tool specs (name, description, parameters)."""
        specs = specs or []
        start = time.monotonic()
        if specs and self.tool_mode == "native":
            try:
                return self._native(messages, specs, start)
            except GatewayError as e:
                tools_refused = (
                    e.kind == "http"
                    and self.cfg.tool_mode == "auto"
                    and ("HTTP 400" in str(e) or "HTTP 404" in str(e) or "HTTP 422" in str(e))
                    and any(w in str(e).lower() for w in ("tool", "function"))
                )
                if not tools_refused:
                    raise
                self.tool_mode, self.fell_back = "json", True
        if specs:
            return self._json(messages, specs, start)
        return self._plain(messages, start)

    def _body(self, messages: list[dict]) -> dict:
        return {"model": self.cfg.model, "messages": messages, "temperature": 0.2}

    def _parse(self, out: dict, rid: str | None, start: float) -> tuple[dict, str, str | None]:
        if "choices" not in out or not out["choices"]:
            err = out.get("error")
            detail = (err.get("message") if isinstance(err, dict) else err) or "no choices"
            if any(w in str(detail).lower() for w in BLOCK_WORDS):
                raise GatewayError("blocked", f"Blocked by the AI security policy: {detail}", rid)
            raise GatewayError("bad_response", f"The model endpoint answered: {detail}", rid)
        choice = out["choices"][0]
        if choice.get("finish_reason") == "content_filter":
            raise GatewayError("blocked", "Blocked by the AI security policy (content filter)", rid)
        return choice.get("message") or {}, choice.get("finish_reason") or "", rid

    def _plain(self, messages: list[dict], start: float) -> Reply:
        out, rid = self._post(self._body(messages))
        msg, _, rid = self._parse(out, rid, start)
        return Reply(
            msg.get("content") or "",
            model=out.get("model", ""),
            seconds=round(time.monotonic() - start, 1),
            request_id=rid or out.get("id"),
            usage=out.get("usage") or {},
        )

    def _native(self, messages: list[dict], specs: list[dict], start: float) -> Reply:
        body = self._body(messages) | {"tools": tool_definitions(specs), "tool_choice": "auto"}
        out, rid = self._post(body)
        msg, _, rid = self._parse(out, rid, start)
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (ValueError, TypeError):
                args = {"_unparsed": str(raw)[:200]}
            calls.append(ToolCall(tc.get("id") or f"call_{len(calls)}", fn.get("name", ""), args))
        return Reply(
            msg.get("content") or "",
            calls,
            model=out.get("model", ""),
            seconds=round(time.monotonic() - start, 1),
            request_id=rid or out.get("id"),
            usage=out.get("usage") or {},
            tool_mode="native",
        )

    def _json(self, messages: list[dict], specs: list[dict], start: float) -> Reply:
        out, rid = self._post(self._body(to_json_messages(messages, specs)))
        msg, _, rid = self._parse(out, rid, start)
        text = msg.get("content") or ""
        call, rest = parse_json_call(text, {s["name"] for s in specs})
        return Reply(
            rest if call else text,
            [call] if call else [],
            model=out.get("model", ""),
            seconds=round(time.monotonic() - start, 1),
            request_id=rid or out.get("id"),
            usage=out.get("usage") or {},
            tool_mode="json",
        )

    def test(self) -> dict:
        """Connection test for the settings page: a tiny request, with and without tools."""
        started = time.monotonic()
        reply = self.chat(
            [
                {"role": "system", "content": "You are a connectivity test."},
                {"role": "user", "content": "Reply with the single word OK."},
            ]
        )
        probe_spec = {
            "name": "ping",
            "description": "Call this tool with message 'pong'.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        }
        tools_ok = False
        try:
            tool_reply = self.chat(
                [
                    {"role": "system", "content": "Use the ping tool, nothing else."},
                    {"role": "user", "content": "Call ping with message pong."},
                ],
                [probe_spec],
            )
            tools_ok = bool(tool_reply.tool_calls)
        except GatewayError:
            tools_ok = False
        return {
            "answer": reply.content[:200],
            "seconds": reply.seconds,
            "total_seconds": round(time.monotonic() - started, 1),
            "model": reply.model or self.cfg.model,
            "request_id": reply.request_id,
            "tool_calls_work": tools_ok,
            "tool_mode": self.tool_mode,
        }
