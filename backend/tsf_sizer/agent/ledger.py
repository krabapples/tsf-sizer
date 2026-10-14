"""The assumptions ledger: every input that shaped the answer, with its source and reason.

The effective value of a key is its newest active row; a key with no row has its default.
Undoing a change deactivates the newest agent or user row, which brings the previous value
back. The ledger maps onto the sizing parameters, so tools and the quick result always use
what the ledger says.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from ..sizing.engine import PORT_COMPAT, SizingParams

PORT_CLASSES = sorted(PORT_COMPAT)
VARIANT_TAGS = {"5G", "RUGGED", "POE"}
FIT_MODES = ("match_or_beat", "usage_only")
MAX_PORTS = 64


@dataclass(frozen=True)
class Spec:
    kind: str  # number | int | bool | choice | list | ports | nullable_number
    default: object
    label: str
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()


KEYS: dict[str, Spec] = {
    "growth_pct": Spec("number", 20.0, "Growth per year (%)", 0, 200),
    "years": Spec("int", 3, "Planning horizon (years)", 1, 10),
    "target_util_pct": Spec("number", 70.0, "Target utilization (%)", 10, 100),
    "peak_throughput_mbps": Spec("nullable_number", None, "Known peak throughput (Mbps)", 0, 1e7),
    "peak_cps": Spec("nullable_number", None, "Known peak connections per second", 0, 1e8),
    "peak_sessions": Spec("nullable_number", None, "Known peak sessions", 0, 1e10),
    "fit_mode": Spec("choice", "match_or_beat", "Fit mode", choices=FIT_MODES),
    "port_rule": Spec("choice", "all", "Port rule", choices=("all", "used")),
    "port_drop": Spec("list", [], "Port types no longer needed"),
    "port_add": Spec("ports", {}, "Extra ports needed"),
    "need_poe": Spec("bool", False, "Customer needs PoE"),
    "include_superseded": Spec("bool", False, "Include previous-generation models"),
    "only_models": Spec("list", [], "Only these models or families"),
    "exclude_models": Spec("list", [], "Models left out"),
    "soft_metrics": Spec("list", [], "Limits that may fall short"),
    "max_size_factor": Spec(
        "number", 5.0, "Maximum size (x current performance, 0 = none)", 0, 100
    ),
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def resolve_class(value: str) -> str | None:
    """'optics', or a port class from loose spelling ('10g sfp+', '10G_SFP+')."""
    v = str(value).strip()
    if v.lower() == "optics":
        return "optics"
    n = re.sub(r"[^a-z0-9+]", "", v.lower())
    for c in PORT_CLASSES:
        if re.sub(r"[^a-z0-9+]", "", c.lower()) == n:
            return c
    return None


def model_names(conn: sqlite3.Connection) -> dict[str, str]:
    """UPPERCASE model and family names -> canonical spelling."""
    out = {}
    for r in conn.execute("SELECT name, family FROM model"):
        out[r["name"].upper()] = r["name"]
        if r["family"]:
            out[r["family"].upper()] = r["family"]
    return out


def coerce(key: str, value, conn: sqlite3.Connection | None = None):
    """Validate and normalise one value. Raises ValueError with a message for the agent."""
    spec = KEYS.get(key)
    if spec is None:
        raise ValueError(f"unknown assumption '{key}'. Known: {', '.join(sorted(KEYS))}")
    k = spec.kind
    if k in ("number", "int", "nullable_number"):
        if value is None:
            if k == "nullable_number":
                return None
            raise ValueError(f"{key} needs a number")
        if isinstance(value, bool):
            raise ValueError(f"{key} needs a number")
        try:
            v = float(str(value).replace(",", "").strip())
        except ValueError:
            raise ValueError(f"'{value}' is not a number for {key}") from None
        if key == "max_size_factor":
            if not (v == 0 or 1 < v <= spec.hi):
                raise ValueError("max_size_factor must be above 1 (or 0 for no limit)")
        elif not (spec.lo <= v <= spec.hi):
            raise ValueError(f"{key} must be between {spec.lo:g} and {spec.hi:g}")
        return int(round(v)) if k == "int" else v
    if k == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "yes", "1", "on")
    if k == "choice":
        v = str(value).strip().lower()
        if v not in spec.choices:
            raise ValueError(f"{key} must be one of {', '.join(spec.choices)}")
        return v
    if k == "list":
        items = value if isinstance(value, list) else [value] if value else []
        out = []
        for raw in items:
            s = str(raw).strip()
            if key == "port_drop":
                c = resolve_class(s)
                if c is None:
                    raise ValueError(f"unknown port type '{raw}'")
                s = c
            elif key in ("only_models", "exclude_models"):
                if s.lower().startswith("variant:"):
                    tag = s.split(":", 1)[1].strip().upper()
                    if key == "only_models" or tag not in VARIANT_TAGS:
                        raise ValueError(f"unknown model variant '{s}'")
                    s = f"VARIANT:{tag}"
                else:
                    names = model_names(conn) if conn is not None else {}
                    if names and s.upper() not in names:
                        raise ValueError(f"unknown model or family '{s}'")
                    s = names.get(s.upper(), s)
            if s not in out:
                out.append(s)
        return out
    if k == "ports":
        if not isinstance(value, dict):
            raise ValueError('port_add needs an object like {"10G_SFP+": 2}')
        out = {}
        for raw, n in value.items():
            c = resolve_class(raw)
            if c is None or c == "optics":
                raise ValueError(f"unknown port type '{raw}'")
            try:
                n = int(n)
            except (TypeError, ValueError):
                raise ValueError(f"'{n}' is not a number of ports") from None
            if not 0 <= n <= MAX_PORTS:
                raise ValueError(f"port count must be between 0 and {MAX_PORTS}")
            if n:
                out[c] = n
        return out
    raise ValueError(f"unsupported assumption '{key}'")


def defaults() -> dict:
    return {
        k: (
            list(s.default)
            if isinstance(s.default, list)
            else dict(s.default)
            if isinstance(s.default, dict)
            else s.default
        )
        for k, s in KEYS.items()
    }


def effective(conn: sqlite3.Connection, analysis_id: int) -> dict[str, dict]:
    """key -> {value, source, reason, id}. Keys without a row carry source 'default'."""
    out = {
        k: {"value": v, "source": "default", "reason": None, "id": None}
        for k, v in defaults().items()
    }
    rows = conn.execute(
        "SELECT id, key, value_json, source, reason FROM assumption "
        "WHERE analysis_id=? AND active=1 ORDER BY id",
        (analysis_id,),
    ).fetchall()
    for r in rows:
        if r["key"] in out:
            out[r["key"]] = {
                "value": json.loads(r["value_json"]),
                "source": r["source"],
                "reason": r["reason"],
                "id": r["id"],
            }
    return out


def values(conn: sqlite3.Connection, analysis_id: int) -> dict:
    return {k: e["value"] for k, e in effective(conn, analysis_id).items()}


def set_value(
    conn: sqlite3.Connection,
    analysis_id: int,
    key: str,
    value,
    source: str,
    reason: str | None = None,
) -> dict | None:
    """Record a new value. Returns the change ({key, old, new}) or None when nothing changed."""
    new = coerce(key, value, conn)
    old = effective(conn, analysis_id)[key]["value"]
    if new == old:
        return None
    conn.execute(
        "INSERT INTO assumption (analysis_id, key, value_json, source, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        (analysis_id, key, json.dumps(new), source, (reason or "")[:500] or None),
    )
    return {"key": key, "old": old, "new": new}


def seed(conn: sqlite3.Connection, analysis_id: int, params: SizingParams, source="form") -> None:
    """Initial ledger from the upload form: only values that differ from the defaults."""
    wanted = from_params(params)
    for key, value in wanted.items():
        set_value(conn, analysis_id, key, value, source)


def undo_last(conn: sqlite3.Connection, analysis_id: int) -> dict | None:
    """Deactivate the newest agent or user change."""
    row = conn.execute(
        "SELECT id, key, value_json FROM assumption WHERE analysis_id=? AND active=1 "
        "AND source IN ('agent', 'user') ORDER BY id DESC LIMIT 1",
        (analysis_id,),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE assumption SET active=0 WHERE id=?", (row["id"],))
    return {"key": row["key"], "undone": json.loads(row["value_json"])}


def history(conn: sqlite3.Connection, analysis_id: int) -> list[dict]:
    return [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "key": r["key"],
            "value": json.loads(r["value_json"]),
            "source": r["source"],
            "reason": r["reason"],
            "active": bool(r["active"]),
        }
        for r in conn.execute(
            "SELECT * FROM assumption WHERE analysis_id=? ORDER BY id DESC", (analysis_id,)
        )
    ]


def to_params(v: dict) -> SizingParams:
    return SizingParams(
        growth_pct_per_year=v["growth_pct"],
        years=v["years"],
        target_util_pct=v["target_util_pct"],
        peak_throughput_mbps=v["peak_throughput_mbps"],
        peak_cps=v["peak_cps"],
        peak_sessions=v["peak_sessions"],
        port_rule=v["port_rule"],
        include_superseded=v["include_superseded"],
        need_poe=v["need_poe"],
        max_size_factor=v["max_size_factor"],
        right_size=v["fit_mode"] == "usage_only",
        only_models=list(v["only_models"]),
        port_drop=list(v["port_drop"]),
        port_add=dict(v["port_add"]),
        soft_metrics=list(v["soft_metrics"]),
        exclude_models=list(v["exclude_models"]),
    )


def from_params(p: SizingParams) -> dict:
    """The ledger entries that differ from the defaults."""
    full = {
        "growth_pct": p.growth_pct_per_year,
        "years": p.years,
        "target_util_pct": p.target_util_pct,
        "peak_throughput_mbps": p.peak_throughput_mbps,
        "peak_cps": p.peak_cps,
        "peak_sessions": p.peak_sessions,
        "fit_mode": "usage_only" if p.right_size else "match_or_beat",
        "port_rule": p.port_rule,
        "port_drop": list(p.port_drop),
        "port_add": dict(p.port_add),
        "need_poe": p.need_poe,
        "include_superseded": p.include_superseded,
        "only_models": list(p.only_models),
        "exclude_models": list(p.exclude_models),
        "soft_metrics": list(p.soft_metrics),
        "max_size_factor": p.max_size_factor,
    }
    base = defaults()
    return {k: v for k, v in full.items() if v != base[k]}
