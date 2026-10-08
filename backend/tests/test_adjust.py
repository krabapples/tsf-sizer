from test_engine import _everyone, _map, summary_as
from test_engine import sized_db as _sized_db

from tsf_sizer.sizing.adjust import Applied, apply_actions, describe, resolve_class, resolve_metric
from tsf_sizer.sizing.engine import SizingParams, adjust_ports, is_optical, size

sized_db = _sized_db  # reuse the engine test fixture
METRICS = {
    "config.aggregate_interfaces": "Maximum aggregate interfaces",
    "feature.lre_routing": "Legacy Routing Engine (LRE) Support",
    "config.security_rules": "Security rulebase",
}
MODELS = {"PA-550": "PA-550", "PA-500": "PA-500", "PA-3430": "PA-3430", "PA-3400": "PA-3400"}


def run(*actions, params=None) -> Applied:
    return apply_actions(params or SizingParams(), list(actions), METRICS, MODELS)


def test_helpers():
    assert is_optical("10G_SFP+") and is_optical("40G_QSFP+") and not is_optical("1G_RJ45")
    assert not is_optical("1G_COMBO")
    assert resolve_class("10G SFP+") == "10G_SFP+" and resolve_class("Optics") == "optics"
    assert resolve_class("nonsense") is None
    assert resolve_metric("Maximum aggregate interfaces", METRICS) == "config.aggregate_interfaces"
    assert resolve_metric("aggregate_interfaces", METRICS) == "config.aggregate_interfaces"
    assert resolve_metric("LRE", METRICS) == "feature.lre_routing"
    assert resolve_metric("quantum", METRICS) is None
    base = {"1G_RJ45": 4, "1G_SFP": 8, "10G_SFP+": 2}
    assert adjust_ports(base, ["optics"], {})[0] == {"1G_RJ45": 4}
    assert adjust_ports(base, [], {"10G_SFP+": 2})[0]["10G_SFP+"] == 4
    assert adjust_ports(base, ["1G_SFP"], {})[0] == {"1G_RJ45": 4, "10G_SFP+": 2}


def test_set_validates_ranges_and_types():
    r = run({"action": "set", "name": "growth_pct", "value": "35"},
            {"action": "set", "name": "years", "value": 5.0},
            {"action": "set", "name": "need_poe", "value": True},
            {"action": "set", "name": "port_rule", "value": "used"},
            {"action": "set", "name": "peak_throughput_mbps", "value": "1,200"})  # fmt: skip
    p = r.params
    assert (p.growth_pct_per_year, p.years, p.need_poe, p.port_rule) == (35, 5, True, "used")
    assert p.peak_throughput_mbps == 1200 and not r.errors
    bad = run({"action": "set", "name": "growth_pct", "value": 900},
              {"action": "set", "name": "colour", "value": 1},
              {"action": "set", "name": "years", "value": "many"},
              {"action": "explode"}, "not a dict")  # fmt: skip
    assert len(bad.errors) == 5 and not bad.changed
    assert run({"action": "set", "name": "peak_cps", "value": None},
               params=SizingParams(peak_cps=5000)).params.peak_cps is None  # fmt: skip


def test_ports_drop_add_keep_and_reset():
    r = run({"action": "ports", "drop": ["optics"]})
    assert r.params.port_drop == ["optics"]
    # asking for optics again lifts "no optics"
    r2 = run({"action": "ports", "add": {"10G_SFP+": 2}}, params=r.params)
    assert r2.params.port_drop == [] and r2.params.port_add == {"10G_SFP+": 2}
    # dropping optics removes optical extras that were asked for before
    r3 = run({"action": "ports", "drop": ["optics"]}, params=r2.params)
    assert r3.params.port_add == {} and r3.params.port_drop == ["optics"]
    r4 = run({"action": "ports", "keep": ["optics"]}, params=r3.params)
    assert r4.params.port_drop == []
    r5 = run({"action": "ports", "add": {"1G_RJ45": 4}}, {"action": "ports", "reset": True})
    assert r5.params.port_add == {} and "port adjustments cleared" in r5.changes
    bad = run({"action": "ports", "drop": ["warp"], "add": {"10G_SFP+": 500}})
    assert len(bad.errors) == 2 or len(bad.errors) == 1 and not bad.changed


def test_ignore_exclude_and_unknowns():
    r = run({"action": "ignore", "metric": "Maximum aggregate interfaces"},
            {"action": "exclude", "models": ["pa-3400", "variant:5G"]},
            {"action": "exclude", "models": ["PA-9999"]},
            {"action": "ignore", "metric": "warp drive"},
            {"action": "explain", "model": "PA-550"})  # fmt: skip
    assert r.params.soft_metrics == ["config.aggregate_interfaces"]
    assert r.params.exclude_models == ["PA-3400", "VARIANT:5G"]
    assert r.explain == ["PA-550"] and len(r.errors) == 2
    back = run({"action": "enforce", "metric": "config.aggregate_interfaces"},
               {"action": "include", "models": ["PA-3400"]}, params=r.params)  # fmt: skip
    assert back.params.soft_metrics == [] and back.params.exclude_models == ["VARIANT:5G"]
    cleared = run({"action": "reset"}, params=r.params)
    assert not cleared.params.adjusted and cleared.changes == ["all adjustments cleared"]
    assert describe(r.params, METRICS) == [
        "Maximum aggregate interfaces: not blocking",
        "PA-3400 left out",
        "variant:5g left out",
    ]


def test_input_params_are_not_mutated():
    p = SizingParams()
    run({"action": "ports", "drop": ["optics"]}, {"action": "ignore", "metric": "LRE"}, params=p)
    assert not p.adjusted


# ---------------------------------------------------------------- the engine honours them


def test_engine_drops_and_adds_ports(sized_db):
    s = summary_as("PA-450R")  # the synthetic sheet gives the PA-3430 8 copper ports
    base = size(sized_db, s, SizingParams(include_superseded=True, max_size_factor=0))
    assert base.ports_needed["baseline"] == {"1G_RJ45": 8}
    more = size(
        sized_db,
        s,
        SizingParams(include_superseded=True, max_size_factor=0, port_add={"10G_SFP+": 2}),
    )
    assert more.ports_needed["baseline"] == {"1G_RJ45": 8, "10G_SFP+": 2}
    assert any("Ports adjusted" in n for n in more.notes)
    assert more.recommended.model == "PA-3430"  # 10 SFP+ cages
    assert any("10G_SFP+" in f for c in more.rejected for f in c.failures if c.model == "PA-540")
    too_many = size(
        sized_db,
        s,
        SizingParams(include_superseded=True, max_size_factor=0, port_add={"10G_SFP+": 30}),
    )
    assert too_many.recommended is None
    # Customer moves to copper: nothing optical is needed any more.
    dropped = size(sized_db, s, SizingParams(include_superseded=True, port_drop=["optics"]))
    assert dropped.ports_needed["baseline"] == {"1G_RJ45": 8}


def test_engine_excludes_models_families_and_variants(sized_db):
    s = summary_as("PA-450R")
    kw = dict(include_superseded=True, max_size_factor=0)
    everyone = set(_everyone(size(sized_db, s, SizingParams(**kw))))
    assert "PA-3430" in everyone and "PA-540" in everyone
    p = SizingParams(**kw, exclude_models=["PA-3430", "PA-500", "VARIANT:5G"])
    left = set(_everyone(size(sized_db, s, p)))
    assert not left & {"PA-3430", "PA-540", "PA-520"} and not any("-5G" in m for m in left)


def test_engine_user_soft_metric_flags_instead_of_excluding(sized_db):
    _map(sized_db, "config.aggregate_interfaces", "Maximum aggregate interfaces",
         {"PA-450R": 6, "PA-3430": 4})  # fmt: skip
    from tsf_sizer.tsf.metrics import Metric

    s = summary_as("PA-450R", ports=[])
    s.metrics["config.aggregate_interfaces"] = Metric(1, "count", "config")
    kw = dict(include_superseded=True, max_size_factor=0, port_rule="used")
    hard = _everyone(size(sized_db, s, SizingParams(**kw)))["PA-3430"]
    assert any("aggregate" in f.lower() for f in hard.failures)
    soft = size(sized_db, s, SizingParams(**kw, soft_metrics=["config.aggregate_interfaces"]))
    cand = _everyone(soft)["PA-3430"]
    assert not any("aggregate" in f.lower() for f in cand.failures)
    assert any("Not blocking at your request" in n for n in cand.notices)
