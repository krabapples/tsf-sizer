"""Apply structured adjustments to the sizing parameters.

The assistant (an LLM) only translates what the user says into the small action language
below. Everything is validated here, in code, before it can change anything: unknown
actions, unknown model/metric/port names and out-of-range numbers are refused with a reason
the assistant can pass on. The sizing itself stays in engine.py.

Actions (dicts):
  {"action": "set", "name": <NUMBER_PARAMS | BOOL_PARAMS | "port_rule">, "value": ...}
  {"action": "ports", "drop": ["optics" | class], "keep": [...], "add": {class: n}, "reset": bool}
  {"action": "ignore", "metric": key}      # may fall short without excluding a model; flagged
  {"action": "enforce", "metric": key}     # undo "ignore"
  {"action": "exclude", "models": [model | family | "variant:5G"]}
  {"action": "include", "models": [...]}   # undo "exclude"
  {"action": "reset"}                      # clear all port/ignore/exclude adjustments
  {"action": "explain", "model": name}     # read-only: why a model was or was not chosen
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .engine import PORT_COMPAT, SizingParams, is_optical

# name used by the assistant -> (SizingParams field, minimum, maximum, integer?)
NUMBER_PARAMS = {
    "growth_pct": ("growth_pct_per_year", 0, 200, False),
    "years": ("years", 1, 10, True),
    "target_util_pct": ("target_util_pct", 10, 100, False),
    "peak_throughput_mbps": ("peak_throughput_mbps", 0, 10_000_000, False),
    "peak_cps": ("peak_cps", 0, 100_000_000, False),
    "peak_sessions": ("peak_sessions", 0, 10_000_000_000, False),
    "max_size_factor": ("max_size_factor", 0, 100, False),
}
NULLABLE = {"peak_throughput_mbps", "peak_cps", "peak_sessions"}  # null clears a known peak
BOOL_PARAMS = {"need_poe": "need_poe", "include_superseded": "include_superseded"}
PORT_CLASSES = sorted(PORT_COMPAT)
VARIANT_TAGS = {"5G", "RUGGED", "POE"}
MAX_PORTS = 64


@dataclass
class Applied:
    params: SizingParams
    changes: list[str] = field(default_factory=list)  # human-readable, one per change
    errors: list[str] = field(default_factory=list)  # refused actions, with the reason
    explain: list[str] = field(default_factory=list)  # models the user asked about

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def resolve_metric(value: str, metrics: dict[str, str]) -> str | None:
    """Accept the metric key, its label, or the last part of the key."""
    v = str(value).strip()
    if v in metrics:
        return v
    n = _norm(v)
    for key, label in metrics.items():
        if n in (_norm(label), _norm(key.split(".", 1)[-1])):
            return key
    hits = [k for k, label in metrics.items() if n and n in _norm(label)]
    return hits[0] if len(hits) == 1 else None


def resolve_class(value: str) -> str | None:
    v = str(value).strip()
    if v.lower() == "optics":
        return "optics"
    for c in PORT_CLASSES:
        if c.lower() == v.lower():
            return c
    # tolerant: "10G SFP+", "sfp+", "10g_sfp+" -> 10G_SFP+
    n = re.sub(r"[^a-z0-9+]", "", v.lower())
    for c in PORT_CLASSES:
        if re.sub(r"[^a-z0-9+]", "", c.lower()) == n:
            return c
    return None


def _number(name: str, value, spec) -> float | int | None:
    field_name, lo, hi, as_int = spec
    if value is None:
        if name in NULLABLE:
            return None
        raise ValueError(f"{name} needs a number")
    if isinstance(value, bool):
        raise ValueError(f"{name} needs a number")
    try:
        v = float(str(value).replace(",", "").strip())
    except ValueError:
        raise ValueError(f"'{value}' is not a number for {name}") from None
    if name == "max_size_factor":
        if not (v == 0 or 1 < v <= hi):
            raise ValueError("max_size_factor must be above 1 (or 0 for no limit)")
    elif not (lo <= v <= hi):
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return int(round(v)) if as_int else v


def apply_actions(
    params: SizingParams,
    actions: list[dict],
    metrics: dict[str, str],
    models: dict[str, str],
) -> Applied:
    """`metrics`: key -> label of every metric that may be ignored.
    `models`: UPPERCASE model or family name -> canonical name."""
    p = replace(
        params,
        port_drop=list(params.port_drop),
        port_add=dict(params.port_add),
        soft_metrics=list(params.soft_metrics),
        exclude_models=list(params.exclude_models),
    )
    out = Applied(p)

    def note(text: str) -> None:
        if text not in out.changes:
            out.changes.append(text)

    for a in actions:
        if not isinstance(a, dict):
            out.errors.append("each action must be an object")
            continue
        kind = str(a.get("action", "")).lower()
        try:
            if kind == "set":
                name = str(a.get("name", ""))
                if name in NUMBER_PARAMS:
                    spec = NUMBER_PARAMS[name]
                    v = _number(name, a.get("value"), spec)
                    if getattr(p, spec[0]) != v:
                        setattr(p, spec[0], v)
                        note(f"{name} = {'none' if v is None else v}")
                elif name in BOOL_PARAMS:
                    v = a.get("value")
                    if not isinstance(v, bool):
                        v = str(v).strip().lower() in ("true", "yes", "1", "on")
                    if getattr(p, BOOL_PARAMS[name]) != v:
                        setattr(p, BOOL_PARAMS[name], v)
                        note(f"{name} = {'yes' if v else 'no'}")
                elif name == "port_rule":
                    v = "used" if str(a.get("value")).lower() == "used" else "all"
                    if p.port_rule != v:
                        p.port_rule = v
                        note(f"port_rule = {v}")
                else:
                    raise ValueError(f"unknown setting '{name}'")
            elif kind == "ports":
                if a.get("reset"):
                    if p.port_drop or p.port_add:
                        note("port adjustments cleared")
                    p.port_drop, p.port_add = [], {}
                for raw in a.get("drop") or []:
                    c = resolve_class(raw)
                    if c is None:
                        raise ValueError(f"unknown port type '{raw}'")
                    if c not in p.port_drop:
                        p.port_drop.append(c)
                        note("optics no longer needed" if c == "optics" else f"no {c} ports needed")
                    # whatever was asked on top of a dropped class is gone too
                    p.port_add = {
                        k: v
                        for k, v in p.port_add.items()
                        if not ((c == "optics" and is_optical(k)) or k == c)
                    }
                for raw in a.get("keep") or []:
                    c = resolve_class(raw)
                    if c is None:
                        raise ValueError(f"unknown port type '{raw}'")
                    if c in p.port_drop:
                        p.port_drop.remove(c)
                        note("optics needed again" if c == "optics" else f"{c} ports needed again")
                for raw, n in (a.get("add") or {}).items():
                    c = resolve_class(raw)
                    if c is None or c == "optics":
                        raise ValueError(f"unknown port type '{raw}'")
                    try:
                        n = int(n)
                    except (TypeError, ValueError):
                        raise ValueError(f"'{n}' is not a number of ports") from None
                    if not 0 <= n <= MAX_PORTS:
                        raise ValueError(f"port count must be between 0 and {MAX_PORTS}")
                    # asking for optics again lifts an earlier "no optics"
                    for d in ("optics", c):
                        if d in p.port_drop and is_optical(c):
                            p.port_drop.remove(d)
                    if n == 0:
                        if p.port_add.pop(c, None) is not None:
                            note(f"extra {c} ports removed")
                    elif p.port_add.get(c) != n:
                        p.port_add[c] = n
                        note(f"{n}x {c} additionally needed")
            elif kind in ("ignore", "enforce"):
                key = resolve_metric(a.get("metric", ""), metrics)
                if key is None:
                    raise ValueError(f"unknown limit '{a.get('metric')}'")
                if kind == "ignore" and key not in p.soft_metrics:
                    p.soft_metrics.append(key)
                    note(f"'{metrics[key]}' no longer excludes a model (shortfall is flagged)")
                if kind == "enforce" and key in p.soft_metrics:
                    p.soft_metrics.remove(key)
                    note(f"'{metrics[key]}' is a hard requirement again")
            elif kind in ("exclude", "include"):
                for raw in a.get("models") or []:
                    s = str(raw).strip()
                    if s.lower().startswith("variant:"):
                        tag = s.split(":", 1)[1].strip().upper()
                        if tag not in VARIANT_TAGS:
                            raise ValueError(f"unknown model variant '{s}'")
                        canon = f"VARIANT:{tag}"
                    elif s.upper() in models:
                        canon = s.upper()
                    else:
                        raise ValueError(f"unknown model or family '{s}'")
                    shown = models.get(canon, canon.lower() if canon.startswith("VARIANT") else s)
                    if kind == "exclude" and canon not in p.exclude_models:
                        p.exclude_models.append(canon)
                        note(f"{shown} left out")
                    if kind == "include" and canon in p.exclude_models:
                        p.exclude_models.remove(canon)
                        note(f"{shown} allowed again")
            elif kind == "reset":
                if p.adjusted:
                    note("all adjustments cleared")
                p.port_drop, p.port_add = [], {}
                p.soft_metrics, p.exclude_models = [], []
            elif kind == "explain":
                m = str(a.get("model", "")).strip().upper()
                if m not in models:
                    raise ValueError(f"unknown model '{a.get('model')}'")
                out.explain.append(models[m])
            else:
                raise ValueError(f"unknown action '{a.get('action')}'")
        except ValueError as e:
            out.errors.append(str(e))
    return out


def describe(params: SizingParams, metrics: dict[str, str]) -> list[str]:
    """The adjustments in effect, for display."""
    d = []
    for c in params.port_drop:
        d.append("no optics needed" if c == "optics" else f"no {c} ports needed")
    for c, n in params.port_add.items():
        d.append(f"{n}x {c} needed")
    for k in params.soft_metrics:
        d.append(f"{metrics.get(k, k)}: not blocking")
    for m in params.exclude_models:
        d.append(f"{m.lower()} left out" if m.startswith("VARIANT:") else f"{m} left out")
    return d
