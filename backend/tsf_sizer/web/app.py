"""FastAPI web app: upload a TSF, get a sizing report.

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
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import db
from ..llm import agent
from ..llm import settings as llm_settings
from ..llm.providers import (
    DEFAULT_BASE_URL,
    LOCAL_PROVIDERS,
    PROVIDERS,
    LLMError,
    chat,
    list_models,
)
from ..llm.writeup import SYSTEM_PROMPT, build_facts
from ..pipeline import regenerate_writeup, rerun_job, resize, run_job
from ..portfolio import catalog
from ..portfolio.importer import AlreadyImportedError, activate, import_workbook
from ..portfolio.supplements import apply_to_all_documents
from ..sizing.adjust import describe
from ..sizing.engine import SizingParams
from . import report as report_view

HERE = Path(__file__).parent
EXAMPLES = [
    "The customer no longer needs the optics",
    "He now needs two 10G fibre ports",
    "Expect 40% growth over 5 years",
    "Ignore the aggregate interface limit",
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
        # A summary that was being written: the sizing result itself is complete.
        conn.execute(
            "UPDATE analysis SET status='done' WHERE status='writing' AND result_json IS NOT NULL"
        )
        # A chat turn that was running when the app stopped will never finish.
        conn.execute(
            "UPDATE analysis_message SET status='error', content='Interrupted by a restart' "
            "WHERE status='pending'"
        )
        # Static datasheet models (PA-800) for versions imported before they existed.
        apply_to_all_documents(conn)

    app = FastAPI(title="TSF Sizer", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.executor = ThreadPoolExecutor(max_workers=settings.workers)
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
            llm = llm_settings.load(c)
        return render(
            request,
            "index.html",
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
        )

    @app.post("/analyses")
    async def create_analysis(
        tsf: UploadFile = File(...),
        config: UploadFile | None = File(None),
        customer: str = Form(""),
        growth_pct: float = Form(20.0),
        years: int = Form(3),
        target_util_pct: float = Form(70.0),
        peak_throughput_mbps: str = Form(""),
        peak_cps: str = Form(""),
        peak_sessions: str = Form(""),
        port_rule: str = Form("all"),
        ai_summary: str = Form(""),
        include_superseded: str = Form(""),
        need_poe: str = Form(""),
        max_size_factor: float = Form(5.0),
    ):
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
        )
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
        app.state.executor.submit(
            run_job,
            settings.db_path,
            analysis_id,
            tsf_path,
            config_path,
            params,
            filename,
            config_name,
            bool(ai_summary),
        )
        return RedirectResponse(f"/analyses/{analysis_id}", status_code=303)

    def stored_params(row) -> SizingParams:
        try:
            return SizingParams(**json.loads(row["params_json"] or "{}"))
        except TypeError:  # parameters saved by another version
            return SizingParams()

    def load(analysis_id: int):
        with conn() as c:
            row = c.execute("SELECT * FROM analysis WHERE id=?", (analysis_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Analysis not found")
        return row

    @app.post("/analyses/{analysis_id}/rerun")
    def rerun_analysis(
        analysis_id: int,
        growth_pct: float = Form(20.0),
        years: int = Form(3),
        target_util_pct: float = Form(70.0),
        peak_throughput_mbps: str = Form(""),
        peak_cps: str = Form(""),
        peak_sessions: str = Form(""),
        port_rule: str = Form("all"),
        ai_summary: str = Form(""),
        include_superseded: str = Form(""),
        need_poe: str = Form(""),
        max_size_factor: float = Form(5.0),
    ):
        """Size a finished analysis again with other assumptions; no re-upload needed."""
        row = load(analysis_id)
        if row["status"] != "done" or not row["result_json"]:
            raise HTTPException(409, "Only a finished analysis can be re-run")
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
        )
        old = stored_params(row)  # the form has no fields for the assistant's adjustments
        params.port_drop, params.port_add = old.port_drop, old.port_add
        params.soft_metrics, params.exclude_models = old.soft_metrics, old.exclude_models
        with conn() as c:
            c.execute("UPDATE analysis SET status='running', error=NULL WHERE id=?", (analysis_id,))
        app.state.executor.submit(
            rerun_job, settings.db_path, analysis_id, params, bool(ai_summary)
        )
        return RedirectResponse(f"/analyses/{analysis_id}", status_code=303)

    @app.get("/analyses/{analysis_id}", response_class=HTMLResponse)
    def show_analysis(request: Request, analysis_id: int):
        row = load(analysis_id)
        if row["status"] != "done":
            return render(request, "status.html", a=dict(row))
        result = json.loads(row["result_json"])
        view = report_view.build(result, dict(row))
        with conn() as c:
            superseded = catalog.superseded_families(c)
            llm = llm_settings.load(c)
        opts = stored_params(row)
        with conn() as c:
            messages = chat_messages(c, analysis_id)
        return render(
            request,
            "report.html",
            a=dict(row),
            r=result,
            v=view,
            opts=opts,
            superseded=superseded,
            llm=llm,
            had_writeup="writeup" in result,
            messages=messages,
            adjustments=describe(opts, agent.metric_catalog()),
            examples=EXAMPLES,
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

    @app.post("/analyses/{analysis_id}/summary")
    def rewrite_summary(analysis_id: int):
        row = load(analysis_id)
        if row["status"] != "done":
            raise HTTPException(409, "The analysis is not finished")
        with conn() as c:
            if not llm_settings.load(c).enabled:
                raise HTTPException(400, "No LLM configured: set one up on the Settings page")
            c.execute("UPDATE analysis SET status='writing' WHERE id=?", (analysis_id,))
        app.state.executor.submit(regenerate_writeup, settings.db_path, analysis_id)
        return RedirectResponse(f"/analyses/{analysis_id}", status_code=303)

    # ------------------------------------------------------------------ LLM settings

    def settings_page(
        request: Request,
        msg: str | None = None,
        ok: bool = True,
        models: list[str] | None = None,
        test: dict | None = None,
    ):
        with conn() as c:
            cfg = llm_settings.load(c)
            last = c.execute(
                "SELECT result_json FROM analysis WHERE status='done' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        preview = None
        if last and last[0]:
            preview = json.dumps(build_facts(json.loads(last[0])), indent=1, ensure_ascii=False)
        return render(
            request,
            "settings.html",
            cfg=cfg,
            providers=PROVIDERS,
            default_urls=DEFAULT_BASE_URL,
            local=cfg.provider in LOCAL_PROVIDERS,
            env_provider=os.environ.get("TSF_SIZER_LLM_PROVIDER", "none"),
            api_key_set=bool(cfg.api_key),
            msg=msg,
            ok=ok,
            models=models,
            test=test,
            preview=preview,
            system_prompt=SYSTEM_PROMPT,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def show_settings(request: Request, msg: str | None = None):
        return settings_page(request, msg)

    @app.post("/settings", response_class=HTMLResponse)
    def save_settings(
        request: Request,
        provider: str = Form("none"),
        base_url: str = Form(""),
        model: str = Form(""),
        temperature: float = Form(0.2),
        max_tokens: int = Form(900),
        timeout: float = Form(180),
        action: str = Form("save"),
    ):
        if provider not in PROVIDERS:
            raise HTTPException(400, "Unknown provider")
        if not (0 <= temperature <= 2 and 100 <= max_tokens <= 8000 and 5 <= timeout <= 1800):
            raise HTTPException(400, "Temperature 0-2, max tokens 100-8000, timeout 5-1800 s")
        base_url = base_url.strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            raise HTTPException(400, "The server URL must start with http:// or https://")
        with conn() as c:
            if action == "reset":
                llm_settings.reset(c)
                return settings_page(request, "Back to the container's environment settings.")
            llm_settings.save(
                c,
                provider=provider,
                base_url=base_url,
                model=model.strip(),
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            cfg = llm_settings.load(c)
        if action == "models":
            try:
                found = list_models(cfg)
            except LLMError as e:
                return settings_page(request, f"Could not list models: {e}", ok=False)
            if not found:
                return settings_page(
                    request,
                    "Connected, but the server has no models. For Ollama: ollama pull <model>.",
                    ok=False,
                    models=[],
                )
            return settings_page(request, f"Found {len(found)} model(s).", models=found)
        if action == "test":
            if not cfg.enabled:
                return settings_page(request, "Choose a provider and model first.", ok=False)
            try:
                reply = chat(cfg, "You are a connectivity test.", "Reply with the single word OK.")
            except LLMError as e:
                return settings_page(request, f"Test failed: {e}", ok=False)
            return settings_page(
                request,
                f"{cfg.label} answered in {reply.seconds} s.",
                test={"text": reply.text[:200]},
            )
        return settings_page(request, "Settings saved.")

    # ------------------------------------------------------------------ assistant

    def chat_messages(c, analysis_id: int) -> list[dict]:
        out = []
        for m in c.execute(
            "SELECT id, role, content, status, changes_json, undone FROM analysis_message "
            "WHERE analysis_id=? ORDER BY id",
            (analysis_id,),
        ):
            d = dict(m)
            d["changes"] = json.loads(d.pop("changes_json") or "null")
            out.append(d)
        return out

    def last_undoable(c, analysis_id: int):
        return c.execute(
            "SELECT id, params_before_json FROM analysis_message WHERE analysis_id=? "
            "AND role='assistant' AND params_before_json IS NOT NULL AND undone=0 "
            "AND status='done' ORDER BY id DESC LIMIT 1",
            (analysis_id,),
        ).fetchone()

    @app.get("/api/analyses/{analysis_id}/chat")
    def chat_state(analysis_id: int):
        load(analysis_id)
        with conn() as c:
            msgs = chat_messages(c, analysis_id)
            can_undo = last_undoable(c, analysis_id) is not None
        return {"messages": msgs, "can_undo": can_undo}

    @app.post("/analyses/{analysis_id}/chat")
    def chat_send(request: Request, analysis_id: int, message: str = Form("")):
        row = load(analysis_id)
        text = message.strip()
        if row["status"] != "done" or not row["result_json"]:
            raise HTTPException(409, "The analysis is not finished")
        if not text or len(text) > agent.MAX_MESSAGE:
            raise HTTPException(400, f"Write a message of 1-{agent.MAX_MESSAGE} characters")
        with conn() as c:
            if not llm_settings.load(c).enabled:
                raise HTTPException(400, "No LLM configured: set one up on the AI settings page")
            c.execute(  # a turn that has been "thinking" for half an hour is lost
                "UPDATE analysis_message SET status='error', content='No answer' "
                "WHERE status='pending' AND created_at < datetime('now', '-30 minutes')"
            )
            if c.execute(
                "SELECT 1 FROM analysis_message WHERE analysis_id=? AND status='pending'",
                (analysis_id,),
            ).fetchone():
                raise HTTPException(409, "The assistant is still working on the last message")
            uid = c.execute(
                "INSERT INTO analysis_message (analysis_id, role, content) VALUES (?, 'user', ?)",
                (analysis_id, text),
            ).lastrowid
            aid = c.execute(
                "INSERT INTO analysis_message (analysis_id, role, status) "
                "VALUES (?, 'assistant', 'pending')",
                (analysis_id,),
            ).lastrowid
        app.state.executor.submit(agent.run_turn, settings.db_path, analysis_id, uid, aid)
        if "application/json" in request.headers.get("accept", ""):
            return {"user_id": uid, "assistant_id": aid}
        return RedirectResponse(f"/analyses/{analysis_id}#assistant", status_code=303)

    def restore(analysis_id: int, params: SizingParams, note: str) -> None:
        with conn() as c:
            row = c.execute(
                "SELECT result_json FROM analysis WHERE id=?", (analysis_id,)
            ).fetchone()
            result = resize(c, json.loads(row[0]), params)
            c.execute(
                "UPDATE analysis SET params_json=?, result_json=?, finished_at=datetime('now') "
                "WHERE id=?",
                (json.dumps(asdict(params)), json.dumps(result, default=str), analysis_id),
            )
            c.execute(
                "INSERT INTO analysis_message (analysis_id, role, content) "
                "VALUES (?, 'assistant', ?)",
                (analysis_id, note),
            )

    @app.post("/analyses/{analysis_id}/chat/undo")
    def chat_undo(analysis_id: int):
        load(analysis_id)
        with conn() as c:
            last = last_undoable(c, analysis_id)
            if last is None:
                raise HTTPException(409, "Nothing to undo")
            c.execute("UPDATE analysis_message SET undone=1 WHERE id=?", (last["id"],))
        before = SizingParams(**json.loads(last["params_before_json"]))
        restore(analysis_id, before, "Undid the last change.")
        return RedirectResponse(f"/analyses/{analysis_id}#assistant", status_code=303)

    @app.post("/analyses/{analysis_id}/adjustments/clear")
    def clear_adjustments(analysis_id: int):
        row = load(analysis_id)
        if row["status"] != "done" or not row["result_json"]:
            raise HTTPException(409, "The analysis is not finished")
        params = stored_params(row)
        params.port_drop, params.port_add = [], {}
        params.soft_metrics, params.exclude_models = [], []
        restore(analysis_id, params, "Cleared all adjustments.")
        return RedirectResponse(f"/analyses/{analysis_id}#assistant", status_code=303)

    @app.post("/analyses/{analysis_id}/delete")
    def delete_analysis(analysis_id: int):
        load(analysis_id)
        with conn() as c:
            c.execute("DELETE FROM analysis_message WHERE analysis_id=?", (analysis_id,))
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
