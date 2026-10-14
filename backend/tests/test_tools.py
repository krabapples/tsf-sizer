import json

import pytest

from tsf_sizer import db
from tsf_sizer.agent import ledger, tools
from tsf_sizer.agent.tools import Toolbox, ToolError

SMALL = {
    "mode": "usage_only",
    "port_rule": "used",
    "peak_throughput_mbps": 300,
    "peak_cps": 1000,
    "peak_sessions": 1000,
    "growth_pct": 0,
}


@pytest.fixture
def box(analysis):
    path, aid = analysis
    conn = db.connect(path)
    yield Toolbox(conn, aid, run_id=7)
    conn.close()


def ok(res):
    assert res["ok"], res.get("error")
    return res["payload"]


def test_every_call_is_stored_with_an_id(box):
    res = box.call("get_evidence", {"topic": "overview"})
    assert res["id"] == "r1" and res["payload"]["result_id"] == "r1"
    row = box.conn.execute("SELECT * FROM tool_result WHERE id=1").fetchone()
    assert (row["tool"], row["run_id"], row["error"]) == ("get_evidence", 7, None)
    assert json.loads(row["result_json"])["overview"]["model"] == "PA-3430"
    assert box.call("get_evidence", {"topic": "ha"})["id"] == "r2"


def test_errors_are_stored_and_returned_not_raised(box):
    for name, args, text in [
        ("get_evidence", {"topic": "gossip"}, "must be one of"),
        ("get_evidence", {}, "missing argument"),
        ("check_fit", {"model": "PA-540", "bogus": 1}, "unknown argument"),
        ("check_fit", {"model": "PA-9999"}, "unknown model"),
        ("check_fit", {"model": "PA-540", "growth_pct": "lots"}, "must be a number"),
        ("nonsense", {}, "unknown tool"),
        ("ask_user", {"question": "?"}, "unknown tool"),  # handled by the runtime, not a data tool
    ]:
        res = box.call(name, args)
        assert not res["ok"] and text in res["error"], (name, res)
    assert (
        box.conn.execute("SELECT count(*) FROM tool_result WHERE error IS NOT NULL").fetchone()[0]
        == 7
    )


def test_unknown_model_suggests_names(box):
    res = box.call("get_model", {"name": "PA-54"})
    assert "PA-540" in res["error"]


def test_evidence_topics(box):
    ev = ok(box.call("get_evidence", {"topic": "overview"}))["overview"]
    assert ev["model"] == "PA-3430" and ev["in_capacity_workbook"] is True
    usage = ok(box.call("get_evidence", {"topic": "usage"}))
    by = {r["metric"]: r for r in usage["usage"]}
    assert by["config.address_objects"]["value"] == 5
    assert by["config.address_objects"]["limit_on_current_model"] == 30000
    assert by["config.address_objects"]["label"] == "Max address entries"
    assert "ports" in ok(box.call("get_evidence", {"topic": "ports"}))
    assert "licenses" in ok(box.call("get_evidence", {"topic": "licenses"}))
    everything = ok(box.call("get_evidence", {"topic": "all"}))
    assert {"overview", "usage", "load_history", "licenses", "ports", "ha"} <= set(everything)


def test_evidence_does_not_carry_customer_names(box):
    box.conn.execute("UPDATE analysis SET customer='ACME Secret Corp'")
    dump = json.dumps(ok(box.call("get_evidence", {"topic": "all"})))
    assert "ACME" not in dump


def test_list_models_filters_and_marks_status(box):
    rows = ok(box.call("list_models", {}))["models"]
    names = [r["model"] for r in rows]
    assert names == sorted(
        names, key=lambda n: next(r for r in rows if r["model"] == n)["threat_gbps"]
    )
    assert "PA-5550" not in names  # not quotable
    assert "PA-5550" in [
        r["model"] for r in ok(box.call("list_models", {"include_unquotable": True}))["models"]
    ]
    old = {r["model"]: r for r in rows}["PA-450R"]
    assert old["superseded_by"] == "PA-500" and old["variants"] == ["rugged"]
    only = ok(box.call("list_models", {"family": "pa-500"}))["models"]
    assert [r["model"] for r in only] == ["PA-540"]
    big = ok(box.call("list_models", {"min_threat_gbps": 100}))["models"]
    assert [r["model"] for r in big] == ["PA-7500"]


def test_get_model_reports_no_data_instead_of_guessing(box):
    m = ok(box.call("get_model", {"name": "pa-540"}))
    assert m["model"] == "PA-540" and m["family"] == "PA-500"
    assert m["ports"] == {"1G_RJ45": 8}
    limits = {x["label"]: x for x in m["limits"]}
    assert limits["Security rulebase"]["value"] == 1500
    assert "Tunnel interfaces" in m["no_data"]
    assert m["source"].startswith("Capacity_Workbook_Test.xlsx")


def test_compare_models(box):
    c = ok(box.call("compare_models", {"models": ["PA-540", "PA-3430"], "metrics": ["security"]}))
    assert c["rows"][0]["values"] == {"PA-540": 1500.0, "PA-3430": 30000.0}
    assert not box.call("compare_models", {"models": []})["ok"]
    assert not box.call("compare_models", {"models": ["PA-540"] * 7})["ok"]


def test_requirements_use_the_ledger_and_overrides(box):
    base = ok(box.call("compute_requirements", {}))
    assert base["inputs"]["growth_pct"] == 20 and base["inputs"]["years"] == 3
    req = {r["metric"]: r for r in base["requirements"]}
    assert req["perf.throughput_threat_gbps"]["required"] == pytest.approx(5.925, abs=0.001)
    ledger.set_value(box.conn, 1, "growth_pct", 0, "agent")
    flat = ok(box.call("compute_requirements", {}))
    assert flat["inputs"]["growth_factor"] == 1
    peak = ok(box.call("compute_requirements", {"peak_throughput_mbps": 300}))
    r = {x["metric"]: x for x in peak["requirements"]}["perf.throughput_threat_gbps"]
    assert r["observed_basis"] == "entered peak" and r["observed"] == 0.3


def test_check_fit_applies_the_baseline_only_in_match_or_beat(box):
    strict = ok(box.call("check_fit", {"model": "PA-540", **SMALL, "mode": "match_or_beat"}))
    assert not strict["meets_all"]
    assert any("not more than current model" in f for f in strict["failures"])
    loose = ok(box.call("check_fit", {"model": "PA-540", **SMALL}))
    assert not any("current model" in f for f in loose["failures"])
    assert loose["counts"]["fail"] == 1  # the virtual systems the customer uses
    assert loose["usable_throughput_gbps"] == pytest.approx(0.84)
    assert loose["throughput_vs_current"] == 0.1


def test_check_fit_reports_ports_and_unknowns(box):
    fit = ok(box.call("check_fit", {"model": "PA-540", **SMALL}))
    assert any(f.startswith("Ports: missing") for f in fit["failures"])
    assert fit["ports"]["available"] == {"1G_RJ45": 8} and fit["ports"]["missing"]
    assert "Tunnel interfaces" in fit["no_data"] or "Concurrent sessions" in fit["no_data"]
    # dropping what the customer no longer needs changes the port result
    gone = ok(box.call("check_fit", {"model": "PA-540", **SMALL, "port_drop": ["optics"]}))
    assert "10G_SFP+" not in gone["ports"]["needed"]


def test_ignoring_a_limit_makes_it_a_notice_not_a_failure(box):
    fit = ok(
        box.call(
            "check_fit",
            {"model": "PA-540", **SMALL, "ignore_metrics": ["config.vsys"], "port_rule": "used"},
        )
    )
    vsys = next(x for x in fit["limits"] if x["metric"] == "config.vsys")
    assert vsys["status"] == "warn"
    assert not any("virtual systems" in f.lower() for f in fit["failures"])
    assert any("not blocking" in n for n in fit["notices"])


def test_poe_variant_is_a_notice_not_a_rejection(box):
    # PA-455R-5G is a special variant in the sample workbook: the tool says so, nothing more.
    fit = ok(box.call("check_fit", {"model": "PA-455R-5G", **SMALL}))
    assert any("special-purpose variant" in n for n in fit["notices"])
    assert fit["variants"] == ["5G", "rugged"]


def test_check_fit_can_reuse_a_stored_requirement_set(box):
    req = ok(box.call("compute_requirements", {"growth_pct": 0, "peak_throughput_mbps": 300}))
    fit = ok(box.call("check_fit", {"model": "PA-540", "requirements_id": req["result_id"]}))
    assert fit["requirement_inputs"]["growth_pct"] == 0
    bad = box.call("check_fit", {"model": "PA-540", "requirements_id": "r999"})
    assert not bad["ok"] and "no result" in bad["error"]
    evidence = box.call("get_evidence", {"topic": "ha"})
    bad = box.call("check_fit", {"model": "PA-540", "requirements_id": evidence["id"]})
    assert "not a compute_requirements" in bad["error"]


def test_what_if_shows_the_difference(box):
    base = box.call("check_fit", {"model": "PA-540", **SMALL, "mode": "match_or_beat"})
    wi = ok(box.call("what_if", {"base_result_id": base["id"], "changes": {"mode": "usage_only"}}))
    assert wi["meets_all"] == {"before": False, "after": False}
    assert any("current model" in f for f in wi["failures_removed"])
    assert wi["limits_changed"] and wi["result"]["mode"] == "usage_only"
    nope = box.call("what_if", {"base_result_id": base["id"], "changes": {"model": "PA-3430"}})
    assert not nope["ok"] and "cannot change model" in nope["error"]
    evidence = box.call("get_evidence", {"topic": "ha"})
    not_fit = box.call("what_if", {"base_result_id": evidence["id"], "changes": {"years": 5}})
    assert "not a check_fit" in not_fit["error"]
    grown = ok(box.call("what_if", {"base_result_id": base["id"], "changes": {"years": 9}}))
    assert grown["result"]["requirement_inputs"]["years"] == 9


def test_baseline_run_is_the_quick_result(box):
    b = ok(box.call("baseline_run", {"include_rejected": True}))
    assert "recommended" in b and "rules" in b and b["rejected_count"] >= 1
    assert b["rejected"] and "failures" in b["rejected"][0]


def test_record_assumption_validates_and_reports_changes(box):
    ch = box.record_assumption("growth_pct", 40, "customer said so")
    assert ch["ok"] and ch["payload"]["change"] == {"key": "growth_pct", "old": 20.0, "new": 40.0}
    again = box.record_assumption("growth_pct", 40, "same")
    assert again["payload"]["unchanged"] is True
    bad = box.record_assumption("growth_pct", 4000, "x")
    assert not bad["ok"] and "between" in bad["error"]
    assert ledger.values(box.conn, 1)["growth_pct"] == 40


def test_needs_an_active_workbook(tmp_path):
    conn = db.connect(tmp_path / "e.db")
    conn.execute(
        "INSERT INTO analysis (id, filename, status, params_json, result_json) "
        "VALUES (1, 'x', 'done', '{}', ?)",
        (
            json.dumps(
                {
                    "summary": {
                        "model": "PA-3430",
                        "family": "3400",
                        "panos": None,
                        "uptime_days": 1,
                        "ha": {},
                        "licenses_active": [],
                        "licenses_expired": [],
                        "sizing_basis": "threat_prevention",
                    }
                }
            ),
        ),
    )
    box = Toolbox(conn, 1)
    res = box.call("list_models", {})
    assert not res["ok"] and "No capacity workbook" in res["error"]
    assert box.call("get_evidence", {"topic": "overview"})["ok"]  # evidence needs no workbook
    with pytest.raises(ToolError):
        Toolbox(conn, 99)


def test_validation_helper_coerces_weak_model_output():
    spec = tools.SPEC_BY_NAME["check_fit"]["parameters"]
    out = tools.validate({"model": "PA-540", "years": "5", "growth_pct": "12.5"}, spec)
    assert out == {"model": "PA-540", "years": 5, "growth_pct": 12.5}
    out = tools.validate(
        {"base_result_id": "r1", "changes": {"x": 1}}, tools.SPEC_BY_NAME["what_if"]["parameters"]
    )
    assert out["changes"] == {"x": 1}
    flag = tools.SPEC_BY_NAME["list_models"]["parameters"]
    assert tools.validate({"include_unquotable": "yes"}, flag) == {"include_unquotable": True}


def test_summaries_for_step_cards(box):
    r = box.call("check_fit", {"model": "PA-540", **SMALL})
    line = tools.summarize("check_fit", r["args"], r["payload"])
    assert "PA-540" in line and "blocked" in line
    assert tools.summarize("check_fit", {}, None, "boom") == "check_fit: boom"
    e = box.call("get_evidence", {"topic": "usage"})
    assert tools.summarize("get_evidence", e["args"], e["payload"]) == "Read the evidence: usage"


def test_lre_is_ignored_by_the_tools(box):
    row = box.conn.execute("SELECT result_json FROM analysis WHERE id=1").fetchone()
    result = json.loads(row["result_json"])
    result["summary"]["metrics"]["feature.lre_routing"] = {
        "value": True, "kind": "count", "source": "old analysis", "unit": None, "note": None,
    }  # fmt: skip
    box.conn.execute("UPDATE analysis SET result_json=? WHERE id=1", (json.dumps(result),))
    fresh = Toolbox(box.conn, 1)
    ev = ok(fresh.call("get_evidence", {"topic": "usage"}))
    assert "lre_routing" not in ev["features_in_use"]
    fit = ok(fresh.call("check_fit", {"model": "PA-5550", "port_needs": {}}))
    assert fit["meets_all"] and not any("Routing" in x for x in fit["failures"] + fit["notices"])
