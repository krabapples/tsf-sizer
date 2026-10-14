"""The sizing policy as editable text. The default ships with the application; saved edits
are versioned in the database and the newest active one is used."""

from __future__ import annotations

import sqlite3
from importlib import resources

MAX_CHARS = 20000


def default_text() -> str:
    return resources.files("tsf_sizer.agent").joinpath("playbook.md").read_text(encoding="utf-8")


def active(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT text FROM playbook_version WHERE active=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["text"] if row else default_text()


def save(conn: sqlite3.Connection, text: str) -> None:
    text = text.strip()
    if not text:
        raise ValueError("The playbook cannot be empty")
    if len(text) > MAX_CHARS:
        raise ValueError(f"The playbook is limited to {MAX_CHARS} characters")
    with conn:
        conn.execute("UPDATE playbook_version SET active=0 WHERE active=1")
        conn.execute("INSERT INTO playbook_version (text) VALUES (?)", (text,))


def reset(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("UPDATE playbook_version SET active=0 WHERE active=1")


def history(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    return [
        {"id": r["id"], "created_at": r["created_at"], "active": bool(r["active"])}
        for r in conn.execute(
            "SELECT id, created_at, active FROM playbook_version ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    ]
