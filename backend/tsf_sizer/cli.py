"""Command-line entry point: `tsf-sizer <command>` or `python -m tsf_sizer.cli <command>`."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import db
from .portfolio import catalog
from .portfolio.importer import AlreadyImportedError, activate, import_workbook


def _cmd_import(args) -> int:
    conn = db.connect(args.db)
    try:
        report = import_workbook(
            conn,
            args.xlsx,
            activate=args.activate,
            imported_by=getpass.getuser(),
            force=args.force,
        )
    except AlreadyImportedError as e:
        print(f"{e}. Use --force to re-import.", file=sys.stderr)
        return 1
    print(report.to_text())
    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report.to_json(), encoding="utf-8")
        out.with_suffix(".txt").write_text(report.to_text(), encoding="utf-8")
        print(f"\nReport written to {out} and {out.with_suffix('.txt')}")
    return 0


def _cmd_activate(args) -> int:
    activate(db.connect(args.db), args.document_id)
    print(f"Document {args.document_id} is now active.")
    return 0


def _cmd_documents(args) -> int:
    for r in catalog.list_documents(db.connect(args.db)):
        flag = "*" if r["is_active"] else " "
        print(
            f"{flag} {r['id']:>3}  {r['panos_release'] or '?':<8} {r['sheet_name']:<20} "
            f"{r['filename']}  ({r['imported_at']})"
        )
    return 0


def _cmd_models(args) -> int:
    for r in catalog.list_models(db.connect(args.db), quotable_only=not args.all):
        print(
            f"{r['name']:<16} {r['family'] or '':<9} {r['kind']:<10} {r['lifecycle']:<8} "
            f"quotable={'yes' if r['customer_quotable'] else 'no'}"
        )
    return 0


def _resolve_doc(conn, args):
    doc = (
        conn.execute("SELECT * FROM source_document WHERE id=?", (args.document,)).fetchone()
        if args.document
        else catalog.active_document(conn)
    )
    if doc is None:
        print(
            "No active portfolio document. Import one with --activate, or pass --document.",
            file=sys.stderr,
        )
    return doc


def _cmd_show(args) -> int:
    conn = db.connect(args.db)
    doc = _resolve_doc(conn, args)
    if doc is None:
        return 1
    rows = catalog.model_values(conn, args.model, doc["id"], args.category)
    if not rows:
        print(f"No values for {args.model} in document {doc['id']}", file=sys.stderr)
        return 1
    cat = None
    for r in rows:
        if r["category"] != cat:
            cat = r["category"]
            print(f"\n{cat}")
        if r["kind"] == "number":
            val = f"{r['num_value']:g}" + (f" {r['unit']}" if r["unit"] else "")
        elif r["kind"] == "bool":
            val = "yes" if r["bool_value"] else "no"
        elif r["kind"] == "special":
            val = r["special"]
        else:
            val = r["raw_value"] or ""
        flags = (" [unconfirmed]" if r["unconfirmed"] else "") + (
            f" [{r['note']}]" if r["note"] else ""
        )
        print(f"  {r['name'][:60]:<60} {val}{flags}")
    ports = catalog.interface_ports(conn, args.model, doc["id"])
    if ports:
        print("\nPorts: " + ", ".join(f"{n}x {c}" for c, n in ports.items()))
    return 0


def _cmd_set_model(args) -> int:
    quotable = None if args.quotable is None else int(args.quotable == "yes")
    catalog.set_model_override(
        db.connect(args.db),
        args.model,
        lifecycle=args.lifecycle,
        customer_quotable=quotable,
        eos_date=args.eos_date,
        price_tier=args.price_tier,
        form_factor_ru=args.rack_units,
        notes=args.note,
    )
    print(f"Updated {args.model}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tsf-sizer")
    p.add_argument("--db", help=f"SQLite path (default $TSF_SIZER_DB or {db.DEFAULT_DB_PATH})")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("import-portfolio", help="Import a Features & Capacities workbook")
    s.add_argument("xlsx")
    s.add_argument("--activate", action="store_true", help="Make this the active version")
    s.add_argument("--force", action="store_true", help="Re-import an already imported file")
    s.add_argument("--report", help="Write the JSON report here (a .txt copy is written too)")
    s.set_defaults(func=_cmd_import)

    s = sub.add_parser("activate", help="Make an imported document the active version")
    s.add_argument("document_id", type=int)
    s.set_defaults(func=_cmd_activate)

    s = sub.add_parser("documents", help="List imported documents")
    s.set_defaults(func=_cmd_documents)

    s = sub.add_parser("models", help="List quotable models")
    s.add_argument("--all", action="store_true", help="Include NPI models and components")
    s.set_defaults(func=_cmd_models)

    s = sub.add_parser("show-model", help="Show the capacities of one model")
    s.add_argument("model")
    s.add_argument("--category", help="Filter on category (substring)")
    s.add_argument("--document", type=int, help="Document id (default: active)")
    s.set_defaults(func=_cmd_show)

    s = sub.add_parser("set-model", help="Set team-maintained metadata for a model")
    s.add_argument("model")
    s.add_argument("--lifecycle", choices=["current", "npi", "eos", "eol"])
    s.add_argument("--quotable", choices=["yes", "no"])
    s.add_argument("--eos-date")
    s.add_argument("--price-tier")
    s.add_argument("--rack-units", type=float)
    s.add_argument("--note")
    s.set_defaults(func=_cmd_set_model)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
