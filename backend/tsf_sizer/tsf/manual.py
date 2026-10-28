"""Evidence entered by the engineer when the customer has no Tech Support File.

The same TsfSummary the TSF parser produces is built from what the engineer knows: the current
model, load, interfaces in use, HA mode and a few configuration counts. Everything is marked as
entered, never as measured, and whatever is not known stays out (the tools report it as no
data). The agent can add to it during the conversation (record_evidence).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping

from .metrics import Metric, PortUse, TsfSummary

SOURCE = "entered by the engineer"
KINDS = {"peak": "peak", "typical": "average", "estimate": "estimate", "count": "count"}

# field -> (metric key, label, unit)
PERF_FIELDS = {
    "throughput_mbps": ("perf.throughput_snapshot_mbps", "Throughput", "Mbps"),
    "cps": ("perf.cps_snapshot", "New connections per second", "cps"),
    "sessions": ("perf.sessions", "Concurrent sessions", "sessions"),
}
COUNT_FIELDS = {
    "gp_users": ("state.gp_users", "GlobalProtect clients"),
    "security_rules": ("config.security_rules", "Security rules"),
    "nat_rules": ("config.nat_rules", "NAT rules"),
    "decryption_rules": ("config.decryption_rules", "Decryption rules"),
    "address_objects": ("config.address_objects", "Address objects"),
    "address_groups": ("config.address_groups", "Address groups"),
    "service_objects": ("config.service_objects", "Service objects"),
    "fqdn_objects": ("config.fqdn_objects", "FQDN objects"),
    "zones": ("config.zones", "Security zones"),
    "virtual_routers": ("config.virtual_routers", "Virtual routers"),
    "vsys": ("config.vsys", "Virtual systems"),
    "ipsec_tunnels": ("config.ipsec_tunnels", "IPsec tunnels"),
    "tunnel_interfaces": ("config.tunnel_interfaces", "Tunnel interfaces"),
    "aggregate_interfaces": ("config.aggregate_interfaces", "Aggregate interfaces"),
    "logical_interfaces": ("config.logical_interfaces", "Interfaces (all, incl. VLAN)"),
    "gp_gateways": ("config.gp_gateways", "GlobalProtect gateways"),
    "routing_peers": ("config.routing_peers", "Routing peers"),
}
HA_MODES = ("standalone", "active-passive", "active-active")
BASES = ("threat_prevention", "app_id")
FIELDS = (
    ["model", "ports", "ha_mode", "poe_in_use", "sizing_basis"]
    + list(PERF_FIELDS)
    + list(COUNT_FIELDS)
)
MAX_VALUE = 1e10


def number(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} needs a number")
    try:
        v = float(str(value).replace(",", "").replace(" ", "").strip())
    except ValueError:
        raise ValueError(f"'{value}' is not a number for {name}") from None
    if not 0 <= v <= MAX_VALUE:
        raise ValueError(f"{name} must be between 0 and {MAX_VALUE:g}")
    return int(v) if v.is_integer() else v


def resolve_model(conn: sqlite3.Connection, name: str) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT name, family FROM model WHERE lower(name)=lower(?)", (str(name).strip(),)
    ).fetchone()
    if row is None:
        like = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM model WHERE name LIKE ? ORDER BY name LIMIT 8",
                (f"%{str(name).strip()}%",),
            )
        ]
        raise ValueError(
            f"unknown model '{name}'." + (f" Did you mean: {', '.join(like)}?" if like else "")
        )
    return row["name"], row["family"]


def _ports(summary: TsfSummary, counts: Mapping[str, int]) -> None:
    summary.ports = [
        PortUse(f"entered-{cls}-{i}", cls, "traffic", None, None, None)
        for cls, n in counts.items()
        for i in range(1, int(n) + 1)
    ]
    summary.port_counts = {cls: int(n) for cls, n in counts.items() if n}


def apply_field(
    conn: sqlite3.Connection,
    summary: TsfSummary,
    field: str,
    value,
    kind: str = "estimate",
    note: str | None = None,
) -> str:
    """Set one entered figure on the summary. Returns a short description for the ledger line."""
    from ..agent import ledger  # port classes live with the ledger

    if field not in FIELDS:
        raise ValueError(f"unknown field '{field}'. Valid: {', '.join(FIELDS)}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    if field == "model":
        summary.model, summary.family = resolve_model(conn, value)
        return f"current model {summary.model}"
    if field == "ports":
        counts = ledger.coerce("port_add", value, conn)
        _ports(summary, counts)
        return "ports in use: " + (", ".join(f"{n}x {c}" for c, n in counts.items()) or "none")
    if field == "ha_mode":
        mode = str(value).strip().lower()
        if mode not in HA_MODES:
            raise ValueError(f"ha_mode must be one of: {', '.join(HA_MODES)}")
        summary.ha = {"enabled": mode != "standalone", "mode": mode}
        for key, on in (
            ("feature.ha_active_passive", mode == "active-passive"),
            ("feature.ha_active_active", mode == "active-active"),
        ):
            summary.metrics[key] = Metric(on, "count", SOURCE)
        return f"HA mode {mode}"
    if field == "poe_in_use":
        on = (
            value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes")
        )
        summary.poe = {"supported": True, "ports_in_use": ["entered"] if on else []}
        return "PoE devices in use" if on else "no PoE devices in use"
    if field == "sizing_basis":
        if value not in BASES:
            raise ValueError(f"sizing_basis must be one of: {', '.join(BASES)}")
        summary.sizing_basis = value
        return f"sized on {value.replace('_', ' ')}"
    if field in PERF_FIELDS:
        key, label, unit = PERF_FIELDS[field]
        v = number(value, field)
        summary.metrics[key] = Metric(v, KINDS[kind], SOURCE, unit, note)
        return f"{label} {v:g} ({kind})"
    key, label = COUNT_FIELDS[field]
    v = number(value, field)
    summary.metrics[key] = Metric(v, "count", SOURCE, None, note)
    return f"{label} {v:g}"


def warnings() -> list[str]:
    return [
        "No Tech Support File: every figure was entered by the engineer, none was measured. "
        "CPU history, resource counters and licenses are unknown. Confirm the figures, or "
        "analyse a Tech Support File, before quoting."
    ]


def build_summary(conn: sqlite3.Connection, data: Mapping[str, str]) -> TsfSummary:
    """The summary for a manual analysis from form values (strings). Raises ValueError."""
    model = str(data.get("model", "")).strip()
    if not model:
        raise ValueError("Enter the current firewall model")
    summary = TsfSummary(
        model=None,
        family=None,
        panos=None,
        uptime_days=None,
        ha={},
        licenses_active=[],
        licenses_expired=[],
        sizing_basis="threat_prevention",
        config_source=SOURCE,
        warnings=warnings(),
    )
    apply_field(conn, summary, "model", model)
    ha = str(data.get("ha_mode", "")).strip()
    if ha:
        apply_field(conn, summary, "ha_mode", ha)
    apply_field(conn, summary, "poe_in_use", bool(data.get("poe_in_use")))
    basis = str(data.get("sizing_basis", "")).strip()
    if basis:
        apply_field(conn, summary, "sizing_basis", basis)
    kind = str(data.get("throughput_kind", "") or "estimate").strip()
    for field in PERF_FIELDS:
        raw = str(data.get(field, "")).strip()
        if raw:
            apply_field(conn, summary, field, raw, kind=kind if field != "sessions" else "peak")
    for field in COUNT_FIELDS:
        raw = str(data.get(field, "")).strip()
        if raw:
            apply_field(conn, summary, field, raw, kind="count")
    ports = {}
    for name, raw in data.items():
        m = re.fullmatch(r"ports_(.+)", name)
        if m and str(raw).strip():
            ports[m.group(1)] = number(raw, name)
    if ports:
        apply_field(conn, summary, "ports", ports)
    return summary


def missing(summary: TsfSummary) -> list[str]:
    """What an engineer would want to know and has not given, for the agent to ask about."""
    m = summary.metrics
    out = []
    if "perf.throughput_snapshot_mbps" not in m:
        out.append("throughput_mbps")
    if "perf.sessions" not in m:
        out.append("sessions")
    if "perf.cps_snapshot" not in m:
        out.append("cps")
    if not summary.ports:
        out.append("ports")
    if not summary.ha:
        out.append("ha_mode")
    if "config.security_rules" not in m:
        out.append("security_rules")
    return out
