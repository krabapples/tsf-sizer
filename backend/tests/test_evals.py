import json
from pathlib import Path

from fake_gateway import text_reply, tool_reply

from tsf_sizer.agent import config, evals

CASE = json.loads((Path(__file__).parent.parent / "evals" / "synthetic_pa3430.json").read_text())


def fit_id(body):
    for m in reversed(body["messages"]):
        if m["role"] == "tool":
            p = json.loads(m["content"])
            if isinstance(p, dict) and p.get("model") == "PA-5550":
                return p["result_id"]


def good(body, gw):
    n = gw.calls
    if n == 1:
        return tool_reply(
            ("ask_user", {"question": "Like for like?", "options": ["Like for like"]})
        )
    if n == 2:
        return tool_reply(("check_fit", {"model": "PA-5550", "port_needs": {}}))
    return tool_reply(
        (
            "submit_verdict",
            {
                "recommendation": {
                    "model": "PA-5550",
                    "reason": "Fits.",
                    "result_ids": [fit_id(body)],
                },
                "open_points": ["The throughput is a snapshot, not a peak."],
                "summary": "PA-5550.",
            },
        )
    )


def cfg(gateway):
    return config.from_env(
        {
            "TSF_SIZER_LLM_MODE": "direct",
            "TSF_SIZER_DIRECT_URL": gateway.url,
            "TSF_SIZER_DIRECT_MODEL": "m",
        }
    )


def test_a_good_run_passes_every_check(gateway, workbook):
    gateway.script = good
    r = evals.run_case(CASE, workbook, cfg(gateway))
    assert r.passed, (r.checks, r.notes)
    assert set(r.checks) == {
        "verdict reached",
        "acceptable model",
        "every figure verified",
        "anomalies mentioned",
        "step budget",
        "nothing identifying sent",
    }


def test_wrong_model_and_missing_anomaly_fail(gateway, workbook):
    gateway.script = good
    case = {**CASE, "expect": {"models_any_of": ["PA-7500"], "keywords_any": ["kernel panic"]}}
    r = evals.run_case(case, workbook, cfg(gateway))
    assert not r.passed
    assert not r.checks["acceptable model"] and not r.checks["anomalies mentioned"]
    assert r.checks["verdict reached"] and r.checks["every figure verified"]


def test_no_verdict_and_gateway_failure_are_reported(gateway, workbook):
    gateway.script = [text_reply("I am not sure.")]
    r = evals.run_case(CASE, workbook, cfg(gateway))
    assert not r.checks["verdict reached"] and not r.passed
    gateway.script = [(401, {"error": "bad key"})]
    r = evals.run_case(CASE, workbook, cfg(gateway))
    assert not r.passed and any("run failed" in n for n in r.notes)


def test_an_identifier_in_a_request_is_caught(gateway, workbook):
    gateway.script = good
    case = json.loads(json.dumps(CASE))
    case["answers"] = ["Like for like, mail ops@corp.example.com"]
    # the filter masks it, so nothing leaks: the check passes
    assert evals.run_case(case, workbook, cfg(gateway)).checks["nothing identifying sent"]
    # a client that sends the raw text (filter bypassed) is caught
    assert evals.LEAK.search("see 10.1.2.3 and a@b.com")


def test_command_line(gateway, workbook, tmp_path, monkeypatch, capsys):
    (tmp_path / "case.json").write_text(json.dumps(CASE))
    gateway.script = good
    for k, v in {
        "TSF_SIZER_LLM_MODE": "direct",
        "TSF_SIZER_DIRECT_URL": gateway.url,
        "TSF_SIZER_DIRECT_MODEL": "m",
    }.items():
        monkeypatch.setenv(k, v)
    code = evals.main(["--workbook", str(workbook), "--cases", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and "PASS  synthetic" in out and "1/1 cases passed" in out
    monkeypatch.setenv("TSF_SIZER_LLM_MODE", "off")
    assert evals.main(["--workbook", str(workbook), "--cases", str(tmp_path)]) == 2
