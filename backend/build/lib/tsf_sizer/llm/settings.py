"""LLM configuration: environment defaults, overridden by settings saved in the UI.

Environment variables (container):
  TSF_SIZER_LLM_PROVIDER     none | ollama | openai | anthropic   (default none)
  TSF_SIZER_LLM_BASE_URL     server URL (defaults per provider, see providers.DEFAULT_BASE_URL)
  TSF_SIZER_LLM_MODEL        model name, e.g. llama3.1:8b, qwen2.5:14b
  TSF_SIZER_LLM_API_KEY      only for OpenAI-compatible servers that need one, or Anthropic
  TSF_SIZER_LLM_TEMPERATURE  default 0.2
  TSF_SIZER_LLM_MAX_TOKENS   default 900
  TSF_SIZER_LLM_TIMEOUT      seconds, default 180 (local models on CPU can be slow)
The API key is never stored in the database or shown in the UI.
"""

from __future__ import annotations

import os
import sqlite3

from .providers import PROVIDERS, LLMConfig

EDITABLE = ("provider", "base_url", "model", "temperature", "max_tokens", "timeout")


def _env_config() -> LLMConfig:
    def num(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except ValueError:
            return default

    return LLMConfig(
        provider=os.environ.get("TSF_SIZER_LLM_PROVIDER", "none").strip().lower() or "none",
        base_url=os.environ.get("TSF_SIZER_LLM_BASE_URL", "").strip(),
        model=os.environ.get("TSF_SIZER_LLM_MODEL", "").strip(),
        api_key=os.environ.get("TSF_SIZER_LLM_API_KEY") or None,
        temperature=num("TSF_SIZER_LLM_TEMPERATURE", 0.2),
        max_tokens=int(num("TSF_SIZER_LLM_MAX_TOKENS", 900)),
        timeout=num("TSF_SIZER_LLM_TIMEOUT", 180),
    )


def load(conn: sqlite3.Connection) -> LLMConfig:
    cfg = _env_config()
    rows = conn.execute("SELECT key, value FROM app_setting WHERE key LIKE 'llm.%'").fetchall()
    for key, value in rows:
        field = key.removeprefix("llm.")
        if field not in EDITABLE or value is None:
            continue
        if field in ("temperature", "timeout"):
            setattr(cfg, field, float(value))
        elif field == "max_tokens":
            setattr(cfg, field, int(float(value)))
        else:
            setattr(cfg, field, value)
    if cfg.provider not in PROVIDERS:
        cfg.provider = "none"
    return cfg


def save(conn: sqlite3.Connection, **values) -> None:
    with conn:
        for field, value in values.items():
            if field not in EDITABLE:
                raise ValueError(f"Not an editable LLM setting: {field}")
            conn.execute(
                "INSERT INTO app_setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value, updated_at=datetime('now')",
                (f"llm.{field}", None if value is None else str(value)),
            )


def reset(conn: sqlite3.Connection) -> None:
    """Forget UI overrides: back to the environment configuration."""
    with conn:
        conn.execute("DELETE FROM app_setting WHERE key LIKE 'llm.%'")
