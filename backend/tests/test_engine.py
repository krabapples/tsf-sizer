from pathlib import Path

import pytest
from conftest import build_workbook

from tsf_sizer import db
from tsf_sizer.portfolio.importer import import_workbook
from tsf_sizer.sizing.engine import Requirement, SizingParams, _check, fit_ports, size, variants
from tsf_sizer.tsf.metrics import PortUse, summarize
from tsf_sizer.tsf.techsupport import parse_techsupport

FIXTURE = Path(__file__).parent / "fixtures" / "techsupport_sample.txt"


@pytest.fixture
def sized_db(tmp_path):
    """Synthetic portfolio where PA-3430 also has 8 copper ports."""
    path = tmp_path / "Capacity_Workbook_Engine.xlsx"
    build_workbook(path, overrides={("Traffic - 10/100/1000", "PA-3430"): 8})
    conn = db.connect(tmp_path / "e.db")
    import_workbook(conn, path, activate=True)
    yield conn
    conn.close()


def summary_as(model: str, ports: list[PortUse] | None = None):
    facts = parse_techsupport(FIXTURE.read_text())
    facts.system["model"] = model
    s = summarize(facts)
    if ports is not None:
        s.ports = ports
    return s


def test_variants():
    assert variants("PA-450R-5G") == {"rugged", "5G"}
    assert variants("PA-545-POE") == {"PoE"}
    assert variants("PA-3430") == set()


@pytest.mark.parametrize(
    ("needed", "available", "ok", "missing"),
    [
        ({"1G_RJ45": 4}, {"1G_RJ45": 8}, True, []),
        ({"1G_RJ45": 4}, {"1G_RJ45": 2, "10G_RJ45": 2}, True, []),
        ({"10G_SFP+": 2}, {"25G_SFP28": 4}, True, []),
        ({"10G_SFP+": 2}, {"10G_RJ45": 8}, False, ["2x 10G_SFP+"]),
        ({"1G_SFP": 2, "10G_SFP+": 2}, {"10G_SFP+": 3}, False, ["1x 1G_SFP"]),
        ({"1G_unknown-media": 2}, {"10G_RJ45": 1, "1G_SFP": 1}, True, []),
    ],
)
def test_fit_ports(needed, available, ok, missing):
    got_ok, plan, got_missing = fit_ports(needed, available)
    assert got_ok is ok
    assert got_missing == missing


def test_fit_ports_prefers_smallest_fit():
    _, plan, _ = fit_ports({"1G_RJ45": 2}, {"10G_RJ45": 4, "1G_RJ45": 2})
    assert plan["assignment"] == {"1G_RJ45": {"1G_RJ45": 2}}
    assert plan["spare"] == {"10G_RJ45": 4}


def _req(**kw):
    base = dict(
        metric="m",
        label="L",
        observed=10,
        observed_basis="count",
        required=12,
        current_capacity=20,
        unit=None,
        is_perf=False,
    )
    return Requirement(**{**base, **kw})


def _cap(**kw):
    base = dict(
        kind="number", num_value=None, bool_value=None, raw_value=None, special=None, unconfirmed=0
    )
    return {**base, **kw}


def test_check_rules():
    assert _check(_req(), _cap(num_value=30, raw_value="30")).status == "pass"
    below_current = _check(_req(), _cap(num_value=15, raw_value="15"))
    assert below_current.status == "fail" and "below current model" in below_current.reason
    too_small = _check(_req(current_capacity=None), _cap(num_value=11, raw_value="11"))
    assert "needs 12, has 11" in too_small.reason
    perf_equal = _check(_req(is_perf=True), _cap(num_value=20, raw_value="20"))
    assert perf_equal.status == "fail" and "not more than current" in perf_equal.reason
    assert (
        _check(_req(), _cap(kind="special", special="system_limit", raw_value="System")).status
        == "pass"
    )
    assert (
        _check(_req(), _cap(kind="special", special="not_applicable", raw_value="-")).status
        == "fail"
    )
    assert (
        _check(
            _req(required=0), _cap(kind="special", special="not_applicable", raw_value="-")
        ).status
        == "pass"
    )
    assert _check(_req(), _cap(kind="bool", bool_value=1, raw_value="Yes")).status == "pass"
    assert _check(_req(), None).status == "unknown"
    unconf = _check(_req(), _cap(num_value=30, raw_value="30", unconfirmed=1))
    assert unconf.unconfirmed is True


def test_size_recommends_bigger_model_with_matching_ports(sized_db):
    s = summary_as("PA-450R")
    result = size(sized_db, s, SizingParams(include_superseded=True, max_size_factor=0))
    assert result.current_in_portfolio
    assert result.recommended is not None and result.recommended.model == "PA-3430"
    rej = {c.model: c.failures for c in result.rejected}
    # Same performance as the current box is not enough.
    assert any("not more than current" in f for f in rej["PA-450R-5G"])
    # Chassis without front-panel ports in the sheet cannot take the 8 copper ports.
    assert any(f.startswith("Ports: missing") for f in rej["PA-7500"])
    # NPI models are never candidates; PA-540 is released by the family default.
    assert "PA-5550" not in rej and "PA-455R-5G" not in rej
    assert "PA-540" in rej
    assert result.ports_needed["baseline"] == {"1G_RJ45": 8}


def test_previous_generation_left_out_unless_included(sized_db):
    s = summary_as("PA-3430")
    off = size(sized_db, s, SizingParams(max_size_factor=0))
    considered = {c.model for c in off.rejected + off.too_large}
    assert not {"PA-450R", "PA-450R-5G"} & considered
    assert off.excluded_families == {"PA-400": "PA-500"}
    assert any("PA-400 series left out" in n for n in off.notes)
    on = size(sized_db, s, SizingParams(include_superseded=True, max_size_factor=0))
    considered = {c.model for c in on.rejected + on.too_large}
    assert {"PA-450R", "PA-450R-5G"} <= considered
    assert on.excluded_families == {}


def test_size_cap_marks_oversized_models(sized_db):
    s = summary_as("PA-450R")  # PA-450R: 1.4 Gbps threat; PA-3430: 15 Gbps
    # Small known peak: the cap is then based on the current model (5 x 1.4 Gbps).
    result = size(sized_db, s, SizingParams(max_size_factor=5, peak_throughput_mbps=300))
    assert result.size_cap_gbps == 7.0
    # The only qualifying model is too large; it is still offered, with a note.
    assert result.recommended.model == "PA-3430"
    assert result.recommended.too_large and "more than 5x" in result.recommended.too_large
    assert any("No model within the size cap" in n for n in result.notes)
    roomy = size(sized_db, s, SizingParams(max_size_factor=20, peak_throughput_mbps=300))
    assert roomy.recommended.model == "PA-3430" and roomy.recommended.too_large is None
    assert not any("size cap" in n for n in roomy.notes)
    # A requirement above the current model raises the cap with it.
    big = size(
        sized_db,
        s,
        SizingParams(max_size_factor=5, peak_throughput_mbps=2000, growth_pct_per_year=0),
    )
    assert big.size_cap_gbps == pytest.approx(2.0 / 0.7 * 5, abs=0.01)


def test_per_item_limits_only_need_usage():
    req = _req(
        metric="config.max_address_group_members", observed=0, required=0, current_capacity=2500
    )
    assert _check(req, _cap(num_value=1000, raw_value="1000")).status == "pass"
    count = _req(metric="config.address_objects", observed=0, required=0, current_capacity=2500)
    assert _check(count, _cap(num_value=1000, raw_value="1000")).status == "fail"


def test_alternatives_are_steps_up_only():
    from tsf_sizer.sizing.engine import Candidate, _alternatives

    def cand(name, size, variant=False):
        return Candidate(name, "F", True, sort_key=size, extra_variants=["PoE"] if variant else [])

    picks = _alternatives(
        [cand("A", 4.5), cand("B", 3.0, True), cand("C", 6.0), cand("D", 7.0, True)]
    )
    assert [c.model for c in picks] == ["C"]
    only_variants = _alternatives([cand("A", 4.5), cand("B", 5.0, True), cand("D", 7.0, True)])
    assert [c.model for c in only_variants] == ["B", "D"]


def test_size_used_port_rule_and_growth(sized_db):
    s = summary_as(
        "PA-450R",
        ports=[
            PortUse("ethernet1/1", "1G_RJ45", "traffic", "untrust", "1000", "up"),
            PortUse("ethernet1/2", "1G_RJ45", "traffic", "trust", "1000", "up"),
        ],
    )
    result = size(sized_db, s, SizingParams(port_rule="used", growth_pct_per_year=0, years=1))
    assert result.ports_needed["baseline"] == {"1G_RJ45": 2}
    reqs = {r.metric: r for r in result.requirements}
    assert reqs["config.security_rules"].required == reqs["config.security_rules"].observed
    thr = reqs["perf.throughput_threat_gbps"]
    assert thr.required == pytest.approx(2.4 / 0.7, rel=1e-3)  # snapshot / target util


def test_size_with_entered_peaks(sized_db):
    s = summary_as("PA-450R")
    result = size(sized_db, s, SizingParams(peak_throughput_mbps=20000, growth_pct_per_year=0))
    reqs = {r.metric: r for r in result.requirements}
    assert reqs["perf.throughput_threat_gbps"].observed_basis == "entered peak"
    # 20 Gbps / 0.7 exceeds every quotable model in the synthetic sheet.
    assert result.recommended is None
    assert any("No quotable model" in n for n in result.notes)


def test_size_unknown_current_model(sized_db):
    s = summary_as("PA-220")
    result = size(sized_db, s, SizingParams())
    assert not result.current_in_portfolio
    assert any("not in the portfolio" in n for n in result.notes)


def test_size_requires_active_portfolio(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    with pytest.raises(LookupError):
        size(conn, summary_as("PA-3430"))


def _give_poe(conn, model: str, ports: int) -> None:
    """The synthetic sheet has no PoE row: add one for a model."""
    conn.execute(
        "INSERT OR IGNORE INTO attribute (canonical_key, category, name, value_type) "
        "VALUES ('interfaces.poe_enabled_interfaces', 'Interfaces', 'PoE Enabled Interfaces', "
        "'number')"
    )
    attr = conn.execute(
        "SELECT id FROM attribute WHERE canonical_key='interfaces.poe_enabled_interfaces'"
    ).fetchone()[0]
    mid = conn.execute("SELECT id FROM model WHERE name=?", (model,)).fetchone()[0]
    conn.execute(
        "INSERT INTO capacity_value (document_id, model_id, attribute_id, raw_value, kind, "
        "num_value) VALUES (1, ?, ?, ?, 'number', ?)",
        (mid, attr, str(ports), ports),
    )
    conn.commit()


def test_poe_needed_requires_poe_ports(sized_db):
    s = summary_as("PA-450R")
    base = SizingParams(include_superseded=True, max_size_factor=0)
    off = size(sized_db, s, base)
    assert off.recommended.model == "PA-3430" and off.need_poe is False
    on = size(sized_db, s, SizingParams(**{**base.__dict__, "need_poe": True}))
    assert on.recommended is None
    assert any("PoE: no PoE ports" in f for c in on.rejected for f in c.failures)
    _give_poe(sized_db, "PA-3430", 4)
    on = size(sized_db, s, SizingParams(**{**base.__dict__, "need_poe": True}))
    assert on.recommended.model == "PA-3430" and on.recommended.poe_ports == 4


def test_poe_in_use_forces_poe(sized_db):
    s = summary_as("PA-450R")
    s.poe = {"supported": True, "ports_in_use": ["ethernet1/3"], "parsed": True}
    r = size(sized_db, s, SizingParams(include_superseded=True, max_size_factor=0))
    assert r.need_poe is True
    assert any("PoE is in use on the current firewall" in n for n in r.notes)


def test_dedicated_poe_models_left_out_unless_needed():
    from tsf_sizer.sizing.engine import variants

    assert "PoE" in variants("PA-545-POE") and "PoE" not in variants("PA-1410")
