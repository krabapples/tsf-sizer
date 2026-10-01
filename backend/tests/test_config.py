import io
import os
import tarfile
from pathlib import Path

import pytest

from tsf_sizer.cli import main as cli_main
from tsf_sizer.tsf.config import ConfigFormatError, count_config
from tsf_sizer.tsf.metrics import summarize
from tsf_sizer.tsf.techsupport import count_rules, parse_techsupport

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = FIXTURES / "config_sample.xml"
TECHSUPPORT = FIXTURES / "techsupport_sample.txt"


@pytest.fixture(scope="module")
def cc():
    return count_config(CONFIG.read_bytes())


def test_metadata(cc):
    assert cc.panos_version == "11.1.0"
    assert cc.panorama_managed is True
    assert cc.vsys == 2


def test_objects_shared_plus_vsys(cc):
    c = cc.counts
    assert c["address_objects"] == 1 + 3 + 1
    assert c["fqdn_objects"] == 2
    assert c["address_groups"] == 2
    assert c["max_address_group_members"] == 2
    assert c["service_objects"] == 1
    assert c["service_groups"] == 1
    assert c["custom_apps"] == 1
    assert c["edl_lists"] == 2
    assert (c["edl_lists_ip"], c["edl_lists_domain"]) == (1, 1)
    assert c["zones"] == 4
    assert cc.by_vsys["vsys2"]["address_objects"] == 1


def test_profiles(cc):
    # decryption profiles are not security profiles
    assert cc.counts["security_profiles"] == 4
    assert cc.profiles_by_type["spyware"] == 2
    assert cc.counts["decryption_profiles"] == 1
    assert cc.counts["custom_url_categories"] == 1
    assert cc.counts["url_list_entries"] == 2


def test_rules(cc):
    c = cc.counts
    assert c["security_rules"] == 4  # 3 + 1, default rules excluded
    assert c["nat_rules"] == 3
    assert (c["nat_rules_dipp"], c["nat_rules_dip"], c["nat_rules_static"]) == (1, 1, 1)
    assert c["decryption_rules"] == 1
    assert c["qos_rules"] == 0


def test_network(cc):
    c, ifs = cc.counts, cc.interfaces
    assert ifs["ethernet_layer3"] == 1 and ifs["ethernet_virtual_wire"] == 2
    assert ifs["ethernet_aggregate_group"] == 2 and ifs["ethernet_ha"] == 1
    assert ifs["subinterfaces"] == 3  # two on ethernet1/1, one on ae1
    assert c["aggregate_interfaces"] == 1 and c["max_aggregate_members"] == 2
    assert c["tunnel_interfaces"] == 2
    assert c["logical_interfaces"] == 6 + 1 + 3 + 2 + 0 + 1
    assert c["vwires"] == 1
    # advanced routing on: legacy virtual-router section ignored
    assert c["virtual_routers"] == 1
    assert c["static_routes"] == 3
    assert c["routing_peers"] == 2
    assert (c["ike_gateways"], c["ipsec_tunnels"], c["gp_gateways"]) == (2, 2, 1)
    assert c["dhcp_relay_interfaces"] == 1


def test_ha(cc):
    assert cc.ha == {
        "enabled": True,
        "mode": "active-passive",
        "ports": {"ha1": "ha1-a", "ha2": "ethernet1/8"},
    }


def test_legacy_routing_counts_virtual_routers():
    data = CONFIG.read_bytes().replace(
        b"<advance-routing>yes</advance-routing>", b"<advance-routing>no</advance-routing>"
    )
    c = count_config(data).counts
    assert c["virtual_routers"] == 2 and c["static_routes"] == 4


@pytest.mark.parametrize(
    "data",
    [
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><config/>',
        b"<config><devices>",
        b"<response><result/></response>",
        b"<config version='11.1.0'><shared/></config>",
    ],
)
def test_rejects_bad_xml(data):
    with pytest.raises(ConfigFormatError):
        count_config(data)


def test_default_rules_not_counted_in_running_policy():
    body = (
        '"r1; index: 1" {\n}\n"vsys1+intrazone-default; index: 2" {\n}\n'
        '"vsys1+interzone-default; index: 3" {\n}\n'
    )
    assert count_rules(body) == 1


def test_config_counts_override_cli_counts(cc):
    facts = parse_techsupport(TECHSUPPORT.read_text())
    s = summarize(facts, cc, ".merged-running-config.xml")
    m = s.metrics
    assert m["config.security_rules"].value == 4
    assert m["config.security_rules"].source == ".merged-running-config.xml"
    assert m["config.security_rules"].note == "running policy shows 3"
    assert m["config.address_objects"].value == 5
    assert m["config.edl_lists_ip"].value == 1
    assert not any("Object counts" in w for w in s.warnings)


def test_warns_when_panorama_managed_without_merged_config(cc):
    facts = parse_techsupport(TECHSUPPORT.read_text())
    s = summarize(facts, cc, "running-config.xml")
    assert any("merged running config was not used" in w for w in s.warnings)
    s2 = summarize(facts)
    assert any("Object counts" in w for w in s2.warnings)


def test_cli_uses_config_from_archive(tmp_path, workbook, capsys):
    tgz = tmp_path / "tsf.tgz"
    with tarfile.open(tgz, "w:gz") as tar:
        for name, path in [
            ("./tmp/cli/techsupport_PA3430_20260115_1000.txt", TECHSUPPORT),
            ("./opt/pancfg/mgmt/saved-configs/.merged-running-config.xml", CONFIG),
        ]:
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    dbp = str(tmp_path / "db.sqlite")
    assert cli_main(["--db", dbp, "import-portfolio", str(workbook), "--activate"]) == 0
    capsys.readouterr()
    assert cli_main(["--db", dbp, "analyze-tsf", str(tgz)]) == 0
    out = capsys.readouterr().out
    assert ".merged-running-config.xml (Panorama-managed)" in out
    assert "config.address_objects" in out


def test_cli_config_option_and_bad_config(tmp_path, capsys):
    dbp = str(tmp_path / "db.sqlite")
    assert cli_main(["--db", dbp, "analyze-tsf", str(TECHSUPPORT), "--config", str(CONFIG)]) == 0
    bad = tmp_path / "bad.xml"
    bad.write_text("<nope/>")
    assert cli_main(["--db", dbp, "analyze-tsf", str(TECHSUPPORT), "--config", str(bad)]) == 1


REAL = os.environ.get("TSF_CONFIG")


@pytest.mark.skipif(not REAL, reason="set TSF_CONFIG to test against a real config XML")
def test_real_config():
    cc = count_config(Path(REAL).read_bytes())
    assert cc.vsys >= 1
    assert cc.counts["security_rules"] >= 0
    assert cc.counts["zones"] >= 1
