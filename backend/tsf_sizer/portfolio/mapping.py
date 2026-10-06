"""Links between TSF metrics and capacity-sheet rows.

Targets are matched on (category, attribute name), case- and whitespace-
insensitive. If a row is renamed, the import report lists the mapping as
unresolved instead of failing silently.
"""

from __future__ import annotations

import re

# (tsf_metric, category, attribute name, compare_as)
TSF_METRIC_MAP: list[tuple[str, str, str, str]] = [
    # Performance
    (
        "perf.throughput_threat_gbps",
        "Performance",
        "Threat prevention throughput 64k (appmix)",
        "throughput",
    ),
    (
        "perf.throughput_appid_gbps",
        "Performance",
        "App-ID firewall throughput 64k (appmix)",
        "throughput",
    ),
    ("perf.throughput_ipsec_gbps", "Performance", "IPSec VPN throughput", "throughput"),
    (
        "perf.decrypt_threat_gbps",
        "Performance",
        "SSL-ECDHE-Threat-64k (Gbps) TLS 1.2",
        "throughput",
    ),
    ("perf.cps", "Performance", "Connections per second", "rate"),
    ("perf.sessions", "Sessions", "Max sessions for L7 inspection", "count"),
    ("perf.decrypt_sessions", "SSL Decryption", "Concurrent Sessions", "count"),
    # Policy
    ("config.security_rules", "Policy", "Security rulebase", "count"),
    ("config.decryption_rules", "Policy", "SSL decryption rulebase", "count"),
    ("config.app_override_rules", "Policy", "App Override rulebase", "count"),
    ("config.tunnel_inspection_rules", "Policy", "Tunnel content inspection rules", "count"),
    ("config.sdwan_rules", "Policy", "SD-WAN Rules", "count"),
    ("config.pbf_rules", "Policy", "Policy Based Forwarding", "count"),
    ("config.auth_rules", "Policy", "Captive Portal", "count"),
    ("config.dos_rules", "Policy", "DoS Protection", "count"),
    ("config.qos_rules", "QoS", "Number of QoS policies", "count"),
    ("config.schedules", "Policy", "Security rule schedules", "count"),
    # NAT
    ("config.nat_rules", "NAT", "NAT rule capacity", "count"),
    ("config.nat_rules_static", "NAT", "Max NAT rules (static)", "count"),
    ("config.nat_rules_dip", "NAT", "Max NAT rules (DIP)", "count"),
    ("config.nat_rules_dipp", "NAT", "Max NAT rules (DIPP)", "count"),
    # Objects
    ("config.address_objects", "Objects (Addresses & Services)", "Max address entries", "count"),
    ("config.address_groups", "Objects (Addresses & Services)", "Max address groups", "count"),
    (
        "config.max_address_group_members",
        "Objects (Addresses & Services)",
        "Max members per address group",
        "count",
    ),
    ("config.service_objects", "Objects (Addresses & Services)", "Max services entries", "count"),
    ("config.service_groups", "Objects (Addresses & Services)", "Max services groups", "count"),
    ("config.fqdn_objects", "Objects (Addresses & Services)", "FQDN", "count"),
    (
        "state.dag_ips",
        "Objects (Addresses & Services)",
        "Total IPs across all Dynamic Address Groups",
        "count",
    ),
    # EDL
    (
        "config.edl_lists",
        "External Dynamic List (EDL), formerly DBL",
        "Max number of custom lists",
        "count",
    ),
    (
        "state.edl_ips",
        "External Dynamic List (EDL), formerly DBL",
        "Max number of IPs per system",
        "count",
    ),
    (
        "state.edl_domains",
        "External Dynamic List (EDL), formerly DBL",
        "Max number of DNS per system",
        "count",
    ),
    (
        "state.edl_urls",
        "External Dynamic List (EDL), formerly DBL",
        "Max number of URL per system",
        "count",
    ),
    # Profiles, apps, URL
    ("config.security_profiles", "Security Profiles", "Max security profiles", "count"),
    ("config.ssl_inbound_certs", "SSL Decryption", "Max SSL inbound certificates", "count"),
    (
        "config.custom_apps",
        "Application Signatures",
        "Custom App-IDs (Virtual System Specific)",
        "count",
    ),
    ("config.custom_url_categories", "URL Filtering", "Max custom categories", "count"),
    (
        "config.url_list_entries",
        "URL Filtering",
        "Max total entries - allow/block list and custom categories",
        "count",
    ),
    # Network
    ("config.zones", "Security Zones", "Max security zones", "count"),
    ("config.virtual_routers", "Virtual Routers", "Max VRs", "count"),
    ("config.vwires", "Virtual Wires", "Max virtual wires w/physical interfaces", "count"),
    ("config.vsys", "Virtual Systems", "Max virtual systems", "count"),
    ("config.shared_gateways", "Virtual Systems", "Max shared gateway", "count"),
    ("config.logical_interfaces", "Interfaces", "Max interfaces (ifNet)", "count"),
    ("config.tunnel_interfaces", "Interfaces", "Tunnel interfaces", "count"),
    ("config.aggregate_interfaces", "Interfaces", "Maximum aggregate interfaces", "count"),
    (
        "config.max_aggregate_members",
        "Interfaces",
        "Maximum number of members per aggregate",
        "count",
    ),
    ("config.sdwan_interfaces", "Interfaces", "Maximum SD-WAN Virtual Interfaces", "count"),
    ("state.routes_v4", "Routing", "Forwarding table size V4 (entries per device)", "count"),
    ("state.routes_v6", "Routing", "Forwarding table size V6 (entries per device)", "count"),
    ("config.routing_peers", "Routing", "Max routing peers (protocol dependent)", "count"),
    (
        "config.bfd_sessions",
        "Routing",
        "Bidirectional Forwarding Detection (BFD) Sessions",
        "count",
    ),
    ("state.arp_entries", "L2 Forwarding", "ARP table size per device", "count"),
    ("config.dhcp_relay_interfaces", "Address Assignment", "DHCP Relay Enabled Interface", "count"),
    # VPN
    ("config.ipsec_tunnels", "IPSec VPN / GRE", "IPSec VPN (Site-to-site) / GRE Tunnels", "count"),
    ("config.gp_gateways", "SSL VPN", "Max number of GP Gateways", "count"),
    (
        "state.gp_users",
        "SSL VPN",
        "GlobalProtect Client VPN (SSL, IPSec and XAUTH clients)",
        "count",
    ),
    # User-ID
    ("state.userid_mappings", "User ID", "User IP Mappings (data plane)", "count"),
    ("config.userid_groups", "User ID", "Active and unique groups used in policy", "count"),
    ("config.ts_agents", "User ID", "Terminal Server Agents", "count"),
    ("config.userid_agents", "User ID", "UserID Agents", "count"),
    # Features in use (Yes/No rows)
    ("feature.ha_active_active", "High Availability (HA)", "Active-active", "feature"),
    ("feature.ha_active_passive", "High Availability (HA)", "Active-passive", "feature"),
    ("feature.gtp", "Mobile Protocols", "GTP", "feature"),
    ("feature.sctp", "Mobile Protocols", "SCTP", "feature"),
    ("feature.hsm", "SSL Decryption", "HSM Supported", "feature"),
    ("feature.lre_routing", "Routing", "Legacy Routing Engine (LRE) Support", "feature"),
]

# "Traffic - <label>" rows in the Interfaces category -> port speed class.
INTERFACE_ROWS: dict[str, str] = {
    "10/100/1000": "1G_RJ45",
    "100M/1G/2.5G": "2.5G_RJ45",
    "100M/1G/2.5G/5G": "5G_RJ45",
    "100M/1G/10G": "10G_RJ45",
    "SFP/RJ45 Combo": "1G_COMBO",
    "SFP (1G)": "1G_SFP",
    "SFP+ (10G)": "10G_SFP+",
    "SFP28 (25G)": "25G_SFP28",
    "QSFP+ (40G)": "40G_QSFP+",
    "QSFP28 (100G)": "100G_QSFP28",
    "SFP-DD (100G)": "100G_SFP-DD",
    "QSFP-DD (400G)": "400G_QSFP-DD",
}
INTERFACE_ROW_PREFIX = "Traffic - "


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def is_throughput_row(category: str, name: str) -> bool:
    return norm(category) == "performance" and bool(re.search(r"throughput|gbps|64k", name, re.I))
