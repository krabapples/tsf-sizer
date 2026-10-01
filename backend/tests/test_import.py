import os
from pathlib import Path

import pytest
from conftest import build_workbook

from tsf_sizer.cli import main as cli_main
from tsf_sizer.portfolio import catalog
from tsf_sizer.portfolio.importer import AlreadyImportedError, activate, import_workbook
from tsf_sizer.portfolio.mapping import TSF_METRIC_MAP


def _value(conn, doc_id, model, attr_key):
    return conn.execute(
        """SELECT v.* FROM capacity_value v
           JOIN model m ON m.id = v.model_id JOIN attribute a ON a.id = v.attribute_id
           WHERE v.document_id=? AND m.name=? AND a.canonical_key=?""",
        (doc_id, model, attr_key),
    ).fetchone()


def test_document_metadata(conn, workbook):
    report = import_workbook(conn, workbook)
    doc = conn.execute("SELECT * FROM source_document").fetchone()
    assert doc["sheet_name"] == "12.1.2 L2- L7"
    assert doc["panos_release"] == "12.1.2"
    assert doc["is_active"] == 0
    assert report.activated is False
    assert report.attribute_count == 23


def test_models_split_components_and_npi(conn, workbook):
    report = import_workbook(conn, workbook)
    assert report.models["components"] == ["PA-7500 MPC"]
    # Whole PA-500 and PA-5500 families are NPI, plus the explicitly listed PA-455R-5G.
    assert set(report.models["npi (not quotable)"]) == {"PA-5550", "PA-540", "PA-455R-5G"}
    assert set(report.models["quotable"]) == {"PA-7500", "PA-3430", "PA-450R", "PA-450R-5G"}

    models = {r["name"]: r for r in catalog.list_models(conn)}
    assert models["PA-7500 MPC"]["kind"] == "component"
    assert models["PA-7500 MPC"]["parent_model_id"] == models["PA-7500"]["id"]
    assert models["PA-7500 MPC"]["customer_quotable"] == 0
    # The sheet says NPI, but the team default releases the PA-500 family ...
    assert models["PA-540"]["lifecycle"] == "current"
    assert models["PA-540"]["customer_quotable"] == 1
    # ... while a model-level NPI mark in another family still holds.
    assert models["PA-455R-5G"]["customer_quotable"] == 0
    assert models["PA-5550"]["customer_quotable"] == 0
    assert models["PA-450R-5G"]["family"] == "PA-400"
    assert models["PA-450R-5G"]["superseded_by"] == "PA-500"
    assert models["PA-5550"]["family"] == "PA-5500"
    quotable = {r["name"] for r in catalog.list_models(conn, quotable_only=True)}
    assert quotable == {"PA-7500", "PA-3430", "PA-450R", "PA-450R-5G", "PA-540"}
    assert any("PA-505" in w and "PA-510" in w for w in report.warnings)


def test_combined_column_values_shared(conn, workbook):
    import_workbook(conn, workbook)
    a = _value(conn, 1, "PA-450R", "policy.security_rulebase")
    b = _value(conn, 1, "PA-450R-5G", "policy.security_rulebase")
    assert a["num_value"] == b["num_value"] == 2000


def test_throughput_normalized_to_gbps(conn, workbook):
    import_workbook(conn, workbook)
    key = "performance.app_id_firewall_throughput_64k_appmix"
    assert _value(conn, 1, "PA-7500", key)["num_value"] == 1500
    bare = _value(conn, 1, "PA-5550", key)
    assert bare["num_value"] == 175 and bare["unit"] == "Gbps" and bare["assumed"] == 1
    ipsec = _value(conn, 1, "PA-540", "performance.ipsec_vpn_throughput")
    assert ipsec["num_value"] == pytest.approx(0.65)
    attr = conn.execute("SELECT * FROM attribute WHERE canonical_key=?", (key,)).fetchone()
    assert attr["is_throughput"] == 1 and attr["unit"] == "Gbps"


def test_unconfirmed_cells_flagged(conn, workbook):
    report = import_workbook(conn, workbook)
    assert report.unconfirmed_count == len(
        {(a, h) for a, h in [("Security rulebase", "PA-3430"), ("Max address entries", "PA-540")]}
    )
    assert _value(conn, 1, "PA-3430", "policy.security_rulebase")["unconfirmed"] == 1
    assert _value(conn, 1, "PA-7500", "policy.security_rulebase")["unconfirmed"] == 0


def test_suffixes_bug_notes_and_specials(conn, workbook):
    import_workbook(conn, workbook)
    assert (
        _value(conn, 1, "PA-540", "objects_addresses_services.max_address_entries")["num_value"]
        == 4000
    )
    dns = _value(
        conn, 1, "PA-7500", "external_dynamic_list_edl_formerly_dbl.max_number_of_dns_per_system"
    )
    assert dns["num_value"] == 4_000_000
    arp = _value(conn, 1, "PA-3430", "l2_forwarding.arp_table_size_per_device")
    assert arp["num_value"] == 16000 and arp["note"] == "PAN-255203"
    vsys = _value(conn, 1, "PA-450R", "virtual_systems.max_virtual_systems")
    assert vsys["special"] == "not_applicable"
    cfg = _value(conn, 1, "PA-3430", "virtual_systems.max_sessions_per_virtual_system")
    assert cfg["special"] == "configurable"


def test_duplicate_row_names_get_suffix(conn, workbook):
    report = import_workbook(conn, workbook)
    keys = {r[0] for r in conn.execute("SELECT canonical_key FROM attribute")}
    assert "objects_addresses_services.max_address_groups" in keys
    assert "objects_addresses_services.max_address_groups__2" in keys
    assert len(report.duplicate_attributes) == 1


def test_indented_row_gets_parent(conn, workbook):
    import_workbook(conn, workbook)
    attr = conn.execute(
        "SELECT * FROM attribute WHERE canonical_key='interfaces.tunnel_interfaces'"
    ).fetchone()
    assert attr["name"] == "Tunnel interfaces"
    assert attr["parent_key"] == "interfaces.max_interfaces_ifnet"


def test_merged_cell_spreads_and_is_flagged(conn, workbook):
    report = import_workbook(conn, workbook)
    key = "interfaces.maximum_aggregates_with_qos_support"
    assert _value(conn, 1, "PA-5550", key)["raw_value"] == "Merged note"
    assert _value(conn, 1, "PA-3430", key)["raw_value"] == "Merged note"
    flagged = {(i.model, i.reason) for i in report.needs_review if i.attribute.startswith("Max")}
    assert ("PA-3430", "text in numeric row") in flagged


def test_mixed_number_and_yes_no_row_is_numeric(conn, workbook):
    report = import_workbook(conn, workbook)
    attr = conn.execute(
        "SELECT value_type FROM attribute WHERE canonical_key="
        "'routing.bidirectional_forwarding_detection_bfd_sessions'"
    ).fetchone()
    assert attr[0] == "number"
    assert not any(i.attribute.startswith("Bidirectional") for i in report.needs_review)
    assert report.info_counts["Yes/No in numeric row (supported / not supported)"] >= 3


def test_review_items(conn, workbook):
    report = import_workbook(conn, workbook)
    reasons = report.needs_review_counts
    assert reasons["TBD value"] == 2
    assert reasons["unexpected value in Yes/No row"] == 2  # "A/P only"
    assert report.needs_review_total == sum(reasons.values())
    assert report.info_total == sum(report.info_counts.values())


def test_interface_ports(conn, workbook):
    report = import_workbook(conn, workbook)
    assert report.interface_ports["PA-3430"] == {"10G_SFP+": 10, "100G_QSFP28": 2}
    assert report.interface_ports["PA-540"] == {"1G_RJ45": 8}
    assert report.interface_ports["PA-7500"] == {}  # "Based on NCs"
    assert "PA-7500 MPC" not in report.interface_ports
    assert catalog.interface_ports(conn, "PA-5550", 1) == {"100G_QSFP28": 16}
    assert any("Mystery" in r for r in report.unknown_interface_rows)


def test_tsf_metric_map(conn, workbook):
    report = import_workbook(conn, workbook)
    assert report.tsf_map_resolved + len(report.tsf_map_unresolved) == len(TSF_METRIC_MAP)
    caps = catalog.mapped_capacities(conn, "PA-3430", 1)
    assert caps["perf.throughput_threat_gbps"]["num_value"] == 15
    assert caps["config.address_objects"]["num_value"] == 30000
    assert caps["config.security_rules"]["unconfirmed"] == 1
    # The synthetic sheet lacks most rows, so those mappings must be reported, not dropped.
    assert any(u.startswith("config.nat_rules ->") for u in report.tsf_map_unresolved)


def test_reimport_same_file_refused_then_forced(conn, workbook):
    import_workbook(conn, workbook)
    with pytest.raises(AlreadyImportedError):
        import_workbook(conn, workbook)
    report = import_workbook(conn, workbook, force=True)
    assert conn.execute("SELECT count(*) FROM source_document").fetchone()[0] == 1
    assert report.document_id == 2  # ids are never reused
    assert report.activated is False


def test_forced_reimport_keeps_active_status(conn, workbook):
    import_workbook(conn, workbook, activate=True)
    report = import_workbook(conn, workbook, force=True)
    assert report.activated is True
    assert catalog.active_document(conn)["id"] == report.document_id


def test_diff_against_previous_version(conn, tmp_path, workbook):
    import_workbook(conn, workbook, activate=True)
    v2 = tmp_path / "Features_Capacities_12_1_2_v2.xlsx"
    build_workbook(v2, overrides={("Security rulebase", "PA-3430"): 35000})
    report = import_workbook(conn, v2)
    assert report.diff["previous_document_id"] == 1
    assert report.diff["changed_total"] == 1
    change = report.diff["changed"][0]
    assert change["model"] == "PA-3430" and change["old"] == "30000" and change["new"] == "35000"
    # Not active until explicitly activated.
    assert catalog.active_document(conn)["id"] == 1
    activate(conn, report.document_id)
    assert catalog.active_document(conn)["id"] == report.document_id
    active = conn.execute("SELECT count(*) FROM source_document WHERE is_active=1").fetchone()
    assert active[0] == 1


def test_overrides_survive_reimport(conn, workbook):
    import_workbook(conn, workbook)
    catalog.set_model_override(
        conn, "PA-540", lifecycle="current", customer_quotable=1, price_tier="S"
    )
    import_workbook(conn, workbook, force=True)
    m = {r["name"]: r for r in catalog.list_models(conn)}["PA-540"]
    assert m["lifecycle"] == "current" and m["customer_quotable"] == 1 and m["price_tier"] == "S"


def test_workbook_without_model_columns_is_rejected(conn, tmp_path):
    import openpyxl

    from tsf_sizer.portfolio.workbook import WorkbookFormatError

    p = tmp_path / "empty.xlsx"
    openpyxl.Workbook().save(p)
    with pytest.raises(WorkbookFormatError):
        import_workbook(conn, p)


def test_cli_roundtrip(tmp_path, workbook, capsys):
    dbp = str(tmp_path / "cli.db")
    rep = tmp_path / "out" / "report.json"
    assert (
        cli_main(
            ["--db", dbp, "import-portfolio", str(workbook), "--activate", "--report", str(rep)]
        )
        == 0
    )
    assert rep.exists() and rep.with_suffix(".txt").exists()
    assert cli_main(["--db", dbp, "import-portfolio", str(workbook)]) == 1
    assert cli_main(["--db", dbp, "show-model", "PA-3430", "--category", "policy"]) == 0
    out = capsys.readouterr().out
    assert "Security rulebase" in out and "[unconfirmed]" in out
    assert cli_main(["--db", dbp, "set-model", "PA-540", "--quotable", "yes"]) == 0
    assert cli_main(["--db", dbp, "set-family", "PA-400", "--superseded-by", ""]) == 0
    assert cli_main(["--db", dbp, "set-family", "PA-400", "--quotable", "sheet"]) == 0
    assert cli_main(["--db", dbp, "set-family", "PA-9"]) == 1
    assert cli_main(["--db", dbp, "models"]) == 0
    assert "PA-540" in capsys.readouterr().out


# Runs only when the real workbook is supplied: PORTFOLIO_XLSX=/path/to/file.xlsx pytest
REAL = os.environ.get("PORTFOLIO_XLSX")


@pytest.mark.skipif(not REAL, reason="set PORTFOLIO_XLSX to test against the real workbook")
def test_real_workbook_imports_cleanly(conn):
    report = import_workbook(conn, Path(REAL))
    assert report.tsf_map_unresolved == []
    assert report.attribute_count > 200
    assert report.models["quotable"]
    unparsed = report.needs_review_total / report.cell_count
    assert unparsed < 0.01
