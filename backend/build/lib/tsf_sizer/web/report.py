"""Turn an analysis result (JSON) into the view model the report template renders."""

from __future__ import annotations

PERF_LABELS = {
    "perf.throughput_snapshot_mbps": "Throughput at TSF time",
    "perf.throughput_avg_since_boot_mbps": "Average throughput since boot",
    "perf.cps_snapshot": "New connections per second (at TSF time)",
    "perf.sessions": "Concurrent sessions (peak estimate)",
}
PERIOD_LABELS = {
    "second": "Last 60 seconds",
    "minute": "Last 60 minutes",
    "hour": "Last 24 hours",
    "day": "Last 7 days",
    "week": "Last 13 weeks",
}
TIERS = ("Recommended", "Better", "Best")


def fmt_num(v, digits: int = 1) -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        if float(v).is_integer():
            return f"{int(v):,}"
        if abs(v) >= 100:
            return f"{v:,.0f}"
        return f"{v:,.{digits}f}".rstrip("0").rstrip(".") if abs(v) >= 1 else f"{v:.3g}"
    return str(v)


def register_filters(env) -> None:
    env.filters["num"] = fmt_num
    env.filters["pct"] = lambda v: "–" if v is None else f"{v:g}%"
    env.filters["clamp"] = lambda v: 0 if v is None else max(0.0, min(float(v), 100.0))


def _meter_level(pct: float | None, target: float) -> str:
    if pct is None:
        return "none"
    if pct > 100:
        return "over"
    if pct >= target:
        return "tight"
    return "ok"


def build(result: dict, row: dict) -> dict:
    s = result["summary"]
    sizing = result.get("sizing") or {}
    usage = result.get("usage") or {}
    params = sizing.get("params") or {}
    target = params.get("target_util_pct", 70)
    m = s["metrics"]

    def mv(key):
        return (m.get(key) or {}).get("value")

    ha = s.get("ha") or {}
    header = {
        "model": s.get("model"),
        "panos": s.get("panos"),
        "uptime": s.get("uptime_days"),
        "ha": (
            f"{ha.get('mode')} · {ha.get('local_state')} · HA1 {ha.get('ha1_interface')}, "
            f"HA2 {ha.get('ha2_interface')}"
        )
        if ha.get("enabled")
        else "Standalone",
        "panorama": s.get("panorama_managed"),
        "config_source": s.get("config_source"),
        "basis": s.get("sizing_basis", "").replace("_", " "),
    }

    traffic_ports = [p for p in s.get("ports", []) if p["role"] == "traffic"]
    ha_ports = [p for p in s.get("ports", []) if p["role"] == "ha"]
    kpis = [
        {
            "label": "Throughput at TSF time",
            "value": fmt_num(mv("perf.throughput_snapshot_mbps")),
            "unit": "Mbps",
            "sub": f"avg since boot {fmt_num(mv('perf.throughput_avg_since_boot_mbps'))} Mbps",
        },
        {
            "label": "Sessions (peak est.)",
            "value": fmt_num(mv("perf.sessions")),
            "unit": "",
            "sub": f"now {fmt_num(mv('perf.sessions_snapshot'))}",
        },
        {
            "label": "Peak dataplane CPU",
            "value": fmt_num(mv("perf.dp_cpu_peak_pct")),
            "unit": "%",
            "sub": (m.get("perf.dp_cpu_peak_pct") or {}).get("note") or "",
        },
        {
            "label": "Security rules",
            "value": fmt_num(mv("config.security_rules")),
            "unit": "",
            "sub": f"NAT {fmt_num(mv('config.nat_rules'))}",
        },
        {
            "label": "Address objects",
            "value": fmt_num(mv("config.address_objects")),
            "unit": "",
            "sub": f"groups {fmt_num(mv('config.address_groups'))}",
        },
        {
            "label": "Ports in use",
            "value": str(len(traffic_ports) + len(ha_ports)),
            "unit": "",
            "sub": f"{len(traffic_ports)} traffic · {len(ha_ports)} HA",
        },
    ]

    # Recommendation cards
    cards = []
    picks = ([sizing["recommended"]] if sizing.get("recommended") else []) + sizing.get(
        "alternatives", []
    )
    current_caps = {r["metric"]: r.get("current_capacity") for r in sizing.get("requirements", [])}
    spec_defs = (
        ("perf.throughput_threat_gbps", "Threat throughput", "Gbps"),
        ("perf.throughput_appid_gbps", "App-ID throughput", "Gbps"),
        ("perf.sessions", "Sessions", ""),
        ("perf.cps", "Connections/s", ""),
    )
    # Common scale per spec across all cards, so bars compare between models too.
    scale: dict[str, float] = {}
    for cand in picks:
        for c in cand["checks"]:
            if c.get("candidate_capacity") is not None:
                scale[c["metric"]] = max(
                    scale.get(c["metric"], 0),
                    c["candidate_capacity"],
                    current_caps.get(c["metric"]) or 0,
                )
    for tier, cand in zip(TIERS, picks, strict=False):
        checks = {c["metric"]: c for c in cand["checks"]}
        specs = []
        for key, label, unit in spec_defs:
            c = checks.get(key)
            if not c or c.get("candidate_capacity") is None:
                continue
            top = scale.get(key) or 1
            cur = current_caps.get(key)
            specs.append(
                {
                    "label": label,
                    "value": fmt_num(c["candidate_capacity"]),
                    "unit": unit,
                    "headroom": c.get("headroom_pct"),
                    "pct": round(100 * c["candidate_capacity"] / top, 1),
                    "current_pct": round(100 * cur / top, 1) if cur else None,
                    "current": fmt_num(cur) if cur else None,
                    "times": round(c["candidate_capacity"] / cur, 1) if cur else None,
                }
            )
        plan = cand.get("port_plan", {})
        avail = plan.get("available", {})
        cards.append(
            {
                "tier": tier,
                "model": cand["model"],
                "family": cand.get("family"),
                "specs": specs,
                "tightest": cand.get("tightest", []),
                "unconfirmed": cand.get("unconfirmed_used", []),
                "ports": ", ".join(f"{n}× {c}" for c, n in avail.items()) or "–",
                "dedicated_ha": plan.get("dedicated_ha"),
                "variants": cand.get("extra_variants", []),
                "unknown_features": cand.get("unknown_features", []),
                "too_large": cand.get("too_large"),
                "poe_ports": cand.get("poe_ports", 0),
            }
        )

    # Requirements vs current model vs recommended model
    rec = sizing.get("recommended") or {}
    rec_checks = {c["metric"]: c for c in rec.get("checks", [])}
    req_rows = []
    for r in sizing.get("requirements", []):
        cur = r.get("current_capacity")
        util = round(100 * r["observed"] / cur, 1) if cur else None
        chk = rec_checks.get(r["metric"], {})
        interesting = bool(r["observed"]) or r.get("is_perf")
        req_rows.append(
            {
                "label": r["label"],
                "unit": r.get("unit") or "",
                "observed": r["observed"],
                "basis": r.get("observed_basis"),
                "required": r["required"],
                "current": cur,
                "util": util,
                "level": _meter_level(util, target),
                "rec": chk.get("candidate_capacity")
                if chk.get("candidate_capacity") is not None
                else chk.get("candidate_raw"),
                "headroom": chk.get("headroom_pct"),
                "status": chk.get("status"),
                "unconfirmed": chk.get("unconfirmed"),
                "interesting": interesting,
            }
        )
    req_rows.sort(key=lambda x: (not x["interesting"], -(x["util"] or 0)))

    # Ports whose media the TSF didn't show: display the type the engine assumed.
    current_ports = usage.get("ports_available") or {}
    ports = []
    for p in s.get("ports", []):
        p = dict(p)
        cls = p["speed_class"]
        if "unknown" in cls:
            speed = cls.split("_")[0] if cls != "unknown" else None
            options = [c for c in current_ports if speed is None or c.startswith(speed + "_")]
            if len(options) == 1:
                p["speed_class"] = options[0]
                p["assumed"] = True
        ports.append(p)

    rejected = [
        {
            "model": c["model"],
            "family": c.get("family"),
            "reasons": ["Too large: " + c["too_large"]],
        }
        for c in sizing.get("too_large", [])
    ] + [
        {"model": c["model"], "family": c.get("family"), "reasons": c["failures"]}
        for c in sizing.get("rejected", [])
    ]

    history = []
    for period in ("second", "minute", "hour", "day", "week"):
        h = (s.get("resource_history") or {}).get(period)
        if h:
            history.append({"period": PERIOD_LABELS[period], **h})

    warnings = []
    for w in s.get("warnings", []) + usage.get("warnings", []) + sizing.get("notes", []):
        if w not in warnings:
            warnings.append(w)
    if result.get("portfolio_error"):
        warnings.insert(0, result["portfolio_error"])

    writeup = result.get("writeup")
    if writeup and writeup.get("text"):
        from ..llm.writeup import render_markdown

        writeup = dict(writeup, html=render_markdown(writeup["text"]))

    return {
        "writeup": writeup,
        "header": header,
        "kpis": kpis,
        "cards": cards,
        "req_rows": req_rows,
        "rejected": rejected,
        "size_cap": sizing.get("size_cap_gbps"),
        "need_poe": sizing.get("need_poe"),
        "poe": s.get("poe") or {},
        "excluded_families": sizing.get("excluded_families") or {},
        "history": history,
        "warnings": warnings,
        "ports": ports,
        "params": params,
        "target": target,
        "port_rule": (sizing.get("ports_needed") or {}).get("rule"),
        "rec_plan": (rec.get("port_plan") or {}).get("assignment", {}),
        "licenses_active": s.get("licenses_active", []),
        "licenses_expired": s.get("licenses_expired", []),
        "portfolio": result.get("portfolio") or {},
        "files_used": result.get("files_used", {}),
        "generated_at": result.get("generated_at"),
        "customer": row.get("customer"),
    }
