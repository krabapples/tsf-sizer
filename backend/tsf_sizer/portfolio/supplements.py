"""Static data for models the capacity workbook does not contain.

The workbook only covers current platforms. Customers still run older ones, and a TSF from
such a firewall cannot be compared with anything unless the model is known. These values
come from vendor datasheets, are fixed (the platforms are end of sale), and are added to every
imported workbook version, and to existing ones at start-up. If a workbook ever has a column
for one of these models, the workbook wins and the supplement is skipped for that version.

Supplement models are never recommended: their family is set to "not quotable".
Only values the datasheet states are stored; everything else stays empty ("no data").
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

SOURCE_NOTE = "PA-800 Series datasheet (measured on PAN-OS 11.0)"


@dataclass(frozen=True)
class SupplementModel:
    name: str
    family: str
    # TSF metric -> (raw datasheet text, number in the attribute's unit)
    values: dict[str, tuple[str, float]] = field(default_factory=dict)
    ports: dict[str, tuple[str, int]] = field(default_factory=dict)  # speed class -> (label, n)
    dedicated_ha: bool = False
    note: str = ""


# PA-800 Series datasheet, Table 1 (HTTP/appmix; appmix is what the workbook rows use) and
# Table 3 (I/O). The datasheet gives no rule, object, zone or routing limits, so those are
# left empty rather than guessed.
PA_800 = (
    SupplementModel(
        name="PA-820",
        family="PA-800",
        values={
            "perf.throughput_appid_gbps": ("1.5 Gbps (appmix)", 1.5),
            "perf.throughput_threat_gbps": ("840 Mbps (appmix)", 0.84),
            "perf.throughput_ipsec_gbps": ("1.4 Gbps", 1.4),
            "perf.sessions": ("128,000", 128000),
            "perf.cps": ("8,100", 8100),
        },
        ports={"1G_RJ45": ("10/100/1000", 4), "1G_SFP": ("Gigabit SFP", 8)},
        dedicated_ha=True,
        note="End of sale. Fixed AC power supply.",
    ),
    SupplementModel(
        name="PA-850",
        family="PA-800",
        values={
            "perf.throughput_appid_gbps": ("1.9 Gbps (appmix)", 1.9),
            "perf.throughput_threat_gbps": ("1.0 Gbps (appmix)", 1.0),
            "perf.throughput_ipsec_gbps": ("1.8 Gbps", 1.8),
            "perf.sessions": ("192,000", 192000),
            "perf.cps": ("13,100", 13100),
        },
        # The datasheet lists two I/O options; the first (8x SFP) is stored. The other has
        # 4x SFP and 4x SFP+ instead.
        ports={"1G_RJ45": ("10/100/1000", 4), "1G_SFP": ("Gigabit SFP", 8)},
        dedicated_ha=True,
        note="End of sale. Also available with 4x SFP + 4x SFP+ instead of 8x SFP.",
    ),
)

SUPPLEMENTS = PA_800
FAMILY_NOTES = {"PA-800": "End of sale; data from the PA-800 datasheet. Never recommended."}
HA_KEYS = ("interfaces.dedicated_ha_control_interface", "interfaces.dedicated_ha_data_interface")


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
            "INSERT OR IGNORE INTO model_override (model_name, lifecycle, notes) "
            "VALUES (?, 'eol', ?)",
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
        for metric, (raw, num) in m.values.items():
            attr = mapped.get(metric)
            if attr is None:
                continue
            unit = conn.execute("SELECT unit FROM attribute WHERE id=?", (attr,)).fetchone()[0]
            conn.execute(
                """INSERT INTO capacity_value (document_id, model_id, attribute_id, raw_value,
                     kind, num_value, unit, unconfirmed, assumed, note)
                   VALUES (?, ?, ?, ?, 'number', ?, ?, 0, 0, ?)""",
                (document_id, model_id, attr, raw, num, unit, SOURCE_NOTE),
            )
        if m.dedicated_ha:
            for key in HA_KEYS:
                if key in ha_attrs:
                    conn.execute(
                        """INSERT INTO capacity_value (document_id, model_id, attribute_id,
                             raw_value, kind, bool_value, unconfirmed, assumed, note)
                           VALUES (?, ?, ?, 'Yes', 'bool', 1, 0, 0, ?)""",
                        (document_id, model_id, ha_attrs[key], SOURCE_NOTE),
                    )
        for speed, (label, n) in m.ports.items():
            conn.execute(
                "INSERT INTO interface_port (document_id, model_id, speed_class, label, count,"
                " unconfirmed) VALUES (?, ?, ?, ?, ?, 0)",
                (document_id, model_id, speed, label, n),
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
