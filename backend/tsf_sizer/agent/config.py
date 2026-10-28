"""LLM access configuration, read from environment variables only.

All model calls go through one OpenAI-style endpoint: the Prisma AIRS AI Gateway
(TSF_SIZER_LLM_MODE=gateway). A plain OpenAI-compatible server such as Ollama's /v1 endpoint
can be used for development and tests (TSF_SIZER_LLM_MODE=direct). Nothing here is written to
the database or shown in the UI except whether a variable is set. A variable NAME_FILE names a
file whose content is the value (for mounted secrets).

Gateway variables (TSF_SIZER_GATEWAY_*):
  URL          base URL including the version path, e.g. https://gateway.example/v1
  API_KEY      key issued for this application (secret)
  AUTH_HEADER  header that carries the key            (default Authorization)
  AUTH_SCHEME  prefix for the key, empty for none     (default Bearer)
  MODEL        model name exactly as the gateway exposes it
  HEADERS      extra headers as JSON, e.g. to select a routing configuration (secret)
  CHAT_PATH    path appended to URL for chat requests (default /chat/completions)
  CA_BUNDLE    CA bundle file for a gateway with a private certificate
  VERIFY_TLS   true | false                           (default true)
  TIMEOUT_S    per request timeout                    (default 120)
  MAX_RETRIES  retries on timeouts, 429 and 5xx       (default 2)
  TOOL_MODE    native | json | auto                   (default auto)
Direct mode: TSF_SIZER_DIRECT_URL, TSF_SIZER_DIRECT_MODEL, TSF_SIZER_DIRECT_API_KEY.
Agent: TSF_SIZER_AGENT_MAX_STEPS (default 40); TSF_SIZER_AGENT_ALLOW_DOCUMENTS (default true:
the agent may read the text extracted from customer documents, identifiers masked).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

MODES = ("off", "gateway", "direct")
TOOL_MODES = ("native", "json", "auto")
DEFAULT_MAX_STEPS = 40


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    mode: str = "off"
    url: str = ""
    model: str = ""
    api_key: str | None = field(default=None, repr=False)
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    headers: dict = field(default_factory=dict, repr=False)
    chat_path: str = "/chat/completions"
    ca_bundle: str | None = None
    verify_tls: bool = True
    timeout_s: float = 120.0
    max_retries: int = 2
    tool_mode: str = "auto"
    max_steps: int = DEFAULT_MAX_STEPS
    allow_documents: bool = True
    problems: tuple = ()  # configuration mistakes found while reading the environment

    @property
    def enabled(self) -> bool:
        return self.mode in ("gateway", "direct") and bool(self.url) and bool(self.model)

    @property
    def label(self) -> str:
        if not self.enabled:
            return "Off"
        via = "AI gateway" if self.mode == "gateway" else "direct connection"
        return f"{self.model} via {via}"

    @property
    def endpoint(self) -> str:
        return self.url.rstrip("/") + "/" + self.chat_path.strip("/")


def _read(env: Mapping[str, str], name: str) -> str | None:
    """The value of NAME, or the content of the file named by NAME_FILE."""
    path = env.get(name + "_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as e:
            raise ConfigError(f"{name}_FILE cannot be read: {e.strerror or e}") from None
    value = env.get(name)
    return value.strip() if value is not None else None


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _num(value: str | None, default: float, name: str, problems: list[str]) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        problems.append(f"{name} must be a number (got '{value}')")
        return default


def from_env(env: Mapping[str, str] | None = None) -> LLMConfig:
    env = os.environ if env is None else env
    problems: list[str] = []
    mode = (env.get("TSF_SIZER_LLM_MODE") or "off").strip().lower()
    if mode not in MODES:
        problems.append(f"TSF_SIZER_LLM_MODE must be one of {', '.join(MODES)} (got '{mode}')")
        mode = "off"

    def get(name: str) -> str | None:
        try:
            return _read(env, name)
        except ConfigError as e:
            problems.append(str(e))
            return None

    max_steps = int(
        _num(get("TSF_SIZER_AGENT_MAX_STEPS"), DEFAULT_MAX_STEPS, "AGENT_MAX_STEPS", problems)
    )
    max_steps = max(1, min(max_steps, 200))
    allow_documents = _bool(get("TSF_SIZER_AGENT_ALLOW_DOCUMENTS"), True)
    if mode == "direct":
        return LLMConfig(
            mode=mode,
            url=get("TSF_SIZER_DIRECT_URL") or "",
            model=get("TSF_SIZER_DIRECT_MODEL") or "",
            api_key=get("TSF_SIZER_DIRECT_API_KEY") or None,
            timeout_s=_num(get("TSF_SIZER_DIRECT_TIMEOUT_S"), 300.0, "DIRECT_TIMEOUT_S", problems),
            tool_mode=_tool_mode(get("TSF_SIZER_DIRECT_TOOL_MODE"), problems),
            max_steps=max_steps,
            allow_documents=allow_documents,
            problems=tuple(problems),
        )
    if mode == "off":
        return LLMConfig(
            mode="off",
            max_steps=max_steps,
            allow_documents=allow_documents,
            problems=tuple(problems),
        )

    headers: dict = {}
    raw_headers = get("TSF_SIZER_GATEWAY_HEADERS")
    if raw_headers:
        try:
            headers = json.loads(raw_headers)
            if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
            ):
                raise ValueError
        except ValueError:
            headers = {}
            problems.append("TSF_SIZER_GATEWAY_HEADERS must be a JSON object of text values")
    url = get("TSF_SIZER_GATEWAY_URL") or ""
    if url and not url.startswith(("http://", "https://")):
        problems.append("TSF_SIZER_GATEWAY_URL must start with https:// (or http://)")
        url = ""
    return LLMConfig(
        mode=mode,
        url=url,
        model=get("TSF_SIZER_GATEWAY_MODEL") or "",
        api_key=get("TSF_SIZER_GATEWAY_API_KEY") or None,
        auth_header=get("TSF_SIZER_GATEWAY_AUTH_HEADER") or "Authorization",
        auth_scheme=(
            "Bearer"
            if get("TSF_SIZER_GATEWAY_AUTH_SCHEME") is None
            else get("TSF_SIZER_GATEWAY_AUTH_SCHEME") or ""
        ),
        headers=headers,
        chat_path=get("TSF_SIZER_GATEWAY_CHAT_PATH") or "/chat/completions",
        ca_bundle=get("TSF_SIZER_GATEWAY_CA_BUNDLE") or None,
        verify_tls=_bool(get("TSF_SIZER_GATEWAY_VERIFY_TLS"), True),
        timeout_s=_num(get("TSF_SIZER_GATEWAY_TIMEOUT_S"), 120.0, "GATEWAY_TIMEOUT_S", problems),
        max_retries=max(
            0, int(_num(get("TSF_SIZER_GATEWAY_MAX_RETRIES"), 2, "GATEWAY_MAX_RETRIES", problems))
        ),
        tool_mode=_tool_mode(get("TSF_SIZER_GATEWAY_TOOL_MODE"), problems),
        max_steps=max_steps,
        allow_documents=allow_documents,
        problems=tuple(problems),
    )


def _tool_mode(value: str | None, problems: list[str]) -> str:
    value = (value or "auto").lower()
    if value not in TOOL_MODES:
        problems.append(f"TOOL_MODE must be one of {', '.join(TOOL_MODES)} (got '{value}')")
        return "auto"
    return value


def status(cfg: LLMConfig, env: Mapping[str, str] | None = None) -> list[dict]:
    """What the settings page shows: each variable and whether it is set. Never the value of
    a secret."""
    env = os.environ if env is None else env

    def is_set(name: str) -> bool:
        return bool((env.get(name) or "").strip() or (env.get(name + "_FILE") or "").strip())

    secret = {"TSF_SIZER_GATEWAY_API_KEY", "TSF_SIZER_GATEWAY_HEADERS", "TSF_SIZER_DIRECT_API_KEY"}
    names = [
        ("TSF_SIZER_LLM_MODE", cfg.mode),
        *(
            [
                ("TSF_SIZER_GATEWAY_URL", cfg.url),
                ("TSF_SIZER_GATEWAY_API_KEY", None),
                ("TSF_SIZER_GATEWAY_MODEL", cfg.model),
                ("TSF_SIZER_GATEWAY_AUTH_HEADER", cfg.auth_header),
                ("TSF_SIZER_GATEWAY_AUTH_SCHEME", cfg.auth_scheme),
                ("TSF_SIZER_GATEWAY_HEADERS", None),
                ("TSF_SIZER_GATEWAY_CHAT_PATH", cfg.chat_path),
                ("TSF_SIZER_GATEWAY_CA_BUNDLE", cfg.ca_bundle or ""),
                ("TSF_SIZER_GATEWAY_VERIFY_TLS", "true" if cfg.verify_tls else "false"),
                ("TSF_SIZER_GATEWAY_TIMEOUT_S", f"{cfg.timeout_s:g}"),
                ("TSF_SIZER_GATEWAY_MAX_RETRIES", str(cfg.max_retries)),
                ("TSF_SIZER_GATEWAY_TOOL_MODE", cfg.tool_mode),
            ]
            if cfg.mode == "gateway"
            else []
        ),
        *(
            [
                ("TSF_SIZER_DIRECT_URL", cfg.url),
                ("TSF_SIZER_DIRECT_MODEL", cfg.model),
                ("TSF_SIZER_DIRECT_API_KEY", None),
            ]
            if cfg.mode == "direct"
            else []
        ),
        ("TSF_SIZER_AGENT_MAX_STEPS", str(cfg.max_steps)),
        ("TSF_SIZER_AGENT_ALLOW_DOCUMENTS", "true" if cfg.allow_documents else "false"),
    ]
    return [
        {
            "name": n,
            "set": is_set(n),
            "value": None if n in secret else value,
            "secret": n in secret,
        }
        for n, value in names
    ]
