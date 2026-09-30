"""Turn workbook column headers into model records."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .workbook import SummaryEntry

APPLIANCE = "appliance"
COMPONENT = "component"
VM = "vm"

_PA_DIGITS = re.compile(r"^PA-(\d)(\d)(\d)(\d)?", re.I)
_FAMILY = re.compile(r"^PA-\d+00$", re.I)


@dataclass(frozen=True)
class ModelSpec:
    name: str
    column: int
    header: str
    kind: str
    parent: str | None
    family: str | None


def family_of(name: str) -> str | None:
    """PA-5450 -> PA-5400, PA-455R-5G -> PA-400, VM-300 -> PA-VM."""
    if name.upper().startswith("VM-"):
        return "PA-VM"
    m = _PA_DIGITS.match(name)
    if not m:
        return None
    if m.group(4) is not None:
        return f"PA-{m.group(1)}{m.group(2)}00"
    return f"PA-{m.group(1)}00"


def expand_headers(model_columns: dict[int, str]) -> list[ModelSpec]:
    """Split combined headers and recognise chassis components.

    "PA-450R and PA-450R-5G" -> two models sharing one column.
    "PA-7500 MPC" where "PA-7500" is also a column -> component of PA-7500.
    """
    headers = {c: re.sub(r"\s+", " ", h).strip() for c, h in model_columns.items()}
    base_names = set(headers.values())
    specs: list[ModelSpec] = []
    for col, header in headers.items():
        parts = [p.strip() for p in re.split(r"\s+and\s+|\s*/\s*(?=PA-)", header) if p.strip()]
        for part in parts:
            parent = None
            kind = VM if part.upper().startswith("VM-") else APPLIANCE
            if " " in part:
                base, _suffix = part.split(" ", 1)
                if base in base_names:
                    parent, kind = base, COMPONENT
            specs.append(ModelSpec(part, col, header, kind, parent, family_of(part)))
    return specs


def npi_sets(summary: list[SummaryEntry]) -> tuple[set[str], set[str]]:
    """Return (npi_models, npi_families) from the Summary sheet.

    A row counts as NPI when its status column mentions NPI. If the platform
    column is a family name (e.g. "PA-5500"), the whole family is NPI.
    """
    models: set[str] = set()
    families: set[str] = set()
    for e in summary:
        if "npi" not in e.status.lower():
            continue
        models.update(m.upper() for m in e.models)
        if _FAMILY.match(e.platform):
            families.add(e.platform.upper())
    return models, families


def sheet_lifecycle(spec: ModelSpec, npi_models: set[str], npi_families: set[str]) -> str:
    name = (spec.parent or spec.name).upper()
    fam = (spec.family or "").upper()
    if name in npi_models or spec.name.upper() in npi_models or fam in npi_families:
        return "npi"
    return "current"
