"""Parse a single capacity-sheet cell into a typed value.

The PLM capacity workbook mixes plain numbers, Yes/No, throughputs with units
("1.5 Tbps", "650 Mbps"), K/M suffixes ("4M"), placeholders ("-", "N/A") and
free text ("System", "Based on MPC", "16000 (PAN-255203)"). Everything is
normalized here so the sizing engine only ever sees clean values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Kinds of parsed value.
NUMBER = "number"
BOOL = "bool"
TEXT = "text"
EMPTY = "empty"
SPECIAL = "special"

# Values of ParsedValue.special.
NOT_APPLICABLE = "not_applicable"
SYSTEM_LIMIT = "system_limit"
CONFIGURABLE = "configurable"
SEE_COMPONENT = "see_component"
TBD = "tbd"

_THROUGHPUT_FACTORS = {"tbps": 1000.0, "gbps": 1.0, "mbps": 0.001}
_SUFFIX_FACTORS = {"k": 1_000.0, "m": 1_000_000.0}

_SPECIAL_TEXT = {
    "-": NOT_APPLICABLE,
    "n/a": NOT_APPLICABLE,
    "na": NOT_APPLICABLE,
    "not supported": NOT_APPLICABLE,
    "system": SYSTEM_LIMIT,
    "system limit": SYSTEM_LIMIT,
    "configurable": CONFIGURABLE,
    "user configurable": CONFIGURABLE,
    "tbd": TBD,
}

_NUM = r"(\d+(?:[.,]\d+)*)"
_RE_PLAIN = re.compile(rf"^{_NUM}$")
_RE_THROUGHPUT = re.compile(rf"^{_NUM}\s*(tbps|gbps|mbps)$", re.I)
_RE_SUFFIX = re.compile(rf"^{_NUM}\s*([km])$", re.I)
_RE_BUG_NOTE = re.compile(r"^(.*?)\s*\(?\s*(PAN-\d+)\s*\)?\s*$", re.I)
_RE_RATE = re.compile(rf"^{_NUM}\s*/\s*s(?:ec)?$", re.I)
_RE_WATT = re.compile(rf"^{_NUM}\s*w$", re.I)
_RE_YES_NUM = re.compile(rf"^yes\s*/\s*{_NUM}$", re.I)
_RE_BASED_ON = re.compile(r"^based on\b", re.I)


@dataclass(frozen=True)
class ParsedValue:
    kind: str
    raw: str | None
    num: float | None = None
    bool_: bool | None = None
    text: str | None = None
    unit: str | None = None
    special: str | None = None
    note: str | None = None
    # True when the value was interpreted with an assumption the admin should see,
    # e.g. a bare number in a throughput row taken as Gbps.
    assumed: bool = False


def _to_float(s: str) -> float:
    # "1,500" -> 1500; the sheet uses '.' as decimal separator.
    return float(s.replace(",", ""))


def _raw(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_cell(value: object, *, throughput_row: bool = False) -> ParsedValue:
    """Parse one cell. `throughput_row` makes bare numbers mean Gbps."""
    raw = _raw(value)

    if value is None or raw == "":
        return ParsedValue(EMPTY, None)

    if isinstance(value, bool):
        return ParsedValue(BOOL, raw, bool_=value)

    if isinstance(value, (int, float)):
        if throughput_row:
            return ParsedValue(NUMBER, raw, num=float(value), unit="Gbps", assumed=True)
        return ParsedValue(NUMBER, raw, num=float(value))

    return _parse_text(raw, throughput_row=throughput_row)


def _parse_text(s: str, *, throughput_row: bool, note: str | None = None) -> ParsedValue:
    low = s.lower()

    if low in _SPECIAL_TEXT:
        return ParsedValue(SPECIAL, s, special=_SPECIAL_TEXT[low], note=note)
    if _RE_BASED_ON.match(s):
        return ParsedValue(SPECIAL, s, special=SEE_COMPONENT, text=s, note=note)
    if low == "yes":
        return ParsedValue(BOOL, s, bool_=True, note=note)
    if low == "no":
        return ParsedValue(BOOL, s, bool_=False, note=note)

    if m := _RE_THROUGHPUT.match(s):
        gbps = _to_float(m.group(1)) * _THROUGHPUT_FACTORS[m.group(2).lower()]
        return ParsedValue(NUMBER, s, num=round(gbps, 6), unit="Gbps", note=note)
    if m := _RE_PLAIN.match(s):
        if throughput_row:
            return ParsedValue(
                NUMBER, s, num=_to_float(m.group(1)), unit="Gbps", assumed=True, note=note
            )
        return ParsedValue(NUMBER, s, num=_to_float(m.group(1)), note=note)
    if m := _RE_SUFFIX.match(s):
        num = _to_float(m.group(1)) * _SUFFIX_FACTORS[m.group(2).lower()]
        return ParsedValue(NUMBER, s, num=num, note=note)
    if m := _RE_RATE.match(s):
        return ParsedValue(NUMBER, s, num=_to_float(m.group(1)), unit="per_s", note=note)
    if m := _RE_WATT.match(s):
        return ParsedValue(NUMBER, s, num=_to_float(m.group(1)), unit="W", note=note)
    if m := _RE_YES_NUM.match(s):
        # e.g. ECMP "Yes/8": supported, with 8 paths.
        return ParsedValue(NUMBER, s, num=_to_float(m.group(1)), bool_=True, note=note)

    # "16000 (PAN-255203)", "400(PAN-297080)", "4 (PAN-296613)": value plus bug reference.
    if note is None and (m := _RE_BUG_NOTE.match(s)) and m.group(1):
        inner = _parse_text(m.group(1).strip(), throughput_row=throughput_row, note=m.group(2))
        return ParsedValue(
            inner.kind,
            s,
            inner.num,
            inner.bool_,
            inner.text,
            inner.unit,
            inner.special,
            inner.note,
            inner.assumed,
        )

    return ParsedValue(TEXT, s, text=s, note=note)
