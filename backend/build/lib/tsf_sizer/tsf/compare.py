"""Compare TSF usage with a model's capacities from the portfolio database."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field

from ..portfolio import catalog
from .metrics import TsfSummary


@dataclass
class UsageRow:
    metric: str
    attribute: str
    observed: float | int | bool | None
    observed_kind: str
    capacity: float | None
    capacity_raw: str | None
    utilization_pct: float | None
    unconfirmed: bool
    note: str | None = None


@dataclass
class UsageReport:
    model: str
    document_id: int
    rows: list[UsageRow] = field(default_factory=list)
    ports_needed: dict[str, int] = field(default_factory=dict)
    ports_available: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_port_media(
    port_counts: dict[str, int], model_ports: dict[str, int]
) -> tuple[dict[str, int], list[str]]:
    """Replace '<speed>G_unknown-media' with the model's only port class at that speed."""
    out: dict[str, int] = {}
    notes = []
    for cls, n in port_counts.items():
        if cls == "unknown" and len(model_ports) == 1:
            only = next(iter(model_ports))
            out[only] = out.get(only, 0) + n
            notes.append(
                f"{n} port(s) with unknown speed and media assumed {only} "
                "(the model's only port type)"
            )
            continue
        m = re.match(r"^([\d.]+G)_unknown-media$", cls)
        if not m:
            out[cls] = out.get(cls, 0) + n
            continue
        candidates = [c for c in model_ports if c.startswith(m.group(1) + "_")]
        if len(candidates) == 1:
            out[candidates[0]] = out.get(candidates[0], 0) + n
            notes.append(
                f"{n} port(s) at {m.group(1)} with unknown media assumed "
                f"{candidates[0]} (the model's only {m.group(1)} port type)"
            )
        else:
            out[cls] = out.get(cls, 0) + n
            notes.append(f"{n} port(s) at {m.group(1)}: media unknown, check the TSF config")
    return out, notes


def usage_against_model(
    conn: sqlite3.Connection,
    summary: TsfSummary,
    model: str | None = None,
    document_id: int | None = None,
) -> UsageReport:
    model = model or summary.model
    if not model:
        raise ValueError("Model unknown: pass one explicitly")
    if document_id is None:
        doc = catalog.active_document(conn)
        if doc is None:
            raise LookupError("No active portfolio document; import the capacity workbook first")
        document_id = doc["id"]
    caps = catalog.mapped_capacities(conn, model, document_id)
    if not caps or all(c["kind"] is None for c in caps.values()):
        raise LookupError(f"{model} is not in portfolio document {document_id}")

    report = UsageReport(model=model, document_id=document_id)
    m = summary.metrics

    def add(metric_key: str, cap_key: str, observed, kind, note=None, scale=1.0):
        cap = caps.get(cap_key)
        if cap is None:
            return
        num = cap["num_value"]
        util = None
        if isinstance(observed, (int, float)) and not isinstance(observed, bool) and num:
            util = round(100.0 * observed * scale / num, 1)
        report.rows.append(
            UsageRow(
                metric=metric_key,
                attribute=cap["name"],
                observed=observed,
                observed_kind=kind,
                capacity=num,
                capacity_raw=cap["raw_value"],
                utilization_pct=util,
                unconfirmed=bool(cap["unconfirmed"]),
                note=note,
            )
        )

    # Throughput: compare snapshot and since-boot average with the datasheet figure.
    thr_cap = (
        "perf.throughput_threat_gbps"
        if summary.sizing_basis == "threat_prevention"
        else "perf.throughput_appid_gbps"
    )
    for key in ("perf.throughput_snapshot_mbps", "perf.throughput_avg_since_boot_mbps"):
        if key in m:
            add(key, thr_cap, m[key].value, m[key].kind, m[key].note, scale=0.001)
    if "perf.cps_snapshot" in m:
        add("perf.cps_snapshot", "perf.cps", m["perf.cps_snapshot"].value, "snapshot")

    for key, metric in m.items():
        if key.startswith(
            ("perf.throughput", "perf.cps", "perf.packet", "perf.dp_cpu", "perf.sessions_snapshot")
        ):
            continue
        if key in caps:
            add(key, key, metric.value, metric.kind, metric.note)

    model_ports = catalog.interface_ports(conn, model, document_id)
    needed, notes = resolve_port_media(summary.port_counts, model_ports)
    report.ports_needed = needed
    report.ports_available = model_ports
    report.warnings.extend(notes)
    if "perf.dp_cpu_peak_pct" in m:
        report.warnings.append(
            f"Peak dataplane CPU in available history: {m['perf.dp_cpu_peak_pct'].value}% "
            f"({m['perf.dp_cpu_peak_pct'].note})."
        )
    return report
