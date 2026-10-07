import base64
import time
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from tsf_sizer.web.app import Settings, create_app

FIXTURES = Path(__file__).parent / "fixtures"
TECHSUPPORT = FIXTURES / "techsupport_sample.txt"
CONFIG = FIXTURES / "config_sample.xml"


def make_client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("TSF_SIZER_DB", str(tmp_path / "web.db"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    app = create_app(Settings())
    return TestClient(app), app


@pytest.fixture
def client(tmp_path, monkeypatch, workbook):
    c, app = make_client(tmp_path, monkeypatch)
    with c:
        with workbook.open("rb") as fh:
            r = c.post(
                "/portfolio", files={"workbook": (workbook.name, fh)}, follow_redirects=False
            )
        assert r.status_code == 303 and "imported=1" in r.headers["location"]
        assert c.post("/portfolio/1/activate", follow_redirects=False).status_code == 303
        c.app_state = app
        yield c


def wait_done(c, analysis_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = c.get(f"/api/analyses/{analysis_id}").json()
        if st["status"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError("analysis did not finish")


def upload(c, **form):
    files = {"tsf": ("techsupport_PA3430_20260115.txt", TECHSUPPORT.read_bytes())}
    if form.pop("with_config", False):
        files["config"] = (".merged-running-config.xml", CONFIG.read_bytes())
    return c.post("/analyses", files=files, data=form, follow_redirects=False)


def test_index_and_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200
    assert "Upload and analyze" in page.text
    assert "No portfolio loaded" not in page.text


def test_index_warns_without_portfolio(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch)
    with c:
        assert "No portfolio loaded" in c.get("/").text


def test_full_flow(client, tmp_path):
    r = upload(client, customer="ACME", with_config=True, growth_pct="10", years="2")
    assert r.status_code == 303
    analysis_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert wait_done(client, analysis_id)["status"] == "done"
    page = client.get(f"/analyses/{analysis_id}")
    assert page.status_code == 200
    assert "Replacement for PA-3430" in page.text
    assert "ACME" in page.text
    assert ".merged-running-config.xml" in page.text
    assert "Models that don" in page.text
    data = client.get(f"/analyses/{analysis_id}/report.json").json()
    assert data["summary"]["model"] == "PA-3430"
    assert data["sizing"]["params"]["growth_pct_per_year"] == 10
    assert data["config"]["counts"]["address_objects"] == 5
    # Uploads never stay on disk.
    assert list((tmp_path / "uploads").iterdir()) == []
    # History lists it.
    assert "ACME" in client.get("/").text


def test_bad_file_reports_error(client):
    r = client.post("/analyses", files={"tsf": ("notes.txt", b"hello")}, follow_redirects=False)
    analysis_id = int(r.headers["location"].rsplit("/", 1)[1])
    st = wait_done(client, analysis_id)
    assert st["status"] == "error" and "TsfFormatError" in st["error"]
    assert "The analysis failed" in client.get(f"/analyses/{analysis_id}").text


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"growth_pct": "500"}, "Growth"),
        ({"peak_cps": "abc"}, "not a number"),
        ({"peak_sessions": "-5"}, "negative"),
    ],
)
def test_input_validation(client, form, message):
    r = upload(client, **form)
    assert r.status_code == 400 and message in r.text


def test_empty_upload_rejected(client):
    r = client.post("/analyses", files={"tsf": ("empty.tgz", b"")}, follow_redirects=False)
    assert r.status_code == 400 and "empty" in r.text


def test_upload_limit(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch, TSF_SIZER_MAX_UPLOAD_MB="1")
    with c:
        r = c.post(
            "/analyses",
            files={"tsf": ("big.tgz", b"x" * (1024 * 1024 + 1))},
            follow_redirects=False,
        )
        assert r.status_code == 413
        assert list((tmp_path / "uploads").iterdir()) == []


def test_not_found_and_delete(client):
    assert client.get("/analyses/999").status_code == 404
    assert "Not found" in client.get("/nope").text
    r = upload(client)
    analysis_id = int(r.headers["location"].rsplit("/", 1)[1])
    wait_done(client, analysis_id)
    assert client.post(f"/analyses/{analysis_id}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/analyses/{analysis_id}").status_code == 404


def test_portfolio_page_and_reimport(client, workbook):
    page = client.get("/portfolio?imported=1")
    assert "Import report, document 1" in page.text and "PA-3430" in page.text
    assert "Activate this version" not in page.text  # already active
    assert "Capacity_Workbook_Test.xlsx" in client.get("/portfolio").text
    with workbook.open("rb") as fh:
        r = client.post(
            "/portfolio", files={"workbook": (workbook.name, fh)}, follow_redirects=False
        )
    assert "Already imported" in unquote(r.headers["location"])
    r = client.post(
        "/portfolio", files={"workbook": ("bad.xlsx", b"not a workbook")}, follow_redirects=False
    )
    assert "Import failed" in unquote(r.headers["location"])


def test_basic_auth(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch, TSF_SIZER_USER="se", TSF_SIZER_PASSWORD="pw")
    with c:
        assert c.get("/").status_code == 401
        assert c.get("/healthz").status_code == 200
        good = base64.b64encode(b"se:pw").decode()
        bad = base64.b64encode(b"se:nope").decode()
        assert c.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
        assert c.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_restart_marks_interrupted_jobs(tmp_path, monkeypatch):
    c, _ = make_client(tmp_path, monkeypatch)
    with c:
        from tsf_sizer import db

        with db.connect(tmp_path / "web.db") as conn:
            conn.execute(
                "INSERT INTO analysis (filename, status, params_json) VALUES ('x', 'running', '{}')"
            )
        (tmp_path / "uploads" / "leftover.tsf").write_bytes(b"x")
    c2, _ = make_client(tmp_path, monkeypatch)
    with c2:
        assert c2.get("/api/analyses/1").json()["status"] == "error"
        assert list((tmp_path / "uploads").iterdir()) == []


def test_switch_and_size_cap_reach_the_engine(client):
    page = client.get("/")
    assert "Include previous-generation models" in page.text
    assert "PA-400 series, succeeded by PA-500" in page.text
    assert "Customer needs PoE" in page.text
    r = upload(client, include_superseded="1", max_size_factor="3", need_poe="1")
    analysis_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert wait_done(client, analysis_id)["status"] == "done"
    params = client.get(f"/analyses/{analysis_id}/report.json").json()["sizing"]["params"]
    assert params["include_superseded"] is True and params["max_size_factor"] == 3
    assert params["need_poe"] is True
    assert upload(client, max_size_factor="1").status_code == 400


def test_family_settings_page(client):
    page = client.get("/portfolio")
    assert "Model families" in page.text and "PA-500" in page.text
    r = client.post(
        "/portfolio/families/PA-400",
        data={"quotable": "", "superseded_by": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Include previous-generation models" not in client.get("/").text
    r = client.post(
        "/portfolio/families/PA-400",
        data={"quotable": "no", "superseded_by": "PA-500"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "PA-400 series, succeeded by PA-500" in client.get("/").text
    assert (
        client.post(
            "/portfolio/families/PA-400", data={"superseded_by": "PA-400"}, follow_redirects=False
        ).status_code
        == 400
    )
    assert (
        client.post("/portfolio/families/PA-9", data={}, follow_redirects=False).status_code == 404
    )


def test_portfolio_warns_when_performance_mapping_is_missing(client):
    assert "no performance data" not in client.get("/portfolio").text
    from tsf_sizer import db

    conn = db.connect(client.app_state.state.settings.db_path)
    with conn:
        conn.execute("DELETE FROM tsf_metric_map WHERE tsf_metric='perf.cps'")
    conn.close()
    page = client.get("/portfolio").text
    assert "no performance data" in page and "perf.cps" in page


def _post_tsf(c, text: str, name="techsupport_test.txt"):
    r = c.post("/analyses", files={"tsf": (name, text.encode())}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("model: PA-3430", "model: M-200"),
        ("model: PA-3430", "model: Panorama"),
        ("model: PA-3430", "model: PA-3430\nsystem-mode: panorama"),
        ("model: PA-3430", "model: PA-3430\nsystem-mode: logger"),
    ],
)
def test_panorama_tsf_is_rejected_with_a_clear_message(client, old, new):
    text = TECHSUPPORT.read_text().replace(old, new)
    analysis_id = _post_tsf(client, text)
    st = wait_done(client, analysis_id)
    assert st["status"] == "error" and st["error"].startswith("PanoramaTsfError")
    page = client.get(f"/analyses/{analysis_id}").text
    assert "Panorama Tech Support File, not a firewall" in page and "Panorama file" in page
    assert "Generate Tech Support File" in page
    assert client.get(f"/analyses/{analysis_id}/report.json").status_code == 409


def test_firewall_managed_by_panorama_is_still_analyzed(client):
    # Mentions of Panorama elsewhere in the TSF (e.g. its config) must not trigger the check.
    text = TECHSUPPORT.read_text() + "\npanorama-server 192.0.2.99\n"
    assert wait_done(client, _post_tsf(client, text))["status"] == "done"


def test_rerun_with_other_assumptions_without_the_tsf(client, tmp_path):
    analysis_id = int(
        upload(client, growth_pct="10", years="2", peak_cps="5,000")
        .headers["location"]
        .rsplit("/", 1)[1]
    )
    wait_done(client, analysis_id)
    first = client.get(f"/analyses/{analysis_id}/report.json").json()
    assert first["sizing"]["params"]["growth_pct_per_year"] == 10
    page = client.get(f"/analyses/{analysis_id}").text
    assert f'action="/analyses/{analysis_id}/rerun"' in page
    assert 'value="5,000"' in page or 'value="5000"' in page  # form is prefilled

    r = client.post(
        f"/analyses/{analysis_id}/rerun",
        data={
            "growth_pct": "50",
            "years": "5",
            "target_util_pct": "60",
            "port_rule": "used",
            "need_poe": "1",
            "max_size_factor": "3",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert wait_done(client, analysis_id)["status"] == "done"
    second = client.get(f"/analyses/{analysis_id}/report.json").json()
    params = second["sizing"]["params"]
    assert (params["growth_pct_per_year"], params["years"], params["target_util_pct"]) == (
        50,
        5,
        60,
    )
    assert params["port_rule"] == "used" and params["need_poe"] is True
    assert params["peak_cps"] is None  # a blank field clears the earlier peak
    assert second["summary"] == first["summary"]  # figures from the TSF are reused
    assert second["generated_at"] and "writeup" not in second
    assert list((tmp_path / "uploads").iterdir()) == []
    # Bad input is refused and the stored result is untouched.
    bad = client.post(f"/analyses/{analysis_id}/rerun", data={"growth_pct": "500"})
    assert bad.status_code == 400
    assert client.get(f"/analyses/{analysis_id}/report.json").json() == second


def test_rerun_needs_a_finished_analysis(client):
    r = client.post("/analyses", files={"tsf": ("notes.txt", b"hello")}, follow_redirects=False)
    analysis_id = int(r.headers["location"].rsplit("/", 1)[1])
    wait_done(client, analysis_id)  # fails: not a TSF
    assert client.post(f"/analyses/{analysis_id}/rerun").status_code == 409
    assert client.post("/analyses/999/rerun").status_code == 404
