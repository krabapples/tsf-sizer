"""FastAPI web app: upload a TSF, get a sizing report.

The agent reaches its LLM through the Prisma AIRS AI Gateway; see agent/config.py for the
TSF_SIZER_LLM_MODE and TSF_SIZER_GATEWAY_* variables.

Configuration (environment variables):
  TSF_SIZER_DB          SQLite path (default /data/app.db in the container)
  TSF_SIZER_DATA        data directory for temporary uploads (default: next to the DB)
  TSF_SIZER_MAX_UPLOAD_MB   upload limit per file (default 1024)
  TSF_SIZER_USER / TSF_SIZER_PASSWORD   enable HTTP basic auth when both are set
  TSF_SIZER_WORKERS     parallel analyses (default 2)

Run with:  uvicorn --factory tsf_sizer.web.app:create_app
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import db, documents
from ..agent import config as llm_config
from ..agent import ledger, playbook, runtime
from ..agent.gateway import GatewayClient, GatewayError
from ..pipeline import manual_result, refresh_quick_result, run_job
from ..portfolio import catalog
from ..portfolio.importer import AlreadyImportedError, activate, import_workbook
from ..portfolio.supplements import apply_to_all_documents
from ..sizing.engine import SizingParams
from ..tsf import manual
from . import agent_view
from . import report as report_view

HERE = Path(__file__).parent
EXAMPLES = [
    "The customer no longer needs the optics",
    "He now needs two 10G fibre ports",
    "The box is oversized: find a smaller model",
    "Expect 40% growth over 5 years",
    "Why was the PA-560 not chosen?",
]
CHUNK = 1024 * 1024


class Settings:
    def __init__(self) -> None:
        self.db_path = os.environ.get("TSF_SIZER_DB") or db.DEFAULT_DB_PATH
        self.data_dir = Path(os.environ.get("TSF_SIZER_DATA") or Path(self.db_path).parent)
        self.upload_dir = self.data_dir / "uploads"
        self.max_upload = int(os.environ.get("TSF_SIZER_MAX_UPLOAD_MB", "1024")) * CHUNK
        self.user = os.environ.get("TSF_SIZER_USER")
        self.password = os.environ.get("TSF_SIZER_PASSWORD")
        self.workers = int(os.environ.get("TSF_SIZER_WORKERS", "2"))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    # Leftovers from a crash or restart: uploads are never kept.
    for leftover in settings.upload_dir.glob("*"):
        leftover.unlink(missing_ok=True)
    with db.connect(settings.db_path) as conn:
        conn.execute(
            "UPDATE analysis SET status='error', error='Interrupted by a restart' "
            "WHERE status IN ('queued', 'running')"
        )
        # An agent run that was working when the app stopped will never finish.
        conn.execute(
            "UPDATE agent_run SET status='error', outcome='Interrupted by a restart' "
            "WHERE status='running'"
        )
        # Static datasheet models (PA-800) for versions imported before they existed.
        apply_to_all_documents(conn)

    app = FastAPI(title="TSF Sizer", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.executor = ThreadPoolExecutor(max_workers=settings.workers)
    app.state.agent_executor = ThreadPoolExecutor(max_workers=settings.workers)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    report_view.register_filters(templates.env)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    if not (settings.user and settings.password):
        logging.getLogger("uvicorn.error").warning(
            "No login configured (TSF_SIZER_USER / TSF_SIZER_PASSWORD): anyone who can reach "
            "this port can open every report. Set a login, or bind to 127.0.0.1."
        )
    if settings.user and settings.password:
        expected = base64.b64encode(f"{settings.user}:{settings.password}".encode()).decode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            header = request.headers.get("authorization", "")
            if not (
                header.startswith("Basic ") and secrets.compare_digest(header[6:].strip(), expected)
            ):
                return Response(
                    "Authentication required",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="TSF Sizer"'},
                )
            return await call_next(request)

    @contextmanager
    def conn():
        """Connection for one request: commit on success, always close."""
        c = db.connect(settings.db_path)
        try:
            with c:
                yield c
        finally:
            c.close()

    def render(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request, name, ctx)

    async def save_upload(upload: UploadFile, suffix: str) -> Path:
        dest = settings.upload_dir / f"{uuid.uuid4().hex}{suffix}"
        size = 0
        with dest.open("wb") as fh:
            while chunk := await upload.read(CHUNK):
                size += len(chunk)
                if size > settings.max_upload:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        413, f"{upload.filename} is larger than {settings.max_upload // CHUNK} MB"
                    )
                fh.write(chunk)
        if size == 0:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, f"{upload.filename or 'File'} is empty")
        return dest

    async def save_documents(uploads) -> list[tuple[Path, str]]:
        """Save the customer's PDFs and screenshots. All or nothing: a bad file removes the rest."""
        files = [u for u in uploads if getattr(u, "filename", None)]
        if len(files) > documents.MAX_DOCUMENTS:
            raise HTTPException(400, f"At most {documents.MAX_DOCUMENTS} documents at a time")
        saved: list[tuple[Path, str]] = []
        try:
            for u in files:
                ext = Path(u.filename).suffix.lower()
                if ext not in documents.EXTENSIONS:
                    raise HTTPException(400, f"{u.filename}: only PDF, PNG and JPEG files")
                saved.append((await save_upload(u, ext), Path(u.filename).name[:200]))
        except HTTPException:
            for path, _ in saved:
                path.unlink(missing_ok=True)
            raise
        return saved

    def queue_documents(analysis_id: int, saved: list[tuple[Path, str]]) -> list[int]:
        """Record the files and read them in the background. Returns the document ids."""
        if not saved:
            return []
        with conn() as c:
            ids = [documents.add_pending(c, analysis_id, name) for _, name in saved]
        items = [(i, p, n) for i, (p, n) in zip(ids, saved, strict=True)]
        app.state.executor.submit(documents.process, settings.db_path, items)
        return ids

    def wait_for_documents(analysis_id: int, timeout: float = 600.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with conn() as c:
                pending = c.execute(
                    "SELECT 1 FROM document WHERE analysis_id=? AND status='reading'",
                    (analysis_id,),
                ).fetchone()
            if not pending:
                return
            time.sleep(0.5)

    # ------------------------------------------------------------------ pages

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with conn() as c:
            doc = catalog.active_document(c)
            rows = c.execute(
                "SELECT id, created_at, customer, filename, status, error, result_json "
                "FROM analysis ORDER BY id DESC LIMIT 50"
            ).fetchall()
        history = []
        for r in rows:
            item = dict(r)
            res = json.loads(r["result_json"]) if r["result_json"] else {}
            item["model"] = (res.get("summary") or {}).get("model")
            rec = (res.get("sizing") or {}).get("recommended")
            item["recommended"] = rec["model"] if rec else None
            history.append(item)
        with conn() as c:
            superseded = catalog.superseded_families(c)
        llm = llm_config.from_env()
        with conn() as c:
            current_models = [
                dict(r)
                for r in c.execute(
                    "SELECT DISTINCT m.name, m.family FROM model m "
                    "JOIN capacity_value v ON v.model_id = m.id "
                    "JOIN source_document d ON d.id = v.document_id AND d.is_active = 1 "
                    "WHERE m.kind != 'component' ORDER BY m.family, m.name"
                )
            ]
        return render(
            request,
            "index.html",
            current_models=current_models,
            port_classes=manual_port_classes,
            count_fields=[(k, v[1]) for k, v in manual.COUNT_FIELDS.items()],
            superseded=superseded,
            llm=llm,
            portfolio=doc,
            history=history,
            defaults=SizingParams(),
            max_mb=settings.max_upload // CHUNK,
        )

    def build_params(
        growth_pct: float,
        years: int,
        target_util_pct: float,
        peak_throughput_mbps: str,
        peak_cps: str,
        peak_sessions: str,
        port_rule: str,
        include_superseded: str,
        need_poe: str,
        max_size_factor: float,
        goal: str = "match_or_beat",
        optics: str = "keep",
        port_add_text: str = "",
        only_models_text: str = "",
        exclude_models_text: str = "",
    ) -> SizingParams:
        def opt(v: str) -> float | None:
            v = v.strip().replace(",", "")
            if not v:
                return None
            try:
                f = float(v)
            except ValueError:
                raise HTTPException(400, f"'{v}' is not a number") from None
            if f < 0:
                raise HTTPException(400, "Peaks cannot be negative")
            return f

        if not (0 <= growth_pct <= 200 and 1 <= years <= 10 and 10 <= target_util_pct <= 100):
            raise HTTPException(400, "Growth 0-200%, years 1-10, target utilization 10-100%")
        if not (max_size_factor == 0 or 1 < max_size_factor <= 100):
            raise HTTPException(400, "Maximum size must be above 1x (or 0 for no limit)")
        try:
            with conn() as c:
                port_add = ledger.coerce(
                    "port_add", agent_view.parse_value("port_add", port_add_text), c
                )
                only = ledger.coerce(
                    "only_models", agent_view.parse_value("only_models", only_models_text), c
                )
                exclude = ledger.coerce(
                    "exclude_models",
                    agent_view.parse_value("exclude_models", exclude_models_text),
                    c,
                )
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return SizingParams(
            growth_pct_per_year=growth_pct,
            years=years,
            target_util_pct=target_util_pct,
            peak_throughput_mbps=opt(peak_throughput_mbps),
            peak_cps=opt(peak_cps),
            peak_sessions=opt(peak_sessions),
            port_rule="used" if port_rule == "used" else "all",
            include_superseded=bool(include_superseded),
            need_poe=bool(need_poe),
            max_size_factor=max_size_factor,
            right_size=goal == "usage_only",
            port_drop=["optics"] if optics == "none" else [],
            port_add=port_add,
            only_models=only,
            exclude_models=exclude,
        )

    @app.post("/analyses")
    async def create_analysis(
        tsf: UploadFile = File(...),
        config: UploadFile | None = File(None),
        documents_upload: list[UploadFile] = File([], alias="documents"),
        customer: str = Form(""),
        growth_pct: float = Form(20.0),
        years: int = Form(3),
        target_util_pct: float = Form(70.0),
        peak_throughput_mbps: str = Form(""),
        peak_cps: str = Form(""),
        peak_sessions: str = Form(""),
        port_rule: str = Form("all"),
        include_superseded: str = Form(""),
        need_poe: str = Form(""),
        max_size_factor: float = Form(5.0),
        goal: str = Form("match_or_beat"),
        optics: str = Form("keep"),
        port_add_text: str = Form(""),
        only_models_text: str = Form(""),
        exclude_models_text: str = Form(""),
        notes: str = Form(""),
    ):
        notes = notes.strip()
        if len(notes) > 1000:
            raise HTTPException(400, "The notes are limited to 1000 characters")
        params = build_params(
            growth_pct,
            years,
            target_util_pct,
            peak_throughput_mbps,
            peak_cps,
            peak_sessions,
            port_rule,
            include_superseded,
            need_poe,
            max_size_factor,
            goal,
            optics,
            port_add_text,
            only_models_text,
            exclude_models_text,
        )
        saved_docs = await save_documents(documents_upload)
        tsf_path = await save_upload(tsf, ".tsf")
        config_path, config_name = None, None
        if config is not None and config.filename:
            config_path = await save_upload(config, ".xml")
            config_name = Path(config.filename).name[:200]
        filename = Path(tsf.filename or "tsf").name[:200]
        with conn() as c:
            cur = c.execute(
                "INSERT INTO analysis (customer, filename, status, params_json) "
                "VALUES (?, ?, 'queued', ?)",
                (customer.strip()[:200] or None, filename, json.dumps(params.__dict__)),
            )
            analysis_id = cur.lastrowid
        queue_documents(analysis_id, saved_docs)
        app.state.executor.submit(
            run_job,
            settings.db_path,
            analysis_id,
            tsf_path,
            config_path,
            params,
            filename,
            config_name,
            lambda aid: (wait_for_documents(aid), start_agent(aid, notes=notes or None)),
        )
        return RedirectResponse(f"/analyses/{analysis_id}", status_code=303)

    manual_port_classes = [
        "1G_RJ45", "2.5G_RJ45", "5G_RJ45", "10G_RJ45", "1G_SFP", "10G_SFP+", "25G_SFP28",
        "40G_QSFP+", "100G_QSFP28",
    ]  # fmt: skip

    @app.post("/analyses/manual")
    async def create_manual_analysis(request: Request):
        """An analysis without a Tech Support File: the engineer enters what is known."""
        form = await request.form()
        data = {k: v for k, v in form.items() if isinstance(v, str)}
        saved_docs = await save_documents(form.getlist("documents"))

        def num(key: str, default: float) -> float:
            raw = data.get(key, "").strip().replace(",", "")
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                raise HTTPException(400, f"'{raw}' is not a number for {key}") from None

        notes = data.get("notes", "").strip()
        if len(notes) > 1000:
            raise HTTPException(400, "The notes are limited to 1000 characters")
        growth, years = num("growth_pct", 20.0), int(num("years", 3))
        params = build_params(
            growth,
            years,
            num("target_util_pct", 70.0),
            data.get("peak_throughput_mbps", ""),
            data.get("peak_cps", ""),
            data.get("peak_sessions", ""),
            data.get("port_rule", "all"),
            data.get("include_superseded", ""),
            data.get("need_poe", ""),
            num("max_size_factor", 5.0),
            data.get("goal", "match_or_beat"),
            data.get("optics", "keep"),
            data.get("port_add_text", ""),
            data.get("only_models_text", ""),
            data.get("exclude_models_text", ""),
        )
        try:
            with conn() as c:
                summary = manual.build_summary(c, data)
                result = manual_result(c, summary, params)
                cur = c.execute(
                    "INSERT INTO analysis (customer, filename, status, params_json, result_json, "
                    "finished_at) VALUES (?, 'Manual input', 'done', ?, ?, datetime('now'))",
                    (
                        data.get("customer", "").strip()[:200] or None,
                        json.dumps(params.__dict__),
                        json.dumps(result, default=str),
                    ),
                )
                analysis_id = cur.lastrowid
                ledger.seed(c, analysis_id, params)
        except ValueError as e:
            for path, _ in saved_docs:
                path.unlink(missing_ok=True)
            raise HTTPException(400, str(e)) from None
        queue_documents(analysis_id, saved_docs)
        app.state.agent_executor.submit(
            lambda: (wait_for_documents(analysis_id), start_agent(analysis_id, notes=notes or None))
        )
        return RedirectResponse(f"/analyses/{analysis_id}", status_code=303)

    def start_agent(
        analysis_id: int, user_text: str | None = None, notes: str | None = None
    ) -> int | None:
        """Start an agent run (the first one reads the evidence; later ones answer the
        engineer). None if the LLM is off or a run is already working."""
        if not llm_config.from_env().enabled:
            return None
        with conn() as c:
            if runtime.active_run(c, analysis_id):
                return None
            run_id = runtime.start_run(c, analysis_id, user_text, notes)
        app.state.agent_executor.submit(runtime.run_agent, settings.db_path, analysis_id, run_id)
        return run_id

    def load(analysis_id: int):
        with conn() as c:
            row = c.execute("SELECT * FROM analysis WHERE id=?", (analysis_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Analysis not found")
        return row

    @app.get("/analyses/{analysis_id}", response_class=HTMLResponse)
    def show_analysis(request: Request, analysis_id: int, verdict: int | None = None):
        row = load(analysis_id)
        if row["status"] != "done":
            return render(request, "status.html", a=dict(row))
        result = json.loads(row["result_json"])
        view = report_view.build(result, dict(row))
        cfg = llm_config.from_env()
        with conn() as c:
            superseded = catalog.superseded_families(c)
            messages = agent_view.messages(c, analysis_id)
            run = agent_view.run_state(c, analysis_id)
            vv = agent_view.verdict_view(c, analysis_id, verdict)
            ledger_rows = agent_view.ledger_rows(c, analysis_id)
            ledger_history = ledger.history(c, analysis_id)
            docs = documents.list_for(c, analysis_id)
        return render(
            request,
            "report.html",
            a=dict(row),
            r=result,
            v=view,
            llm=cfg,
            superseded=superseded,
            messages=messages,
            run=run,
            vv=vv,
            ledger_rows=ledger_rows,
            ledger_history=ledger_history,
            docs=docs,
            docs_reading=any(d["status"] == "reading" for d in docs),
            ocr=documents.ocr_status(),
            examples=EXAMPLES,
            last_id=messages[-1]["id"] if messages else 0,
        )

    @app.get("/api/analyses/{analysis_id}")
    def analysis_status(analysis_id: int):
        row = load(analysis_id)
        return {"id": row["id"], "status": row["status"], "error": row["error"]}

    @app.get("/analyses/{analysis_id}/report.json")
    def analysis_json(analysis_id: int):
        row = load(analysis_id)
        if row["status"] != "done":
            raise HTTPException(409, "Analysis not finished")
        return JSONResponse(
            json.loads(row["result_json"]),
            headers={
                "Content-Disposition": f'attachment; filename="tsf-sizing-{analysis_id}.json"'
            },
        )

    # ------------------------------------------------------------------ agent

    @app.get("/api/analyses/{analysis_id}/agent")
    def agent_state(analysis_id: int, after: int = 0):
        load(analysis_id)
        with conn() as c:
            msgs = agent_view.messages(c, analysis_id, after)
            run = agent_view.run_state(c, analysis_id)
        return {"messages": msgs, "run": run, "enabled": llm_config.from_env().enabled}

    @app.get("/analyses/{analysis_id}/agent/messages", response_class=HTMLResponse)
    def agent_messages_html(request: Request, analysis_id: int, after: int = 0):
        """The conversation as HTML, for the page to append while a run is working."""
        load(analysis_id)
        with conn() as c:
            msgs = agent_view.messages(c, analysis_id, after)
            run = agent_view.run_state(c, analysis_id)
        resp = render(
            request, "_agent_messages.html", messages=msgs, run=run, a={"id": analysis_id}
        )
        resp.headers["X-Run-Status"] = (run or {}).get("status", "none")
        resp.headers["X-Last-Id"] = str(msgs[-1]["id"] if msgs else after)
        return resp

    def need_finished(analysis_id: int):
        row = load(analysis_id)
        if row["status"] != "done" or not row["result_json"]:
            raise HTTPException(409, "The analysis is not finished")
        if not llm_config.from_env().enabled:
            raise HTTPException(
                400, "No LLM configured: set the TSF_SIZER_LLM_MODE and gateway variables"
            )
        return row

    @app.post("/analyses/{analysis_id}/agent/start")
    def agent_start(analysis_id: int):
        need_finished(analysis_id)
        start_agent(analysis_id)
        return RedirectResponse(f"/analyses/{analysis_id}#agent", status_code=303)

    @app.post("/analyses/{analysis_id}/agent/messages")
    def agent_send(request: Request, analysis_id: int, message: str = Form("")):
        need_finished(analysis_id)
        text = message.strip()
        if not text or len(text) > runtime.MAX_MESSAGE:
            raise HTTPException(400, f"Write a message of 1-{runtime.MAX_MESSAGE} characters")
        run_id = start_agent(analysis_id, text)
        if run_id is None:
            raise HTTPException(409, "The agent is still working on the last message")
        if "application/json" in request.headers.get("accept", ""):
            return {"run_id": run_id}
        return RedirectResponse(f"/analyses/{analysis_id}#agent", status_code=303)

    @app.post("/analyses/{analysis_id}/documents")
    async def add_documents(
        analysis_id: int, documents_upload: list[UploadFile] = File([], alias="documents")
    ):
        """Customer PDFs and screenshots added later; the agent is asked to read them."""
        row = load(analysis_id)
        if row["status"] != "done":
            raise HTTPException(409, "The analysis is not finished")
        saved = await save_documents(documents_upload)
        if not saved:
            raise HTTPException(400, "Choose at least one PDF or screenshot")
        ids = queue_documents(analysis_id, saved)
        text = (
            f"I added {len(ids)} customer document(s), ids {', '.join(map(str, ids))}. Read "
            "them and use the figures that matter."
        )

        def then_ask() -> None:
            wait_for_documents(analysis_id)
            start_agent(analysis_id, text)

        app.state.agent_executor.submit(then_ask)
        return RedirectResponse(f"/analyses/{analysis_id}#evidence", status_code=303)

    # ------------------------------------------------------------------ ledger

    @app.post("/analyses/{analysis_id}/ledger/undo")
    def ledger_undo(analysis_id: int):
        load(analysis_id)
        with conn() as c:
            if ledger.undo_last(c, analysis_id) is None:
                raise HTTPException(409, "Nothing to undo")
            refresh_quick_result(c, analysis_id)
        return RedirectResponse(f"/analyses/{analysis_id}#ledger", status_code=303)

    @app.post("/analyses/{analysis_id}/ledger/refresh")
    def ledger_refresh(analysis_id: int):
        """Size the quick result again, for instance after a new capacity workbook."""
        load(analysis_id)
        with conn() as c:
            refresh_quick_result(c, analysis_id)
        return RedirectResponse(f"/analyses/{analysis_id}#quick", status_code=303)

    @app.post("/analyses/{analysis_id}/ledger/{key}")
    def ledger_set(analysis_id: int, key: str, value: str = Form("")):
        row = load(analysis_id)
        if row["status"] != "done" or not row["result_json"]:
            raise HTTPException(409, "The analysis is not finished")
        try:
            parsed = agent_view.parse_value(key, value)
            with conn() as c:
                ledger.set_value(c, analysis_id, key, parsed, "user", "set on the Ledger tab")
                refresh_quick_result(c, analysis_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return RedirectResponse(f"/analyses/{analysis_id}#ledger", status_code=303)

    # ------------------------------------------------------------------ settings

    def settings_page(
        request: Request,
        msg: str | None = None,
        ok: bool = True,
        test: dict | None = None,
        playbook_text: str | None = None,
    ):
        cfg = llm_config.from_env()
        with conn() as c:
            current = playbook.active(c)
            hist = playbook.history(c)
        return render(
            request,
            "settings.html",
            cfg=cfg,
            variables=llm_config.status(cfg),
            ocr=documents.ocr_status(),
            msg=msg,
            ok=ok,
            test=test,
            playbook=playbook_text if playbook_text is not None else current,
            playbook_is_default=current == playbook.default_text(),
            playbook_history=hist,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def show_settings(request: Request, msg: str | None = None):
        return settings_page(request, msg)

    @app.post("/settings/test", response_class=HTMLResponse)
    def test_connection(request: Request):
        cfg = llm_config.from_env()
        if not cfg.enabled:
            return settings_page(
                request, "The LLM is off or incomplete: see the variables below.", ok=False
            )
        try:
            result = GatewayClient(cfg).test()
        except GatewayError as e:
            suffix = f" (request id {e.request_id})" if e.request_id else ""
            return settings_page(request, f"Test failed ({e.kind}): {e}{suffix}", ok=False)
        return settings_page(
            request, f"{cfg.label} answered in {result['seconds']} s.", test=result
        )

    @app.post("/settings/playbook", response_class=HTMLResponse)
    def save_playbook(request: Request, text: str = Form(""), action: str = Form("save")):
        with conn() as c:
            if action == "reset":
                playbook.reset(c)
                return settings_page(request, "Back to the built-in playbook.")
            try:
                playbook.save(c, text)
            except ValueError as e:
                return settings_page(request, str(e), ok=False, playbook_text=text)
        return settings_page(request, "Playbook saved. New agent runs use it.")

    @app.post("/analyses/{analysis_id}/delete")
    def delete_analysis(analysis_id: int):
        load(analysis_id)
        with conn() as c:
            for table in (
                "agent_message", "agent_run", "tool_result", "assumption", "verdict", "document",
            ):  # fmt: skip
                c.execute(f"DELETE FROM {table} WHERE analysis_id=?", (analysis_id,))  # noqa: S608
            c.execute("DELETE FROM analysis WHERE id=?", (analysis_id,))
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------------ portfolio

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio(request: Request, imported: int | None = None, msg: str | None = None):
        with conn() as c:
            docs = catalog.list_documents(c)
            models = catalog.list_models(c)
            families = catalog.list_families(c)
            # Performance values the sizing needs but the active import has no mapping for
            # (e.g. imported before the app learned a workbook's new layout).
            missing_perf = []
            for d in docs:
                if d["is_active"]:
                    mapped = {
                        r[0]
                        for r in c.execute(
                            "SELECT tsf_metric FROM tsf_metric_map WHERE document_id=?", (d["id"],)
                        )
                    }
                    missing_perf = [
                        m for m in ("perf.throughput_threat_gbps", "perf.cps") if m not in mapped
                    ]
            report = None
            if imported:
                r = c.execute(
                    "SELECT report_json FROM source_document WHERE id=?", (imported,)
                ).fetchone()
                report = json.loads(r[0]) if r and r[0] else None
        return render(
            request,
            "portfolio.html",
            families=families,
            active_ids={d["id"] for d in docs if d["is_active"]},
            docs=docs,
            models=models,
            report=report,
            imported=imported,
            msg=msg,
            missing_perf=missing_perf,
        )

    @app.post("/portfolio")
    async def upload_portfolio(workbook: UploadFile = File(...), force: str = Form("")):
        path = await save_upload(workbook, ".xlsx")
        try:
            with conn() as c:
                rep = import_workbook(
                    c,
                    path,
                    imported_by="web",
                    force=bool(force),
                    filename=Path(workbook.filename or "workbook.xlsx").name[:200],
                )
        except AlreadyImportedError as e:
            return RedirectResponse(
                f"/portfolio?msg=Already imported as document {e.document_id}. "
                "Switch on Re-import to import it again (needed after an app update).",
                status_code=303,
            )
        except Exception as e:  # noqa: BLE001 - show any workbook problem to the admin
            return RedirectResponse(
                f"/portfolio?msg=Import failed: {type(e).__name__}: {e}", status_code=303
            )
        finally:
            path.unlink(missing_ok=True)
        return RedirectResponse(f"/portfolio?imported={rep.document_id}", status_code=303)

    @app.post("/portfolio/families/{family}")
    def update_family(family: str, quotable: str = Form(""), superseded_by: str = Form("")):
        with conn() as c:
            known = {f["family"] for f in catalog.list_families(c)}
            if family not in known:
                raise HTTPException(404, "Unknown family")
            successor = superseded_by.strip() or None
            if successor is not None and (successor not in known or successor == family):
                raise HTTPException(400, "Successor must be another family in the portfolio")
            q = {"yes": 1, "no": 0}.get(quotable)
            catalog.set_family(c, family, quotable=q, superseded_by=successor)
        return RedirectResponse(f"/portfolio?msg=Saved settings for {family}", status_code=303)

    @app.post("/portfolio/{document_id}/activate")
    def activate_document(document_id: int):
        with conn() as c:
            try:
                activate(c, document_id)
            except KeyError:
                raise HTTPException(404, "Document not found") from None
        return RedirectResponse(
            f"/portfolio?msg=Document {document_id} is now active", status_code=303
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/") or exc.status_code == 401:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code,
        )

    return app
