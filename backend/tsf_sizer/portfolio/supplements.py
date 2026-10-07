"""Static data for models the current capacity workbook does not contain.

The workbook only covers current platforms. Customers still run older ones, and a TSF from
such a firewall cannot be compared with anything unless the model is known. The data here is
fixed (the platforms are end of sale) and added to every imported workbook version, and to
existing ones at start-up. If a workbook ever has a column for one of these models, the
workbook wins and the supplement is skipped for that version.

Sources:
* supplement_data.json: limits, sessions, port layouts and HA ports of PA-820/850, PA-3220/
  3250/3260 and PA-5220/5250/5260/5280, taken once from an older capacity workbook (PAN-OS
  11.0) by tools/build_supplement.py.
* PA-800 (PAN-OS 11.0) and PA-5200 (PAN-OS 11.2) datasheets: throughput and new sessions per
  second, which that workbook does not have. For the PA-3200 these are not known yet and stay
  empty.

Supplement models are never recommended: their families are set to "not quotable".
Values that are not stated stay empty ("no data"); nothing is guessed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from importlib import resources

DATA_NOTE = "older capacity workbook for PAN-OS 11.0"
PA800_SHEET = "PA-800 Series datasheet (PAN-OS 11.0)"
PA5200_SHEET = "PA-5200 Series datasheet (PAN-OS 11.2)"

# Datasheet Table 1 (appmix figures; appmix is what the workbook rows use).
# model -> (source, {metric: (raw datasheet text, value in the attribute's unit)})
_PA800 = {
    "PA-820": {
        "perf.throughput_appid_gbps": ("1.5 Gbps (appmix)", 1.5),
        "perf.throughput_threat_gbps": ("840 Mbps (appmix)", 0.84),
        "perf.throughput_ipsec_gbps": ("1.4 Gbps", 1.4),
        "perf.cps": ("8,100", 8100),
    },
    "PA-850": {
        "perf.throughput_appid_gbps": ("1.9 Gbps (appmix)", 1.9),
        "perf.throughput_threat_gbps": ("1.0 Gbps (appmix)", 1.0),
        "perf.throughput_ipsec_gbps": ("1.8 Gbps", 1.8),
        "perf.cps": ("13,100", 13100),
    },
}


def _pa5200(fw, threat, ipsec, cps):
    return {
        "perf.throughput_appid_gbps": (f"{fw:g} Gbps (appmix)", fw),
        "perf.throughput_threat_gbps": (f"{threat:g} Gbps (appmix)", threat),
        "perf.throughput_ipsec_gbps": (f"{ipsec:g} Gbps", ipsec),
        "perf.cps": (f"{cps:,}", cps),
    }


DATASHEET = {
    **{m: (PA800_SHEET, v) for m, v in _PA800.items()},
    "PA-5220": (PA5200_SHEET, _pa5200(15, 8.8, 9.5, 150000)),
    "PA-5250": (PA5200_SHEET, _pa5200(35, 19, 18.4, 368000)),
    "PA-5260": (PA5200_SHEET, _pa5200(55, 31, 25, 500000)),
    "PA-5280": (PA5200_SHEET, _pa5200(55, 31, 25, 500000)),
}
NO_PERFORMANCE = "Throughput and connections per second are not known for this model."
MODEL_NOTES = {
    "PA-850": "End of sale. Also sold with 4 SFP + 4 SFP+ instead of 8 SFP; the 8 SFP layout "
    "is stored.",
    "PA-3220": f"End of sale. {NO_PERFORMANCE}",
    "PA-3250": f"End of sale. {NO_PERFORMANCE}",
    "PA-3260": f"End of sale. {NO_PERFORMANCE}",
    "PA-5220": "End of sale.",
    "PA-5250": "End of sale.",
    "PA-5260": "End of sale.",
    "PA-5280": "End of sale.",
}
FAMILY_NOTES = {
    "PA-800": "End of sale; static data. Never recommended.",
    "PA-3200": "End of sale; static data. Never recommended.",
    "PA-5200": "End of sale; static data. Never recommended.",
}
HA_KEYS = ("interfaces.dedicated_ha_control_interface", "interfaces.dedicated_ha_data_interface")


@dataclass(frozen=True)
class SupplementModel:
    name: str
    family: str
    values: dict[str, dict] = field(default_factory=dict)  # TSF metric -> parsed cell
    ports: dict[str, int] = field(default_factory=dict)  # speed class -> count
    dedicated_ha: bool = False
    note: str = ""


def family_of(model: str) -> str:
    digits = model.removeprefix("PA-")
    return f"PA-{digits[0]}00" if len(digits) == 3 else f"PA-{digits[:2]}00"


def _load() -> tuple[SupplementModel, ...]:
    raw = json.loads(
        resources.files("tsf_sizer.portfolio").joinpath("supplement_data.json").read_text()
    )
    out = []
    for name, d in raw["models"].items():
        values = dict(d["values"])
        source, sheet_values = DATASHEET.get(name, (None, {}))
        for metric, (text, num) in sheet_values.items():
            values[metric] = {"kind": "number", "raw": text, "num": num, "source": source}
        out.append(
            SupplementModel(
                name=name,
                family=family_of(name),
                values=values,
                ports=d["ports"],
                dedicated_ha=d["dedicated_ha"],
                note=MODEL_NOTES.get(name, "End of sale."),
            )
        )
    return tuple(out)


SUPPLEMENTS = _load()


def apply_supplements(conn: sqlite3.Connection, document_id: int) -> list[str]:
    """Add the supplement models to one imported workbook version. Idempotent.

    Runs inside the caller's transaction. Returns the names added or refreshed."""
    mapped = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT tsf_metric, attribute_id FROM tsf_metric_map WHERE document_id=?",
            (document_id,),
        )
    }
    ha_attrs = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT canonical_key, id FROM attribute WHERE canonical_key IN ({','.join('?' * 2)})",
            HA_KEYS,
        )
    }
    done = []
    for fam, note in FAMILY_NOTES.items():
        # Not quotable: usable as the current model, never offered as a replacement.
        conn.execute(
            "INSERT OR IGNORE INTO family_setting (family, quotable, superseded_by, notes) "
            "VALUES (?, 0, NULL, ?)",
            (fam, note),
        )
    for m in SUPPLEMENTS:
        row = conn.execute("SELECT id, sheet_column FROM model WHERE name=?", (m.name,)).fetchone()
        if row is not None and row[1] != "supplement":
            has_values = conn.execute(
                "SELECT 1 FROM capacity_value WHERE document_id=? AND model_id=? LIMIT 1",
                (document_id, row[0]),
            ).fetchone()
            if has_values:
                continue  # the workbook has this model: it wins
        if row is None:
            model_id = conn.execute(
                "INSERT INTO model (name, family, kind, sheet_lifecycle, sheet_column) "
                "VALUES (?, ?, 'appliance', 'current', 'supplement')",
                (m.name, m.family),
            ).lastrowid
        else:
            model_id = row[0]
        conn.execute(
            "INSERT INTO model_override (model_name, lifecycle, notes) VALUES (?, 'eol', ?) "
            "ON CONFLICT(model_name) DO UPDATE SET notes=excluded.notes "
            "WHERE model_override.notes LIKE 'End of sale%'",
            (m.name, m.note),
        )
        conn.execute(
            "DELETE FROM capacity_value WHERE document_id=? AND model_id=?",
            (document_id, model_id),
        )
        conn.execute(
            "DELETE FROM interface_port WHERE document_id=? AND model_id=?",
            (document_id, model_id),
        )
        for metric, v in m.values.items():
            attr = mapped.get(metric)
            if attr is None:
                continue
            unit = conn.execute("SELECT unit FROM attribute WHERE id=?", (attr,)).fetchone()[0]
            conn.execute(
                """INSERT INTO capacity_value (document_id, model_id, attribute_id, raw_value,
                     kind, num_value, bool_value, unit, special, unconfirmed, assumed, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)""",
                (
                    document_id,
                    model_id,
                    attr,
                    v.get("raw"),
                    v["kind"],
                    v.get("num"),
                    None if v.get("bool") is None else int(v["bool"]),
                    unit or v.get("unit"),
                    v.get("special"),
                    v.get("source") or DATA_NOTE,
                ),
            )
        if m.dedicated_ha:
            for key in HA_KEYS:
                if key in ha_attrs:
                    conn.execute(
                        """INSERT INTO capacity_value (document_id, model_id, attribute_id,
                             raw_value, kind, bool_value, unconfirmed, assumed, note)
                           VALUES (?, ?, ?, 'Yes', 'bool', 1, 0, 0, ?)""",
                        (document_id, model_id, ha_attrs[key], DATA_NOTE),
                    )
        for speed, n in m.ports.items():
            conn.execute(
                "INSERT INTO interface_port (document_id, model_id, speed_class, label, count,"
                " unconfirmed) VALUES (?, ?, ?, ?, ?, 0)",
                (document_id, model_id, speed, speed, n),
            )
        done.append(m.name)
    return done


def apply_to_all_documents(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """Refresh every imported version (used at start-up, so upgrades need no re-import)."""
    out = {}
    with conn:
        for (doc_id,) in conn.execute("SELECT id FROM source_document").fetchall():
            added = apply_supplements(conn, doc_id)
            if added:
                out[doc_id] = added
    return out
