# tsf-sizer backend

Python backend for the TSF sizing tool. Only the **portfolio importer** is built so far: it loads the PLM "Features & Capacities" workbook into SQLite. The TSF parser, sizing engine and web frontend come next (see `../docs/tsf-sizing-agent-plan.md`).

## Setup

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## Import the capacity workbook

```bash
tsf-sizer --db ../data/app.db import-portfolio Features_Capacities_12_1_2_Orion.xlsx \
    --report ../data/reports/import-12.1.2.json
```

The import prints a report and writes it to JSON and `.txt`. It covers:

- models found, split into quotable, NPI (not quotable) and chassis components
- how every cell was read: numbers, Yes/No, not applicable, "System", "Configurable", …
- orange (unconfirmed) cells and values read with an assumption (e.g. a bare number in a throughput row taken as Gbps)
- **cells that need a human decision** (text in a numeric row, TBD, unexpected values in Yes/No rows)
- interface port counts per model and speed class
- whether every TSF metric mapping found its spreadsheet row
- changes compared with the previous import

A new import is **not active** until you activate it. After checking the report:

```bash
tsf-sizer --db ../data/app.db activate <document-id>     # or pass --activate on import
```

Importing the same file twice is refused; `--force` replaces the earlier import (it stays active if it was active).

## Look things up

```bash
tsf-sizer --db ../data/app.db documents                  # imported versions (* = active)
tsf-sizer --db ../data/app.db models [--all]             # quotable models (--all: + NPI, components)
tsf-sizer --db ../data/app.db show-model PA-3430 --category objects
```

## Team-maintained model metadata

NPI status is read from the workbook's `Summary` sheet. Anything the workbook doesn't say (released after all, end-of-sale date, price tier, rack units) is stored as an override that survives re-imports:

```bash
tsf-sizer --db ../data/app.db set-model PA-540 --lifecycle current --quotable yes
tsf-sizer --db ../data/app.db set-model PA-3220 --lifecycle eol --eos-date 2025-08-31
```

## Tests

```bash
pytest                                     # synthetic workbook, no confidential data needed
PORTFOLIO_XLSX=/path/to/real.xlsx pytest   # also run the check against the real workbook
ruff check . && ruff format --check .
```

The real workbook is internal. `.gitignore` keeps `*.xlsx`, TSF archives and `data/` out of git, so never commit them or bake them into an image.
