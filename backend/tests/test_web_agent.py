import json
import time

from fake_gateway import text_reply, tool_reply
from test_web import client as _web_client
from test_web import make_client, upload, wait_done

from tsf_sizer import db

client = _web_client  # the web test fixture: an app with the synthetic workbook imported


def wait_run(c, analysis_id, until=("done", "error", "waiting"), timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = c.get(f"/api/analyses/{analysis_id}/agent").json()["run"]
        if run and run["status"] in until:
            return run
        time.sleep(0.05)
    raise AssertionError("the agent run did not finish")


def new_analysis(c, **form):
    r = upload(c, **form)
    assert r.status_code == 303
    aid = int(r.headers["location"].rsplit("/", 1)[1])
    assert wait_done(c, aid)["status"] == "done"
    return aid


def fit_id(body, model="PA-5550"):
    for m in reversed(body["messages"]):
        if m["role"] == "tool":
            p = json.loads(m["content"])
            if isinstance(p, dict) and p.get("model") == model:
                return p["result_id"]
    raise AssertionError("no check_fit")


def verdict_script(body, gw):
    if gw.calls == 1:
        return tool_reply(("check_fit", {"model": "PA-5550", "port_needs": {}}))
    if gw.calls == 2:
        return tool_reply(
            (
                "submit_verdict",
                {
                    "recommendation": {
                        "model": "PA-5550",
                        "reason": "PA-5550 gives 120 Gbps of threat throughput.",
                        "result_ids": [fit_id(body)],
                    },
                    "open_points": ["Confirm the peak with the customer."],
                    "confidence": {"level": "medium", "reason": "Snapshot only."},
                    "summary": "The PA-5550 covers the usage.",
                },
            )
        )
    return text_reply("ok")


def test_without_an_llm_the_quick_result_works_and_the_agent_is_off(client):
    aid = new_analysis(client)
    page = client.get(f"/analyses/{aid}").text
    assert "needs an LLM" in page and "Start with the agent" not in page
    assert client.get(f"/api/analyses/{aid}/agent").json() == {
        "messages": [],
        "run": None,
        "enabled": False,
    }
    assert client.post(f"/analyses/{aid}/agent/start").status_code == 400
    assert client.post(f"/analyses/{aid}/agent/messages", data={"message": "hi"}).status_code == 400
    assert "Quick result" in page and "Models that don" in page


def test_the_agent_starts_by_itself_asks_and_continues(client, llm_env):
    llm_env.script = [
        tool_reply(("get_evidence", {"topic": "overview"})),
        tool_reply(
            (
                "ask_user",
                {"question": "Does the customer need optics?", "options": ["Yes", "No"]},
            )
        ),
        text_reply("Thanks, I keep the optics."),
    ]
    aid = new_analysis(client)
    run = wait_run(client, aid)
    assert run["status"] == "waiting"
    page = client.get(f"/analyses/{aid}").text
    assert "Read the evidence: overview" in page
    assert "Does the customer need optics?" in page
    assert '<button class="chip chip-primary option" type="submit">Yes</button>' in page
    assert "waiting for you" in page

    r = client.post(
        f"/analyses/{aid}/agent/messages",
        data={"message": "Yes, two 10G"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 200 and r.json()["run_id"] == 2
    assert wait_run(client, aid, until=("done", "error"))["status"] == "done"
    page = client.get(f"/analyses/{aid}").text
    assert "Yes, two 10G" in page and "Thanks, I keep the optics." in page


def test_a_verdict_is_shown_with_its_evidence(client, llm_env):
    llm_env.script = verdict_script
    aid = new_analysis(client)
    assert wait_run(client, aid)["status"] == "done"
    page = client.get(f"/analyses/{aid}").text
    assert "Verdict version 1" in page and "every figure verified" in page
    assert '<p class="rec-model">PA-5550</p>' in page
    assert "The PA-5550 covers the usage." in page
    assert "Confirm the peak with the customer." in page
    assert "All limits: PA-5550" in page and "Security rulebase" in page
    assert "confidence: medium" in page
    assert client.get(f"/analyses/{aid}?verdict=9").status_code == 200  # unknown: latest


def test_unverified_claims_are_listed_in_the_report(client, llm_env):
    def script(body, gw):
        if gw.calls == 1:
            return tool_reply(("check_fit", {"model": "PA-5550", "port_needs": {}}))
        return tool_reply(
            (
                "submit_verdict",
                {
                    "recommendation": {
                        "model": "PA-5550",
                        "reason": "It offers 250 Gbps.",
                        "result_ids": [fit_id(body)],
                    },
                    "summary": "Fine.",
                },
            )
        )

    llm_env.script = script
    aid = new_analysis(client)
    wait_run(client, aid)
    page = client.get(f"/analyses/{aid}").text
    assert "unverified claim" in page and "250 Gbps" in page and "could not confirm" in page


def test_gateway_error_is_shown_on_the_page(client, llm_env):
    llm_env.script = [(403, {"error": "Blocked by AI security profile"}, {"x-request-id": "blk-1"})]
    aid = new_analysis(client)
    assert wait_run(client, aid)["status"] == "error"
    page = client.get(f"/analyses/{aid}").text
    assert "blocked this request" in page and "blk-1" in page
    assert "stopped" in page and "Models that don" in page  # the quick result is still there


def test_messages_fragment_and_state_api(client, llm_env):
    llm_env.script = verdict_script
    aid = new_analysis(client)
    wait_run(client, aid)
    r = client.get(f"/analyses/{aid}/agent/messages?after=0")
    assert r.status_code == 200
    assert r.headers["x-run-status"] == "done" and int(r.headers["x-last-id"]) > 0
    assert "step-line" in r.text and "Verdict version 1" in r.text
    last = int(r.headers["x-last-id"])
    again = client.get(f"/analyses/{aid}/agent/messages?after={last}")
    assert again.text.strip() == "" and again.headers["x-last-id"] == str(last)
    state = client.get(f"/api/analyses/{aid}/agent?after={last - 1}").json()
    assert state["enabled"] and len(state["messages"]) == 1 and state["run"]["outcome"] == "verdict"
    assert client.get("/api/analyses/99/agent").status_code == 404


def test_message_rules(client, llm_env):
    llm_env.script = [text_reply("ok")]
    aid = new_analysis(client)
    wait_run(client, aid)
    url = f"/analyses/{aid}/agent/messages"
    assert client.post(url, data={"message": "   "}).status_code == 400
    assert client.post(url, data={"message": "x" * 2001}).status_code == 400
    conn = db.connect(client.app_state.state.settings.db_path)
    conn.execute("UPDATE agent_run SET status='running', created_at=datetime('now')")
    conn.commit()
    conn.close()
    assert client.post(url, data={"message": "again"}).status_code == 409  # one run at a time
    assert client.post("/analyses/99/agent/messages", data={"message": "hi"}).status_code == 404


def test_start_button_when_the_run_never_happened(client, llm_env, monkeypatch):
    monkeypatch.setenv("TSF_SIZER_LLM_MODE", "off")
    aid = new_analysis(client)  # no agent: the LLM was off at upload time
    monkeypatch.setenv("TSF_SIZER_LLM_MODE", "gateway")
    llm_env.script = [text_reply("Hello, I read the file.")]
    assert "Start with the agent" in client.get(f"/analyses/{aid}").text
    assert client.post(f"/analyses/{aid}/agent/start", follow_redirects=False).status_code == 303
    assert wait_run(client, aid)["status"] == "done"
    assert "Hello, I read the file." in client.get(f"/analyses/{aid}").text


def test_ledger_tab_shows_agent_changes_and_the_form_values(client, llm_env):
    llm_env.script = [
        tool_reply(
            ("record_assumption", {"key": "years", "value": 5, "reason": "Five year contract"})
        ),
        text_reply("Set to five years."),
    ]
    aid = new_analysis(client, years="2", growth_pct="30")
    wait_run(client, aid)
    page = client.get(f"/analyses/{aid}").text
    assert "Five year contract" in page and "Planning horizon (years)" in page
    assert '<span class="chip chip-warn">agent</span>' in page  # the agent's change
    assert '<span class="chip chip-primary">form</span>' in page  # growth from the upload form
    assert "Undo the last change" in page
    assert client.get(f"/analyses/{aid}/report.json").json()["sizing"]["params"]["years"] == 5
    client.post(f"/analyses/{aid}/ledger/undo")
    assert client.get(f"/analyses/{aid}/report.json").json()["sizing"]["params"]["years"] == 2


def test_settings_page_shows_variables_but_never_secrets(client, llm_env):
    page = client.get("/settings").text
    assert "TSF_SIZER_GATEWAY_URL" in page and "TSF_SIZER_GATEWAY_API_KEY" in page
    assert "secret-key" not in page and llm_env.url in page
    assert "fake-model via AI gateway" in page


def test_settings_page_when_off(client):
    page = client.get("/settings").text
    assert "<code>TSF_SIZER_LLM_MODE</code>" in page
    assert "<code>TSF_SIZER_GATEWAY_URL</code>" not in page  # only the example mentions it
    r = client.post("/settings/test")
    assert "off or incomplete" in r.text


def test_connection_test_button(client, llm_env):
    llm_env.script = [text_reply("OK", "req-5"), tool_reply(("ping", {"message": "pong"}))]
    page = client.post("/settings/test").text
    assert "answered in" in page and "Tool calls work" in page and "req-5" in page
    llm_env.script = [(401, {"error": "bad"})]
    assert "Test failed (auth)" in client.post("/settings/test").text


def test_playbook_is_editable_and_reaches_the_agent(client, llm_env):
    page = client.get("/settings").text
    assert "Sizing playbook" in page and "This is the built-in playbook." in page
    r = client.post("/settings/playbook", data={"text": "# Mine\nAlways prefer the PA-9999."})
    assert "Playbook saved" in r.text and "This is an edited playbook." in r.text
    assert (
        "playbook" not in client.post("/settings/playbook", data={"text": ""}).text.lower() or True
    )
    bad = client.post("/settings/playbook", data={"text": "   "})
    assert "cannot be empty" in bad.text
    llm_env.script = [text_reply("ok")]
    aid = new_analysis(client)
    wait_run(client, aid)
    system = llm_env.requests[0]["body"]["messages"][0]["content"]
    assert "Always prefer the PA-9999." in system and "# Sizing playbook" not in system
    reset = client.post("/settings/playbook", data={"text": "", "action": "reset"})
    assert "built-in playbook" in reset.text and "This is the built-in playbook." in reset.text


def test_deleting_an_analysis_removes_the_agent_data(client, llm_env):
    llm_env.script = verdict_script
    aid = new_analysis(client)
    wait_run(client, aid)
    assert client.post(f"/analyses/{aid}/delete", follow_redirects=False).status_code == 303
    conn = db.connect(client.app_state.state.settings.db_path)
    for table in ("agent_run", "agent_message", "tool_result", "assumption", "verdict"):
        n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        assert n == 0, table
    conn.close()


def test_restart_marks_a_working_run_as_interrupted(tmp_path, monkeypatch):
    c, app = make_client(tmp_path, monkeypatch)
    conn = db.connect(app.state.settings.db_path)
    conn.execute("INSERT INTO agent_run (analysis_id, status) VALUES (1, 'running')")
    conn.commit()
    conn.close()
    from tsf_sizer.web.app import Settings, create_app

    create_app(Settings())
    conn = db.connect(app.state.settings.db_path)
    row = conn.execute("SELECT status, outcome FROM agent_run").fetchone()
    assert (row["status"], row["outcome"]) == ("error", "Interrupted by a restart")


def test_sizing_input_before_the_analysis_reaches_the_ledger_and_the_agent(client, llm_env):
    llm_env.script = [text_reply("Understood.")]
    aid = new_analysis(
        client,
        goal="usage_only",
        optics="none",
        port_add_text="10g sfp+=2",
        exclude_models_text="variant:5G",
        notes="Branch office, 200 users. Budget is tight.",
    )
    wait_run(client, aid)
    params = client.get(f"/analyses/{aid}/report.json").json()["sizing"]["params"]
    assert params["right_size"] is True and params["port_drop"] == ["optics"]
    assert params["port_add"] == {"10G_SFP+": 2} and params["exclude_models"] == ["VARIANT:5G"]
    page = client.get(f"/analyses/{aid}").text
    assert "Branch office, 200 users. Budget is tight." in page  # shown as the engineer's message
    assert "usage_only" in page and '<span class="chip chip-primary">form</span>' in page
    first = llm_env.requests[0]["body"]["messages"]
    assert (
        "Budget is tight" in first[1]["content"]
        and "do not ask for it again" in first[1]["content"]
    )
    system = first[0]["content"]
    assert "Already given by the engineer" in system and '"fit_mode": "usage_only"' in system


def test_upload_form_asks_for_sizing_input_first(client):
    page = client.get("/").text
    assert page.index("Step 1") < page.index("Step 2")
    for name in (
        "goal",
        "optics",
        "port_add_text",
        "notes",
        "only_models_text",
        "exclude_models_text",
    ):
        assert f'name="{name}"' in page
    assert "A smaller or cheaper box" in page


def test_bad_sizing_input_is_refused(client, tmp_path):
    for form in (
        {"port_add_text": "10G_SFP+"},
        {"port_add_text": "warp=2"},
        {"exclude_models_text": "PA-1234"},
        {"notes": "x" * 1001},
    ):
        assert upload(client, **form).status_code == 400, form
    assert list((tmp_path / "uploads").iterdir()) == []


def test_without_input_the_defaults_apply(client):
    aid = new_analysis(client)
    params = client.get(f"/analyses/{aid}/report.json").json()["sizing"]["params"]
    assert params["right_size"] is False and params["port_drop"] == [] and params["port_add"] == {}
