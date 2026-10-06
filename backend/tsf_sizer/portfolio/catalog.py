"""Read access to the imported portfolio (used by the CLI, and later the sizing engine)."""

from __future__ import annotations

import sqlite3


def active_document(conn: sqlite3.Connection, release: str | None = None) -> sqlite3.Row | None:
    if release:
        return conn.execute(
            "SELECT * FROM source_document WHERE is_active=1 AND panos_release=?", (release,)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM source_document WHERE is_active=1 ORDER BY panos_release DESC, id DESC"
    ).fetchone()


def list_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, filename, sheet_name, panos_release, imported_at, is_active "
        "FROM source_document ORDER BY id"
    ).fetchall()


def list_models(conn: sqlite3.Connection, *, quotable_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM model_effective"
    if quotable_only:
        sql += " WHERE customer_quotable = 1"
    return conn.execute(sql + " ORDER BY family, name").fetchall()


def model_values(
    conn: sqlite3.Connection, model: str, document_id: int, category: str | None = None
) -> list[sqlite3.Row]:
    sql = """SELECT a.category, a.name, a.canonical_key, a.value_type, v.*
             FROM capacity_value v
             JOIN attribute a ON a.id = v.attribute_id
             JOIN model m ON m.id = v.model_id
             WHERE v.document_id = ? AND m.name = ?"""
    params: list = [document_id, model]
    if category:
        sql += " AND lower(a.category) LIKE ?"
        params.append(f"%{category.lower()}%")
    return conn.execute(sql + " ORDER BY v.sheet_row", params).fetchall()


def mapped_capacities(conn: sqlite3.Connection, model: str, document_id: int) -> dict[str, dict]:
    """Capacity values keyed by TSF metric, the shape the sizing engine will use."""
    rows = conn.execute(
        """SELECT t.tsf_metric, t.compare_as, a.name, v.kind, v.num_value, v.bool_value,
                  v.special, v.unit, v.unconfirmed, v.raw_value
           FROM tsf_metric_map t
           JOIN attribute a ON a.id = t.attribute_id
           JOIN model m ON m.name = ?
           LEFT JOIN capacity_value v
             ON v.document_id = t.document_id AND v.attribute_id = t.attribute_id
            AND v.model_id = m.id
           WHERE t.document_id = ?""",
        (model, document_id),
    ).fetchall()
    return {r["tsf_metric"]: dict(r) for r in rows}


def interface_ports(conn: sqlite3.Connection, model: str, document_id: int) -> dict[str, int]:
    rows = conn.execute(
        """SELECT p.speed_class, p.count FROM interface_port p
           JOIN model m ON m.id = p.model_id
           WHERE p.document_id = ? AND m.name = ?""",
        (document_id, model),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def set_model_override(conn: sqlite3.Connection, model: str, **fields) -> None:
    allowed = {
        "lifecycle",
        "customer_quotable",
        "eos_date",
        "form_factor_ru",
        "price_tier",
        "notes",
    }
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not fields:
        return
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
    with conn:
        conn.execute(
            f"INSERT INTO model_override (model_name, {cols}) VALUES (?, {marks}) "
            f"ON CONFLICT(model_name) DO UPDATE SET {updates}, updated_at=datetime('now')",
            [model, *fields.values()],
        )


def list_families(conn: sqlite3.Connection) -> list[dict]:
    """Families found in the portfolio with their team settings and model counts."""
    rows = conn.execute(
        """SELECT m.family,
                  count(*) AS models,
                  sum(CASE WHEN m.sheet_lifecycle = 'npi' THEN 1 ELSE 0 END) AS sheet_npi,
                  sum(CASE WHEN e.customer_quotable = 1 THEN 1 ELSE 0 END) AS quotable_models,
                  group_concat(m.name, ', ') AS names,
                  f.quotable, f.superseded_by, f.notes
           FROM model m
           JOIN model_effective e ON e.id = m.id
           LEFT JOIN family_setting f ON f.family = m.family
           WHERE m.kind != 'component' AND m.family IS NOT NULL
           GROUP BY m.family ORDER BY m.family"""
    ).fetchall()
    return [dict(r) for r in rows]


def superseded_families(conn: sqlite3.Connection) -> dict[str, str]:
    """{family: successor} for families marked as previous generation."""
    rows = conn.execute(
        "SELECT family, superseded_by FROM family_setting "
        "WHERE superseded_by IS NOT NULL AND superseded_by != ''"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def set_family(
    conn: sqlite3.Connection,
    family: str,
    *,
    quotable: int | None,
    superseded_by: str | None,
    notes: str | None = None,
) -> None:
    """Set a family's quotable override (None = follow the sheet) and successor."""
    with conn:
        conn.execute(
            """INSERT INTO family_setting (family, quotable, superseded_by, notes)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(family) DO UPDATE SET quotable=excluded.quotable,
                 superseded_by=excluded.superseded_by,
                 notes=COALESCE(excluded.notes, family_setting.notes),
                 updated_at=datetime('now')""",
            (family, quotable, superseded_by or None, notes),
        )
