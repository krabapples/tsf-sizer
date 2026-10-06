import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_web import client as _web_client
from test_web import upload, wait_done

from tsf_sizer import db
from tsf_sizer.llm import settings as llm_settings
from tsf_sizer.llm.providers import LLMConfig, LLMError, chat, list_models
from tsf_sizer.llm.writeup import build_facts, fact_check, render_markdown, write


class FakeLLM(BaseHTTPRequestHandler):
    """Speaks just enough Ollama, OpenAI and Anthropic API for the tests."""

    requests: list = []
    answer = None  # callable(user_prompt) -> str
    missing = False  # Ollama: model not pulled

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        FakeLLM.requests.append(("GET", self.path, dict(self.headers), None))
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": "qwen2.5:14b"}, {"name": "llama3.1:8b"}]})
        elif self.path in ("/v1/models", "/models"):
            self._send(200, {"data": [{"id": "local-model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLLM.requests.append(("POST", self.path, dict(self.headers), body))
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        text = FakeLLM.answer(user) if FakeLLM.answer else "OK"
        if self.path == "/api/chat" and FakeLLM.missing:
            self._send(404, {"error": f"model '{body['model']}' not found"})
        elif self.path == "/api/chat":
            self._send(200, {"message": {"role": "assistant", "content": text}})
        elif self.path in ("/v1/chat/completions", "/chat/completions"):
            self._send(200, {"choices": [{"message": {"content": text}}]})
        elif self.path == "/v1/messages":
            if self.headers.get("x-api-key") != "secret":
                self._send(401, {"error": "bad key"})
                return
            self._send(200, {"content": [{"type": "text", "text": text}]})
        else:
            self._send(404, {"error": "not found"})


client = _web_client  # reuse the web test fixture


@pytest.fixture
def fake_llm():
    FakeLLM.requests = []
    FakeLLM.answer = None
    FakeLLM.missing = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# --------------------------------------------------------------------------- providers


def test_ollama_chat_and_models(fake_llm):
    cfg = LLMConfig(provider="ollama", base_url=fake_llm, model="qwen2.5:14b", max_tokens=300)
    assert chat(cfg, "sys", "hello").text == "OK"
    _, path, _, body = FakeLLM.requests[-1]
    assert path == "/api/chat" and body["stream"] is False
    assert body["options"]["num_predict"] == 300
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert list_models(cfg) == ["llama3.1:8b", "qwen2.5:14b"]


def test_openai_compatible(fake_llm):
    cfg = LLMConfig(provider="openai", base_url=fake_llm + "/v1/", model="m", api_key="k")
    assert chat(cfg, "sys", "hi").text == "OK"
    _, path, headers, body = FakeLLM.requests[-1]
    assert path == "/v1/chat/completions" and headers["Authorization"] == "Bearer k"
    assert body["model"] == "m"
    assert list_models(cfg) == ["local-model"]


def test_anthropic(fake_llm):
    cfg = LLMConfig(provider="anthropic", base_url=fake_llm, model="claude", api_key="secret")
    assert chat(cfg, "sys", "hi").text == "OK"
    _, path, headers, body = FakeLLM.requests[-1]
    assert path == "/v1/messages" and body["system"] == "sys"
    assert {k.lower(): v for k, v in headers.items()}["anthropic-version"] == "2023-06-01"
    with pytest.raises(LLMError, match="HTTP 401"):
        chat(LLMConfig("anthropic", fake_llm, "claude", api_key="wrong"), "s", "u")
    with pytest.raises(LLMError, match="API_KEY"):
        chat(LLMConfig("anthropic", fake_llm, "claude"), "s", "u")


def test_errors_are_llm_errors(fake_llm):
    with pytest.raises(LLMError, match="No LLM configured"):
        chat(LLMConfig(), "s", "u")
    with pytest.raises(LLMError, match="Cannot reach"):
        chat(LLMConfig("ollama", "http://127.0.0.1:9", "m", timeout=5), "s", "u")
    FakeLLM.missing = True
    with pytest.raises(LLMError, match="ollama pull m"):
        chat(LLMConfig("ollama", fake_llm, "m"), "s", "u")
    FakeLLM.missing = False
    FakeLLM.answer = lambda _u: "   "
    with pytest.raises(LLMError, match="empty"):
        chat(LLMConfig("ollama", fake_llm, "m"), "s", "u")


# --------------------------------------------------------------------------- settings


def test_settings_env_and_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("TSF_SIZER_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("TSF_SIZER_LLM_MODEL", "llama3.1:8b")
    monkeypatch.setenv("TSF_SIZER_LLM_API_KEY", "k")
    conn = db.connect(str(tmp_path / "s.db"))
    cfg = llm_settings.load(conn)
    assert cfg.enabled and cfg.model == "llama3.1:8b" and cfg.api_key == "k"
    assert cfg.url == "http://host.docker.internal:11434"
    llm_settings.save(conn, provider="openai", model="m", max_tokens=500, temperature=0.5)
    cfg = llm_settings.load(conn)
    assert (cfg.provider, cfg.model, cfg.max_tokens, cfg.temperature) == ("openai", "m", 500, 0.5)
    assert cfg.api_key == "k"  # never stored, always from the environment
    with pytest.raises(ValueError):
        llm_settings.save(conn, api_key="x")
    assert not conn.execute("SELECT 1 FROM app_setting WHERE value='k'").fetchone()
    llm_settings.reset(conn)
    assert llm_settings.load(conn).provider == "ollama"


# --------------------------------------------------------------------------- write-up


def test_fact_check():
    facts = {"current_model": "PA-440", "recommended": {"model": "PA-550", "sessions": 300000}}
    ok = fact_check("## Why PA-550\n- 300,000 sessions, 3 ports, 299,999.6 rounds", facts)
    assert ok == {"unverified_numbers": ["299999.6"], "unverified_models": []}
    bad = fact_check("Consider the PA-5450 with 64000 sessions.", facts)
    assert bad["unverified_models"] == ["PA-5450"]
    assert bad["unverified_numbers"] == ["64000"]


def test_render_markdown_is_safe():
    out = render_markdown("## Summary\nA **bold** <script>x</script> claim.\n\n- one\n- two\n")
    assert "<h3>Summary</h3>" in out and "<strong>bold</strong>" in out
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<ul>\n<li>one</li>\n<li>two</li>\n</ul>" in out


def test_write_without_recommendation_or_server():
    cfg = LLMConfig("ollama", "http://127.0.0.1:9", "m", timeout=5)
    assert write(cfg, {"sizing": {}})["error"] == "No recommendation to explain."
    res = write(cfg, {"sizing": {"recommended": {"model": "PA-550"}}})
    assert "Cannot reach" in res["error"] and res["local"] is True


def _answer_from_facts(user: str) -> str:
    facts = json.loads(user.split("\n", 1)[1])
    rec = facts["recommended"]["model"]
    return f"## Summary\nReplace {facts['current_model']} with **{rec}**.\n\n- Also PA-9999."


def _analysis_with_recommendation(client, **form) -> int:
    """The synthetic portfolio has no model that fits the sample TSF, so promote the
    closest rejected candidate to 'recommended' in the stored result."""
    analysis_id = int(
        upload(client, with_config=True, **form).headers["location"].rsplit("/", 1)[1]
    )
    assert wait_done(client, analysis_id)["status"] == "done"
    conn = db.connect(client.app_state.state.settings.db_path)
    try:
        result = json.loads(
            conn.execute("SELECT result_json FROM analysis WHERE id=?", (analysis_id,)).fetchone()[
                0
            ]
        )
        result["sizing"]["recommended"] = result["sizing"]["rejected"][0]
        with conn:
            conn.execute(
                "UPDATE analysis SET result_json=? WHERE id=?", (json.dumps(result), analysis_id)
            )
    finally:
        conn.close()
    return analysis_id


def _rewrite(client, analysis_id) -> dict:
    r = client.post(f"/analyses/{analysis_id}/summary", follow_redirects=False)
    assert r.status_code == 303
    assert wait_done(client, analysis_id)["status"] == "done"
    return client.get(f"/analyses/{analysis_id}/report.json").json()


def test_report_summary_flow(client, fake_llm):
    # Configure through the UI, list models, test.
    form = {"provider": "ollama", "base_url": fake_llm, "model": "qwen2.5:14b"}
    page = client.post("/settings", data=form | {"action": "models"})
    assert "Found 2 model(s)" in page.text and "llama3.1:8b" in page.text
    page = client.post("/settings", data=form | {"action": "test"})
    assert "answered in" in page.text and "OK" in page.text
    assert client.post("/settings", data=form | {"base_url": "ftp://x"}).status_code == 400
    assert client.post("/settings", data=form | {"provider": "evil"}).status_code == 400

    FakeLLM.answer = _answer_from_facts
    analysis_id = _analysis_with_recommendation(client)
    data = _rewrite(client, analysis_id)
    w = data["writeup"]
    assert "error" not in w, w
    assert w["model"] == "qwen2.5:14b" and w["local"] is True
    assert "Replace PA-3430" in w["text"] and w["unverified_models"] == ["PA-9999"]
    page = client.get(f"/analyses/{analysis_id}").text
    assert "<strong>" + data["sizing"]["recommended"]["model"] + "</strong>" in page
    assert "Fact check" in page and "PA-9999" in page

    # Only aggregated facts leave the app: no hostname, serial or IPs from the TSF.
    sent = [b for _m, p, _h, b in FakeLLM.requests if p == "/api/chat"][-1]
    blob = json.dumps(sent)
    summary = data["summary"]
    for secret in (summary.get("hostname"), summary.get("serial")):
        if secret:
            assert secret not in blob
    assert json.loads(sent["messages"][1]["content"].split("\n", 1)[1]) == build_facts(data)

    # Rewrite with the current settings.
    FakeLLM.answer = lambda _u: "## Summary\nRewritten."
    assert _rewrite(client, analysis_id)["writeup"]["text"] == "## Summary\nRewritten."

    # Settings page previews exactly what is sent.
    assert "Exact data sent for the latest analysis" in client.get("/settings").text


def test_pipeline_writes_summary(client, fake_llm):
    client.post(
        "/settings",
        data={"provider": "ollama", "base_url": fake_llm, "model": "m", "action": "save"},
    )
    analysis_id = int(upload(client, ai_summary="1").headers["location"].rsplit("/", 1)[1])
    assert wait_done(client, analysis_id)["status"] == "done"
    w = client.get(f"/analyses/{analysis_id}/report.json").json()["writeup"]
    # No model fits the synthetic portfolio, so there is nothing to explain (and no call).
    assert w["error"] == "No recommendation to explain." and w["model"] == "m"


def test_ai_summary_can_be_skipped(client, fake_llm):
    client.post(
        "/settings",
        data={"provider": "ollama", "base_url": fake_llm, "model": "m", "action": "save"},
    )
    analysis_id = int(upload(client, ai_summary="").headers["location"].rsplit("/", 1)[1])
    assert wait_done(client, analysis_id)["status"] == "done"
    assert "writeup" not in client.get(f"/analyses/{analysis_id}/report.json").json()
    assert not [r for r in FakeLLM.requests if r[1] == "/api/chat"]


def test_summary_needs_llm(client):
    analysis_id = int(upload(client).headers["location"].rsplit("/", 1)[1])
    wait_done(client, analysis_id)
    assert client.post(f"/analyses/{analysis_id}/summary").status_code == 400
    assert "AI summary" in client.get("/settings").text


def test_llm_failure_does_not_fail_analysis(client):
    client.post(
        "/settings",
        data={
            "provider": "ollama",
            "base_url": "http://127.0.0.1:9",
            "model": "m",
            "timeout": "5",
            "action": "save",
        },
    )
    analysis_id = _analysis_with_recommendation(client)
    data = _rewrite(client, analysis_id)
    assert "Cannot reach" in data["writeup"]["error"]
    assert data["sizing"]["recommended"]
    assert "Try again" in client.get(f"/analyses/{analysis_id}").text


def test_localhost_hint_inside_container(monkeypatch):
    monkeypatch.setattr("tsf_sizer.llm.providers.os.path.exists", lambda p: p == "/.dockerenv")
    with pytest.raises(LLMError, match="host.docker.internal"):
        chat(LLMConfig("ollama", "http://localhost:9", "m", timeout=5), "s", "u")
