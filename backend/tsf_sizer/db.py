"""SQLite storage for the portfolio catalogue (and, later, analyses)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "data/app.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

-- One row per imported workbook sheet. Values are versioned by document so
-- releases (12.1.2, 12.1.5, ...) and re-imports can live side by side.
CREATE TABLE IF NOT EXISTS source_document (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- ids never reused (audit trail)
    filename      TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    sheet_name    TEXT NOT NULL,
    panos_release TEXT,
    imported_at   TEXT NOT NULL DEFAULT (datetime('now')),
    imported_by   TEXT,
    is_active     INTEGER NOT NULL DEFAULT 0,
    report_json   TEXT,
    UNIQUE (sha256, sheet_name)
);

-- Model identity is global; what the sheet says about it is per document.
CREATE TABLE IF NOT EXISTS model (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    family          TEXT,
    kind            TEXT NOT NULL,          -- appliance | component | vm
    parent_model_id INTEGER REFERENCES model(id),
    sheet_lifecycle TEXT NOT NULL,          -- lifecycle derived from the workbook (npi | current)
    sheet_column    TEXT                    -- column header it came from
);

-- Team-maintained metadata. Never overwritten by an import.
CREATE TABLE IF NOT EXISTS model_override (
    model_name        TEXT PRIMARY KEY,
    lifecycle         TEXT,                 -- current | npi | eos | eol
    customer_quotable INTEGER,              -- 0/1
    eos_date          TEXT,
    form_factor_ru    REAL,
    price_tier        TEXT,
    notes             TEXT,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS attribute (
    id            INTEGER PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,     -- e.g. objects_addresses_services.max_address_entries
    category      TEXT NOT NULL,
    name          TEXT NOT NULL,
    parent_key    TEXT,                     -- for indented sub-rows
    value_type    TEXT NOT NULL,            -- number | bool | text
    unit          TEXT,
    is_throughput INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS capacity_value (
    document_id  INTEGER NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    model_id     INTEGER NOT NULL REFERENCES model(id),
    attribute_id INTEGER NOT NULL REFERENCES attribute(id),
    sheet_row    INTEGER,
    raw_value    TEXT,
    kind         TEXT NOT NULL,             -- number | bool | text | special | empty
    num_value    REAL,
    bool_value   INTEGER,
    text_value   TEXT,
    unit         TEXT,
    special      TEXT,                      -- not_applicable | system_limit | configurable | ...
    unconfirmed  INTEGER NOT NULL DEFAULT 0,-- orange cell in the workbook
    assumed      INTEGER NOT NULL DEFAULT 0,-- interpreted with an assumption (see note)
    note         TEXT,
    PRIMARY KEY (document_id, model_id, attribute_id)
);

CREATE TABLE IF NOT EXISTS interface_port (
    document_id INTEGER NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    model_id    INTEGER NOT NULL REFERENCES model(id),
    speed_class TEXT NOT NULL,              -- e.g. 10G_SFP+, 25G_SFP28
    label       TEXT NOT NULL,              -- original row label
    count       INTEGER NOT NULL,
    unconfirmed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (document_id, model_id, speed_class)
);

-- Which TSF metric is compared against which capacity attribute.
CREATE TABLE IF NOT EXISTS tsf_metric_map (
    document_id  INTEGER NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    tsf_metric   TEXT NOT NULL,
    attribute_id INTEGER NOT NULL REFERENCES attribute(id),
    compare_as   TEXT NOT NULL,             -- count | throughput | rate | feature
    PRIMARY KEY (document_id, tsf_metric)
);

-- One row per uploaded TSF analysis (the web app's history).
CREATE TABLE IF NOT EXISTS analysis (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    customer     TEXT,
    filename     TEXT NOT NULL,
    status       TEXT NOT NULL,            -- queued | running | done | error
    error        TEXT,
    params_json  TEXT NOT NULL,
    result_json  TEXT,
    created_by   TEXT
);

-- Team-maintained settings per product family. Never overwritten by an import.
--   quotable:       1/0 overrides the sheet's NPI status for the whole family
--   superseded_by:  family that replaces this one; superseded families are
--                   left out of recommendations unless the user includes them
CREATE TABLE IF NOT EXISTS family_setting (
    family        TEXT PRIMARY KEY,
    quotable      INTEGER,
    superseded_by TEXT,
    notes         TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Defaults from the presales team (2026-10): the PA-500 series is released and
-- succeeds the PA-400. Inserted once; edits on the Portfolio page are kept.
INSERT OR IGNORE INTO family_setting (family, quotable, superseded_by, notes) VALUES
    ('PA-500', 1, NULL, 'Released; successor of the PA-400 series'),
    ('PA-400', NULL, 'PA-500', 'Previous generation');

-- Effective model metadata: workbook-derived values with family and model
-- overrides on top (model override wins over family setting wins over sheet).
DROP VIEW IF EXISTS model_effective;
CREATE VIEW model_effective AS
SELECT
    m.id, m.name, m.family, m.kind, m.parent_model_id, m.sheet_column,
    COALESCE(
        o.lifecycle,
        CASE WHEN f.quotable = 1 AND m.sheet_lifecycle = 'npi' THEN 'current' END,
        m.sheet_lifecycle
    ) AS lifecycle,
    COALESCE(
        o.customer_quotable,
        CASE WHEN m.kind = 'component' THEN 0 END,
        f.quotable,
        CASE WHEN m.sheet_lifecycle = 'npi' THEN 0 ELSE 1 END
    ) AS customer_quotable,
    f.superseded_by,
    o.eos_date, o.form_factor_ru, o.price_tier, o.notes
FROM model m
LEFT JOIN model_override o ON o.model_name = m.name
LEFT JOIN family_setting f ON f.family = m.family;
"""


def db_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.environ.get("TSF_SIZER_DB") or DEFAULT_DB_PATH)


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    p = db_path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
