import io
import os
import tarfile
from pathlib import Path

import pytest

from tsf_sizer.cli import main as cli_main
from tsf_sizer.portfolio.importer import import_workbook
from tsf_sizer.tsf.archive import TsfFormatError, read_tsf
from tsf_sizer.tsf.compare import resolve_port_media, usage_against_model
from tsf_sizer.tsf.metrics import speed_class, summarize
from tsf_sizer.tsf.techsupport import (
    TechSupport,
    count_rules,
    parse_resource_monitor,
    parse_techsupport,
    split_sections,
)

FIXTURE = Path(__file__).parent / "fixtures" / "techsupport_sample.txt"
UPTIME_S = 45 * 86400 + 3 * 3600 + 20 * 60 + 10


@pytest.fixture(scope="module")
def text():
    return FIXTURE.read_text()


@pytest.fixture(scope="module")
def facts(text):
    return parse_techsupport(text)


@pytest.fixture(scope="module")
def summary(facts):
    return summarize(facts)


def test_split_sections(text):
    sections = split_sections(text)
    cmds = [s.command for s in sections]
    assert cmds[0] == "show admins all"
    assert "show running resource-monitor" in cmds
    assert len(cmds) == len(set(cmds))


def test_server_errors_count_as_missing(text):
    ts = TechSupport(text)
    assert ts.get("show interface ethernet1/9") is None
    assert ts.get("show interface ethernet1/1") is not None


def test_system_info_skips_identifiers(facts):
    assert facts.system["model"] == "PA-3430"
    assert facts.system["sw-version"] == "11.1.6"
    assert facts.system["uptime_seconds"] == UPTIME_S
    assert "hostname" not in facts.system and "serial" not in facts.system
    assert "ip-address" not in facts.system


def test_session_info(facts):
    s = facts.sessions
    assert s["sessions_supported"] == 2_500_000
    assert s["sessions_allocated"] == 180_000
    assert s["throughput_kbps"] == 2_400_000
    assert s["cps"] == 9000
    assert s["packet_rate_pps"] == 450_000


def test_resource_monitor_series(text):
    body = TechSupport(text).get("show running resource-monitor")
    rm = parse_resource_monitor(body)
    assert set(rm) == {"second", "minute", "hour", "day", "week"}
    assert rm["second"]["cpu"][0] == [None, 20, 25, 22]
    assert rm["week"]["cpu"][2] == [None] * 8
    assert rm["hour"]["util"]["session_max"] == [12, 6]


def test_resource_monitor_summary(facts):
    summ = facts.resource_monitor["summary"]
    assert summ["second"]["dp_cpu_peak_pct"] == 25
    assert summ["hour"]["dp_cpu_peak_pct"] == 58
    assert summ["week"]["dp_cpu_peak_pct"] == 73
    assert summ["week"]["samples_with_data"] == 2
    assert summ["week"]["session_util_peak_pct"] == 16
    assert summ["minute"]["packet_buffer_peak_pct"] == 5


def test_policies_and_nat(facts):
    assert facts.policies["security_rules"] == 3
    assert facts.policies["nat_rules"] == 2
    assert facts.policies["decryption_rules"] == 1
    assert facts.policies["dos_rules"] == 0
    assert facts.policies["qos_rules"] is None  # command not in this file
    assert facts.nat_types == {"static": 1, "dip": 0, "dipp": 1, "other": 0}
    assert count_rules(None) is None


def test_licenses_ha_network(facts):
    assert {x["feature"]: x["expired"] for x in facts.licenses} == {
        "Threat Prevention": False,
        "WildFire License": True,
    }
    assert facts.ha["enabled"] and facts.ha["mode"] == "Active-Passive"
    assert facts.ha["ha1_interface"] == "ha1-a" and facts.ha["ha2_interface"] == "hsci"
    assert facts.ha["peer_model"] == "PA-3430"
    net = facts.network
    assert (net["routes_v4"], net["routes_v6"]) == (1200, 30)
    assert (net["ipsec_tunnels"], net["ike_gateways"]) == (2, 2)
    assert net["gp_current_users"] == 37
    assert net["registered_ips"] == 250
    assert net["config_size_bytes"] == 2_500_000


def test_only_pressure_counters_kept(facts):
    assert facts.counters_of_interest == {"session_alloc_failure": 12}


def test_interfaces(facts):
    hw = {h["name"]: h for h in facts.interfaces["hardware"]}
    assert hw["ethernet1/3"]["state"] == "down"
    lg = {i["name"]: i for i in facts.interfaces["logical"]}
    assert lg["ethernet1/2.10"]["zone"] == "trust"
    assert lg["ethernet1/2"]["zone"] is None
    assert facts.interfaces["details"]["ethernet1/1"]["port_type"] == "SFP+"


def test_summary_ports(summary):
    ports = {p.name: p for p in summary.ports}
    assert (ports["ethernet1/1"].role, ports["ethernet1/1"].speed_class) == ("traffic", "10G_SFP+")
    # Parent without zone but with subinterfaces is still in use.
    assert (ports["ethernet1/2"].role, ports["ethernet1/2"].speed_class) == ("traffic", "10G_RJ45")
    assert ports["ethernet1/3"].role == "unused"
    assert summary.port_counts == {"10G_SFP+": 1, "10G_RJ45": 1}


def test_summary_metrics(summary):
    m = summary.metrics
    assert summary.sizing_basis == "threat_prevention"
    assert m["perf.sessions"].value == 400_000  # 16% of 2.5M
    assert m["perf.sessions"].kind == "estimate"
    assert m["perf.dp_cpu_peak_pct"].value == 73
    assert m["perf.throughput_snapshot_mbps"].value == 2400
    expected = round(3_888_000_000_000 * 8 / UPTIME_S / 1e6, 1)
    assert m["perf.throughput_avg_since_boot_mbps"].value == expected
    assert m["config.zones"].value == 4
    assert m["config.tunnel_interfaces"].value == 1
    assert m["config.nat_rules_dipp"].value == 1
    assert m["config.vsys"].value == 1
    assert m["state.gp_users"].value == 37
    assert m["feature.ha_active_passive"].value is True
    assert "config.qos_rules" not in m


def test_summary_warnings(summary):
    w = " ".join(summary.warnings)
    assert "Uptime is only" not in w  # 45 days
    assert "Only 2 week(s)" in w
    assert "WildFire License" in w
    assert "session_alloc_failure=12" in w
    assert "Decryption rules" in w
    assert "HA links use data ports" not in w  # dedicated HA ports in this fixture


@pytest.mark.parametrize(
    ("port_type", "cap", "speed", "expected"),
    [
        ("RJ45", "auto, 1Gb/s-full", "1000", "1G_RJ45"),
        ("RJ45", "1Gb/s-full, 2.5Gb/s-full, 5Gb/s-full", "1000", "5G_RJ45"),
        ("SFP", "1Gb/s-full", "1000", "1G_SFP"),
        ("SFP+", "1Gb/s-full, 10Gb/s-full", "1000", "10G_SFP+"),
        ("SFP28", "", "25000", "25G_SFP28"),
        ("QSFP28", "100Gb/s-full", "100000", "100G_QSFP28"),
        (None, None, "1000", "1G_unknown-media"),
        (None, None, "ukn", "unknown"),
    ],
)
def test_speed_class(port_type, cap, speed, expected):
    assert speed_class(port_type, cap, speed) == expected


def test_resolve_port_media():
    resolved, notes = resolve_port_media(
        {"1G_unknown-media": 2, "unknown": 1, "10G_SFP+": 1}, {"1G_RJ45": 8}
    )
    assert resolved == {"1G_RJ45": 3, "10G_SFP+": 1}
    assert len(notes) == 2
    ambiguous, _ = resolve_port_media({"1G_unknown-media": 1}, {"1G_RJ45": 4, "1G_SFP": 2})
    assert ambiguous == {"1G_unknown-media": 1}


# --------------------------------------------------------------------------- archive


def _make_tgz(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_read_tsf_archive_picks_only_wanted_files(tmp_path, text):
    tgz = tmp_path / "tsf.tgz"
    _make_tgz(
        tgz,
        {
            "./tmp/cli/techsupport_PA3430_20260115_1000.txt": text.encode(),
            "./opt/pancfg/mgmt/saved-configs/.merged-running-config.xml": b"<config/>",
            "./opt/pancfg/mgmt/saved-configs/running-config.xml": b"<config/>",
            "./var/log/pan/ms.log": b"x" * 1000,
            "../../etc/evil": b"nope",
        },
    )
    files = read_tsf(tgz)
    assert set(files.files) == {"techsupport", "merged_config", "running_config"}
    assert files.member_names["techsupport"].startswith("tmp/cli/techsupport_")
    assert not (tmp_path.parent / "etc" / "evil").exists()
    assert "PA-3430" in files.text("techsupport")


def test_read_tsf_loose_text_file():
    files = read_tsf(FIXTURE)
    assert set(files.files) == {"techsupport"}


def test_read_tsf_rejects_other_files(tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("hello")
    with pytest.raises(TsfFormatError):
        read_tsf(other)
    tgz = tmp_path / "logs.tgz"
    _make_tgz(tgz, {"./var/log/pan/ms.log": b"x"})
    with pytest.raises(TsfFormatError, match="techsupport"):
        read_tsf(tgz)


# --------------------------------------------------------------------------- compare


def test_usage_against_model(conn, workbook, summary):
    import_workbook(conn, workbook, activate=True)
    report = usage_against_model(conn, summary)
    rows = {r.metric: r for r in report.rows}
    thr = rows["perf.throughput_snapshot_mbps"]
    assert thr.capacity == 15 and thr.utilization_pct == 16.0  # 2.4 of 15 Gbps
    assert rows["config.security_rules"].unconfirmed is True  # orange in the fixture sheet
    assert rows["config.security_rules"].utilization_pct == 0.0
    assert report.ports_available == {"10G_SFP+": 10, "100G_QSFP28": 2}
    assert report.ports_needed == {"10G_SFP+": 1, "10G_RJ45": 1}
    with pytest.raises(LookupError):
        usage_against_model(conn, summary, model="PA-9999")


def test_cli_analyze(tmp_path, workbook, capsys):
    dbp = str(tmp_path / "cli.db")
    assert cli_main(["--db", dbp, "import-portfolio", str(workbook), "--activate"]) == 0
    capsys.readouterr()
    out_json = tmp_path / "r.json"
    assert cli_main(["--db", dbp, "analyze-tsf", str(FIXTURE), "--json", str(out_json)]) == 0
    out = capsys.readouterr().out
    assert "PA-3430" in out and "Usage vs PA-3430" in out
    assert out_json.exists()
    assert cli_main(["--db", dbp, "analyze-tsf", str(tmp_path / "missing.tgz")]) == 1


# Runs only when a real techsupport file is supplied: TSF_TECHSUPPORT=/path pytest
REAL = os.environ.get("TSF_TECHSUPPORT")


@pytest.mark.skipif(not REAL, reason="set TSF_TECHSUPPORT to test against a real file")
def test_real_techsupport():
    f = parse_techsupport(read_tsf(Path(REAL)).text("techsupport"))
    s = summarize(f)
    assert s.model and s.panos
    assert f.missing_commands == []
    assert "perf.dp_cpu_peak_pct" in s.metrics
    assert any(p.role == "traffic" for p in s.ports)
