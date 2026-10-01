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
import os
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import db
from ..pipeline import run_job
from ..portfolio import catalog
from ..portfolio.importer import AlreadyImportedError, activate, import_workbook
from ..sizing.engine import SizingParams
from . import report as report_view

HERE = Path(__file__).parent
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

    app = FastAPI(title="TSF Sizer", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.executor = ThreadPoolExecutor(max_workers=settings.workers)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    report_view.register_filters(templates.env)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

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

    def conn():
        return db.connect(settings.db_path)

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
        return render(
            request,
            "index.html",
            superseded=superseded,
            portfolio=doc,
            history=history,
            defaults=SizingParams(),
            max_mb=settings.max_upload // CHUNK,
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
        include_superseded: str = Form(""),
        max_size_factor: float = Form(5.0),
    ):
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
        params = SizingParams(
            growth_pct_per_year=growth_pct,
            years=years,
            target_util_pct=target_util_pct,
            peak_throughput_mbps=opt(peak_throughput_mbps),
            peak_cps=opt(peak_cps),
            peak_sessions=opt(peak_sessions),
            port_rule="used" if port_rule == "used" else "all",
            include_superseded=bool(include_superseded),
            max_size_factor=max_size_factor,
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
        )
        return RedirectResponse(f"/analyses/{analysis_id}", status_code=303)

    def load(analysis_id: int):
        with conn() as c:
            row = c.execute("SELECT * FROM analysis WHERE id=?", (analysis_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Analysis not found")
        return row

    @app.get("/analyses/{analysis_id}", response_class=HTMLResponse)
    def show_analysis(request: Request, analysis_id: int):
        row = load(analysis_id)
        if row["status"] != "done":
            return render(request, "status.html", a=dict(row))
        result = json.loads(row["result_json"])
        view = report_view.build(result, dict(row))
        return render(request, "report.html", a=dict(row), r=result, v=view)

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

    @app.post("/analyses/{analysis_id}/delete")
    def delete_analysis(analysis_id: int):
        load(analysis_id)
        with conn() as c:
            c.execute("DELETE FROM analysis WHERE id=?", (analysis_id,))
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------------ portfolio

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio(request: Request, imported: int | None = None, msg: str | None = None):
        with conn() as c:
            docs = catalog.list_documents(c)
            models = catalog.list_models(c)
            families = catalog.list_families(c)
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
            docs=docs,
            models=models,
            report=report,
            imported=imported,
            msg=msg,
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
                f"/portfolio?msg=Already imported as document {e.document_id}", status_code=303
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
