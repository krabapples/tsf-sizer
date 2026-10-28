import json

import pytest
from fake_gateway import text_reply, tool_reply
from test_web import client as _web_client
from test_web import wait_done

from tsf_sizer import db
from tsf_sizer.agent import ledger, runtime
from tsf_sizer.agent.tools import Toolbox
from tsf_sizer.tsf import manual

client = _web_client  # the web test fixture: an app with the synthetic workbook imported

FORM = {
    "model": "pa-3430",
    "ha_mode": "active-passive",
    "throughput_mbps": "2,400",
    "throughput_kind": "peak",
    "cps": "9000",
    "sessions": "400000",
    "ports_10G_SFP+": "4",
    "ports_100G_QSFP28": "2",
    "security_rules": "220",
    "address_objects": "995",
}


@pytest.fixture
def conn(tmp_path, workbook):
    from tsf_sizer.portfolio.importer import import_workbook

    c = db.connect(tmp_path / "m.db")
    import_workbook(c, workbook, activate=True)
    yield c
    c.close()


def post(c, **extra):
    return c.post("/analyses/manual", data={**FORM, **extra}, follow_redirects=False)


def test_summary_is_built_from_entered_figures_only(conn):
    s = manual.build_summary(conn, FORM)
    assert (s.model, s.family, s.ha["mode"]) == ("PA-3430", "PA-3400", "active-passive")
    m = s.metrics
    assert m["perf.throughput_snapshot_mbps"].value == 2400
    assert m["perf.throughput_snapshot_mbps"].kind == "peak"
    assert m["perf.throughput_snapshot_mbps"].source == manual.SOURCE
    assert m["perf.sessions"].value == 400000 and m["config.security_rules"].value == 220
    assert m["feature.ha_active_passive"].value is True
    assert m["feature.ha_active_active"].value is False
    assert s.port_counts == {"10G_SFP+": 4, "100G_QSFP28": 2} and len(s.ports) == 6
    assert "perf.cps_snapshot" in m and "config.nat_rules" not in m  # nothing is invented
    assert s.warnings and "No Tech Support File" in s.warnings[0]
    assert s.licenses_active == [] and s.resource_history == {} and s.uptime_days is None


def test_only_the_model_is_required(conn):
    s = manual.build_summary(conn, {"model": "PA-540"})
    assert s.model == "PA-540" and s.ports == [] and s.ha == {}
    assert manual.missing(s) == [
        "throughput_mbps", "sessions", "cps", "ports", "ha_mode", "security_rules"
    ]  # fmt: skip


@pytest.mark.parametrize(
    "data,message",
    [
        ({}, "Enter the current firewall model"),
        ({"model": "PA-9999"}, "unknown model"),
        ({"model": "PA-540", "throughput_mbps": "fast"}, "not a number"),
        ({"model": "PA-540", "cps": "-5"}, "between 0"),
        ({"model": "PA-540", "ports_warp": "2"}, "unknown port type"),
        ({"model": "PA-540", "ports_1G_RJ45": "999"}, "between 0 and 64"),
        ({"model": "PA-540", "ha_mode": "triple"}, "ha_mode must be"),
        ({"model": "PA-540", "sizing_basis": "vibes"}, "sizing_basis must be"),
        ({"model": "PA-540", "throughput_mbps": "5", "throughput_kind": "wish"}, "kind must be"),
    ],
)
def test_bad_input_is_refused_with_a_reason(conn, data, message):
    with pytest.raises(ValueError, match=message):
        manual.build_summary(conn, data)


def test_the_throughput_note_says_it_was_entered(client):
    r = post(client, throughput_kind="estimate")
    aid = int(r.headers["location"].rsplit("/", 1)[1])
    notes = client.get(f"/analyses/{aid}/report.json").json()["sizing"]["notes"]
    assert any("estimate value entered by the engineer" in n for n in notes)
    r = post(client, throughput_kind="peak")
    aid = int(r.headers["location"].rsplit("/", 1)[1])
    notes = client.get(f"/analyses/{aid}/report.json").json()["sizing"]["notes"]
    assert not any("entered by the engineer" in n for n in notes)  # a peak needs no warning


def test_a_manual_analysis_is_sized_and_shown_like_any_other(client):
    r = post(client, customer="ACME", goal="usage_only", notes="Branch office", growth_pct="30")
    assert r.status_code == 303
    aid = int(r.headers["location"].rsplit("/", 1)[1])
    assert wait_done(client, aid)["status"] == "done"
    data = client.get(f"/analyses/{aid}/report.json").json()
    assert data["input_mode"] == "manual" and data["source"] == "Manual input"
    assert data["summary"]["model"] == "PA-3430"
    assert data["sizing"]["params"]["right_size"] is True
    assert data["sizing"]["params"]["growth_pct_per_year"] == 30
    page = client.get(f"/analyses/{aid}").text
    assert "Replacement for PA-3430" in page and "ACME" in page
    assert "No Tech Support File: every figure was entered" in page
    assert "Manual input" in page
    conn = db.connect(client.app_state.state.settings.db_path)
    led = ledger.effective(conn, aid)
    assert led["growth_pct"]["source"] == "form" and led["fit_mode"]["value"] == "usage_only"
    row = conn.execute("SELECT filename, status FROM analysis WHERE id=?", (aid,)).fetchone()
    assert (row["filename"], row["status"]) == ("Manual input", "done")


def test_bad_manual_input_returns_400_and_creates_nothing(client):
    for extra in ({"model": ""}, {"model": "PA-9999"}, {"cps": "x"}, {"notes": "n" * 1001},
                  {"growth_pct": "abc"}, {"ports_warp": "1"}):  # fmt: skip
        assert post(client, **extra).status_code == 400, extra
    conn = db.connect(client.app_state.state.settings.db_path)
    assert conn.execute("SELECT count(*) FROM analysis").fetchone()[0] == 0


def test_the_index_offers_both_modes(client):
    page = client.get("/").text
    assert "I have a Tech Support File" in page and "No file: I enter what I know" in page
    assert 'action="/analyses/manual"' in page and 'name="model"' in page
    assert 'name="ports_10G_SFP+"' in page and 'name="throughput_mbps"' in page
    assert 'id="tsf-growth_pct"' in page and 'id="m-growth_pct"' in page  # no duplicate ids
    assert page.count('name="peak_throughput_mbps"') == 1  # the manual form has its own load
    assert "PA-3430" in page  # the model list


def test_the_agent_gets_the_manual_evidence_and_can_add_to_it(client, llm_env):
    def script(body, gw):
        n = gw.calls
        if n == 1:
            return tool_reply(("get_evidence", {"topic": "overview"}))
        if n == 2:
            ev = json.loads(
                next(m for m in reversed(body["messages"]) if m["role"] == "tool")["content"]
            )
            assert ev["overview"]["input_mode"] == "manual"
            assert "not_provided" in ev["overview"]
            return tool_reply(
                (
                    "record_evidence",
                    {"field": "throughput_mbps", "value": 3100, "kind": "peak", "note": "from NMS"},
                ),
                ("record_evidence", {"field": "nat_rules", "value": 40}),
                ("record_evidence", {"field": "ports", "value": {"1G_RJ45": 8}}),
            )
        return text_reply("Recorded.")

    llm_env.script = script
    r = post(client, notes="No file available", throughput_mbps="")
    aid = int(r.headers["location"].rsplit("/", 1)[1])
    import time

    for _ in range(100):
        run = client.get(f"/api/analyses/{aid}/agent").json()["run"]
        if run and run["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert run["status"] == "done", run
    system = llm_env.requests[0]["body"]["messages"][0]["content"]
    assert 'input_mode is "manual"' in system and "Without a Tech Support File" in system
    assert "No file available" in llm_env.requests[0]["body"]["messages"][1]["content"]
    data = client.get(f"/analyses/{aid}/report.json").json()
    m = data["summary"]["metrics"]
    assert m["perf.throughput_snapshot_mbps"]["value"] == 3100
    assert m["perf.throughput_snapshot_mbps"]["kind"] == "peak"
    assert m["perf.throughput_snapshot_mbps"]["note"] == "from NMS"
    assert m["config.nat_rules"]["value"] == 40
    assert data["summary"]["port_counts"] == {"1G_RJ45": 8}
    # the quick result was sized again with the new figures
    reqs = {r["metric"]: r for r in data["sizing"]["requirements"]}
    assert reqs["perf.throughput_threat_gbps"]["observed"] == 3.1
    page = client.get(f"/analyses/{aid}").text
    assert (
        "Evidence: Throughput 3100 (peak)" in page and "Evidence: ports in use: 8x 1G_RJ45" in page
    )


def test_evidence_cannot_be_changed_on_a_tsf_analysis(analysis):
    path, aid = analysis
    conn = db.connect(path)
    box = Toolbox(conn, aid)
    res = box.record_evidence("throughput_mbps", 5000)
    assert not res["ok"] and "made from a Tech Support File" in res["error"]
    assert (
        box.call("get_evidence", {"topic": "overview"})["payload"]["overview"]["input_mode"]
        == "tsf"
    )
    conn.close()


def test_the_tool_validates_what_it_records(client, llm_env):
    aid = int(post(client).headers["location"].rsplit("/", 1)[1])
    conn = db.connect(client.app_state.state.settings.db_path)
    box = Toolbox(conn, aid)
    for field, value, text in [
        ("throughput_mbps", "lots", "not a number"),
        ("cps", -1, "between 0"),
        ("ha_mode", "mesh", "ha_mode must be"),
        ("model", "PA-0", "unknown model"),
        ("colour", 1, "unknown field"),
    ]:
        res = box.record_evidence(field, value)
        assert not res["ok"] and text in res["error"], (field, res)
    ok = box.record_evidence("model", "PA-540")
    assert ok["ok"] and box.current == "PA-540"
    assert runtime.SPEC_BY_NAME["record_evidence"]["parameters"]["required"] == ["field", "value"]
    conn.close()
