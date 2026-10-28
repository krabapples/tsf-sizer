import json
import shutil
import time

import pytest
from docfiles import image_pdf_bytes, png_bytes, text_pdf_bytes
from fake_gateway import text_reply, tool_reply
from test_manual import FORM
from test_web import client as _web_client

from tsf_sizer import db, documents
from tsf_sizer.agent.tools import Toolbox

client = _web_client  # the web test fixture: an app with the synthetic workbook imported
needs_ocr = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# --------------------------------------------------------------------------- extraction


def test_a_text_pdf_is_read_from_its_text_layer(tmp_path):
    p = write(
        tmp_path,
        "a.pdf",
        text_pdf_bytes(["Peak throughput 1850 Mbps\nSessions 143000", "Page two"]),
    )
    r = documents.extract(p, "a.pdf")
    assert r.kind == "pdf" and [pg.number for pg in r.pages] == [1, 2]
    assert "Peak throughput 1850 Mbps" in r.pages[0].text and "Sessions 143000" in r.pages[0].text
    assert r.pages[0].method == "text" and r.method == "text" and not r.warnings


@needs_ocr
def test_a_screenshot_is_read_with_ocr(tmp_path):
    r = documents.extract(write(tmp_path, "s.png", png_bytes()), "s.png")
    text = r.pages[0].text
    assert r.kind == "image" and r.method == "ocr"
    for word in ("Throughput", "1,850", "6,100", "143,000", "61"):
        assert word in text, text


@needs_ocr
def test_a_pdf_that_is_a_screenshot_is_read_with_ocr(tmp_path):
    r = documents.extract(write(tmp_path, "s.pdf", image_pdf_bytes()), "s.pdf")
    assert r.pages[0].method == "ocr" and "1,850" in r.pages[0].text


def test_without_ocr_a_screenshot_is_reported_not_silently_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = documents.extract(write(tmp_path, "s.png", png_bytes()), "s.png")
    assert r.pages[0].text == "" and any("OCR is not installed" in w for w in r.warnings)
    r = documents.extract(write(tmp_path, "s.pdf", image_pdf_bytes()), "s.pdf")
    assert any("OCR is not installed" in w or "No text found" in w for w in r.warnings)
    assert documents.ocr_status() == {"available": False, "version": None}


def test_only_pdf_and_images_and_damaged_files_are_refused(tmp_path):
    for name, data, message in [
        ("n.txt", b"hello", "not a PDF, PNG or JPEG"),
        ("x.pdf", b"%PDF-1.4 this is not a pdf", "damaged or password protected"),
        ("x.png", b"\x89PNG\r\n\x1a\n" + b"junk", "cannot be read"),
    ]:
        with pytest.raises(documents.DocumentError, match=message):
            documents.extract(write(tmp_path, name, data), name)


def test_long_documents_are_cut_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_PAGES", 2)
    r = documents.extract(
        write(tmp_path, "l.pdf", text_pdf_bytes(["one", "two", "three"])), "l.pdf"
    )
    assert len(r.pages) == 2 and any("first 2 of 3 pages" in w for w in r.warnings)


def test_ocr_lines_already_in_the_text_layer_are_not_repeated():
    assert documents._merge("Throughput 5\nCPU 3", "Throughput 5\nCPU 3") == "Throughput 5\nCPU 3"
    merged = documents._merge("Throughput 5", "Throughput 5\nSessions 9")
    assert merged.endswith("Sessions 9") and merged.count("Throughput 5") == 1


def test_process_stores_results_and_failures_and_deletes_the_files(tmp_path):
    path = str(tmp_path / "d.db")
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO analysis (id, filename, status, params_json) VALUES (1,'x','done','{}')"
    )
    good = documents.add_pending(conn, 1, "good.pdf")
    bad = documents.add_pending(conn, 1, "bad.txt")
    conn.commit()
    items = [
        (good, write(tmp_path, "g.pdf", text_pdf_bytes(["Hello 42"])), "good.pdf"),
        (bad, write(tmp_path, "b.txt", b"nope"), "bad.txt"),
    ]
    documents.process(path, items)
    rows = {d["id"]: d for d in documents.list_for(conn, 1)}
    assert rows[good]["status"] == "done" and rows[good]["pages_data"][0]["text"] == "Hello 42"
    assert rows[bad]["status"] == "error" and "not a PDF" in rows[bad]["warnings"][0]
    assert not any(p.exists() for _, p, _ in items)


# --------------------------------------------------------------------------- the agent's tools


@pytest.fixture
def box(analysis):
    path, aid = analysis
    conn = db.connect(path)
    pages = [
        {
            "n": 1,
            "text": "Throughput max 1,850 Mbps from 10.1.2.3 on fw1.corp.example.com",
            "method": "ocr",
        },
        {"n": 2, "text": "", "method": "empty"},
    ]
    conn.execute(
        "INSERT INTO document (analysis_id, filename, kind, status, pages, chars, method, "
        "pages_json, warnings_json) "
        "VALUES (1, 'ACME secret.pdf', 'pdf', 'done', 2, 60, 'ocr', ?, '[]')",
        (json.dumps(pages),),
    )
    conn.commit()
    yield Toolbox(conn, aid)
    conn.close()


def test_list_documents_has_no_file_names(box):
    res = box.call("list_documents", {})
    assert res["ok"]
    d = res["payload"]["documents"][0]
    assert d["label"] == "Document 1 (PDF)" and d["pages"] == 2 and d["read_with"] == "ocr"
    assert "ACME" not in json.dumps(res["payload"])


def test_read_document_returns_a_page_and_says_when_ocr_was_used(box):
    p = box.call("read_document", {"document_id": 1})["payload"]
    assert "1,850 Mbps" in p["text"] and p["pages"] == 2 and p["next_page"] == 2
    assert "OCR: digits may be misread" in p["source"]
    empty = box.call("read_document", {"document_id": 1, "page": 2})["payload"]
    assert empty["text"] == "" and empty["no_data"] == ["text on this page"]


def test_read_document_errors(box):
    for args, text in [
        ({"document_id": 9}, "no document 9"),
        ({"document_id": 1, "page": 3}, "between 1 and 2"),
        ({"document_id": 1, "page": 0}, "between 1 and 2"),
        ({}, "missing argument"),
    ]:
        res = box.call("read_document", args)
        assert not res["ok"] and text in res["error"]
    box.conn.execute("UPDATE document SET status='reading'")
    assert "not readable yet" in box.call("read_document", {"document_id": 1})["error"]


def test_long_pages_are_cut(box):
    box.conn.execute(
        "UPDATE document SET pages_json=?",
        (json.dumps([{"n": 1, "text": "x" * 9000, "method": "text"}]),),
    )
    p = box.call("read_document", {"document_id": 1})["payload"]
    assert len(p["text"]) == 6000 and p["truncated"] is True


def test_documents_can_be_switched_off(box, monkeypatch):
    monkeypatch.setenv("TSF_SIZER_AGENT_ALLOW_DOCUMENTS", "false")
    for tool, args in (("list_documents", {}), ("read_document", {"document_id": 1})):
        assert "switched off" in box.call(tool, args)["error"]


def test_the_overview_mentions_documents(box):
    ov = box.call("get_evidence", {"topic": "overview"})["payload"]["overview"]
    assert ov["customer_documents"] == 1


# --------------------------------------------------------------------------- the web app


def post_manual(c, files, **extra):
    return c.post("/analyses/manual", data={**FORM, **extra}, files=files, follow_redirects=False)


def wait_docs(c, aid, timeout=20):
    conn = db.connect(c.app_state.state.settings.db_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = conn.execute("SELECT status FROM document WHERE analysis_id=?", (aid,)).fetchall()
        if rows and all(r["status"] != "reading" for r in rows):
            return
        time.sleep(0.1)
    raise AssertionError("documents were not read in time")


@needs_ocr
def test_documents_sent_with_a_manual_analysis_are_read_and_shown(client):
    files = [
        ("documents", ("panorama.png", png_bytes(), "image/png")),
        ("documents", ("notes.pdf", text_pdf_bytes(["Peak 1850 Mbps"]), "application/pdf")),
    ]
    r = post_manual(client, files)
    assert r.status_code == 303
    aid = int(r.headers["location"].rsplit("/", 1)[1])
    wait_docs(client, aid)
    page = client.get(f"/analyses/{aid}").text
    assert "Customer documents" in page and "panorama.png" in page and "notes.pdf" in page
    assert "1,850" in page and "Peak 1850 Mbps" in page and "OCR can misread digits" in page
    assert client.post(f"/analyses/{aid}/delete", follow_redirects=False).status_code == 303
    conn = db.connect(client.app_state.state.settings.db_path)
    assert conn.execute("SELECT count(*) FROM document").fetchone()[0] == 0


def test_documents_are_validated_before_anything_is_created(client, tmp_path):
    assert (
        post_manual(
            client, [("documents", ("x.exe", b"MZ", "application/octet-stream"))]
        ).status_code
        == 400
    )
    too_many = [
        ("documents", (f"{i}.pdf", text_pdf_bytes(["x"]), "application/pdf")) for i in range(11)
    ]
    assert post_manual(client, too_many).status_code == 400
    assert post_manual(client, [], model="PA-9999").status_code == 400
    conn = db.connect(client.app_state.state.settings.db_path)
    assert conn.execute("SELECT count(*) FROM analysis").fetchone()[0] == 0
    assert list((tmp_path / "uploads").iterdir()) == []


def test_the_agent_reads_a_document_and_records_the_confirmed_peak(client, llm_env):
    seen = {}

    def script(body, gw):
        n = gw.calls
        if n == 1:
            return tool_reply(("list_documents", {}))
        if n == 2:
            return tool_reply(("read_document", {"document_id": 1}))
        if n == 3:
            seen["doc"] = next(m for m in reversed(body["messages"]) if m["role"] == "tool")[
                "content"
            ]
            return tool_reply(
                (
                    "record_evidence",
                    {
                        "field": "throughput_mbps",
                        "value": 1850,
                        "kind": "peak",
                        "note": "document 1, page 1 (OCR), confirmed by the engineer",
                    },
                )
            )
        return text_reply("Recorded the peak from document 1.")

    llm_env.script = script
    files = [
        (
            "documents",
            (
                "ACME-panorama.pdf",
                text_pdf_bytes(["Throughput max 1850 Mbps\nfirewall 10.1.2.3"]),
                "application/pdf",
            ),
        )
    ]
    r = post_manual(client, files, throughput_mbps="")
    aid = int(r.headers["location"].rsplit("/", 1)[1])
    for _ in range(200):
        run = client.get(f"/api/analyses/{aid}/agent").json()["run"]
        if run and run["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert run["status"] == "done", run
    sent = json.dumps([x["body"] for x in llm_env.requests])
    assert "1850 Mbps" in seen["doc"] and "[ip]" in seen["doc"] and "10.1.2.3" not in sent
    assert "ACME" not in sent  # file names are not sent
    data = client.get(f"/analyses/{aid}/report.json").json()
    thr = data["summary"]["metrics"]["perf.throughput_snapshot_mbps"]
    assert thr["value"] == 1850 and thr["kind"] == "peak" and "document 1" in thr["note"]


def test_documents_added_later_ask_the_agent_to_read_them(client, llm_env):
    llm_env.script = [text_reply("Ready."), text_reply("I read document 1.")]
    r = post_manual(client, [])
    aid = int(r.headers["location"].rsplit("/", 1)[1])
    for _ in range(100):
        run = client.get(f"/api/analyses/{aid}/agent").json()["run"]
        if run and run["status"] == "done":
            break
        time.sleep(0.05)
    add = client.post(
        f"/analyses/{aid}/documents",
        files=[("documents", ("later.pdf", text_pdf_bytes(["Max 2000 Mbps"]), "application/pdf"))],
        follow_redirects=False,
    )
    assert add.status_code == 303
    wait_docs(client, aid)
    for _ in range(200):
        if llm_env.calls >= 2:
            break
        time.sleep(0.05)
    last_user = [m for m in llm_env.requests[-1]["body"]["messages"] if m["role"] == "user"][-1]
    assert "I added 1 customer document(s), ids 1" in last_user["content"]
    assert "later.pdf" not in json.dumps(llm_env.requests[-1]["body"])
    bad = client.post(
        f"/analyses/{aid}/documents", files=[("documents", ("x.txt", b"hi", "text/plain"))]
    )
    assert bad.status_code == 400
    assert client.post("/analyses/99/documents", files=[]).status_code in (404, 422)


def test_the_pages_offer_documents_and_show_the_ocr_status(client):
    index = client.get("/").text
    assert index.count('name="documents"') == 2 and "Customer documents" in index
    post = post_manual(client, [])
    aid = int(post.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/analyses/{aid}").text
    assert "Add customer documents" in page and f'action="/analyses/{aid}/documents"' in page
    settings = client.get("/settings").text
    assert "Reading customer documents" in settings
    assert "TSF_SIZER_AGENT_ALLOW_DOCUMENTS" in settings
