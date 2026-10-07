"""Build tsf_sizer/portfolio/supplement_data.json from an older capacity workbook.

Usage:  python tools/build_supplement.py OLD_WORKBOOK.xlsx

One-off: the result is committed, the workbook is not. For every model in MODELS it stores,
per TSF metric (see mapping.TSF_METRIC_MAP), the cell exactly as the workbook parses it,
plus the port layout and whether the HA ports are dedicated. Rows are matched the same way
the importer does it (section + name, first occurrence wins). Only numbers and Yes/No values
are kept; no notes, bug references or other text from the workbook.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl

from tsf_sizer.portfolio import values as V
from tsf_sizer.portfolio.mapping import ALIASES, TSF_METRIC_MAP, norm
from tsf_sizer.portfolio.models import expand_headers
from tsf_sizer.portfolio.workbook import read_workbook

MODELS = ["PA-820", "PA-850", "PA-3220", "PA-3250", "PA-3260",
          "PA-5220", "PA-5250", "PA-5260", "PA-5280"]  # fmt: skip
OUT = Path(__file__).resolve().parents[1] / "tsf_sizer" / "portfolio" / "supplement_data.json"

# Port layouts. The sheet's cells are option lists ("4/8", "0/16") that do not combine
# mechanically, so the layouts are written out here (and agree with the PA-800 datasheet):
#   PA-850: 4 RJ45 + 8 SFP, or 4 RJ45 + 4 SFP + 4 SFP+ (the first is stored)
#   PA-3200: the sheet lists SFP and SFP+ in separate rows, but the datasheet has shared
#            "1G/10G SFP/SFP+" cages: PA-3220 = 4 SFP + 4 SFP/SFP+, PA-3250 = 8 SFP/SFP+,
#            PA-3260 = 8 SFP/SFP+ + 4 QSFP+ (stored as SFP+)
#   PA-5200: 4 RJ45 (100M/1G/10G) + 16 cages that take SFP or SFP+ (stored as SFP+; the sizing
#            lets SFP needs use SFP+ cages) + 4 QSFP (40G on PA-5220, 40/100G on the others)
PORTS = {
    "PA-820": {"1G_RJ45": 4, "1G_SFP": 8},
    "PA-850": {"1G_RJ45": 4, "1G_SFP": 8},
    "PA-3220": {"1G_RJ45": 12, "1G_SFP": 4, "10G_SFP+": 4},
    "PA-3250": {"1G_RJ45": 12, "10G_SFP+": 8},
    "PA-3260": {"1G_RJ45": 12, "10G_SFP+": 8, "40G_QSFP+": 4},
    "PA-5220": {"10G_RJ45": 4, "10G_SFP+": 16, "40G_QSFP+": 4},
    "PA-5250": {"10G_RJ45": 4, "10G_SFP+": 16, "100G_QSFP28": 4},
    "PA-5260": {"10G_RJ45": 4, "10G_SFP+": 16, "100G_QSFP28": 4},
    "PA-5280": {"10G_RJ45": 4, "10G_SFP+": 16, "100G_QSFP28": 4},
}
PORT_VARIANTS = {"PA-850": ["alternative: 4 SFP + 4 SFP+ instead of 8 SFP"]}
PORT_ROWS = ("10/100/1000", "100/1000/10,000", "SFP", "XFP/SFP+", "QSFP+ (40G) / QSFP28 (100G)")
HA_ROWS = ("Dedicated HA control interface", "Dedicated HA data interface")


def _ports(cell, cls: str) -> tuple[dict[str, int], str | None]:
    """(ports by speed class, variant note)."""
    if cell is None or str(cell).strip() in ("", "-", "None"):
        return {}, None
    s = str(cell).strip()
    if cls == "QSFP":
        n = int(s[0]) if s[0].isdigit() else 0
        gb = 100 if "100" in s else 40
        return ({f"{gb}G_QSFP{'28' if gb == 100 else '+'}": n} if n else {}), None
    if "/" in s:  # option list, e.g. "4/8" or "0/16"
        opts = [int(x) for x in s.split("/")]
        return ({cls: opts[-1]} if opts[-1] else {}), s
    try:
        n = int(float(s))
    except ValueError:
        return {}, None
    return ({cls: n} if n else {}), None


def main(path: str) -> None:
    sheets = openpyxl.load_workbook(path, read_only=True).sheetnames
    index: dict[tuple[str, str], tuple] = {}  # (section, name) -> row; first occurrence wins
    by_name: dict[str, list] = {}
    cols: dict[str, dict[str, int]] = {}
    rows_by_sheet = {}
    for sh in sheets:
        wb = read_workbook(path, sheet=sh)
        cols[sh] = {s.name: s.column for s in expand_headers(wb.model_columns)}
        rows_by_sheet[sh] = wb.rows
        for r in wb.rows:
            index.setdefault((norm(r.category), norm(r.name)), (sh, r))
            by_name.setdefault(norm(r.name), []).append((sh, r))

    def find(cat: str, name: str):
        hit = index.get((norm(cat), norm(name)))
        if hit is None and len(by_name.get(norm(name), [])) == 1:
            hit = by_name[norm(name)][0]
        return hit

    out: dict = {"source": "older capacity workbook, PAN-OS 11.0 (static)", "models": {}}
    for model in MODELS:
        values, ha = {}, []
        for metric, cat, name, _cmp in TSF_METRIC_MAP:
            hit = None
            for c, n in [(cat, name), *ALIASES.get(metric, [])]:
                hit = find(c, n)
                if hit:
                    break
            if hit is None:
                continue
            sh, row = hit
            if model not in cols[sh]:
                continue
            p = V.parse_cell(row.cells[cols[sh][model]].value)
            if p.kind in (V.EMPTY, V.TEXT) or p.special == V.TBD:
                continue
            values[metric] = {
                k: v
                for k, v in {
                    "raw": p.raw,
                    "kind": p.kind,
                    "num": p.num,
                    "bool": p.bool_,
                    "unit": p.unit,
                    "special": p.special,
                }.items()
                if v is not None
            }
        net = next(sh for sh in sheets if sh.startswith("L2-L4"))
        for r in rows_by_sheet[net]:
            if r.category == "Interfaces" and r.name in HA_ROWS:
                ha.append(str(r.cells[cols[net][model]].value).strip().lower() == "yes")
            if r.category == "Interfaces" and r.name in PORT_ROWS:
                cell = str(r.cells[cols[net][model]].value).strip()
                print(f"   {model:8} {r.name:30} sheet says {cell}")
        ports = PORTS[model]
        variants = PORT_VARIANTS.get(model, [])
        out["models"][model] = {
            "values": values,
            "ports": ports,
            "port_variants": variants,
            "dedicated_ha": len(ha) == 2 and all(ha),
        }
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    for m, d in out["models"].items():
        print(
            f"{m:8} {len(d['values']):3} metrics  ports={d['ports']} variants={d['port_variants']}"
        )


if __name__ == "__main__":
    main(sys.argv[1])
