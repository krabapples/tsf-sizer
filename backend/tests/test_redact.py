from tsf_sizer.agent.redact import redact, redact_text


def test_masks_identifiers():
    text = (
        "host fw1.corp.example.com at 10.1.2.3/24 and 2001:db8:0:0:0:0:0:1, mac aa:bb:cc:dd:ee:ff,"
        " mail jane.doe@example.org, serial 007951000123456"
    )
    out = redact_text(text)
    for leaked in ("example.com", "10.1.2.3", "2001:db8", "aa:bb", "jane", "007951000123456"):
        assert leaked not in out
    assert "[host]" in out and "[ip]" in out and "[email]" in out and "[serial]" in out


def test_keeps_what_the_agent_needs():
    text = (
        "PA-3220 on PAN-OS 11.1.13-h9, 83,886 sessions, 0.334 Gbps at 14:34:41, ethernet1/1, "
        "file techsupport_x.txt, workbook Features_12.2.2.xlsx, 12.2.2, e.g. 1,000,000"
    )
    assert redact_text(text) == text


def test_redact_walks_structures_and_keeps_keys():
    out = redact({"a": ["see 10.0.0.1", {"b": "x@y.com"}], "10.0.0.2": 5, "n": 3})
    assert out == {"a": ["see [ip]", {"b": "[email]"}], "10.0.0.2": 5, "n": 3}
