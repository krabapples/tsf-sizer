"""Turn parsed TSF facts into sizing metrics.

Metric keys match `tsf_sizer.portfolio.mapping.TSF_METRIC_MAP`, so every metric
can be compared with the capacity row it maps to. Each metric records where it
came from and whether it is a peak, a snapshot or an estimate.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .config import ConfigCounts
from .techsupport import TechSupportFacts

# Config counts that map directly to TSF metric keys ("config.<name>").
CONFIG_METRICS = (
    "address_objects",
    "address_groups",
    "max_address_group_members",
    "service_objects",
    "service_groups",
    "fqdn_objects",
    "edl_lists",
    "security_profiles",
    "custom_apps",
    "custom_url_categories",
    "url_list_entries",
    "zones",
    "virtual_routers",
    "vwires",
    "vsys",
    "shared_gateways",
    "logical_interfaces",
    "tunnel_interfaces",
    "aggregate_interfaces",
    "max_aggregate_members",
    "routing_peers",
    "dhcp_relay_interfaces",
    "ipsec_tunnels",
    "ike_gateways",
    "gp_gateways",
    "schedules",
    "security_rules",
    "nat_rules",
    "nat_rules_static",
    "nat_rules_dip",
    "nat_rules_dipp",
    "decryption_rules",
    "app_override_rules",
    "auth_rules",
    "dos_rules",
    "pbf_rules",
    "qos_rules",
    "tunnel_inspection_rules",
    "sdwan_rules",
)

SNAPSHOT = "snapshot"  # value at the moment the TSF was taken
PEAK = "peak"  # highest value in the available history
AVERAGE = "average"
COUNT = "count"  # configured / current count
ESTIMATE = "estimate"


@dataclass
class Metric:
    value: float | int | bool | None
    kind: str
    source: str
    unit: str | None = None
    note: str | None = None


@dataclass
class PortUse:
    name: str
    speed_class: str
    role: str  # traffic | ha | unused
    zone: str | None
    runtime_speed: str | None
    state: str | None


@dataclass
class TsfSummary:
    model: str | None
    family: str | None
    panos: str | None
    uptime_days: float | None
    ha: dict
    licenses_active: list[str]
    licenses_expired: list[str]
    sizing_basis: str
    poe: dict = field(default_factory=dict)
    config_source: str | None = None
    panorama_managed: bool | None = None
    metrics: dict[str, Metric] = field(default_factory=dict)
    ports: list[PortUse] = field(default_factory=list)
    port_counts: dict[str, int] = field(default_factory=dict)
    resource_history: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TsfSummary:
        """Inverse of to_dict(): lets a stored analysis be sized again without the TSF."""
        d = dict(d)
        d["metrics"] = {k: Metric(**v) for k, v in (d.get("metrics") or {}).items()}
        d["ports"] = [PortUse(**p) for p in d.get("ports") or []]
        return cls(**d)


# Capability strings / port types -> speed classes used in the capacity sheet.
def speed_class(port_type: str | None, capability: str | None, runtime_speed: str | None) -> str:
    cap = (capability or "").lower()
    pt = (port_type or "").upper()
    top = 0
    for gb, token in (
        (400, "400gb"),
        (100, "100gb"),
        (40, "40gb"),
        (25, "25gb"),
        (10, "10gb"),
        (5, "5gb"),
        (2.5, "2.5gb"),
        (1, "1gb"),
    ):
        if token in cap:
            top = gb
            break
    if not top and runtime_speed and runtime_speed.isdigit():
        top = int(runtime_speed) / 1000
    if pt.startswith("RJ45") or pt == "COPPER":
        return {10: "10G_RJ45", 5: "5G_RJ45", 2.5: "2.5G_RJ45"}.get(top, "1G_RJ45")
    if "QSFP-DD" in pt or top == 400:
        return "400G_QSFP-DD"
    if "QSFP" in pt or top in (40, 100):
        return "100G_QSFP28" if top == 100 else "40G_QSFP+"
    if "SFP28" in pt or top == 25:
        return "25G_SFP28"
    if "SFP" in pt:
        return "10G_SFP+" if top >= 10 else "1G_SFP"
    # Media unknown (no per-port detail in the TSF): keep the speed, resolve the
    # media later from the model's port list in the capacity sheet.
    if top:
        return f"{top:g}G_unknown-media"
    return "unknown"


def _m(metrics, key, value, kind, source, unit=None, note=None):
    if value is not None:
        metrics[key] = Metric(value, kind, source, unit, note)


def summarize(
    f: TechSupportFacts, config: ConfigCounts | None = None, config_source: str | None = None
) -> TsfSummary:
    sysinfo = f.system
    uptime_s = sysinfo.get("uptime_seconds")
    lic_active = sorted(x["feature"] for x in f.licenses if not x["expired"])
    lic_expired = sorted(x["feature"] for x in f.licenses if x["expired"])
    threat = any("threat prevention" in x.lower() for x in lic_active)

    s = TsfSummary(
        model=sysinfo.get("model"),
        family=sysinfo.get("family"),
        panos=sysinfo.get("sw-version"),
        uptime_days=round(uptime_s / 86400, 1) if uptime_s else None,
        ha=f.ha,
        licenses_active=lic_active,
        licenses_expired=lic_expired,
        sizing_basis="threat_prevention" if threat else "app_id",
    )
    s.poe = f.poe
    m = s.metrics

    # ---- performance
    sess = f.sessions
    rm = f.resource_monitor.get("summary", {})
    _m(
        m,
        "perf.throughput_snapshot_mbps",
        round(sess["throughput_kbps"] / 1000, 1)
        if sess.get("throughput_kbps") is not None
        else None,
        SNAPSHOT,
        "show session info",
        "Mbps",
    )
    _m(m, "perf.cps_snapshot", sess.get("cps"), SNAPSHOT, "show session info", "cps")
    _m(
        m,
        "perf.packet_rate_snapshot",
        sess.get("packet_rate_pps"),
        SNAPSHOT,
        "show session info",
        "pps",
    )
    _m(
        m,
        "perf.sessions_snapshot",
        sess.get("sessions_allocated"),
        SNAPSHOT,
        "show session info",
        "sessions",
    )

    peaks = [
        v["session_util_peak_pct"]
        for v in rm.values()
        if v.get("session_util_peak_pct") is not None
    ]
    if peaks and sess.get("sessions_supported"):
        pct = max(peaks)
        est = round(sess["sessions_supported"] * pct / 100)
        _m(
            m,
            "perf.sessions",
            max(est, sess.get("sessions_allocated") or 0),
            ESTIMATE,
            "show running resource-monitor",
            "sessions",
            f"session table peaked at {pct}% of {sess['sessions_supported']} "
            "(whole percents, so this is an upper estimate)",
        )
    cpu_peaks = {
        p: v["dp_cpu_peak_pct"] for p, v in rm.items() if v.get("dp_cpu_peak_pct") is not None
    }
    if cpu_peaks:
        best = max(cpu_peaks.values())
        period = max(cpu_peaks, key=lambda p: cpu_peaks[p])
        _m(
            m,
            "perf.dp_cpu_peak_pct",
            best,
            PEAK,
            "show running resource-monitor",
            "%",
            f"highest per-core maximum, period: {period}",
        )

    # Average throughput since boot from traffic-port byte counters.
    traffic_ports = [p for p in _ports(f) if p.role == "traffic"]
    rx = sum(
        f.interfaces.get("details", {}).get(p.name, {}).get("rx-bytes", 0) for p in traffic_ports
    )
    if rx and uptime_s:
        _m(
            m,
            "perf.throughput_avg_since_boot_mbps",
            round(rx * 8 / uptime_s / 1e6, 1),
            AVERAGE,
            "show interface <port> (rx-bytes) / uptime",
            "Mbps",
            "average over the whole uptime, not a peak",
        )

    # ---- policy and config counts
    for key, val in f.policies.items():
        _m(m, f"config.{key}", val, COUNT, "show running *-policy")
    nat = f.nat_types
    if f.policies.get("nat_rules") is not None:
        _m(m, "config.nat_rules_static", nat.get("static"), COUNT, "show running nat-policy")
        _m(m, "config.nat_rules_dip", nat.get("dip"), COUNT, "show running nat-policy")
        _m(m, "config.nat_rules_dipp", nat.get("dipp"), COUNT, "show running nat-policy")

    net = f.network
    _m(m, "config.ipsec_tunnels", net.get("ipsec_tunnels"), COUNT, "show vpn tunnel")
    _m(m, "config.ike_gateways", net.get("ike_gateways"), COUNT, "show vpn gateway")
    _m(m, "state.routes_v4", net.get("routes_v4"), COUNT, "show advanced-routing resource")
    _m(m, "state.routes_v6", net.get("routes_v6"), COUNT, "show advanced-routing resource")
    _m(
        m,
        "state.gp_users",
        net.get("gp_current_users"),
        SNAPSHOT,
        "show global-protect-gateway statistics",
    )
    _m(
        m,
        "state.dag_ips",
        net.get("registered_ips"),
        COUNT,
        "show object registered-ip all option count",
    )

    logical = f.interfaces.get("logical", [])
    zones = {i["zone"] for i in logical if i.get("zone")}
    _m(
        m,
        "config.zones",
        len(zones) if logical else None,
        COUNT,
        "show interface all",
        note="zones bound to interfaces only; the config file gives the full count",
    )
    _m(
        m,
        "config.logical_interfaces",
        len(logical) if logical else None,
        COUNT,
        "show interface all",
    )
    _m(
        m,
        "config.tunnel_interfaces",
        sum(1 for i in logical if re.match(r"tunnel\.\d+", i["name"])) if logical else None,
        COUNT,
        "show interface all",
    )
    _m(
        m,
        "config.aggregate_interfaces",
        f.interfaces.get("aggregate_groups"),
        COUNT,
        "show interface all",
    )
    if sysinfo.get("multi-vsys"):
        _m(
            m,
            "config.vsys",
            1 if sysinfo["multi-vsys"] == "off" else None,
            COUNT,
            "show system info",
            None if sysinfo["multi-vsys"] == "off" else "multi-vsys on: count from config",
        )

    # ---- config XML: complete object counts; replaces counts derived from CLI output
    if config is not None:
        s.config_source = config_source or "config XML"
        s.panorama_managed = config.panorama_managed
        src = s.config_source
        for name in CONFIG_METRICS:
            if name not in config.counts:
                continue
            key = f"config.{name}"
            prev = m.get(key)
            note = None
            if prev is not None and name.endswith("_rules") and prev.value != config.counts[name]:
                note = f"running policy shows {prev.value}"
            m[key] = Metric(config.counts[name], COUNT, src, note=note)
        for kind in ("ip", "domain", "url"):
            if config.counts.get(f"edl_lists_{kind}"):
                _m(m, f"config.edl_lists_{kind}", config.counts[f"edl_lists_{kind}"], COUNT, src)

    # ---- features
    if f.ha.get("enabled"):
        mode = (f.ha.get("mode") or "").lower()
        _m(m, "feature.ha_active_passive", "passive" in mode, COUNT, "show high-availability all")
        _m(
            m,
            "feature.ha_active_active",
            "active-active" in mode,
            COUNT,
            "show high-availability all",
        )
    _m(m, "feature.gtp", bool(sess.get("sessions_gtpu")), SNAPSHOT, "show session info")
    _m(m, "feature.sctp", bool(sess.get("sessions_sctp")), SNAPSHOT, "show session info")
    if f.poe.get("supported") is not None:
        _m(
            m,
            "state.poe_ports_in_use",
            len(f.poe.get("ports_in_use", [])),
            SNAPSHOT,
            "show poe detail",
        )
    _m(
        m,
        "feature.lre_routing",
        sysinfo.get("advanced-routing") == "off",
        COUNT,
        "show system info",
    )

    # ---- ports
    s.ports = _ports(f)
    counts: dict[str, int] = {}
    for p in s.ports:
        if p.role in ("traffic", "ha"):
            counts[p.speed_class] = counts.get(p.speed_class, 0) + 1
    s.port_counts = counts
    s.resource_history = rm

    s.warnings = _warnings(f, s, uptime_s)
    return s


def _ports(f: TechSupportFacts) -> list[PortUse]:
    logical = {i["name"]: i for i in f.interfaces.get("logical", [])}
    details = f.interfaces.get("details", {})
    ha_ifaces = {
        f.ha.get("ha1_interface"),
        f.ha.get("ha1_backup_interface"),
        f.ha.get("ha2_interface"),
    }
    out = []
    for hw in f.interfaces.get("hardware", []):
        name = hw["name"]
        if not name.startswith("ethernet"):
            continue
        lg = logical.get(name, {})
        subifs = [n for n in logical if n.startswith(name + ".")]
        if lg.get("forwarding") == "ha" or name in ha_ifaces:
            role = "ha"
        elif lg.get("zone") or lg.get("has_address") or subifs or lg.get("forwarding"):
            role = "traffic"
        else:
            role = "unused"
        d = details.get(name, {})
        out.append(
            PortUse(
                name,
                speed_class(d.get("port_type"), d.get("capability"), hw.get("speed")),
                role,
                lg.get("zone"),
                hw.get("speed"),
                hw.get("state"),
            )
        )
    return out


def _warnings(f: TechSupportFacts, s: TsfSummary, uptime_s: int | None) -> list[str]:
    w = []
    if uptime_s is not None and uptime_s < 30 * 86400:
        w.append(
            f"Uptime is only {s.uptime_days} days: CPU/session history before the last "
            "reboot is lost, so peaks may be understated."
        )
    week = f.resource_monitor.get("summary", {}).get("week", {})
    if week and week.get("samples_with_data", 0) < 4:
        w.append(
            f"Only {week.get('samples_with_data', 0)} week(s) of resource history "
            "available (13 possible)."
        )
    w.append(
        "Throughput, CPS and packet rate are snapshots at TSF time"
        + (f" ({f.system['time']})" if f.system.get("time") else "")
        + "; enter the known peak if the TSF was taken off-peak."
    )
    if s.licenses_expired:
        w.append("Expired licenses: " + ", ".join(s.licenses_expired))
    if any(v for v in f.counters_of_interest.values()):
        w.append(
            "Resource-pressure counters are non-zero: "
            + ", ".join(f"{k}={v}" for k, v in f.counters_of_interest.items() if v)
        )
    if f.policies.get("decryption_rules"):
        w.append(
            "Decryption rules are configured: size on decryption throughput, or confirm "
            "with the official sizing tool."
        )
    if f.ha.get("enabled") and any(p.role == "ha" for p in s.ports):
        w.append(
            "HA links use data ports on this model; a target with dedicated HA ports frees them up."
        )
    if f.poe.get("supported") and not f.poe.get("parsed"):
        w.append(
            "The firewall supports PoE but the PoE status could not be read from the TSF: "
            "tick 'Customer needs PoE' if PoE devices are connected."
        )
    if f.missing_commands:
        w.append("Missing from techsupport file: " + ", ".join(f.missing_commands))
    src = s.config_source or ""
    if not src:
        w.append(
            "Object counts (addresses, services, groups, FQDN, EDL) need the config XML from "
            "the TSF; they are not in the techsupport text."
        )
    elif s.panorama_managed and "merged" not in src:
        w.append(
            "Panorama-managed firewall but the merged running config was not used: pushed "
            "objects and rules may be missing. Use .merged-running-config.xml."
        )
    return w
