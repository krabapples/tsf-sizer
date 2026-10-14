import json

import pytest

from tsf_sizer import db
from tsf_sizer.agent import ledger
from tsf_sizer.sizing.engine import SizingParams


@pytest.fixture
def c(tmp_path, workbook):
    from tsf_sizer.portfolio.importer import import_workbook

    conn = db.connect(tmp_path / "l.db")
    import_workbook(conn, workbook, activate=True)
    conn.execute(
        "INSERT INTO analysis (id, filename, status, params_json) VALUES (1, 'x', 'done', '{}')"
    )
    yield conn
    conn.close()


def test_defaults_when_nothing_recorded(c):
    eff = ledger.effective(c, 1)
    assert eff["growth_pct"] == {"value": 20.0, "source": "default", "reason": None, "id": None}
    assert eff["fit_mode"]["value"] == "match_or_beat"
    assert ledger.values(c, 1)["port_drop"] == []


def test_set_records_source_and_reason_and_ignores_no_change(c):
    ch = ledger.set_value(c, 1, "growth_pct", "40", "agent", "customer expects growth")
    assert ch == {"key": "growth_pct", "old": 20.0, "new": 40.0}
    assert ledger.set_value(c, 1, "growth_pct", 40, "agent") is None
    e = ledger.effective(c, 1)["growth_pct"]
    assert (e["value"], e["source"], e["reason"]) == (40.0, "agent", "customer expects growth")


@pytest.mark.parametrize(
    "key,value,message",
    [
        ("growth_pct", 500, "between"),
        ("growth_pct", "lots", "not a number"),
        ("years", 0, "between"),
        ("fit_mode", "whatever", "one of"),
        ("max_size_factor", 1, "above 1"),
        ("port_drop", ["warp drive"], "unknown port type"),
        ("port_add", {"10G_SFP+": 500}, "between 0 and 64"),
        ("port_add", {"optics": 2}, "unknown port type"),
        ("port_add", [1], "object"),
        ("exclude_models", ["PA-1234"], "unknown model"),
        ("exclude_models", ["variant:gold"], "unknown model variant"),
        ("only_models", ["variant:5G"], "unknown model variant"),
        ("colour", "red", "unknown assumption"),
    ],
)
def test_bad_values_are_refused_with_a_reason(c, key, value, message):
    with pytest.raises(ValueError, match=message):
        ledger.coerce(key, value, c)


def test_values_are_normalised(c):
    assert ledger.coerce("port_drop", ["optics", "10g sfp+"], c) == ["optics", "10G_SFP+"]
    assert ledger.coerce("port_add", {"1g rj45": "3", "10G_SFP+": 0}, c) == {"1G_RJ45": 3}
    assert ledger.coerce("exclude_models", ["pa-540", "variant:5g"], c) == ["PA-540", "VARIANT:5G"]
    assert ledger.coerce("only_models", ["PA-500"], c) == ["PA-500"]  # a family
    assert ledger.coerce("need_poe", "yes") is True and ledger.coerce("need_poe", "no") is False
    assert ledger.coerce("peak_cps", None) is None and ledger.coerce("peak_cps", "5,000") == 5000
    with pytest.raises(ValueError):
        ledger.coerce("growth_pct", None)


def test_undo_brings_back_the_previous_value_but_never_removes_form_values(c):
    ledger.set_value(c, 1, "years", 5, "form")
    ledger.set_value(c, 1, "years", 7, "agent")
    ledger.set_value(c, 1, "years", 9, "user")
    assert ledger.undo_last(c, 1) == {"key": "years", "undone": 9}
    assert ledger.values(c, 1)["years"] == 7
    ledger.undo_last(c, 1)
    assert ledger.values(c, 1)["years"] == 5
    assert ledger.undo_last(c, 1) is None  # the form's value stays
    assert ledger.values(c, 1)["years"] == 5
    hist = ledger.history(c, 1)
    assert [h["active"] for h in hist] == [False, False, True]


def test_params_round_trip(c):
    p = SizingParams(
        growth_pct_per_year=35,
        years=4,
        need_poe=True,
        right_size=True,
        only_models=["PA-500"],
        port_drop=["optics"],
        port_add={"10G_SFP+": 2},
        soft_metrics=["config.aggregate_interfaces"],
        exclude_models=["VARIANT:5G"],
        peak_cps=1234,
        max_size_factor=0,
    )
    ledger.seed(c, 1, p)
    back = ledger.to_params(ledger.values(c, 1))
    for field in (
        "growth_pct_per_year years need_poe right_size only_models port_drop port_add "
        "soft_metrics exclude_models peak_cps max_size_factor"
    ).split():
        assert getattr(back, field) == getattr(p, field), field
    assert ledger.from_params(SizingParams()) == {}


def test_values_are_stored_as_json(c):
    ledger.set_value(c, 1, "port_add", {"10G_SFP+": 2}, "agent")
    raw = c.execute("SELECT value_json FROM assumption").fetchone()[0]
    assert json.loads(raw) == {"10G_SFP+": 2}
