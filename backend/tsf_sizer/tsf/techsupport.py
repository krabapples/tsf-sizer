"""Parse the CLI output file inside a TSF (tmp/cli/techsupport_<model>_<date>.txt).

The file is a sequence of CLI commands, each echoed as "> <command>" at the start
of a line and followed by its output. Only commands relevant for sizing are
parsed; identifiers (hostname, serial, IP and MAC addresses, admin names) are
never copied into the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CMD = re.compile(r"^> ?(.*?)\s*$")
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class Section:
    command: str
    body: str
    line: int


def split_sections(text: str) -> list[Section]:
    sections: list[Section] = []
    cmd: str | None = None
    start = 0
    buf: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = _CMD.match(line)
        if m:
            if cmd is not None:
                sections.append(Section(cmd, "\n".join(buf), start))
            cmd, start, buf = m.group(1), i, []
        elif cmd is not None:
            buf.append(line)
    if cmd is not None:
        sections.append(Section(cmd, "\n".join(buf), start))
    return sections


class TechSupport:
    """Lookup of command output by command name (first match, whitespace-insensitive)."""

    def __init__(self, text: str):
        self.sections = split_sections(text)
        self._by_cmd: dict[str, Section] = {}
        for s in self.sections:
            self._by_cmd.setdefault(" ".join(s.command.split()), s)

    def get(self, command: str) -> str | None:
        s = self._by_cmd.get(" ".join(command.split()))
        if s is None or "Server error" in s.body[:200] or "Command deprecated" in s.body[:200]:
            return None
        return s.body


# --------------------------------------------------------------------------- helpers


def _kv(body: str, sep: str = ":") -> dict[str, str]:
    out: dict[str, str] = {}
    for line in body.splitlines():
        if sep in line:
            k, v = line.split(sep, 1)
            k = k.strip()
            if k and k not in out:
                out[k] = v.strip()
    return out


def _int(s: str | None) -> int | None:
    if s is None:
        return None
    m = _NUM.search(s.replace(",", ""))
    return int(float(m.group())) if m else None


def _uptime_seconds(s: str | None) -> int | None:
    if not s:
        return None
    m = re.match(r"(?:(\d+)\s+days?,\s*)?(\d+):(\d+):(\d+)", s.strip())
    if not m:
        return None
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + sec


# --------------------------------------------------------------------------- parsers

SYSTEM_FIELDS = (
    "model",
    "family",
    "system-mode",
    "sw-version",
    "uptime",
    "multi-vsys",
    "advanced-routing",
    "operational-mode",
    "cloud-mode",
    "app-version",
    "threat-version",
    "time",
)


def parse_system_info(body: str) -> dict:
    kv = _kv(body)
    info = {k: kv[k] for k in SYSTEM_FIELDS if k in kv}
    info["uptime_seconds"] = _uptime_seconds(kv.get("uptime"))
    return info


_PANORAMA_MODEL = re.compile(r"^(panorama|m-\d+)", re.I)
_PANORAMA_FILE = re.compile(r"techsupport_(panorama|m-\d+)", re.I)
_PANORAMA_MODES = {"panorama", "logger", "management-only", "panorama-only"}


def panorama_reason(system: dict, filename: str | None = None) -> str | None:
    """Why this TSF comes from Panorama (M-series appliance or Panorama VM), else None.

    Only `show system info` fields and the techsupport file name are used: a firewall's TSF
    mentions Panorama in its config too (it is managed by one), which says nothing here.
    """
    model = str(system.get("model") or "").strip()
    mode = str(system.get("system-mode") or "").strip().lower()
    if model and _PANORAMA_MODEL.match(model):
        return f"model {model}"
    if mode in _PANORAMA_MODES:
        return f"system mode {mode}"
    if not model and filename and _PANORAMA_FILE.search(filename):
        return f"file name {filename}"
    return None


def parse_session_info(body: str) -> dict:
    kv = _kv(body)

    def g(label: str) -> int | None:
        return _int(kv.get(label))

    return {
        "sessions_supported": g("Number of sessions supported"),
        "sessions_allocated": g("Number of allocated sessions"),
        "sessions_tcp": g("Number of active TCP sessions"),
        "sessions_udp": g("Number of active UDP sessions"),
        "sessions_gtpu": g("Number of active GTPu sessions"),
        "sessions_sctp": g("Number of active SCTP sessions"),
        "session_table_util_pct": g("Session table utilization"),
        "sessions_since_boot": g("Number of sessions created since bootup"),
        "packet_rate_pps": g("Packet rate"),
        "throughput_kbps": g("Throughput"),
        "cps": g("New connection establish rate"),
    }


_RM_PERIODS = {
    "last 60 seconds": "second",
    "last 60 minutes": "minute",
    "last 24 hours": "hour",
    "last 7 days": "day",
    "last 13 weeks": "week",
}


def _cell(tok: str) -> int | None:
    return None if tok == "*" else int(tok)


def parse_resource_monitor(body: str) -> dict:
    """Parse `show running resource-monitor` into per-period CPU and utilization series.

    CPU rows: per-second blocks list one value per core; longer periods list avg/max
    pairs per core. '*' means no data (core 0 is the management core on small
    platforms, and history is lost on reboot).
    """
    result: dict[str, dict] = {}
    lines = body.splitlines()
    i = 0
    period = None
    metric = None
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r"CPU load \(%\) during (last \d+ \w+):", line)
        if m:
            period = _RM_PERIODS.get(m.group(1))
            result.setdefault(period, {"cpu": [], "util": {}})
            i += 1
            # skip header rows ("core 0 1 2", "avg max ...")
            while i < len(lines) and re.match(r"^\s*(core|avg)\b", lines[i]):
                i += 1
            while i < len(lines) and re.match(r"^\s*([*\d]+\s+)*[*\d]+\s*$", lines[i]):
                result[period]["cpu"].append([_cell(t) for t in lines[i].split()])
                i += 1
            continue
        m = re.match(r"Resource utilization \(%\) during (last \d+ \w+):", line)
        if m:
            period = _RM_PERIODS.get(m.group(1))
            result.setdefault(period, {"cpu": [], "util": {}})
            metric = None
            i += 1
            continue
        m = re.match(r"^([a-z][a-z ]+?)(?: \((average|maximum)\))?:$", line)
        if m and period is not None and not line.startswith("CPU"):
            name = m.group(1).strip().replace(" ", "_")
            metric = name + (f"_{m.group(2)[:3]}" if m.group(2) else "")
            result[period]["util"].setdefault(metric, [])
            i += 1
            continue
        if metric and period and re.match(r"^\s*([*\d]+\s+)*[*\d]+\s*$", lines[i]):
            result[period]["util"][metric].extend(_cell(t) for t in lines[i].split())
        i += 1
    return result


def summarize_resource_monitor(rm: dict) -> dict:
    """Peak and average dataplane CPU / session-table use per period."""
    out = {}
    for period, data in rm.items():
        rows = data["cpu"]
        if not rows:
            continue
        if period == "second":
            vals = [v for r in rows for v in r if v is not None]
            peak, avg, samples = (max(vals) if vals else None), None, len(rows)
        else:
            maxes = [v for r in rows for v in r[1::2] if v is not None]
            avgs = [v for r in rows for v in r[0::2] if v is not None]
            samples = sum(1 for r in rows if any(v is not None for v in r))
            peak = max(maxes) if maxes else None
            avg = round(sum(avgs) / len(avgs), 1) if avgs else None
        util = data["util"]
        sess = [v for v in util.get("session_max", util.get("session", [])) if v is not None]
        pbuf = [
            v for v in util.get("packet_buffer_max", util.get("packet_buffer", [])) if v is not None
        ]
        out[period] = {
            "dp_cpu_peak_pct": peak,
            "dp_cpu_avg_pct": avg,
            "samples_with_data": samples,
            "session_util_peak_pct": max(sess) if sess else None,
            "packet_buffer_peak_pct": max(pbuf) if pbuf else None,
        }
    return out


def parse_interface_all(body: str) -> dict:
    hw, logical = [], []
    section = None
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("total configured hardware interfaces"):
            section = "hw"
            continue
        if s.startswith("total configured logical interfaces"):
            section = "logical"
            continue
        if not s or s.startswith(("name", "---", "aggregation groups")):
            continue
        parts = s.split()
        if section == "hw" and len(parts) >= 3:
            speed, duplex, state = (parts[2].split("/") + ["", "", ""])[:3]
            hw.append(
                {"name": parts[0], "speed": speed, "duplex": duplex, "state": state.split("(")[0]}
            )
        elif section == "logical" and len(parts) >= 4:
            # name id vsys [zone] forwarding tag address
            name, _id, vsys = parts[0], parts[1], parts[2]
            rest = parts[3:]
            fwd_idx = next(
                (
                    j
                    for j, p in enumerate(rest)
                    if p in ("ha", "N/A") or p.startswith(("lr:", "vr:", "vw:", "vlan:"))
                ),
                None,
            )
            zone = " ".join(rest[:fwd_idx]) if fwd_idx else None
            forwarding = rest[fwd_idx] if fwd_idx is not None else None
            address = rest[-1] if rest else None
            logical.append(
                {
                    "name": name,
                    "vsys": vsys,
                    "zone": zone or None,
                    "forwarding": forwarding,
                    "has_address": bool(address) and address != "N/A" and forwarding != "N/A",
                }
            )
    m = re.search(r"aggregation groups:\s*(\d+)", body)
    return {"hardware": hw, "logical": logical, "aggregate_groups": int(m.group(1)) if m else 0}


def parse_interface_detail(body: str) -> dict:
    kv = _kv(body)
    port_type = None
    m = re.search(r"Port Type:\s*(\S+)", body)
    if m:
        port_type = m.group(1)
    counters = {}
    for key in ("rx-bytes", "tx-bytes"):
        m = re.search(rf"^{key}\s+(\d+)", body, re.M)
        if m:
            counters[key] = int(m.group(1))
    return {
        "port_type": port_type,
        "capability": kv.get("Capability"),
        "mode": kv.get("Operation mode"),
        **counters,
    }


def parse_ha(body: str) -> dict:
    if not body.strip() or "HA not enabled" in body:
        return {"enabled": False}
    mode = re.search(r"^\s*Mode:\s*(.+)$", body, re.M)
    state = re.search(r"Local Information:.*?State:\s*(\S+)", body, re.S)
    ha1 = re.search(r"HA1 Control Link Information:.*?Interface:\s*(\S+)", body, re.S)
    ha1b = re.search(r"HA1 Backup.*?Interface:\s*(\S+)", body, re.S)
    ha2 = re.search(r"HA2 Data Link Information:.*?Interface:\s*(\S+)", body, re.S)
    peer = re.search(r"Peer Information:.*?Model:\s*(\S+)", body, re.S)
    return {
        "enabled": True,
        "mode": mode.group(1).strip() if mode else None,
        "local_state": state.group(1) if state else None,
        "ha1_interface": ha1.group(1) if ha1 else None,
        "ha1_backup_interface": ha1b.group(1) if ha1b else None,
        "ha2_interface": ha2.group(1) if ha2 else None,
        "peer_model": peer.group(1) if peer else None,
    }


def parse_licenses(body: str) -> list[dict]:
    out = []
    for block in body.split("License entry:")[1:]:
        kv = _kv(block)
        if "Feature" in kv:
            out.append(
                {
                    "feature": kv["Feature"],
                    "expires": kv.get("Expires"),
                    "expired": kv.get("Expired?", "").lower() == "yes",
                }
            )
    return out


_POE_PORT = re.compile(r"^\s*(ethernet\d+/\d+)\b(.*)$", re.I | re.M)
_POE_ON = re.compile(r"\b(delivering|powered|power[- ]?on|on|yes|enabled)\b", re.I)
_POE_WATTS = re.compile(r"(\d+(?:\.\d+)?)\s*(m?W)\b")


def parse_poe(body: str | None) -> dict:
    """PoE status from `show poe detail`.

    Returns supported (True/False/None when unknown) and the ports that look like
    they deliver power (enabled and/or drawing more than 0 W).
    """
    if body is None:
        return {"supported": None, "ports_in_use": [], "parsed": False}
    if re.search(r"does not support poe|poe (is )?not supported", body, re.I):
        return {"supported": False, "ports_in_use": [], "parsed": True}
    in_use = []
    lines = _POE_PORT.findall(body)
    for name, rest in lines:
        watts = [float(v) / (1000 if u.lower() == "mw" else 1) for v, u in _POE_WATTS.findall(rest)]
        if (watts and max(watts) > 0) or (not watts and _POE_ON.search(rest)):
            in_use.append(name)
    return {"supported": True, "ports_in_use": sorted(set(in_use)), "parsed": bool(lines)}


_RULE = re.compile(r'^"(.+); index: \d+" \{', re.M)


_DEFAULT_RULE = re.compile(r"(^|\+)(intrazone|interzone)-default$")


def count_rules(body: str | None) -> int | None:
    """Count configured rules; the built-in intrazone/interzone defaults don't count."""
    if body is None:
        return None
    return sum(1 for name in _RULE.findall(body) if not _DEFAULT_RULE.search(name))


def nat_rule_types(body: str | None) -> dict[str, int]:
    out = {"static": 0, "dip": 0, "dipp": 0, "other": 0}
    if not body:
        return out
    for rule in re.split(r'^(?=")', body, flags=re.M):
        if not _RULE.match(rule):
            continue
        t = re.search(r"translate-to\s+\"(.*)\";", rule)
        txt = t.group(1) if t else ""
        if "dynamic-ip-and-port" in txt:
            out["dipp"] += 1
        elif "dynamic-ip" in txt:
            out["dip"] += 1
        elif "static" in txt:
            out["static"] += 1
        else:
            out["other"] += 1
    return out


def parse_counters(body: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in body.splitlines():
        parts = line.split()
        if len(parts) >= 3 and re.fullmatch(r"[a-z0-9_]+", parts[0]) and parts[1].isdigit():
            out.setdefault(parts[0], int(parts[1]))
    return out


# Counters that indicate the box ran out of a resource (sessions, memory, buffers).
_PRESSURE = re.compile(
    r"(alloc_fail|_no_mem|nobuf|no_buf|session_limit|resource_limit|overload|_full$)"
)


def _total(body: str | None, pattern: str) -> int | None:
    if body is None:
        return None
    m = re.search(pattern, body)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- facade


@dataclass
class TechSupportFacts:
    system: dict = field(default_factory=dict)
    sessions: dict = field(default_factory=dict)
    resource_monitor: dict = field(default_factory=dict)
    interfaces: dict = field(default_factory=dict)
    ha: dict = field(default_factory=dict)
    licenses: list = field(default_factory=list)
    policies: dict = field(default_factory=dict)
    nat_types: dict = field(default_factory=dict)
    network: dict = field(default_factory=dict)
    counters_of_interest: dict = field(default_factory=dict)
    poe: dict = field(default_factory=dict)
    missing_commands: list = field(default_factory=list)


POLICY_COMMANDS = {
    "security_rules": "show running security-policy",
    "nat_rules": "show running nat-policy",
    "app_override_rules": "show running application-override-policy",
    "auth_rules": "show running authentication-policy",
    "decryption_rules": "show running decryption-policy",
    "dos_rules": "show running dos-policy",
    "pbf_rules": "show running pbf-policy",
    "qos_rules": "show running qos-policy",
    "tunnel_inspection_rules": "show running tunnel-inspect-policy",
    "sdwan_rules": "show running sdwan-policy",
}


def parse_techsupport(text: str) -> TechSupportFacts:
    ts = TechSupport(text)
    f = TechSupportFacts()

    def need(cmd: str) -> str | None:
        body = ts.get(cmd)
        if body is None:
            f.missing_commands.append(cmd)
        return body

    if b := need("show system info"):
        f.system = parse_system_info(b)
    if b := need("show session info"):
        f.sessions = parse_session_info(b)
    if b := need("show running resource-monitor"):
        rm = parse_resource_monitor(b)
        f.resource_monitor = {"series": rm, "summary": summarize_resource_monitor(rm)}
    if b := need("show interface all"):
        f.interfaces = parse_interface_all(b)
        details = {}
        for hw in f.interfaces["hardware"]:
            if hw["name"].startswith(("ethernet", "ae")):
                d = ts.get(f"show interface {hw['name']}")
                if d:
                    details[hw["name"]] = parse_interface_detail(d)
        f.interfaces["details"] = details
    f.ha = parse_ha(ts.get("show high-availability all") or "")
    f.poe = parse_poe(ts.get("show poe detail"))
    if b := need("request license info"):
        f.licenses = parse_licenses(b)

    for key, cmd in POLICY_COMMANDS.items():
        f.policies[key] = count_rules(ts.get(cmd))
    f.nat_types = nat_rule_types(ts.get("show running nat-policy"))

    ar = ts.get("show advanced-routing resource") or ts.get("show routing resource")
    f.network = {
        "routes_v4": _total(ar, r"Total (?:Active )?IPv4 routes\s*:\s*(\d+)"),
        "routes_v6": _total(ar, r"Total (?:Active )?IPv6 routes\s*:\s*(\d+)"),
        "ipsec_tunnels": _total(ts.get("show vpn tunnel"), r"Total (\d+) tunnels found"),
        "ike_gateways": _total(ts.get("show vpn gateway"), r"Total (\d+) gateways found"),
        "gp_current_users": _total(
            ts.get("show global-protect-gateway statistics"), r"Total Current Users:\s*(\d+)"
        ),
        "registered_ips": _total(
            ts.get("show object registered-ip all option count"), r"Total:\s*(\d+)"
        ),
        "config_size_bytes": _total(
            ts.get("show management-server last-committed config-size"), r"(\d+)\s*bytes"
        ),
    }
    if b := ts.get("show counter global"):
        counters = parse_counters(b)
        f.counters_of_interest = {k: v for k, v in counters.items() if _PRESSURE.search(k)}
    return f
