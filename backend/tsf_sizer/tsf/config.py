"""Count objects, rules and network elements in a PAN-OS config XML.

Use the TSF's `.merged-running-config.xml` when the firewall is Panorama-managed:
it holds the local config plus everything pushed from Panorama (templates and
device-group rules). Only counts are produced; no names, addresses or secrets
leave this module.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# Security profile types that count towards "Max security profiles".
SECURITY_PROFILE_TYPES = (
    "virus",
    "spyware",
    "vulnerability",
    "url-filtering",
    "file-blocking",
    "wildfire-analysis",
    "data-filtering",
    "dns-security",
)

RULEBASES = {
    "security": "security_rules",
    "nat": "nat_rules",
    "decryption": "decryption_rules",
    "application-override": "app_override_rules",
    "authentication": "auth_rules",
    "dos": "dos_rules",
    "pbf": "pbf_rules",
    "qos": "qos_rules",
    "tunnel-inspect": "tunnel_inspection_rules",
    "sdwan": "sdwan_rules",
}


class ConfigFormatError(ValueError):
    pass


@dataclass
class ConfigCounts:
    panos_version: str | None = None
    panorama_managed: bool = False
    vsys: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    by_vsys: dict[str, dict[str, int]] = field(default_factory=dict)
    profiles_by_type: dict[str, int] = field(default_factory=dict)
    interfaces: dict[str, int] = field(default_factory=dict)
    ha: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def parse_xml(data: bytes) -> ET.Element:
    # TSF configs never contain DTDs; refusing them blocks entity-expansion attacks.
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise ConfigFormatError("Config XML contains a DTD/entity declaration; refusing to parse")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ConfigFormatError(f"Config XML is not well-formed: {e}") from e
    if root.tag != "config":
        raise ConfigFormatError(f"Not a PAN-OS config (root element <{root.tag}>)")
    return root


def _entries(parent: ET.Element | None, path: str) -> list[ET.Element]:
    if parent is None:
        return []
    node = parent.find(path)
    return node.findall("entry") if node is not None else []


def _rules(rulebase_parent: ET.Element | None, name: str) -> list[ET.Element]:
    return _entries(rulebase_parent, f"{name}/rules")


def _nat_type(rule: ET.Element) -> str:
    src = rule.find("source-translation")
    if src is not None:
        if src.find("dynamic-ip-and-port") is not None:
            return "dipp"
        if src.find("dynamic-ip") is not None:
            return "dip"
        if src.find("static-ip") is not None:
            return "static"
    if (
        rule.find("destination-translation") is not None
        or rule.find("dynamic-destination-translation") is not None
    ):
        return "static"
    return "other"


def _add(d: dict[str, int], key: str, n: int) -> None:
    d[key] = d.get(key, 0) + n


def count_config(data: bytes) -> ConfigCounts:
    root = parse_xml(data)
    cc = ConfigCounts(panos_version=root.get("version"))
    shared = root.find("shared")
    dev = root.find("devices/entry")
    if dev is None:
        raise ConfigFormatError("Config has no devices/entry section")
    vsys_list = dev.findall("vsys/entry")
    cc.vsys = len(vsys_list)
    cc.panorama_managed = dev.find("deviceconfig/system/panorama") is not None or any(
        "panorama" in e.attrib for e in root.iter("entry")
    )
    c = cc.counts

    # ---- objects: shared + every vsys
    containers = [("shared", shared)] + [(v.get("name") or "vsys", v) for v in vsys_list]
    max_group_members = 0
    for name, cont in containers:
        if cont is None:
            continue
        per: dict[str, int] = {}
        addrs = _entries(cont, "address")
        _add(per, "address_objects", len(addrs))
        _add(per, "fqdn_objects", sum(1 for a in addrs if a.find("fqdn") is not None))
        groups = _entries(cont, "address-group")
        _add(per, "address_groups", len(groups))
        for g in groups:
            max_group_members = max(max_group_members, len(g.findall("static/member")))
        _add(per, "service_objects", len(_entries(cont, "service")))
        _add(per, "service_groups", len(_entries(cont, "service-group")))
        _add(per, "custom_apps", len(_entries(cont, "application")))
        _add(per, "application_groups", len(_entries(cont, "application-group")))
        _add(per, "application_filters", len(_entries(cont, "application-filter")))
        _add(per, "tags", len(_entries(cont, "tag")))
        _add(per, "schedules", len(_entries(cont, "schedule")))
        _add(per, "regions", len(_entries(cont, "region")))
        _add(per, "dynamic_user_groups", len(_entries(cont, "dynamic-user-group")))
        _add(per, "profile_groups", len(_entries(cont, "profile-group")))
        edls = _entries(cont, "external-list")
        _add(per, "edl_lists", len(edls))
        for e in edls:
            t = e.find("type")
            kind = t[0].tag if t is not None and len(t) else "unknown"
            _add(per, f"edl_lists_{kind}", 1)
        profiles = cont.find("profiles")
        if profiles is not None:
            for ptype in SECURITY_PROFILE_TYPES:
                n = len(_entries(profiles, ptype))
                _add(per, "security_profiles", n)
                _add(cc.profiles_by_type, ptype, n)
            cats = _entries(profiles, "custom-url-category")
            _add(per, "custom_url_categories", len(cats))
            _add(per, "url_list_entries", sum(len(x.findall("list/member")) for x in cats))
        if name != "shared":
            _add(per, "zones", len(_entries(cont, "zone")))
            rb = cont.find("rulebase")
            for xml_name, key in RULEBASES.items():
                rules = _rules(rb, xml_name)
                _add(per, key, len(rules))
                if xml_name == "nat":
                    for r in rules:
                        _add(per, f"nat_rules_{_nat_type(r)}", 1)
            _add(
                per,
                "decryption_profiles",
                len(_entries(profiles, "decryption")) if profiles is not None else 0,
            )
        cc.by_vsys[name] = per
        for k, v in per.items():
            _add(c, k, v)

    # Panorama pre/post rulebases kept in shared (older/other layouts).
    for section in ("pre-rulebase", "post-rulebase"):
        rb = shared.find(section) if shared is not None else None
        for xml_name, key in RULEBASES.items():
            _add(c, key, len(_rules(rb, xml_name)))
    c["max_address_group_members"] = max_group_members

    # ---- network
    net = dev.find("network")
    ifs = cc.interfaces
    eth = _entries(net, "interface/ethernet")
    for e in eth:
        mode = next(
            (
                ch.tag
                for ch in e
                if ch.tag
                in (
                    "layer3",
                    "layer2",
                    "virtual-wire",
                    "tap",
                    "ha",
                    "aggregate-group",
                    "decrypt-mirror",
                    "log-card",
                )
            ),
            "unconfigured",
        )
        _add(ifs, f"ethernet_{mode.replace('-', '_')}", 1)
        _add(ifs, "subinterfaces", len(e.findall(f"{mode}/units/entry")))
    ae = _entries(net, "interface/aggregate-ethernet")
    ifs["aggregate_ethernet"] = len(ae)
    for a in ae:
        _add(
            ifs,
            "subinterfaces",
            sum(len(a.findall(f"{m}/units/entry")) for m in ("layer3", "layer2", "virtual-wire")),
        )
    members_per_ae: dict[str, int] = {}
    for e in eth:
        ag = e.find("aggregate-group")
        if ag is not None and ag.text:
            members_per_ae[ag.text] = members_per_ae.get(ag.text, 0) + 1
    ifs["tunnel"] = len(_entries(net, "interface/tunnel/units"))
    ifs["vlan"] = len(_entries(net, "interface/vlan/units"))
    ifs["loopback"] = len(_entries(net, "interface/loopback/units"))

    c["aggregate_interfaces"] = len(ae)
    c["max_aggregate_members"] = max(members_per_ae.values(), default=0)
    c["tunnel_interfaces"] = ifs["tunnel"]
    c["logical_interfaces"] = (
        len(eth)
        + len(ae)
        + ifs.get("subinterfaces", 0)
        + ifs["tunnel"]
        + ifs["vlan"]
        + ifs["loopback"]
    )
    c["vwires"] = len(_entries(net, "virtual-wire"))
    # With advanced routing on, the legacy virtual-router section is ignored by PAN-OS.
    advanced = (dev.findtext("deviceconfig/setting/advance-routing") or "").strip() == "yes"
    logical_routers = _entries(net, "logical-router")
    virtual_routers = [] if advanced else _entries(net, "virtual-router")
    c["virtual_routers"] = len(logical_routers) + len(virtual_routers)
    static = 0
    for lr in logical_routers:
        for vrf in lr.findall("vrf/entry"):
            static += len(vrf.findall("routing-table/ip/static-route/entry"))
            static += len(vrf.findall("routing-table/ipv6/static-route/entry"))
    for vr in virtual_routers:
        static += len(vr.findall("routing-table/ip/static-route/entry"))
        static += len(vr.findall("routing-table/ipv6/static-route/entry"))
    c["static_routes"] = static
    peers = 0
    for r in logical_routers:
        for vrf in r.findall("vrf/entry"):
            peers += len(vrf.findall("bgp/peer-group/entry/peer/entry"))
    for r in virtual_routers:
        peers += len(r.findall("protocol/bgp/peer-group/entry/peer/entry"))
    c["routing_peers"] = peers
    c["ike_gateways"] = len(_entries(net, "ike/gateway"))
    c["ipsec_tunnels"] = len(_entries(net, "tunnel/ipsec"))
    c["gp_gateways"] = len(_entries(net, "tunnel/global-protect-gateway"))
    c["dhcp_relay_interfaces"] = sum(
        1 for e in _entries(net, "dhcp/interface") if e.find("relay") is not None
    )
    # Shared gateways sit next to the vsys entries (location differs between releases).
    c["shared_gateways"] = len(_entries(dev, "shared-gateway")) + len(
        _entries(net, "shared-gateway")
    )
    c["vsys"] = cc.vsys

    # ---- HA
    ha = dev.find("deviceconfig/high-availability")
    if ha is not None:
        enabled = (ha.findtext("enabled") or "").strip() == "yes"
        mode = (
            "active-active"
            if ha.find("group/mode/active-active") is not None
            else ("active-passive" if enabled else None)
        )
        ports = {}
        for link in ("ha1", "ha1-backup", "ha2", "ha2-backup", "ha3"):
            port = ha.findtext(f"interface/{link}/port")
            if port:
                ports[link] = port.strip()
        cc.ha = {"enabled": enabled, "mode": mode, "ports": ports}

    if cc.panorama_managed:
        cc.notes.append(
            "Panorama-managed: counts include pushed template and device-group "
            "config when the merged running config is used."
        )
    if not re.match(r"^\d+\.\d+", cc.panos_version or ""):
        cc.notes.append("Config version attribute missing or unexpected.")
    return cc
