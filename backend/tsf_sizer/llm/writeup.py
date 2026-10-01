"""LLM-written explanation of a sizing result, with a fact check.

The model only ever sees `build_facts()`: model names, counts, capacities,
warnings and the sizing assumptions. No TSF content, hostnames, IP addresses,
serial numbers, admin or object names are included.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime

from .providers import LOCAL_PROVIDERS, LLMConfig, LLMError, chat

SYSTEM_PROMPT = """You are a senior Palo Alto Networks presales engineer. You write a short,
well-argued justification for a firewall replacement, for a colleague who will review it before
it goes to the customer.

Strict rules:
- Use ONLY the facts in the JSON you are given. Never invent numbers, models, features or prices.
- Quote numbers exactly as they appear in the JSON (you may round percentages to whole numbers).
- Mention only models that appear in the JSON.
- Be concrete: compare the recommended model with the current model and the requirement.
- If something is uncertain (snapshot values, short history, unconfirmed capacity values),
  say so plainly.
- English, plain Markdown, at most about 250 words. No tables, no code blocks.

Use exactly this structure:
## Summary
Two or three sentences: current model, recommended model, the main reason.
## Why <recommended model>
Three to five bullet points with the decisive numbers (performance, the closest limits, ports).
## Alternatives
One bullet per alternative, and one bullet on why the most relevant other models were rejected.
## Before quoting
Two to four bullets with the most important checks from the warnings.
"""


def _num(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def build_facts(result: dict) -> dict:
    """The data sent to the LLM: aggregated figures only."""
    s = result.get("summary") or {}
    sizing = result.get("sizing") or {}
    params = sizing.get("params") or {}
    ha = s.get("ha") or {}

    def candidate(c: dict) -> dict:
        checks = {k["metric"]: k for k in c.get("checks", [])}

        def cap(metric):
            k = checks.get(metric) or {}
            return _num(k.get("candidate_capacity"))

        plan = c.get("port_plan") or {}
        return {
            "model": c["model"],
            "family": c.get("family"),
            "threat_throughput_gbps": cap("perf.throughput_threat_gbps"),
            "app_id_throughput_gbps": cap("perf.throughput_appid_gbps"),
            "max_sessions": cap("perf.sessions"),
            "connections_per_second": cap("perf.cps"),
            "ports": plan.get("available"),
            "dedicated_ha_ports": plan.get("dedicated_ha"),
            "poe_ports": c.get("poe_ports"),
            "closest_limits": c.get("tightest", []),
            "relies_on_unconfirmed_values": c.get("unconfirmed_used", []),
        }

    reqs = sorted(
        (r for r in sizing.get("requirements", []) if r.get("observed") or r.get("is_perf")),
        key=lambda r: (
            -((r["observed"] / r["current_capacity"]) if r.get("current_capacity") else 0)
        ),
    )
    usage = [
        {
            "capacity": r["label"],
            "in_use": _num(r["observed"]),
            "basis": r.get("observed_basis"),
            "current_model_limit": _num(r.get("current_capacity")),
            "usage_pct": round(100 * r["observed"] / r["current_capacity"], 1)
            if r.get("current_capacity")
            else None,
            "required_after_growth": _num(r["required"]),
            "unit": r.get("unit"),
        }
        for r in reqs[:12]
    ]
    rec = sizing.get("recommended")
    return {
        "current_model": s.get("model"),
        "panos": s.get("panos"),
        "uptime_days": s.get("uptime_days"),
        "high_availability": ha.get("mode") if ha.get("enabled") else "standalone",
        "sized_on": (s.get("sizing_basis") or "").replace("_", " ") + " throughput",
        "assumptions": {
            "growth_pct_per_year": _num(params.get("growth_pct_per_year")),
            "years": params.get("years"),
            "target_utilization_pct": _num(params.get("target_util_pct")),
            "max_size_factor": _num(params.get("max_size_factor")),
            "port_rule": (sizing.get("ports_needed") or {}).get("rule"),
            "poe_needed": sizing.get("need_poe"),
            "previous_generations_included": params.get("include_superseded"),
        },
        "usage_highlights": usage,
        "recommended": candidate(rec) if rec else None,
        "alternatives": [candidate(c) for c in sizing.get("alternatives", [])],
        "not_qualifying": [
            {
                "model": c["model"],
                "reasons": (
                    ["too large: " + c["too_large"]]
                    if c.get("too_large")
                    else c.get("failures", [])
                )[:2],
            }
            for c in (sizing.get("too_large", [])[:3] + sizing.get("rejected", [])[:6])
        ],
        "warnings": (s.get("warnings", []) + sizing.get("notes", []))[:12],
    }


# --------------------------------------------------------------------------- fact check

_MODEL = re.compile(r"\bPA-\d+[A-Z0-9-]*\b|\bVM-\d+\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w])")


def _norm(n: str) -> str | None:
    n = n.replace(",", "")
    try:
        f = float(n)
    except ValueError:
        return None
    # Not f"{f:g}": that keeps only 6 significant digits (1234567 -> 1.23457e+06).
    return str(int(f)) if f.is_integer() else repr(round(f, 6))


def _allowed_numbers(facts: dict) -> set[str]:
    blob = json.dumps(facts)
    blob = _MODEL.sub(" ", blob)
    allowed = set()
    for m in _NUMBER.findall(blob):
        n = _norm(m)
        if n is None:
            continue
        allowed.add(n)
        f = float(n)
        allowed.add(_norm(str(round(f))))  # rounding is allowed
        allowed.add(_norm(str(round(f, 1))))
    return allowed


def fact_check(text: str, facts: dict) -> dict:
    """Numbers and model names in the text that do not appear in the facts."""
    blob = json.dumps(facts).upper()
    models = sorted({m.upper() for m in _MODEL.findall(text)})
    unknown_models = [m for m in models if m not in blob]
    allowed = _allowed_numbers(facts)
    stripped = _MODEL.sub(" ", text)
    stripped = re.sub(r"^#+ .*$", " ", stripped, flags=re.M)  # headings
    numbers = [_norm(n) for n in _NUMBER.findall(stripped)]
    unknown_numbers = sorted(
        {
            n
            for n in numbers
            if n and n not in allowed and not (float(n).is_integer() and float(n) <= 5)
        },
        key=lambda x: float(x),
    )
    return {"unverified_numbers": unknown_numbers, "unverified_models": unknown_models}


# --------------------------------------------------------------------------- run + render


def write(cfg: LLMConfig, result: dict) -> dict:
    """Generate the write-up. Never raises: failures are returned as {'error': ...}."""
    base = {
        "provider": cfg.provider,
        "model": cfg.model,
        "label": cfg.label,
        "local": cfg.provider in LOCAL_PROVIDERS,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }
    if not (result.get("sizing") or {}).get("recommended"):
        return base | {"error": "No recommendation to explain."}
    facts = build_facts(result)
    user = "Write the justification for this sizing result. Facts (JSON):\n" + json.dumps(
        facts, indent=1, ensure_ascii=False
    )
    try:
        reply = chat(cfg, SYSTEM_PROMPT, user)
    except LLMError as e:
        return base | {"error": str(e)}
    return base | {"text": reply.text, "seconds": reply.seconds, **fact_check(reply.text, facts)}


def render_markdown(text: str) -> str:
    """Tiny, safe Markdown subset -> HTML (headings, bullets, bold, paragraphs).
    Everything is HTML-escaped first, so model output cannot inject markup."""
    out, in_list, para = [], False, []

    def flush_para():
        if para:
            out.append("<p>" + " ".join(para) + "</p>")
            para.clear()

    for raw in text.splitlines():
        line = html.escape(raw.strip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        if not line:
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if line.startswith("#"):
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<h3>" + line.lstrip("#").strip() + "</h3>")
        elif re.match(r"^[-*•] ", line):
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + line[2:].strip() + "</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            para.append(line)
    flush_para()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)
