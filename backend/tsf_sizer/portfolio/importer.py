"""Import a capacity workbook into SQLite and produce an import report."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import values as V
from .mapping import (
    INTERFACE_ROW_PREFIX,
    INTERFACE_ROWS,
    TSF_METRIC_MAP,
    is_throughput_row,
    norm,
    slug,
)
from .models import expand_headers, npi_sets, sheet_lifecycle
from .workbook import AttributeRow, read_workbook

# How many example cells to list per review category in the report.
MAX_EXAMPLES = 400


class AlreadyImportedError(Exception):
    def __init__(self, document_id: int):
        super().__init__(f"This workbook sheet was already imported as document {document_id}")
        self.document_id = document_id


@dataclass
class ReviewItem:
    reason: str
    model: str
    category: str
    attribute: str
    sheet_row: int
    raw: str | None
    interpreted_as: str


@dataclass
class ImportReport:
    document_id: int
    filename: str
    sheet_name: str
    panos_release: str | None
    activated: bool
    models: dict[str, list[str]] = field(default_factory=dict)
    attribute_count: int = 0
    cell_count: int = 0
    kind_counts: dict[str, int] = field(default_factory=dict)
    special_counts: dict[str, int] = field(default_factory=dict)
    unconfirmed_count: int = 0
    assumed_count: int = 0
    duplicate_attributes: list[str] = field(default_factory=list)
    needs_review: list[ReviewItem] = field(default_factory=list)
    needs_review_total: int = 0
    info: list[ReviewItem] = field(default_factory=list)
    info_total: int = 0
    info_counts: dict[str, int] = field(default_factory=dict)
    needs_review_counts: dict[str, int] = field(default_factory=dict)
    interface_ports: dict[str, dict[str, int]] = field(default_factory=dict)
    unknown_interface_rows: list[str] = field(default_factory=list)
    tsf_map_resolved: int = 0
    tsf_map_unresolved: list[str] = field(default_factory=list)
    diff: dict | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def to_text(self) -> str:
        lines = [
            f"Import report: document {self.document_id}",
            f"  File:       {self.filename}",
            f"  Sheet:      {self.sheet_name}  (PAN-OS {self.panos_release or '?'})",
            f"  Active:     {'yes' if self.activated else 'no (run activate to use it)'}",
            "",
            f"Models ({sum(len(v) for v in self.models.values())}):",
        ]
        for group, names in self.models.items():
            lines.append(f"  {group:<22} {', '.join(names) if names else '-'}")
        lines += [
            "",
            f"Attributes: {self.attribute_count}   Cells: {self.cell_count}",
            "  By kind:    " + _counts(self.kind_counts),
            "  Special:    " + _counts(self.special_counts),
            f"  Unconfirmed (orange) cells: {self.unconfirmed_count}",
            f"  Interpreted with an assumption: {self.assumed_count}",
        ]
        if self.duplicate_attributes:
            lines.append(
                "  Duplicate row names (stored with __2 suffix): "
                + "; ".join(self.duplicate_attributes)
            )
        lines += ["", f"Needs review: {self.needs_review_total} cell(s)"]
        by_reason: dict[str, list[ReviewItem]] = defaultdict(list)
        for item in self.needs_review:
            by_reason[item.reason].append(item)
        for reason, items in by_reason.items():
            lines.append(f"  [{reason}] {self.needs_review_counts.get(reason, len(items))}")
            for it in items[:15]:
                lines.append(
                    f"    row {it.sheet_row:>3} {it.model:<14} {it.attribute[:48]:<48} {it.raw!r}"
                )
            shown = min(len(items), 15)
            total = self.needs_review_counts.get(reason, len(items))
            if total > shown:
                lines.append(f"    ... {total - shown} more (see JSON report)")
        lines += ["", f"Informational: {self.info_total} cell(s)"]
        for reason, n in sorted(self.info_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  [{reason}] {n}")
        lines += ["", "Interface ports (from 'Traffic - ...' rows):"]
        for model, ports in self.interface_ports.items():
            desc = ", ".join(f"{n}x {cls}" for cls, n in ports.items()) or "-"
            lines.append(f"  {model:<22} {desc}")
        if self.unknown_interface_rows:
            lines.append("  Unknown interface rows: " + "; ".join(self.unknown_interface_rows))
        lines += [
            "",
            f"TSF metric mapping: {self.tsf_map_resolved} resolved, "
            f"{len(self.tsf_map_unresolved)} unresolved",
        ]
        for u in self.tsf_map_unresolved:
            lines.append(f"  unresolved: {u}")
        if self.diff is not None:
            d = self.diff
            lines += [
                "",
                f"Changes vs document {d['previous_document_id']}: "
                f"{d['changed_total']} changed, {d['added_total']} added, "
                f"{d['removed_total']} removed",
            ]
            for ch in d["changed"][:30]:
                lines.append(
                    f"  {ch['model']:<14} {ch['attribute'][:50]:<50} {ch['old']!r} -> {ch['new']!r}"
                )
        if self.warnings:
            lines += ["", "Warnings:"] + [f"  - {w}" for w in self.warnings]
        return "\n".join(lines)


def _counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def _describe(p: V.ParsedValue) -> str:
    if p.kind == V.NUMBER:
        s = f"{p.num:g}" + (f" {p.unit}" if p.unit else "")
        return s + (" (supported)" if p.bool_ else "")
    if p.kind == V.BOOL:
        return "yes" if p.bool_ else "no"
    if p.kind == V.SPECIAL:
        return p.special or "special"
    if p.kind == V.TEXT:
        return f"text {p.text!r}"
    return "empty"


def _attribute_type(parsed: list[V.ParsedValue]) -> str:
    """Decide what a row holds.

    Rows like BFD sessions ("1024" on big models, "No" on small ones) or
    Aggregate Interfaces (32 / "Yes") are numeric limits where Yes/No mean
    "supported, no fixed number" / "not supported". So a row counts as numeric
    as soon as a quarter of its typed cells are numbers.
    """
    kinds = Counter(p.kind for p in parsed if p.kind in (V.NUMBER, V.BOOL, V.TEXT))
    total = sum(kinds.values())
    if not total:
        return "text"
    if kinds[V.NUMBER] and kinds[V.NUMBER] * 4 >= total:
        return "number"
    kind, _ = kinds.most_common(1)[0]
    return kind


def _upsert_attribute(conn, key, row: AttributeRow, parent_key, value_type, unit, thr) -> int:
    conn.execute(
        """INSERT INTO attribute (canonical_key, category, name, parent_key, value_type, unit,
                                  is_throughput)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(canonical_key) DO UPDATE SET
             category=excluded.category, name=excluded.name, parent_key=excluded.parent_key,
             value_type=excluded.value_type, unit=excluded.unit,
             is_throughput=excluded.is_throughput""",
        (key, row.category, row.name, parent_key, value_type, unit, int(thr)),
    )
    return conn.execute("SELECT id FROM attribute WHERE canonical_key=?", (key,)).fetchone()[0]


def _upsert_model(conn, spec, lifecycle) -> int:
    parent_id = None
    if spec.parent:
        r = conn.execute("SELECT id FROM model WHERE name=?", (spec.parent,)).fetchone()
        parent_id = r[0] if r else None
    conn.execute(
        """INSERT INTO model (name, family, kind, parent_model_id, sheet_lifecycle, sheet_column)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
             family=excluded.family, kind=excluded.kind, parent_model_id=excluded.parent_model_id,
             sheet_lifecycle=excluded.sheet_lifecycle, sheet_column=excluded.sheet_column""",
        (spec.name, spec.family, spec.kind, parent_id, lifecycle, spec.header),
    )
    return conn.execute("SELECT id FROM model WHERE name=?", (spec.name,)).fetchone()[0]


def import_workbook(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    activate: bool = False,
    imported_by: str | None = None,
    force: bool = False,
    filename: str | None = None,
) -> ImportReport:
    """Import a workbook. `filename` is the name to record when `path` is a temp file."""
    wb = read_workbook(path)
    display_name = filename or wb.path.name

    existing = conn.execute(
        "SELECT id, is_active FROM source_document WHERE sha256=? AND sheet_name=?",
        (wb.sha256, wb.sheet_name),
    ).fetchone()
    if existing and not force:
        raise AlreadyImportedError(existing[0])

    with conn:
        if existing:
            conn.execute("DELETE FROM source_document WHERE id=?", (existing[0],))
        cur = conn.execute(
            """INSERT INTO source_document (filename, sha256, sheet_name, panos_release,
                                            imported_by)
               VALUES (?, ?, ?, ?, ?)""",
            (display_name, wb.sha256, wb.sheet_name, wb.release, imported_by),
        )
        doc_id = cur.lastrowid
        report = ImportReport(doc_id, display_name, wb.sheet_name, wb.release, activated=False)
        report.warnings.extend(wb.warnings)

        # Models. Parents first so components can reference them.
        specs = sorted(expand_headers(wb.model_columns), key=lambda s: s.parent is not None)
        npi_models, npi_families = npi_sets(wb.summary)
        if not wb.summary:
            report.warnings.append(
                "No Summary sheet found: NPI (unreleased) models could not be detected. "
                "Mark them with set-model before quoting."
            )
        model_ids: dict[str, int] = {}
        groups: dict[str, list[str]] = {"quotable": [], "npi (not quotable)": [], "components": []}
        for spec in specs:
            lc = sheet_lifecycle(spec, npi_models, npi_families)
            model_ids[spec.name] = _upsert_model(conn, spec, lc)
            if spec.kind == "component":
                groups["components"].append(spec.name)
            elif lc == "npi":
                groups["npi (not quotable)"].append(spec.name)
            else:
                groups["quotable"].append(spec.name)
        report.models = groups
        unknown_npi = sorted(n for n in npi_models if n not in {s.name.upper() for s in specs})
        if unknown_npi:
            report.warnings.append(
                "Summary sheet lists NPI models without a capacity column: "
                + ", ".join(unknown_npi)
            )

        # Attributes and values.
        seen_keys: Counter[str] = Counter()
        parent_key: str | None = None
        attr_by_name: dict[tuple[str, str], int] = {}
        kind_counts: Counter[str] = Counter()
        special_counts: Counter[str] = Counter()
        review: list[ReviewItem] = []
        info: list[ReviewItem] = []

        for row in wb.rows:
            base_key = f"{slug(row.category)}.{slug(row.name)}"
            seen_keys[base_key] += 1
            key = base_key if seen_keys[base_key] == 1 else f"{base_key}__{seen_keys[base_key]}"
            if seen_keys[base_key] > 1:
                report.duplicate_attributes.append(
                    f"{row.category} / {row.name} (row {row.sheet_row})"
                )
            if not row.indented:
                parent_key = None
            thr = is_throughput_row(row.category, row.name)
            parsed = {
                col: V.parse_cell(cell.value, throughput_row=thr) for col, cell in row.cells.items()
            }
            vtype = _attribute_type(list(parsed.values()))
            unit = "Gbps" if thr else None
            attr_id = _upsert_attribute(
                conn, key, row, parent_key if row.indented else None, vtype, unit, thr
            )
            attr_by_name.setdefault((norm(row.category), norm(row.name)), attr_id)
            if not row.indented:
                parent_key = key
            report.attribute_count += 1

            for spec in specs:
                p = parsed[spec.column]
                unconfirmed = row.cells[spec.column].unconfirmed
                conn.execute(
                    """INSERT INTO capacity_value (document_id, model_id, attribute_id, sheet_row,
                         raw_value, kind, num_value, bool_value, text_value, unit, special,
                         unconfirmed, assumed, note)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        doc_id,
                        model_ids[spec.name],
                        attr_id,
                        row.sheet_row,
                        p.raw,
                        p.kind,
                        p.num,
                        None if p.bool_ is None else int(p.bool_),
                        p.text,
                        p.unit,
                        p.special,
                        int(unconfirmed),
                        int(p.assumed),
                        p.note,
                    ),
                )
                report.cell_count += 1
                kind_counts[p.kind] += 1
                if p.special:
                    special_counts[p.special] += 1
                report.unconfirmed_count += int(unconfirmed)
                report.assumed_count += int(p.assumed)

                def item(reason: str, p=p, spec=spec, row=row) -> ReviewItem:
                    return ReviewItem(
                        reason,
                        spec.name,
                        row.category,
                        row.name,
                        row.sheet_row,
                        p.raw,
                        _describe(p),
                    )

                # Only cells of the model's own column count once for combined headers.
                if spec.name != _first_name_for_column(specs, spec.column):
                    continue
                if vtype == "number" and p.kind == V.TEXT:
                    review.append(item("text in numeric row"))
                elif vtype == "bool" and p.kind in (V.TEXT, V.NUMBER):
                    review.append(item("unexpected value in Yes/No row"))
                elif p.special == V.TBD:
                    review.append(item("TBD value"))
                elif p.kind == V.EMPTY and vtype == "number" and spec.kind != "component":
                    info.append(item("empty cell in numeric row"))
                elif vtype == "number" and p.kind == V.BOOL:
                    info.append(item("Yes/No in numeric row (supported / not supported)"))
                if p.note:
                    info.append(item(f"bug reference {p.note} stripped"))
                if p.assumed:
                    info.append(item("bare number in throughput row taken as Gbps"))
                if p.special == V.SEE_COMPONENT:
                    info.append(item("value defined on chassis components"))

        report.kind_counts = dict(kind_counts)
        report.special_counts = dict(special_counts)
        report.needs_review_total = len(review)
        report.needs_review = review[:MAX_EXAMPLES]
        report.info_total = len(info)
        report.info = info[:MAX_EXAMPLES]
        report.needs_review_counts = dict(Counter(i.reason for i in review))
        report.info_counts = dict(Counter(i.reason for i in info))

        _import_interfaces(conn, doc_id, wb.rows, specs, model_ids, report)
        _import_tsf_map(conn, doc_id, attr_by_name, report)
        report.diff = _diff_previous(conn, doc_id, wb.sheet_name, wb.release)

        if activate or (existing and existing[1]):
            _activate(conn, doc_id)
            report.activated = True
        conn.execute(
            "UPDATE source_document SET report_json=? WHERE id=?", (report.to_json(), doc_id)
        )
    return report


def _first_name_for_column(specs, column: int) -> str:
    return next(s.name for s in specs if s.column == column)


def _import_interfaces(conn, doc_id, rows, specs, model_ids, report: ImportReport) -> None:
    ports: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows:
        if norm(row.category) != "interfaces" or not row.name.startswith(INTERFACE_ROW_PREFIX):
            continue
        label = row.name[len(INTERFACE_ROW_PREFIX) :].strip()
        cls = INTERFACE_ROWS.get(label)
        if cls is None:
            if label.lower() != "5g cellular":
                report.unknown_interface_rows.append(f"row {row.sheet_row}: {row.name}")
            continue
        for spec in specs:
            if spec.kind == "component":
                continue
            cell = row.cells[spec.column]
            p = V.parse_cell(cell.value)
            if p.kind != V.NUMBER or not p.num:
                continue
            conn.execute(
                """INSERT INTO interface_port (document_id, model_id, speed_class, label, count,
                                               unconfirmed)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (doc_id, model_ids[spec.name], cls, label, int(p.num), int(cell.unconfirmed)),
            )
            ports[spec.name][cls] = int(p.num)
    report.interface_ports = {s.name: ports.get(s.name, {}) for s in specs if s.kind != "component"}


def _import_tsf_map(conn, doc_id, attr_by_name, report: ImportReport) -> None:
    for metric, category, name, compare_as in TSF_METRIC_MAP:
        attr_id = attr_by_name.get((norm(category), norm(name)))
        if attr_id is None:
            report.tsf_map_unresolved.append(f"{metric} -> {category} / {name}")
            continue
        conn.execute(
            "INSERT INTO tsf_metric_map (document_id, tsf_metric, attribute_id, compare_as) "
            "VALUES (?, ?, ?, ?)",
            (doc_id, metric, attr_id, compare_as),
        )
        report.tsf_map_resolved += 1


def _snapshot(conn, doc_id) -> dict[tuple[str, str], tuple]:
    rows = conn.execute(
        """SELECT m.name, a.canonical_key, a.name AS attr_name, v.raw_value, v.unconfirmed
           FROM capacity_value v
           JOIN model m ON m.id = v.model_id
           JOIN attribute a ON a.id = v.attribute_id
           WHERE v.document_id = ?""",
        (doc_id,),
    ).fetchall()
    return {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}


def _diff_previous(conn, doc_id, sheet_name, release) -> dict | None:
    prev = conn.execute(
        """SELECT id FROM source_document
           WHERE id < ? AND (panos_release IS ? OR sheet_name = ?)
           ORDER BY id DESC LIMIT 1""",
        (doc_id, release, sheet_name),
    ).fetchone()
    if prev is None:
        return None
    old, new = _snapshot(conn, prev[0]), _snapshot(conn, doc_id)
    changed = []
    for k in sorted(old.keys() & new.keys()):
        (attr, o_raw, o_unc), (_, n_raw, n_unc) = old[k], new[k]
        if o_raw != n_raw or o_unc != n_unc:
            changed.append(
                {
                    "model": k[0],
                    "attribute": attr,
                    "old": o_raw,
                    "new": n_raw,
                    "unconfirmed_old": bool(o_unc),
                    "unconfirmed_new": bool(n_unc),
                }
            )
    added = sorted(f"{m} / {a}" for m, a in new.keys() - old.keys())
    removed = sorted(f"{m} / {a}" for m, a in old.keys() - new.keys())
    return {
        "previous_document_id": prev[0],
        "changed_total": len(changed),
        "changed": changed[:MAX_EXAMPLES],
        "added_total": len(added),
        "added": added[:MAX_EXAMPLES],
        "removed_total": len(removed),
        "removed": removed[:MAX_EXAMPLES],
    }


def _activate(conn, doc_id: int) -> None:
    release = conn.execute(
        "SELECT panos_release FROM source_document WHERE id=?", (doc_id,)
    ).fetchone()[0]
    conn.execute("UPDATE source_document SET is_active=0 WHERE panos_release IS ?", (release,))
    conn.execute("UPDATE source_document SET is_active=1 WHERE id=?", (doc_id,))


def activate(conn: sqlite3.Connection, doc_id: int) -> None:
    if conn.execute("SELECT 1 FROM source_document WHERE id=?", (doc_id,)).fetchone() is None:
        raise KeyError(f"No document {doc_id}")
    with conn:
        _activate(conn, doc_id)
