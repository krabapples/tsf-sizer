"""Mask customer identifiers in anything that goes to the LLM.

The tools already return aggregated facts only. This is the second line of defence: it masks
IP addresses, MAC addresses, e-mail addresses, host names and long serial-like numbers in every
string of a payload, so a name or address that slipped into a counter name, a license text or a
user's chat message does not leave the container.
"""

from __future__ import annotations

import re

_FILE_EXT = {
    "txt", "xml", "tgz", "gz", "tar", "json", "xlsx", "xls", "csv", "log", "py", "html",
    "md", "pdf", "db", "cfg", "conf", "yml", "yaml", "js", "css",
}  # fmt: skip

_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\w.]*\w)")
_IPV6_FULL = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![\w:])")
_IPV6_SHORT = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4})?(?![\w:])")
_MAC = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![\w:])")
_EMAIL = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_HOST = re.compile(
    r"(?<![\w.@-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+([A-Za-z]{2,})(?![\w-])"
)
_SERIAL = re.compile(r"(?<![\w.,])\d{12,16}(?![\w.,])")


def _host(m: re.Match) -> str:
    return m.group(0) if m.group(1).lower() in _FILE_EXT else "[host]"


def redact_text(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _MAC.sub("[mac]", text)
    text = _IPV6_FULL.sub("[ip]", text)
    text = _IPV6_SHORT.sub("[ip]", text)
    text = _IPV4.sub("[ip]", text)
    text = _HOST.sub(_host, text)
    return _SERIAL.sub("[serial]", text)


def redact(obj):
    """The same structure with every string masked (keys are kept)."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj
