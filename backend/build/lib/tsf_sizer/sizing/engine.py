"""Pick replacement models: requirements from TSF usage, hard rules, ranking.

Rules (the team's sizing policy):
  * every capacity must cover usage x growth (performance also / target utilization)
  * every capacity must be at least the current model's; performance strictly more
  * ports: by default the candidate must offer at least the current model's full
    port layout; optionally only the ports actually in use
  * features in use (HA mode, GTP, SCTP, ...) must be supported
  * only quotable models (not NPI, not chassis components) are considered
  * previous-generation families are left out unless the user includes them
  * models far larger than needed (max_size_factor x the current performance)
    are not recommended
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field

from ..portfolio import catalog
from ..tsf.compare import resolve_port_media
from ..tsf.metrics import TsfSummary

PERF_METRICS = ("perf.throughput", "perf.cps", "perf.sessions")

# Per-item limits (how big one group/aggregate may be) are not object counts: they
# only need to cover usage x growth, not the current model's figure. Without this,
# a successor generation with a lower per-group limit is rejected for a limit the
# customer doesn't use.
BASELINE_EXEMPT = frozenset(
    {
        "config.max_address_group_members",
        "config.max_aggregate_members",
    }
)

# Which candidate port classes can serve a needed port class (best fit first).
PORT_COMPAT: dict[str, tuple[str, ...]] = {
    "1G_RJ45": ("1G_RJ45", "1G_COMBO", "2.5G_RJ45", "5G_RJ45", "10G_RJ45"),
    "2.5G_RJ45": ("2.5G_RJ45", "5G_RJ45", "10G_RJ45"),
    "5G_RJ45": ("5G_RJ45", "10G_RJ45"),
    "10G_RJ45": ("10G_RJ45",),
    "1G_COMBO": ("1G_COMBO",),
    "1G_SFP": ("1G_SFP", "1G_COMBO", "10G_SFP+"),
    "10G_SFP+": ("10G_SFP+", "25G_SFP28"),
    "25G_SFP28": ("25G_SFP28",),
    "40G_QSFP+": ("40G_QSFP+", "100G_QSFP28"),
    "100G_QSFP28": ("100G_QSFP28", "400G_QSFP-DD"),
    "100G_SFP-DD": ("100G_SFP-DD",),
    "400G_QSFP-DD": ("400G_QSFP-DD",),
}
_SPEED_ORDER = ["1G", "2.5G", "5G", "10G", "25G", "40G", "100G", "400G"]


def variants(model: str) -> set[str]:
    """Special-purpose variants: cellular (-5G), rugged (R suffix), PoE."""
    name = model.upper()
    out = set()
    if "-5G" in name:
        out.add("5G")
    if "POE" in name:
        out.add("PoE")
    if re.match(r"^PA-\d+R\b", name):
        out.add("rugged")
    return out


@dataclass
class SizingParams:
    growth_pct_per_year: float = 20.0
    years: int = 3
    target_util_pct: float = 70.0
    peak_throughput_mbps: float | None = None
    peak_cps: float | None = None
    peak_sessions: float | None = None
    port_rule: str = "all"  # "all": current model's full port layout; "used": ports in use
    include_npi: bool = False
    # Previous-generation families (family_setting.superseded_by) are left out unless included.
    include_superseded: bool = False
    # PoE: dedicated PoE models (-POE) are only recommended when PoE is needed; when it is,
    # only models with PoE ports qualify. PoE in use on the current firewall forces it on.
    need_poe: bool = False
    # A candidate may offer at most this many times the performance of the current model
    # (or of the requirement, if higher). 0 disables the cap.
    max_size_factor: float = 5.0

    @property
    def growth_factor(self) -> float:
        return (1 + self.growth_pct_per_year / 100) ** self.years


@dataclass
class Requirement:
    metric: str
    label: str
    observed: float
    observed_basis: str
    required: float
    current_capacity: float | None
    unit: str | None = None
    is_perf: bool = False


@dataclass
class Check:
    metric: str
    label: str
    required: float | None
    current_capacity: float | None
    candidate_capacity: float | None
    candidate_raw: str | None
    status: str  # pass | fail | unknown
    reason: str | None = None
    headroom_pct: float | None = None
    unconfirmed: bool = False


@dataclass
class Candidate:
    model: str
    family: str | None
    passes: bool
    checks: list[Check] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    port_plan: dict = field(default_factory=dict)
    min_headroom_pct: float | None = None
    tightest: list[str] = field(default_factory=list)
    unconfirmed_used: list[str] = field(default_factory=list)
    extra_variants: list[str] = field(default_factory=list)
    poe_ports: int = 0
    too_large: str | None = None
    unknown_features: list[str] = field(default_factory=list)
    sort_key: float = 0.0


@dataclass
class SizingResult:
    params: dict
    current_model: str | None
    current_in_portfolio: bool
    document_id: int
    requirements: list[Requirement] = field(default_factory=list)
    ports_needed: dict = field(default_factory=dict)
    recommended: Candidate | None = None
    alternatives: list[Candidate] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    too_large: list[Candidate] = field(default_factory=list)
    excluded_families: dict = field(default_factory=dict)
    size_cap_gbps: float | None = None
    need_poe: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- requirements


def build_requirements(
    summary: TsfSummary, params: SizingParams, current_caps: dict[str, dict] | None
) -> tuple[list[Requirement], list[str]]:
    m = summary.metrics
    g = params.growth_factor
    u = params.target_util_pct / 100
    notes: list[str] = []
    reqs: list[Requirement] = []
    thr_key = (
        "perf.throughput_threat_gbps"
        if summary.sizing_basis == "threat_prevention"
        else "perf.throughput_appid_gbps"
    )

    def cur(key):
        if not current_caps or key not in current_caps:
            return None
        return current_caps[key].get("num_value")

    # Throughput (Gbps)
    if params.peak_throughput_mbps is not None:
        obs, basis = params.peak_throughput_mbps, "entered peak"
    else:
        cands = [
            (m[k].value, m[k].kind)
            for k in ("perf.throughput_snapshot_mbps", "perf.throughput_avg_since_boot_mbps")
            if k in m and m[k].value is not None
        ]
        obs, basis = max(cands) if cands else (None, None)
        if obs is not None:
            notes.append(
                "Throughput requirement is based on a TSF " + basis + " value; enter "
                "the known peak for a reliable result."
            )
    if obs is not None:
        reqs.append(
            Requirement(
                thr_key,
                "Throughput (" + summary.sizing_basis.replace("_", " ") + ")",
                obs / 1000,
                basis,
                round(obs / 1000 * g / u, 3),
                cur(thr_key),
                "Gbps",
                True,
            )
        )
    # Connections per second
    if params.peak_cps is not None:
        obs, basis = params.peak_cps, "entered peak"
    elif "perf.cps_snapshot" in m:
        obs, basis = m["perf.cps_snapshot"].value, "snapshot"
    else:
        obs = None
    if obs is not None:
        reqs.append(
            Requirement(
                "perf.cps",
                "Connections per second",
                obs,
                basis,
                math.ceil(obs * g / u),
                cur("perf.cps"),
                "cps",
                True,
            )
        )
    # Sessions
    if params.peak_sessions is not None:
        obs, basis = params.peak_sessions, "entered peak"
    elif "perf.sessions" in m:
        obs, basis = m["perf.sessions"].value, "estimated peak"
    elif "perf.sessions_snapshot" in m:
        obs, basis = m["perf.sessions_snapshot"].value, "snapshot"
    else:
        obs = None
    if obs is not None:
        reqs.append(
            Requirement(
                "perf.sessions",
                "Concurrent sessions",
                obs,
                basis,
                math.ceil(obs * g / u),
                cur("perf.sessions"),
                "sessions",
                True,
            )
        )

    # Counts: usage x growth (no utilization margin; limits are hard caps).
    for key, metric in m.items():
        if not key.startswith(("config.", "state.")):
            continue
        if not isinstance(metric.value, (int, float)) or isinstance(metric.value, bool):
            continue
        if current_caps is not None and key not in current_caps:
            continue
        # vsys don't grow by percentage: the count in use is the requirement.
        required = metric.value if key == "config.vsys" else math.ceil(metric.value * g)
        reqs.append(
            Requirement(
                key,
                current_caps[key]["name"] if current_caps else key,
                metric.value,
                metric.kind,
                required,
                cur(key),
            )
        )
    return reqs, notes


# --------------------------------------------------------------------------- checks


def _check(req: Requirement, cap: dict | None) -> Check:
    label, required = req.label, req.required
    if cap is None or cap.get("kind") in (None, "empty"):
        return Check(
            req.metric,
            label,
            required,
            req.current_capacity,
            None,
            None,
            "unknown",
            "no value in the capacity sheet",
        )
    kind, num, raw = cap["kind"], cap["num_value"], cap["raw_value"]
    unconf = bool(cap.get("unconfirmed"))
    if kind == "special":
        if cap["special"] in ("system_limit", "configurable"):
            return Check(
                req.metric,
                label,
                required,
                req.current_capacity,
                None,
                raw,
                "pass",
                f"limit is '{raw}'",
                unconfirmed=unconf,
            )
        ok = not required
        return Check(
            req.metric,
            label,
            required,
            req.current_capacity,
            None,
            raw,
            "pass" if ok else "fail",
            None if ok else "not supported",
            unconfirmed=unconf,
        )
    if kind == "bool" and num is None:
        ok = bool(cap["bool_value"]) or not required
        return Check(
            req.metric,
            label,
            required,
            req.current_capacity,
            None,
            raw,
            "pass" if ok else "fail",
            None if ok else "not supported",
            unconfirmed=unconf,
        )
    if num is None:
        return Check(
            req.metric,
            label,
            required,
            req.current_capacity,
            None,
            raw,
            "unknown",
            f"unparsed value '{raw}'",
            unconfirmed=unconf,
        )

    reasons = []
    if num < required:
        reasons.append(f"needs {_fmt(required)}, has {_fmt(num)}")
    if req.current_capacity is not None and req.metric not in BASELINE_EXEMPT:
        if req.is_perf and num <= req.current_capacity:
            reasons.append(f"not more than current model ({_fmt(req.current_capacity)})")
        elif not req.is_perf and num < req.current_capacity:
            reasons.append(f"below current model ({_fmt(req.current_capacity)})")
    headroom = round(100 * (num - required) / num, 1) if num else None
    return Check(
        req.metric,
        label,
        required,
        req.current_capacity,
        num,
        raw,
        "fail" if reasons else "pass",
        "; ".join(reasons) or None,
        headroom,
        unconf,
    )


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:g}"


def fit_ports(needed: dict[str, int], available: dict[str, int]) -> tuple[bool, dict, list[str]]:
    """Greedy assignment, most demanding port classes first."""
    free = dict(available)
    plan: dict[str, dict[str, int]] = {}
    missing = []

    def speed_rank(cls: str) -> int:
        sp = cls.split("_")[0]
        return _SPEED_ORDER.index(sp) if sp in _SPEED_ORDER else -1

    for cls in sorted(needed, key=speed_rank, reverse=True):
        n = needed[cls]
        options = PORT_COMPAT.get(cls)
        if options is None:  # unknown media: any port at the same or a higher speed
            sp = speed_rank(cls)
            options = tuple(c for c in sorted(free, key=speed_rank) if speed_rank(c) >= sp)
        for opt in options:
            take = min(n, free.get(opt, 0))
            if take:
                plan.setdefault(cls, {})[opt] = take
                free[opt] -= take
                n -= take
            if not n:
                break
        if n:
            missing.append(f"{n}x {cls}")
    return not missing, {"assignment": plan, "spare": {k: v for k, v in free.items() if v}}, missing


# --------------------------------------------------------------------------- engine


def _poe_ports(conn, model: str, doc_id: int) -> int:
    row = conn.execute(
        """SELECT v.num_value FROM capacity_value v
           JOIN attribute a ON a.id = v.attribute_id JOIN model m ON m.id = v.model_id
           WHERE v.document_id=? AND m.name=? AND a.canonical_key=?""",
        (doc_id, model, "interfaces.poe_enabled_interfaces"),
    ).fetchone()
    return int(row[0]) if row and row[0] else 0


def _ha_dedicated(conn, model: str, doc_id: int) -> bool:
    row = conn.execute(
        """SELECT count(*) FROM capacity_value v
           JOIN attribute a ON a.id = v.attribute_id JOIN model m ON m.id = v.model_id
           WHERE v.document_id=? AND m.name=? AND v.bool_value=1 AND a.canonical_key IN
             ('interfaces.dedicated_ha_control_interface',
              'interfaces.dedicated_ha_data_interface')""",
        (doc_id, model),
    ).fetchone()
    return row[0] == 2


def size(
    conn: sqlite3.Connection,
    summary: TsfSummary,
    params: SizingParams | None = None,
    document_id: int | None = None,
) -> SizingResult:
    params = params or SizingParams()
    if document_id is None:
        doc = catalog.active_document(conn)
        if doc is None:
            raise LookupError("No active portfolio document; import the capacity workbook first")
        document_id = doc["id"]

    current = summary.model
    current_caps = catalog.mapped_capacities(conn, current, document_id) if current else {}
    in_portfolio = bool(current_caps) and any(c["kind"] for c in current_caps.values())
    result = SizingResult(
        params=asdict(params) | {"growth_factor": round(params.growth_factor, 3)},
        current_model=current,
        current_in_portfolio=in_portfolio,
        document_id=document_id,
    )
    if not in_portfolio:
        result.notes.append(
            f"{current or 'The current model'} is not in the portfolio sheet: "
            "only usage-based requirements are applied, not the "
            "'at least the current model' rule."
        )
        current_caps = None

    reqs, notes = build_requirements(summary, params, current_caps)
    result.requirements = reqs
    result.notes.extend(notes)

    # Ports
    current_ports = catalog.interface_ports(conn, current, document_id) if in_portfolio else {}
    traffic = {}
    ha = {}
    for p in summary.ports:
        bucket = traffic if p.role == "traffic" else ha if p.role == "ha" else None
        if bucket is not None:
            bucket[p.speed_class] = bucket.get(p.speed_class, 0) + 1
    traffic, n1 = resolve_port_media(traffic, current_ports)
    ha, n2 = resolve_port_media(ha, current_ports)
    result.notes.extend(n1 + n2)
    if params.port_rule == "all" and current_ports:
        base_ports = dict(current_ports)
        rule_text = f"the current {current}'s full port layout"
    else:
        base_ports = dict(traffic)
        rule_text = "the ports in use"
    result.ports_needed = {"rule": rule_text, "traffic": traffic, "ha": ha, "baseline": base_ports}

    features = {
        k: v.value
        for k, v in summary.metrics.items()
        if k.startswith("feature.") and v.value is True
    }
    poe_ports_used = (summary.poe or {}).get("ports_in_use", [])
    need_poe = params.need_poe or bool(poe_ports_used)
    result.need_poe = need_poe
    if poe_ports_used and not params.need_poe:
        result.notes.append(
            f"PoE is in use on the current firewall ({', '.join(poe_ports_used)}): only models "
            "with PoE ports are considered."
        )

    cur = conn.execute(
        "SELECT m.sheet_column, o.notes FROM model m LEFT JOIN model_override o "
        "ON o.model_name = m.name WHERE m.name = ?",
        (current,),
    ).fetchone()
    if cur and cur[0] == "supplement":
        result.notes.append(
            f"{current} is not in the current capacity workbook; its figures come from older, "
            "fixed sources (an older workbook and a vendor datasheet) and some may be missing. "
            "Anything the report shows as 'no data' for the current model is not checked: "
            "compare those limits by hand before quoting." + (f" {cur[1]}" if cur[1] else "")
        )
    superseded = catalog.superseded_families(conn)
    thr_req = next((r for r in reqs if r.metric.startswith("perf.throughput")), None)
    size_cap = None
    if params.max_size_factor and thr_req is not None:
        base = max(thr_req.required, thr_req.current_capacity or 0)
        if thr_req.current_capacity:
            size_cap = round(base * params.max_size_factor, 3)
        else:
            result.notes.append(
                "WARNING: the portfolio workbook has no throughput for the current model "
                f"({current}), so performance is not checked and the size cap is not applied. "
                "The import report on the Portfolio page lists the unresolved rows; fix the "
                "workbook or mapping and re-run before trusting this recommendation."
            )
    result.size_cap_gbps = size_cap

    candidates = []
    # The model list is global (every workbook ever imported adds to it): only models this
    # workbook version has data for can be compared, never "no data passes everything".
    with_data = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT m.name FROM capacity_value v JOIN model m ON m.id = v.model_id "
            "WHERE v.document_id = ?",
            (document_id,),
        )
    }
    for model in catalog.list_models(conn, quotable_only=not params.include_npi):
        if model["kind"] == "component" or model["name"] == current:
            continue
        if model["name"] not in with_data:
            continue
        if model["family"] in superseded and not params.include_superseded:
            result.excluded_families[model["family"]] = superseded[model["family"]]
            continue
        caps = catalog.mapped_capacities(conn, model["name"], document_id)
        if not caps or not any(c["kind"] for c in caps.values()):
            continue
        cand = Candidate(model["name"], model["family"], True)
        for req in reqs:
            chk = _check(req, caps.get(req.metric))
            cand.checks.append(chk)
            if chk.status == "fail":
                cand.failures.append(f"{chk.label}: {chk.reason}")
            if chk.unconfirmed and chk.status == "pass":
                cand.unconfirmed_used.append(chk.label)
        for fkey in features:
            cap = caps.get(fkey)
            if cap is None or cap.get("kind") in (None, "empty"):
                # Row missing from the sheet: unknown, not a reason to reject.
                cand.unknown_features.append(fkey.removeprefix("feature.").replace("_", " "))
                continue
            if not (cap.get("bool_value") == 1 or (cap.get("num_value") or 0) > 0):
                cand.failures.append(f"{cap['name']}: not supported")

        poe_ports = _poe_ports(conn, model["name"], document_id)
        if need_poe and not poe_ports:
            cand.failures.append("PoE: no PoE ports")
        elif not need_poe and "PoE" in variants(model["name"]):
            cand.failures.append("PoE model: PoE not needed (tick 'Customer needs PoE' to include)")
        cand.poe_ports = poe_ports

        # Ports: baseline + HA links when the candidate has no dedicated HA ports
        avail = catalog.interface_ports(conn, model["name"], document_id)
        need = dict(base_ports)
        dedicated = _ha_dedicated(conn, model["name"], document_id)
        if ha and not dedicated:
            if params.port_rule != "all":
                for k, v in ha.items():
                    need[k] = need.get(k, 0) + v
        ok, plan, missing = fit_ports(need, avail)
        cand.port_plan = plan | {"needed": need, "available": avail, "dedicated_ha": dedicated}
        if not ok:
            cand.failures.append("Ports: missing " + ", ".join(missing))
        cand.passes = not cand.failures
        heads = [
            (c.headroom_pct, c.label)
            for c in cand.checks
            if c.status == "pass" and c.headroom_pct is not None and c.required
        ]
        if heads:
            heads.sort()
            cand.min_headroom_pct = heads[0][0]
            cand.tightest = [f"{lbl} ({h:g}% headroom)" for h, lbl in heads[:3]]
        thr = next(
            (c.candidate_capacity for c in cand.checks if c.metric.startswith("perf.throughput")),
            None,
        )
        cand.sort_key = thr if thr is not None else float("inf")
        cand.extra_variants = sorted(
            variants(model["name"]) - variants(current or "") - ({"PoE"} if need_poe else set())
        )
        if size_cap is not None and thr is not None and thr > size_cap:
            cand.too_large = (
                f"{_fmt(thr)} Gbps is more than {params.max_size_factor:g}x the current "
                f"{current}'s {_fmt(thr_req.current_capacity)} Gbps (cap {_fmt(size_cap)} Gbps)"
            )
        candidates.append(cand)

    # Smallest passing model first; special-purpose variants (5G, rugged, PoE) only
    # rank ahead when the current box is one too.
    def order(c: Candidate):
        return (bool(c.extra_variants), c.sort_key, c.model)

    passing = sorted([c for c in candidates if c.passes and not c.too_large], key=order)
    oversized = sorted([c for c in candidates if c.passes and c.too_large], key=order)
    failing = sorted([c for c in candidates if not c.passes], key=lambda c: (c.sort_key, c.model))
    if passing:
        result.recommended = passing[0]
        result.alternatives = _alternatives(passing)
    elif oversized:
        # Nothing within the size cap qualifies: offer the smallest model that does.
        result.recommended = oversized.pop(0)
        result.notes.append(
            f"No model within the size cap qualifies; {result.recommended.model} is the smallest "
            "that meets every rule but is far larger than the current model."
        )
    else:
        result.notes.append("No quotable model meets every rule; see the rejected list.")
    for fam, successor in sorted(result.excluded_families.items()):
        result.notes.append(
            f"{fam} series left out: superseded by the {successor} series. Tick 'Include "
            "previous-generation models' to consider it."
        )
    result.rejected = failing
    result.too_large = oversized
    return result


def _alternatives(passing: list[Candidate]) -> list[Candidate]:
    """'Better' and 'Best': the next two steps up within the size cap. Special-purpose
    variants (PoE, 5G, rugged) only appear when no standard model is left."""
    rest = [c for c in passing[1:] if c.sort_key >= passing[0].sort_key]  # steps up only
    standard = [c for c in rest if not c.extra_variants]
    variant = [c for c in rest if c.extra_variants]
    return (standard or variant)[:2]
