"""End-to-end analysis of one TSF: parse -> summarize -> compare -> size.

Used by the web app (in a background thread) and usable from scripts.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import db
from .llm import settings as llm_settings
from .llm.writeup import write as write_summary
from .portfolio import catalog
from .sizing.engine import SizingParams, size
from .tsf.archive import read_tsf
from .tsf.compare import usage_against_model
from .tsf.config import count_config
from .tsf.metrics import summarize
from .tsf.techsupport import parse_techsupport


def analyze(
    conn: sqlite3.Connection,
    tsf_path: Path,
    params: SizingParams,
    config_path: Path | None = None,
    source_name: str | None = None,
    config_name: str | None = None,
) -> dict:
    files = read_tsf(tsf_path)
    if source_name and files.member_names.get("techsupport") == tsf_path.name:
        files.member_names["techsupport"] = source_name  # loose .txt upload: show its real name
    facts = parse_techsupport(files.text("techsupport"))

    config, config_source = None, None
    if config_path is not None:
        config = count_config(config_path.read_bytes())
        config_source = config_name or "uploaded config XML"
    else:
        for key in ("merged_config", "running_config"):
            if key in files.files:
                config = count_config(files.files[key])
                config_source = files.member_names[key]
                break

    summary = summarize(facts, config, config_source)
    doc = catalog.active_document(conn)
    usage, sizing, portfolio_error = None, None, None
    if doc is None:
        portfolio_error = "No active portfolio: import the capacity workbook on the Portfolio page."
    else:
        try:
            usage = usage_against_model(conn, summary, document_id=doc["id"])
        except (LookupError, ValueError) as e:
            portfolio_error = str(e)
        sizing = size(conn, summary, params, document_id=doc["id"])

    return {
        "source": source_name or files.source,
        "files_used": files.member_names,
        "summary": summary.to_dict(),
        "facts": {
            "resource_series": facts.resource_monitor.get("series", {}),
            "licenses": facts.licenses,
            "counters_of_interest": facts.counters_of_interest,
        },
        "config": asdict(config) if config else None,
        "usage": usage.to_dict() if usage else None,
        "sizing": sizing.to_dict() if sizing else None,
        "portfolio": dict(doc) if doc is not None else None,
        "portfolio_error": portfolio_error,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }


def run_job(
    db_path: str,
    analysis_id: int,
    tsf_path: Path,
    config_path: Path | None,
    params: SizingParams,
    source_name: str,
    config_name: str | None = None,
    ai_summary: bool = True,
) -> None:
    """Background job: never raises; records the outcome on the analysis row.

    Uploaded files are deleted afterwards whatever happens.
    """
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute("UPDATE analysis SET status='running' WHERE id=?", (analysis_id,))
        result = analyze(conn, tsf_path, params, config_path, source_name, config_name)
        if ai_summary:
            _add_writeup(conn, analysis_id, result)
        with conn:
            conn.execute(
                "UPDATE analysis SET status='done', result_json=?, finished_at=datetime('now') "
                "WHERE id=?",
                (json.dumps(result, default=str), analysis_id),
            )
    except Exception as e:  # noqa: BLE001 - any failure must reach the user, not kill the worker
        for attempt in range(3):  # the error must be recorded, or the page waits forever
            try:
                with conn:
                    conn.execute(
                        "UPDATE analysis SET status='error', error=?, "
                        "finished_at=datetime('now') WHERE id=?",
                        (f"{type(e).__name__}: {e}", analysis_id),
                    )
                break
            except sqlite3.OperationalError:
                time.sleep(1 + attempt)
    finally:
        conn.close()
        for p in (tsf_path, config_path):
            if p is not None:
                p.unlink(missing_ok=True)


def _add_writeup(conn: sqlite3.Connection, analysis_id: int, result: dict) -> None:
    """Ask the configured LLM for the written justification (if one is configured).

    Failures end up in result['writeup']['error']; the analysis itself never fails on it.
    """
    cfg = llm_settings.load(conn)
    if not cfg.enabled:
        return
    with conn:
        conn.execute("UPDATE analysis SET status='writing' WHERE id=?", (analysis_id,))
    result["writeup"] = write_summary(cfg, result)


def regenerate_writeup(db_path: str, analysis_id: int) -> None:
    """Background job: (re)write the summary of a finished analysis with the current LLM."""
    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT result_json FROM analysis WHERE id=?", (analysis_id,)).fetchone()
        if row is None or not row[0]:
            return
        result = json.loads(row[0])
        try:
            _add_writeup(conn, analysis_id, result)
        finally:
            with conn:
                conn.execute(
                    "UPDATE analysis SET status='done', result_json=? WHERE id=?",
                    (json.dumps(result, default=str), analysis_id),
                )
    finally:
        conn.close()
