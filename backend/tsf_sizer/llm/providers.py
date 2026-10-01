"""Minimal clients for the LLM back-ends the app supports (standard library only).

* ollama     - local Ollama server, native /api/chat
* openai     - any OpenAI-compatible server: LM Studio, vLLM, LocalAI, llama.cpp
               server, Azure/OpenAI (base URL including /v1)
* anthropic  - Claude via the Anthropic Messages API

All calls are plain HTTPS/HTTP POSTs; nothing is sent anywhere else.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

PROVIDERS = {
    "none": "Off",
    "ollama": "Ollama (local)",
    "openai": "OpenAI-compatible server",
    "anthropic": "Anthropic Claude",
}
DEFAULT_BASE_URL = {
    "ollama": "http://host.docker.internal:11434",
    "openai": "http://host.docker.internal:1234/v1",
    "anthropic": "https://api.anthropic.com",
}
LOCAL_PROVIDERS = {"ollama"}


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    provider: str = "none"
    base_url: str = ""
    model: str = ""
    api_key: str | None = None
    temperature: float = 0.2
    max_tokens: int = 900
    timeout: float = 180.0

    @property
    def enabled(self) -> bool:
        return self.provider in PROVIDERS and self.provider != "none" and bool(self.model)

    @property
    def url(self) -> str:
        return (self.base_url or DEFAULT_BASE_URL.get(self.provider, "")).rstrip("/")

    @property
    def label(self) -> str:
        return f"{self.model} via {PROVIDERS.get(self.provider, self.provider)}"


@dataclass
class LLMReply:
    text: str
    model: str
    provider: str
    seconds: float


def _request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - admin-configured URL
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise LLMError(f"{url} answered HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"Cannot reach {url}: {e.reason}") from None
    except TimeoutError:
        raise LLMError(f"{url} did not answer within {timeout:.0f} s") from None
    except json.JSONDecodeError:
        raise LLMError(f"{url} did not return JSON") from None


def _auth_headers(cfg: LLMConfig) -> dict:
    if cfg.provider == "anthropic":
        return {"x-api-key": cfg.api_key or "", "anthropic-version": "2023-06-01"}
    if cfg.provider == "openai" and cfg.api_key:
        return {"Authorization": f"Bearer {cfg.api_key}"}
    return {}


def chat(cfg: LLMConfig, system: str, user: str) -> LLMReply:
    """One system + user turn; returns the assistant text."""
    if not cfg.enabled:
        raise LLMError("No LLM configured")
    if cfg.provider == "anthropic" and not cfg.api_key:
        raise LLMError("Anthropic needs TSF_SIZER_LLM_API_KEY")
    start = time.monotonic()
    if cfg.provider == "ollama":
        try:
            out = _request(
                "POST",
                f"{cfg.url}/api/chat",
                timeout=cfg.timeout,
                body={
                    "model": cfg.model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "options": {"temperature": cfg.temperature, "num_predict": cfg.max_tokens},
                },
            )
        except LLMError as e:
            if "HTTP 404" in str(e) and "not found" in str(e):
                raise LLMError(f"{e} - download it first: ollama pull {cfg.model}") from None
            raise
        text = (out.get("message") or {}).get("content", "")
    elif cfg.provider == "openai":
        out = _request(
            "POST",
            f"{cfg.url}/chat/completions",
            headers=_auth_headers(cfg),
            timeout=cfg.timeout,
            body={
                "model": cfg.model,
                "temperature": cfg.temperature,
                "max_tokens": cfg.max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        choices = out.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content", "")
    elif cfg.provider == "anthropic":
        out = _request(
            "POST",
            f"{cfg.url}/v1/messages",
            headers=_auth_headers(cfg),
            timeout=cfg.timeout,
            body={
                "model": cfg.model,
                "max_tokens": cfg.max_tokens,
                "temperature": cfg.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    else:
        raise LLMError(f"Unknown provider {cfg.provider}")
    if not text.strip():
        raise LLMError(f"{cfg.label} returned an empty answer")
    return LLMReply(text.strip(), cfg.model, cfg.provider, round(time.monotonic() - start, 1))


def list_models(cfg: LLMConfig) -> list[str]:
    """Models the server offers (for the settings page)."""
    if cfg.provider == "ollama":
        out = _request("GET", f"{cfg.url}/api/tags", timeout=15)
        return sorted(m.get("name", "") for m in out.get("models", []) if m.get("name"))
    if cfg.provider == "openai":
        out = _request("GET", f"{cfg.url}/models", headers=_auth_headers(cfg), timeout=15)
        return sorted(m.get("id", "") for m in out.get("data", []) if m.get("id"))
    if cfg.provider == "anthropic":
        if not cfg.api_key:
            raise LLMError("Anthropic needs TSF_SIZER_LLM_API_KEY")
        out = _request("GET", f"{cfg.url}/v1/models", headers=_auth_headers(cfg), timeout=15)
        return sorted(m.get("id", "") for m in out.get("data", []) if m.get("id"))
    return []
