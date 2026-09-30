import pytest

from tsf_sizer.portfolio import values as V
from tsf_sizer.portfolio.values import parse_cell


@pytest.mark.parametrize(
    ("cell", "throughput", "kind", "num", "unit", "special", "note", "assumed"),
    [
        (None, False, V.EMPTY, None, None, None, None, False),
        ("  ", False, V.EMPTY, None, None, None, None, False),
        (65000.0, False, V.NUMBER, 65000.0, None, None, None, False),
        ("1000", False, V.NUMBER, 1000.0, None, None, None, False),
        ("1,500", False, V.NUMBER, 1500.0, None, None, None, False),
        ("1.5 Tbps", True, V.NUMBER, 1500.0, "Gbps", None, None, False),
        ("35.0 Gbps", True, V.NUMBER, 35.0, "Gbps", None, None, False),
        ("650 Mbps", True, V.NUMBER, 0.65, "Gbps", None, None, False),
        (375.0, True, V.NUMBER, 375.0, "Gbps", None, None, True),
        ("375", True, V.NUMBER, 375.0, "Gbps", None, None, True),
        ("4M", False, V.NUMBER, 4_000_000.0, None, None, None, False),
        ("16K", False, V.NUMBER, 16_000.0, None, None, None, False),
        ("16000 (PAN-255203)", False, V.NUMBER, 16000.0, None, None, "PAN-255203", False),
        ("400(PAN-297080)", False, V.NUMBER, 400.0, None, None, "PAN-297080", False),
        ("200/s", False, V.NUMBER, 200.0, "per_s", None, None, False),
        ("500/sec", False, V.NUMBER, 500.0, "per_s", None, None, False),
        ("60W", False, V.NUMBER, 60.0, "W", None, None, False),
        ("Yes/8", False, V.NUMBER, 8.0, None, None, None, False),
        ("-", False, V.SPECIAL, None, None, V.NOT_APPLICABLE, None, False),
        ("N/A", False, V.SPECIAL, None, None, V.NOT_APPLICABLE, None, False),
        ("NA", False, V.SPECIAL, None, None, V.NOT_APPLICABLE, None, False),
        ("System", False, V.SPECIAL, None, None, V.SYSTEM_LIMIT, None, False),
        ("System Limit", False, V.SPECIAL, None, None, V.SYSTEM_LIMIT, None, False),
        ("Configurable", False, V.SPECIAL, None, None, V.CONFIGURABLE, None, False),
        ("Based on MPC", False, V.SPECIAL, None, None, V.SEE_COMPONENT, None, False),
        ("Based on NCs", False, V.SPECIAL, None, None, V.SEE_COMPONENT, None, False),
        ("TBD", False, V.SPECIAL, None, None, V.TBD, None, False),
        ("2x1G/10G", False, V.TEXT, None, None, None, None, False),
        ("A/P only", False, V.TEXT, None, None, None, None, False),
    ],
)
def test_parse_cell(cell, throughput, kind, num, unit, special, note, assumed):
    p = parse_cell(cell, throughput_row=throughput)
    assert p.kind == kind
    assert p.num == (pytest.approx(num) if num is not None else None)
    assert p.unit == unit
    assert p.special == special
    assert p.note == note
    assert p.assumed == assumed


@pytest.mark.parametrize(("cell", "expected"), [("Yes", True), ("No", False), (" yes ", True)])
def test_parse_bool(cell, expected):
    p = parse_cell(cell)
    assert p.kind == V.BOOL
    assert p.bool_ is expected


def test_raw_value_keeps_original_text():
    assert parse_cell("16000 (PAN-255203)").raw == "16000 (PAN-255203)"
    assert parse_cell(65000.0).raw == "65000"
