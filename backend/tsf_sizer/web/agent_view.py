"""What the analysis page shows of the agent: conversation, verdicts, assumptions ledger."""

from __future__ import annotations

import json
import sqlite3

from ..agent import ledger, verifier


def messages(conn: sqlite3.Connection, analysis_id: int, after: int = 0) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT id, run_id, role, kind, content, data_json, created_at FROM agent_message "
        "WHERE analysis_id=? AND id>? ORDER BY id",
        (analysis_id, after),
    ):
        d = dict(r)
        d["data"] = json.loads(d.pop("data_json") or "null") or {}
        out.append(d)
    return out


def run_state(conn: sqlite3.Connection, analysis_id: int) -> dict | None:
    r = conn.execute(
        "SELECT id, status, outcome, steps, mode, model, created_at, finished_at FROM agent_run "
        "WHERE analysis_id=? ORDER BY id DESC LIMIT 1",
        (analysis_id,),
    ).fetchone()
    if r is None:
        return None
    d = dict(r)
    if d["status"] == "running":
        stale = conn.execute(
            "SELECT 1 FROM agent_run WHERE id=? AND created_at < datetime('now', '-30 minutes')",
            (d["id"],),
        ).fetchone()
        if stale:  # a run that has been silent for half an hour is lost
            d["status"] = "error"
            d["outcome"] = "no answer"
    return d


def verdict_view(conn: sqlite3.Connection, analysis_id: int, version: int | None = None):
    versions = [
        r[0]
        for r in conn.execute(
            "SELECT version FROM verdict WHERE analysis_id=? ORDER BY version DESC", (analysis_id,)
        )
    ]
    if not versions:
        return None
    version = version if version in versions else versions[0]
    row = conn.execute(
        "SELECT * FROM verdict WHERE analysis_id=? AND version=?", (analysis_id, version)
    ).fetchone()
    verdict = json.loads(row["verdict_json"])
    fits = verifier._fit_results(conn, analysis_id)
    by_id = dict(fits)

    def fit_for(model: str | None, result_ids) -> dict | None:
        if not model:
            return None
        for rid in result_ids or []:
            n = str(rid).lstrip("rR")
            p = by_id.get(int(n)) if n.isdigit() else None
            if p and p["model"].lower() == model.lower():
                return p
        for _, p in reversed(fits):
            if p["model"].lower() == model.lower():
                return p
        return None

    rec = verdict.get("recommendation") or {}
    return {
        "version": version,
        "versions": versions,
        "created_at": row["created_at"],
        "verdict": verdict,
        "verified": bool(row["verified"]),
        "problems": json.loads(row["problems_json"] or "[]"),
        "ledger": json.loads(row["ledger_json"] or "{}"),
        "rec_fit": fit_for(rec.get("model"), rec.get("result_ids")),
        "alt_fits": {
            a["model"]: fit_for(a["model"], a.get("result_ids"))
            for a in verdict.get("alternatives") or []
            if a.get("model")
        },
    }


# --------------------------------------------------------------------------- ledger


def fmt_value(key: str, value) -> str:
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "none"
    if isinstance(value, dict):
        return ", ".join(f"{n}x {c}" for c, n in value.items()) or "none"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    return f"{value:,}" if isinstance(value, (int, float)) else str(value)


def ledger_rows(conn: sqlite3.Connection, analysis_id: int) -> list[dict]:
    eff = ledger.effective(conn, analysis_id)
    rows = []
    for key, spec in ledger.KEYS.items():
        e = eff[key]
        rows.append(
            {
                "key": key,
                "label": spec.label,
                "kind": spec.kind,
                "choices": spec.choices,
                "value": e["value"],
                "display": fmt_value(key, e["value"]),
                "input": input_text(key, e["value"]),
                "source": e["source"],
                "reason": e["reason"],
            }
        )
    return rows


def input_text(key: str, value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(value)
    if isinstance(value, dict):
        return ", ".join(f"{c}={n}" for c, n in value.items())
    return f"{value:g}" if isinstance(value, float) else str(value)


def parse_value(key: str, text: str):
    """The value of a ledger form field. Raises ValueError with a message for the page."""
    spec = ledger.KEYS.get(key)
    if spec is None:
        raise ValueError("Unknown assumption")
    text = text.strip()
    if spec.kind == "nullable_number" and not text:
        return None
    if spec.kind == "list":
        return [t.strip() for t in text.split(",") if t.strip()]
    if spec.kind == "ports":
        out = {}
        for part in [p for p in text.split(",") if p.strip()]:
            name, _, n = part.partition("=")
            if not n:
                raise ValueError("Write extra ports as 10G_SFP+=2, 1G_RJ45=4")
            out[name.strip()] = n.strip()
        return out
    return text
