import pytest

from tsf_sizer import db
from tsf_sizer.agent import ledger, verifier
from tsf_sizer.agent.tools import Toolbox


@pytest.fixture
def box(analysis):
    path, aid = analysis
    conn = db.connect(path)
    yield Toolbox(conn, aid)
    conn.close()


def fit(box, model, **args):
    res = box.call("check_fit", {"model": model, "port_needs": {}, **args})
    assert res["ok"], res
    return res["id"], res["payload"]


def verdict(model="PA-5550", reason="Fits.", ids=None, **extra):
    return {
        "recommendation": {"model": model, "reason": reason, "result_ids": ids or []},
        "summary": extra.pop("summary", "Done."),
        **extra,
    }


def problems(box, v):
    return verifier.verify(box.conn, 1, v)


def test_a_checked_model_with_real_figures_passes(box):
    rid, p = fit(box, "PA-5550")
    assert p["meets_all"]
    v = verdict(
        reason=f"PA-5550 gives 120 Gbps of threat throughput, 95.1% headroom ({rid}).",
        ids=[rid],
        summary="It handles 1.67M connections per second and 120,000 Mbps; 95% headroom.",
    )
    assert problems(box, v) == []


def test_unchecked_model_is_a_problem(box):
    msgs = problems(box, verdict("PA-7500"))
    assert any("PA-7500 was never checked" in m for m in msgs)


def test_recommending_a_model_that_fails_is_a_problem(box):
    fit(box, "PA-540", mode="usage_only")
    msgs = problems(box, verdict("PA-540"))
    assert any("does not meet every requirement" in m for m in msgs)


def test_no_recommendation_is_allowed_with_a_reason(box):
    assert problems(box, verdict(None, reason="Nothing fits the ports.")) == []
    assert any("needs a reason" in m for m in problems(box, verdict(None, reason="")))


def test_invented_figures_are_found_in_every_text_field(box):
    rid, _ = fit(box, "PA-5550")
    v = verdict(
        reason="It offers 250 Gbps.",
        ids=[rid],
        summary="About 4.1× the current model.",
        alternatives=[],
        open_points=["Check the 77% CPU peak"],
        confidence={"level": "high", "reason": "Because 3,333 limits pass."},
    )
    text = " ".join(problems(box, v))
    for fig in ("250", "4.1×", "77%", "3,333"):
        assert fig in text, text


def test_figures_match_across_units_and_rounding(box):
    rid, _ = fit(box, "PA-5550")
    v = verdict(
        reason="Throughput is 120 Gbps = 120,000 Mbps = 0.12 Tbps, cps 1.67M, security rules "
        "65K, headroom 95%.",
        ids=[rid],
    )
    assert problems(box, v) == []


def test_ignored_in_prose(box):
    rid, _ = fit(box, "PA-5550")
    v = verdict(
        reason="PA-5550 (r1) on PAN-OS 12.1.2 in 2026, one of 3 candidates, 2 ports, 12 limits.",
        ids=[rid],
    )
    assert problems(box, v) == []


def test_a_cited_result_must_exist_and_be_about_that_model(box):
    rid, _ = fit(box, "PA-5550")
    other, _ = fit(box, "PA-540", mode="usage_only")
    msgs = problems(box, verdict(ids=[other]))
    assert any(f"{other} is a check of PA-540, not PA-5550" in m for m in msgs)
    msgs = problems(box, verdict(ids=["r999"]))
    assert any("r999 is not a check_fit result" in m for m in msgs)
    evidence = box.call("get_evidence", {"topic": "ha"})["id"]
    assert any(evidence in m for m in problems(box, verdict(ids=[evidence])))
    assert problems(box, verdict(ids=[rid])) == []


def test_a_check_with_other_assumptions_than_the_ledger_is_stale(box):
    fit(box, "PA-5550", growth_pct=50)
    msgs = problems(box, verdict())
    assert any("other growth than the ledger" in m for m in msgs)
    ledger.set_value(box.conn, 1, "growth_pct", 50, "agent")
    assert problems(box, verdict()) == []
    ledger.set_value(box.conn, 1, "fit_mode", "usage_only", "agent")
    assert any("fit mode" in m for m in problems(box, verdict()))


def test_alternatives_and_rejected_need_checks(box):
    rid, _ = fit(box, "PA-5550")
    fit(box, "PA-540", mode="usage_only")
    v = verdict(
        ids=[rid],
        alternatives=[{"model": "PA-540", "when_to_prefer": "If cost matters."}],
        rejected=[{"model": "PA-7500", "reason": "Too big."}],
    )
    msgs = problems(box, v)
    assert any("alternative PA-540 does not meet" in m for m in msgs)
    assert any("PA-7500 was never checked" in m for m in msgs)


def test_what_if_results_count_as_checks(box):
    base, _ = fit(box, "PA-5550")
    box.call("what_if", {"base_result_id": base, "changes": {"mode": "usage_only"}})
    ledger.set_value(box.conn, 1, "fit_mode", "usage_only", "agent")
    assert problems(box, verdict()) == []


@pytest.mark.parametrize(
    "text,bad",
    [
        ("12 limits", []),
        ("2026", []),
        ("PA-3430 and VM-300", []),
        ("PAN-OS 11.1.13-h9", []),
        ("1,071 rules", ["1,071"]),
        ("0.5 Gbps", ["0.5 Gbps"]),
        ("65%", ["65%"]),
    ],
)
def test_figure_extraction(text, bad):
    assert verifier.unmatched_figures(text, {9.0, 120.0}) == bad


def test_known_numbers_include_ledger_values(box):
    ledger.set_value(box.conn, 1, "growth_pct", 33, "agent")
    box.call("get_evidence", {"topic": "overview"})
    known = verifier.known_numbers(box.conn, 1)
    assert 33.0 in known and 45.1 in known  # ledger value, uptime from the evidence
