import json

import pytest
from fake_gateway import last_tool_result, text_reply, tool_reply

from tsf_sizer import db
from tsf_sizer.agent import config, ledger, runtime


def run(analysis, text=None, cfg=None):
    path, aid = analysis
    conn = db.connect(path)
    run_id = runtime.start_run(conn, aid, text)
    conn.close()
    runtime.run_agent(path, aid, run_id, cfg=cfg)
    return run_id


def rows(analysis, sql, *args):
    conn = db.connect(analysis[0])
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def run_row(analysis, run_id):
    return rows(analysis, "SELECT * FROM agent_run WHERE id=?", run_id)[0]


def messages(analysis):
    out = rows(analysis, "SELECT * FROM agent_message ORDER BY id")
    for m in out:
        m["data"] = json.loads(m["data_json"] or "null") or {}
    return out


def fit_id(body, model="PA-5550"):
    """The result id of the newest check_fit of `model` in the conversation so far."""
    for m in reversed(body["messages"]):
        if m["role"] == "tool":
            p = json.loads(m["content"])
            if isinstance(p, dict) and p.get("model") == model:
                return p["result_id"]
    raise AssertionError("no check_fit result in the conversation")


def verdict_args(rid, reason="PA-5550 gives 120 Gbps of threat throughput."):
    return {
        "recommendation": {"model": "PA-5550", "reason": reason, "result_ids": [rid]},
        "alternatives": [],
        "open_points": ["Confirm the peak throughput with the customer."],
        "confidence": {"level": "medium", "reason": "The throughput is a snapshot."},
        "summary": "PA-5550 covers the usage.",
    }


def happy_script(body, gw):
    n = gw.calls
    if n == 1:
        return tool_reply(("get_evidence", {"topic": "overview"}))
    if n == 2:
        return tool_reply(
            (
                "record_assumption",
                {"key": "growth_pct", "value": 10, "reason": "Customer said 10%"},
            ),
            ("check_fit", {"model": "PA-5550", "port_needs": {}}),
        )
    if n == 3:
        return tool_reply(("submit_verdict", verdict_args(fit_id(body))))
    return text_reply("unexpected")


def test_a_full_run_ends_in_a_verified_verdict(analysis, llm_env):
    llm_env.script = happy_script
    rid = run(analysis, "Wait: " + "mail ops@corp.example.com about 10.9.8.7 please")
    r = run_row(analysis, rid)
    assert (r["status"], r["outcome"], r["steps"], r["mode"], r["model"]) == (
        "done",
        "verdict",
        4,
        "gateway",
        "fake-model",
    )
    v = rows(analysis, "SELECT * FROM verdict")[0]
    assert v["version"] == 1 and v["verified"] == 1 and v["problems_json"] is None
    assert json.loads(v["ledger_json"])["growth_pct"] == 10
    kinds = [(m["role"], m["kind"]) for m in messages(analysis)]
    assert kinds[0] == ("user", "text") and kinds[-1] == ("assistant", "verdict")
    steps = [m["data"]["summary"] for m in messages(analysis) if m["role"] == "tool"]
    assert steps[0] == "Read the evidence: overview"
    assert steps[1] == "Ledger: growth_pct = 10"
    assert steps[2].startswith("Checked PA-5550 (match_or_beat)")
    assert steps[3] == "Verdict saved (version 1)"
    # the ledger changed: source agent, the quick result was sized again
    e = ledger.effective(db.connect(analysis[0]), 1)["growth_pct"]
    assert (e["value"], e["source"]) == (10, "agent")
    stored = json.loads(rows(analysis, "SELECT result_json FROM analysis")[0]["result_json"])
    assert stored["sizing"]["params"]["growth_pct_per_year"] == 10


def test_what_is_sent_to_the_model(analysis, llm_env):
    llm_env.script = happy_script
    run(analysis, "Wait: " + "mail ops@corp.example.com about 10.9.8.7 please")
    first = llm_env.requests[0]
    assert first["headers"]["authorization"] == "Bearer secret-key"
    system = first["body"]["messages"][0]["content"]
    assert system.startswith("You are the sizing agent")
    assert "PLAYBOOK" in system and "match_or_beat" in system and '"growth_pct": 20.0' in system
    names = {t["function"]["name"] for t in first["body"]["tools"]}
    assert names == {
        "get_evidence", "list_models", "get_model", "compare_models", "compute_requirements",
        "check_fit", "what_if", "baseline_run", "ask_user", "record_assumption", "record_evidence",
        "list_documents", "read_document", "submit_verdict",
    }  # fmt: skip
    sent = json.dumps([r["body"] for r in llm_env.requests])
    assert "corp.example.com" not in sent and "10.9.8.7" not in sent
    assert "[email]" in sent and "[ip]" in sent
    assert "secret-key" not in sent
    # the ledger the model sees is updated within the run
    third = llm_env.requests[2]["body"]["messages"][0]["content"]
    assert '"growth_pct": 10' in third


def test_a_question_ends_the_run_and_the_answer_continues_it(analysis, llm_env):
    llm_env.script = [
        tool_reply(("get_evidence", {"topic": "overview"})),
        tool_reply(
            (
                "ask_user",
                {
                    "question": "Does the customer still need optics?",
                    "options": ["Yes", "No"],
                    "default": "Yes",
                },
            )
        ),
        text_reply("Thanks, I will keep the optics."),
    ]
    first = run(analysis)
    r = run_row(analysis, first)
    assert (r["status"], r["outcome"]) == ("waiting", "question")
    q = messages(analysis)[-1]
    assert (q["role"], q["kind"], q["content"]) == (
        "assistant",
        "question",
        "Does the customer still need optics?",
    )
    assert q["data"] == {"options": ["Yes", "No"], "default": "Yes"}

    second = run(analysis, "Yes, two 10G optics")
    assert run_row(analysis, second)["status"] == "done"
    history = llm_env.requests[-1]["body"]["messages"]
    texts = [m.get("content") for m in history if m["role"] in ("user", "assistant", "tool")]
    assert any(t and t.startswith("Question to the engineer: Does the customer") for t in texts)
    assert "Yes, two 10G optics" in texts
    # results of the earlier run are shortened to their step line; the ids stay valid
    old_tool = next(m for m in history if m["role"] == "tool")
    assert old_tool["content"].startswith("[r1] Read the evidence: overview")
    # every assistant tool call still has its tool message
    ids = {tc["id"] for m in history if m.get("tool_calls") for tc in m["tool_calls"]}
    assert ids == {m["tool_call_id"] for m in history if m["role"] == "tool"}


def test_the_verifier_sends_the_verdict_back_until_it_is_right(analysis, llm_env):
    def script(body, gw):
        n = gw.calls
        if n == 1:
            return tool_reply(("check_fit", {"model": "PA-5550", "port_needs": {}}))
        rid = fit_id(body)
        if n == 2:
            return tool_reply(("submit_verdict", verdict_args(rid, "It offers 250 Gbps.")))
        if n == 3:
            reply = last_tool_result(body)
            assert reply["accepted"] is False and "250" in " ".join(reply["problems"])
            return tool_reply(("submit_verdict", verdict_args(rid)))
        return text_reply("done")

    llm_env.script = script
    rid = run(analysis)
    assert run_row(analysis, rid)["outcome"] == "verdict"
    v = rows(analysis, "SELECT * FROM verdict")
    assert len(v) == 1 and v[0]["verified"] == 1  # the rejected attempt was never stored
    steps = [m["data"]["summary"] for m in messages(analysis) if m["role"] == "tool"]
    assert any("rejected by the verifier" in s for s in steps)


def test_a_verdict_that_stays_wrong_is_saved_with_its_unverified_claims(analysis, llm_env):
    def script(body, gw):
        if gw.calls == 1:
            return tool_reply(("check_fit", {"model": "PA-5550", "port_needs": {}}))
        return tool_reply(("submit_verdict", verdict_args(fit_id(body), "It offers 250 Gbps.")))

    llm_env.script = script
    rid = run(analysis)
    assert run_row(analysis, rid)["outcome"] == "verdict"
    v = rows(analysis, "SELECT * FROM verdict")[0]
    assert v["verified"] == 0 and "250" in v["problems_json"]
    last = messages(analysis)[-1]
    assert last["kind"] == "verdict" and last["data"]["verified"] is False


def test_verdict_versions_increase(analysis, llm_env):
    llm_env.script = happy_script
    run(analysis)
    llm_env.calls = 0
    llm_env.script = lambda body, gw: (
        tool_reply(("check_fit", {"model": "PA-5550", "port_needs": {}}))
        if gw.calls == 1
        else tool_reply(("submit_verdict", verdict_args(fit_id(body))))
    )
    run(analysis, "Check again")
    assert [v["version"] for v in rows(analysis, "SELECT version FROM verdict ORDER BY id")] == [
        1,
        2,
    ]


@pytest.mark.parametrize(
    "response,kind,words",
    [
        (
            (403, {"error": "Blocked by AI security profile"}, {"x-request-id": "blk-7"}),
            "blocked",
            "blk-7",
        ),
        ((401, {"error": "nope"}), "auth", "credentials"),
        ((200, b"<html>login</html>"), "bad_response", "could not use"),
    ],
)
def test_gateway_failures_stop_the_run_with_a_clear_message(
    analysis, llm_env, response, kind, words
):
    llm_env.script = [response]
    rid = run(analysis)
    r = run_row(analysis, rid)
    assert r["status"] == "error" and r["outcome"].startswith(kind)
    note = messages(analysis)[-1]
    assert (note["role"], note["kind"]) == ("notice", "error") and note["data"]["kind"] == kind
    assert words in note["content"] and "quick result is still available" in note["content"]
    assert llm_env.calls == 1  # nothing retried or rephrased


def test_unreachable_endpoint(analysis, llm_env, monkeypatch):
    monkeypatch.setenv("TSF_SIZER_GATEWAY_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("TSF_SIZER_GATEWAY_MAX_RETRIES", "0")
    rid = run(analysis)
    assert run_row(analysis, rid)["status"] == "error"
    assert "could not be reached" in messages(analysis)[-1]["content"]


def test_step_budget(analysis, llm_env, monkeypatch):
    monkeypatch.setenv("TSF_SIZER_AGENT_MAX_STEPS", "3")
    llm_env.script = lambda body, gw: tool_reply(("get_evidence", {"topic": "ha"}))
    rid = run(analysis)
    r = run_row(analysis, rid)
    assert (r["status"], r["outcome"], r["steps"]) == ("done", "step budget", 3)
    assert "step budget (3" in messages(analysis)[-1]["content"]


def test_json_tool_calls_when_the_gateway_has_no_tool_support(analysis, llm_env):
    llm_env.script = [
        (400, {"error": {"message": "this model does not support tools"}}),
        text_reply('{"tool": "get_evidence", "arguments": {"topic": "overview"}}'),
        text_reply("PA-3430 today. What do you want to change?"),
    ]
    rid = run(analysis)
    assert run_row(analysis, rid)["status"] == "done"
    notes = [m["content"] for m in messages(analysis) if m["role"] == "notice"]
    assert any("switched to JSON tool calls" in n for n in notes)
    assert any(m["role"] == "tool" for m in messages(analysis))
    assert "tools" not in llm_env.requests[-1]["body"]


def test_a_bad_tool_call_goes_back_to_the_model_which_can_recover(analysis, llm_env):
    llm_env.script = [
        tool_reply(("check_fit", {"model": "PA-9999"}), ("get_evidence", {"topic": "gossip"})),
        text_reply("Sorry, I used a wrong name."),
    ]
    rid = run(analysis)
    assert run_row(analysis, rid)["outcome"] == "answered"
    tool_msgs = [m for m in messages(analysis) if m["role"] == "tool"]
    assert [m["data"]["ok"] for m in tool_msgs] == [False, False]
    assert "unknown model" in json.loads(tool_msgs[0]["content"])["error"]
    assert "must be one of" in json.loads(tool_msgs[1]["content"])["error"]


def test_plain_text_answer_ends_the_run(analysis, llm_env):
    llm_env.script = [text_reply("The PA-3430 is at 15% of its throughput.")]
    rid = run(analysis, "How busy is the box?")
    assert run_row(analysis, rid)["outcome"] == "answered"
    assert messages(analysis)[-1]["content"].startswith("The PA-3430")


def test_ask_user_without_a_question_is_an_error_the_model_sees(analysis, llm_env):
    llm_env.script = [
        tool_reply(("ask_user", {"options": ["a"]})),
        text_reply("Which optics do you need?"),
    ]
    rid = run(analysis)
    assert run_row(analysis, rid)["status"] == "done"
    assert any("missing argument" in (m["content"] or "") for m in messages(analysis))


def test_no_llm_configured(analysis, monkeypatch):
    monkeypatch.setenv("TSF_SIZER_LLM_MODE", "off")
    rid = run(analysis)
    r = run_row(analysis, rid)
    assert r["status"] == "error" and r["outcome"] == "no LLM configured"
    assert "No LLM is configured" in messages(analysis)[-1]["content"]


def test_internal_errors_are_reported_not_raised(analysis, llm_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(runtime.Toolbox, "call", boom)
    llm_env.script = [tool_reply(("get_evidence", {"topic": "ha"}))]
    rid = run(analysis)
    assert run_row(analysis, rid)["status"] == "error"
    assert "disk on fire" in messages(analysis)[-1]["content"]


def test_large_tool_results_are_truncated(analysis, llm_env, monkeypatch):
    monkeypatch.setattr(runtime, "MAX_TOOL_CHARS", 200)
    llm_env.script = [tool_reply(("list_models", {})), text_reply("ok")]
    run(analysis)
    tool = next(m for m in llm_env.requests[1]["body"]["messages"] if m["role"] == "tool")
    assert tool["content"].endswith('"[truncated]"') and len(tool["content"]) < 260


def test_active_run_detection(analysis):
    path, aid = analysis
    conn = db.connect(path)
    assert runtime.active_run(conn, aid) is None
    rid = runtime.start_run(conn, aid, None)
    assert runtime.active_run(conn, aid)["id"] == rid
    conn.execute("UPDATE agent_run SET status='done'")
    assert runtime.active_run(conn, aid) is None
    conn.execute("UPDATE agent_run SET status='running', created_at=datetime('now','-2 hours')")
    assert runtime.active_run(conn, aid) is None  # lost long ago
    conn.close()


def test_config_object_can_be_passed_in(analysis, gateway):
    gateway.script = [text_reply("hi")]
    cfg = config.from_env(
        {
            "TSF_SIZER_LLM_MODE": "direct",
            "TSF_SIZER_DIRECT_URL": gateway.url,
            "TSF_SIZER_DIRECT_MODEL": "local",
        }
    )
    rid = run(analysis, "hello", cfg=cfg)
    assert run_row(analysis, rid)["status"] == "done"
    assert "authorization" not in gateway.requests[0]["headers"]  # no key configured
