import json
import re
from pathlib import Path

import pytest
from conftest import build_workbook
from test_llm import FakeLLM, fake_llm  # noqa: F401 - fake server fixture
from test_web import CONFIG, TECHSUPPORT, make_client

from tsf_sizer import db, pipeline
from tsf_sizer.llm import agent
from tsf_sizer.llm import settings as llm_settings
from tsf_sizer.portfolio.importer import import_workbook
from tsf_sizer.sizing.engine import SizingParams


def script(user: str) -> str:
    """A stand-in for the model: the interpreter step gets JSON, the reply step gets prose."""
    if "Engineer says:" not in user:  # reply step
        facts = json.loads(user)
        return f"Now {facts['recommended_now']} (was {facts['recommended_before']})."
    said = user.split("Engineer says:", 1)[1].split("\n", 1)[0].strip().lower()
    if "no longer needs the optics" in said:
        out = {"actions": [{"action": "ports", "drop": ["optics"]}], "question": None}
    elif "two 10g" in said:
        out = {"actions": [{"action": "ports", "add": {"10g sfp+": 2}}], "question": None}
    elif "needs optics" in said:
        out = {"actions": [], "question": "How many optical ports, and which speed?"}
    elif "why" in said:
        out = {"actions": [{"action": "explain", "model": "PA-540"}], "question": None}
    elif "poe and bogus" in said:
        actions = [
            {"action": "set", "name": "need_poe", "value": True},
            {"action": "set", "name": "growth_pct", "value": 9999},
            {"action": "exclude", "models": ["PA-1234"]},
        ]
        out = {"actions": actions, "question": None}
    elif "garbage" in said:
        return "I think you should probably buy a bigger one."
    else:
        out = {"actions": [], "question": "What would you like to change?"}
    return "Sure, here you go:\n```json\n" + json.dumps(out) + "\n```"


@pytest.fixture
def chat_app(tmp_path, monkeypatch, fake_llm):  # noqa: F811
    """An app whose portfolio has copper-only PA-450R/PA-540 and a PA-3430 with SFP+ cages,
    one finished analysis of a PA-450R, and the fake LLM configured."""
    FakeLLM.answer = script
    c, app = make_client(tmp_path, monkeypatch)
    path = tmp_path / "Capacity_Workbook_Chat.xlsx"
    build_workbook(path, overrides={("Traffic - 10/100/1000", "PA-3430"): 8})
    conn = db.connect(app.state.settings.db_path)
    import_workbook(conn, path, activate=True)
    llm_settings.save(conn, provider="ollama", base_url=fake_llm, model="m")
    tsf = tmp_path / "techsupport_pa450r.txt"
    tsf.write_text(TECHSUPPORT.read_text().replace("model: PA-3430", "model: PA-450R"))
    params = SizingParams(include_superseded=True, max_size_factor=0)
    result = pipeline.analyze(conn, tsf, params, Path(CONFIG))
    with conn:
        conn.execute(
            "INSERT INTO analysis (id, filename, status, params_json, result_json) "
            "VALUES (1, 'x.txt', 'done', ?, ?)",
            (json.dumps(params.__dict__), json.dumps(result, default=str)),
        )
    conn.close()
    with c:
        yield c, app


def say(c, text, aid=1):
    r = c.post(
        f"/analyses/{aid}/chat", data={"message": text}, headers={"accept": "application/json"}
    )
    assert r.status_code == 200, r.text
    # the job runs in the app's executor: wait for the assistant row to finish
    import time

    for _ in range(100):
        msgs = c.get(f"/api/analyses/{aid}/chat").json()["messages"]
        if not any(m["status"] == "pending" for m in msgs):
            return msgs[-1]
        time.sleep(0.05)
    raise AssertionError("assistant did not answer")


def report(c):
    return c.get("/analyses/1/report.json").json()


def test_no_longer_needs_optics_changes_the_port_requirement(chat_app):
    c, _ = chat_app
    before = report(c)["sizing"]
    assert before["recommended"]["model"] == "PA-540" or before["recommended"]
    reply = say(c, "The customer no longer needs the optics")
    assert reply["status"] == "done" and reply["changes"]["applied"] == ["optics no longer needed"]
    assert reply["changes"]["adjustments"] == ["no optics needed"]
    after = report(c)["sizing"]
    assert after["params"]["port_drop"] == ["optics"]
    assert after["ports_needed"]["baseline"] == {"1G_RJ45": 8}
    page = c.get("/analyses/1").text
    assert "no optics needed" in page and "Refine with the assistant" in page
    assert "no longer needed" in page  # the change is listed in the conversation


def test_now_needs_optics_asks_then_applies(chat_app):
    c, _ = chat_app
    asked = say(c, "He now needs optics")
    assert asked["content"] == "How many optical ports, and which speed?"
    assert asked["changes"]["applied"] == [] and report(c)["sizing"]["params"]["port_add"] == {}
    done = say(c, "He now needs two 10G fibre ports")
    assert done["changes"]["applied"] == ["2x 10G_SFP+ additionally needed"]
    s = report(c)["sizing"]
    assert s["ports_needed"]["baseline"] == {"1G_RJ45": 8, "10G_SFP+": 2}
    assert s["recommended"]["model"] == "PA-3430"  # the only model with SFP+ cages
    assert done["changes"]["recommended_now"] == "PA-3430"
    assert "PA-3430" in done["content"]  # the reply came from the (fake) model


def test_invalid_actions_are_refused_valid_ones_applied(chat_app):
    c, _ = chat_app
    r = say(c, "PoE and bogus settings")
    assert r["changes"]["applied"] == ["need_poe = yes"]
    assert len(r["changes"]["refused"]) == 2
    p = report(c)["sizing"]["params"]
    assert p["need_poe"] is True and p["growth_pct_per_year"] == 20  # 9999 never reached it


def test_unusable_model_answer_changes_nothing(chat_app):
    c, _ = chat_app
    before = report(c)
    r = say(c, "garbage please")
    assert r["status"] == "error" and "usable JSON" in r["content"]
    assert report(c)["sizing"] == before["sizing"]


def test_explain_is_read_only(chat_app):
    c, _ = chat_app
    before = report(c)["sizing"]
    r = say(c, "Why was PA-540 rejected?")
    assert r["status"] == "done" and r["changes"]["applied"] == []
    assert report(c)["sizing"]["params"] == before["params"]


def test_undo_and_clear(chat_app):
    c, _ = chat_app
    say(c, "The customer no longer needs the optics")
    assert c.get("/api/analyses/1/chat").json()["can_undo"] is True
    assert c.post("/analyses/1/chat/undo", follow_redirects=False).status_code == 303
    assert report(c)["sizing"]["params"]["port_drop"] == []
    assert c.get("/api/analyses/1/chat").json()["can_undo"] is False
    assert c.post("/analyses/1/chat/undo").status_code == 409
    say(c, "The customer no longer needs the optics")
    assert c.post("/analyses/1/adjustments/clear", follow_redirects=False).status_code == 303
    assert report(c)["sizing"]["params"]["port_drop"] == []


def test_adjustments_survive_a_form_rerun_and_conversation_is_deleted_with_it(chat_app):
    c, _ = chat_app
    say(c, "The customer no longer needs the optics")
    r = c.post("/analyses/1/rerun", data={"growth_pct": "30", "years": "3", "target_util_pct": "70",
                                          "max_size_factor": "0", "include_superseded": "1"},
               follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    import time

    for _ in range(100):
        if c.get("/api/analyses/1").json()["status"] == "done":
            break
        time.sleep(0.05)
    p = report(c)["sizing"]["params"]
    assert p["growth_pct_per_year"] == 30 and p["port_drop"] == ["optics"]
    assert c.post("/analyses/1/delete", follow_redirects=False).status_code == 303
    assert c.get("/api/analyses/1/chat").status_code == 404


def test_chat_guards(chat_app, fake_llm):  # noqa: F811
    c, app = chat_app
    assert c.post("/analyses/1/chat", data={"message": "   "}).status_code == 400
    assert c.post("/analyses/1/chat", data={"message": "x" * 1001}).status_code == 400
    assert c.post("/analyses/99/chat", data={"message": "hi"}).status_code == 404
    conn = db.connect(app.state.settings.db_path)
    llm_settings.save(conn, provider="none", model="")
    conn.close()
    assert c.post("/analyses/1/chat", data={"message": "hi"}).status_code == 400
    assert "needs an LLM" in c.get("/analyses/1").text


def test_interpreter_json_extraction():
    assert agent.extract_json('text {"a": {"b": 1}} more') == {"a": {"b": 1}}
    assert agent.extract_json("no json here") is None
    assert agent.extract_json('{"broken": ') is None
    assert re.search(r"optics", agent.INTERPRET_SYSTEM)


def test_closest_blocked_lists_smaller_models_missing_one_or_two_rules():
    sizing = {
        "recommended": {"model": "PA-1410", "sort_key": 4.5},
        "rejected": [
            {"model": "PA-550", "sort_key": 4.5, "failures": ["Forwarding table size V4: below"]},
            {"model": "PA-540", "sort_key": 2.2, "failures": ["a", "b", "c"]},  # too many
            {"model": "PA-560", "sort_key": 6.0, "failures": ["x"]},  # bigger, not "smaller"
            {"model": "PA-520", "sort_key": 1.0, "failures": ["Max VRs: needs 4, has 3"]},
        ],
    }
    got = agent.closest_blocked(sizing)
    assert [g["model"] for g in got] == ["PA-550", "PA-520"]
    assert agent.closest_blocked({"recommended": None, "rejected": []}) == []
