"""Check a verdict against the tool results behind it.

A verdict is accepted when every model it recommends or offers was checked and meets all
requirements, and every figure in its text matches a number from a tool result or the ledger.
What cannot be matched is returned as a list of problems: the agent fixes them, or the report
shows the claims as unverified.
"""

from __future__ import annotations

import json
import re
import sqlite3

from . import ledger

_MODEL = re.compile(r"\b(?:PA|VM)-[A-Za-z0-9][A-Za-z0-9-]*")
_RESULT_ID = re.compile(r"\br\d+\b")
_VERSION = re.compile(r"\b\d+\.\d+\.\d+(?:[.-][\w]+)*\b")
_NUMBER = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(%|×|x\b|K\b|M\b|B\b|k\b|"
    r"[TGM]bps|[TGM]b/s)?",
)
_SCALE = {"k": 1e3, "K": 1e3, "M": 1e6, "B": 1e9}
_UNIT_SCALE = {"Tbps", "Gbps", "Mbps", "Tb/s", "Gb/s", "Mb/s"}


def _numbers_in(obj, acc: set[float]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        acc.add(float(obj))
    elif isinstance(obj, str):
        for m in _NUMBER.finditer(_VERSION.sub(" ", obj)):
            try:
                acc.add(float(m.group(1).replace(",", "")))
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, acc)


def known_numbers(conn: sqlite3.Connection, analysis_id: int) -> set[float]:
    acc: set[float] = set()
    for r in conn.execute(
        "SELECT result_json FROM tool_result WHERE analysis_id=? AND error IS NULL", (analysis_id,)
    ):
        _numbers_in(json.loads(r["result_json"]), acc)
    _numbers_in(ledger.values(conn, analysis_id), acc)
    return acc


def _matches(x: float, decimals: int, known: set[float], factors=(1.0,), unit_scale=1.0) -> bool:
    """x as written (rounded to `decimals`) equals a known number, directly or after a unit
    change (Mbps <-> Gbps). `unit_scale` widens the tolerance for K / M / B suffixes."""
    tol = 0.5 * 10.0**-decimals * unit_scale + 1e-9
    return any(abs(k - x * f) < tol * f for k in known for f in factors)


def unmatched_figures(text: str, known: set[float]) -> list[str]:
    """Figures in `text` that no tool result produced."""
    clean = _VERSION.sub(" ", _MODEL.sub(" ", _RESULT_ID.sub(" ", text)))
    out = []
    for m in _NUMBER.finditer(clean):
        raw, unit = m.group(1), m.group(2)
        x = float(raw.replace(",", ""))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        suffix = _SCALE.get(unit, 1.0)
        x *= suffix
        if unit is None and decimals == 0 and (x <= 12 or 2000 <= x <= 2100):
            continue  # small counts and years in prose
        factors = (1.0, 1e-3, 1e3) if unit in _UNIT_SCALE else (1.0,)
        if not _matches(x, decimals, known, factors, suffix):
            out.append(m.group(0).strip())
    return out


def _fit_results(conn: sqlite3.Connection, analysis_id: int) -> list[tuple[int, dict]]:
    """Every check_fit payload of the analysis (also those inside what_if), oldest first."""
    out = []
    for r in conn.execute(
        "SELECT id, tool, result_json FROM tool_result WHERE analysis_id=? AND error IS NULL "
        "AND tool IN ('check_fit', 'what_if') ORDER BY id",
        (analysis_id,),
    ):
        payload = json.loads(r["result_json"])
        if r["tool"] == "what_if":
            payload = payload.get("result") or {}
        if payload.get("model"):
            out.append((r["id"], payload))
    return out


def verify(conn: sqlite3.Connection, analysis_id: int, verdict: dict) -> list[str]:
    problems: list[str] = []
    rec = verdict.get("recommendation") or {}
    alts = verdict.get("alternatives") or []
    rejected = verdict.get("rejected") or []
    fits = _fit_results(conn, analysis_id)
    by_id = {rid: p for rid, p in fits}
    led = ledger.values(conn, analysis_id)

    def latest(model: str) -> dict | None:
        for _, p in reversed(fits):
            if p["model"].lower() == model.lower():
                return p
        return None

    def cited(result_ids, model: str, label: str) -> dict | None:
        payload = None
        for rid in result_ids or []:
            n = str(rid).lstrip("rR")
            p = by_id.get(int(n)) if n.isdigit() else None
            if p is None:
                if not any(str(rid).lstrip("rR") == str(i) for i, _ in fits):
                    problems.append(f"{label}: {rid} is not a check_fit result of this analysis")
                continue
            if p["model"].lower() != model.lower():
                problems.append(f"{label}: {rid} is a check of {p['model']}, not {model}")
            else:
                payload = p
        return payload or latest(model)

    names: list[tuple[str, str]] = []
    if rec.get("model"):
        names.append((rec["model"], "recommendation"))
    for i, a in enumerate(alts, 1):
        if a.get("model"):
            names.append((a["model"], f"alternative {i}"))
    for r in rejected:
        if r.get("model"):
            names.append((r["model"], "rejected model"))

    for model, label in names:
        if latest(model) is None:
            problems.append(f"{label} {model} was never checked with check_fit")
    if rec.get("model"):
        p = cited(rec.get("result_ids"), rec["model"], "recommendation")
        if p is not None:
            if not p["meets_all"]:
                problems.append(
                    f"recommendation {rec['model']} does not meet every requirement "
                    f"({'; '.join(p['failures'][:3])}). Recommend another model, none, or "
                    "change the assumptions in the ledger first"
                )
            inputs = p.get("requirement_inputs") or {}
            stale = [
                n
                for n, a, b in (
                    ("fit mode", p.get("mode"), led["fit_mode"]),
                    ("growth", inputs.get("growth_pct"), led["growth_pct"]),
                    ("years", inputs.get("years"), led["years"]),
                    ("target utilization", inputs.get("target_util_pct"), led["target_util_pct"]),
                )
                if a is not None and a != b
            ]
            if stale:
                problems.append(
                    f"the check of {rec['model']} used other {', '.join(stale)} than the ledger "
                    "has now: run check_fit again"
                )
    for i, a in enumerate(alts, 1):
        if a.get("model"):
            p = cited(a.get("result_ids"), a["model"], f"alternative {i}")
            if p is not None and not p["meets_all"]:
                problems.append(f"alternative {a['model']} does not meet every requirement")
    for r in rejected:
        if r.get("model") and r.get("result_id"):
            cited([r["result_id"]], r["model"], "rejected model")

    texts = {
        "recommendation": rec.get("reason", ""),
        "summary": verdict.get("summary", ""),
        "confidence": (verdict.get("confidence") or {}).get("reason", ""),
    }
    for i, a in enumerate(alts, 1):
        texts[f"alternative {i}"] = a.get("when_to_prefer", "")
    for i, r in enumerate(rejected, 1):
        texts[f"rejected {i}"] = r.get("reason", "")
    for i, p in enumerate(verdict.get("open_points") or [], 1):
        texts[f"open point {i}"] = str(p)
    known = known_numbers(conn, analysis_id)
    for label, text in texts.items():
        bad = unmatched_figures(str(text), known)
        if bad:
            problems.append(
                f"{label}: figure(s) {', '.join(dict.fromkeys(bad))} not found in any tool "
                "result. Use figures from tool results, or remove them"
            )
    if not rec.get("reason"):
        problems.append("recommendation needs a reason")
    return problems
