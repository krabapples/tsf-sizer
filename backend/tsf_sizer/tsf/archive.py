"""Read only the files the sizing needs from a TSF archive.

A TSF is a .tgz of 100-500 MB, mostly logs. We stream through it once and keep
only a small allow-list of members in memory; nothing is written to disk, so
path traversal and symlink tricks in the archive cannot do harm.
"""

from __future__ import annotations

import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

# Member path patterns (relative, without leading "./") -> logical name.
WANTED: list[tuple[str, re.Pattern]] = [
    ("techsupport", re.compile(r"^tmp/cli/techsupport_[^/]+\.txt$")),
    ("merged_config", re.compile(r"^opt/pancfg/mgmt/saved-configs/\.merged-running-config\.xml$")),
    ("running_config", re.compile(r"^opt/pancfg/mgmt/saved-configs/running-config\.xml$")),
    ("platform", re.compile(r"^opt/pancfg/mgmt/devices/[^/]+/platform\.xml$")),
    ("sdb", re.compile(r"^tmp/cli/logs/sdb\.txt$")),
    ("dp_monitor", re.compile(r"^var/log/pan/dp-monitor\.log$")),
]

# Hard limit per member; the largest one we want (techsupport) is a few MB.
MAX_MEMBER_BYTES = 64 * 1024 * 1024


class TsfFormatError(ValueError):
    pass


class PanoramaTsfError(TsfFormatError):
    """The TSF was generated on a Panorama management server, not on a firewall."""


@dataclass
class TsfFiles:
    source: str
    files: dict[str, bytes] = field(default_factory=dict)
    member_names: dict[str, str] = field(default_factory=dict)
    skipped_too_large: list[str] = field(default_factory=list)

    def text(self, name: str) -> str | None:
        data = self.files.get(name)
        return data.decode("utf-8", errors="replace") if data is not None else None


def _normalize(name: str) -> str:
    return re.sub(r"^(\./)+", "", name)


def read_tsf(path: str | Path) -> TsfFiles:
    """Accept a TSF .tgz/.tar.gz/.tar, or a loose techsupport_*.txt file."""
    path = Path(path)
    result = TsfFiles(source=path.name)

    if not tarfile.is_tarfile(path):
        data = path.read_bytes()
        if b"\n> show system info" in data:
            result.files["techsupport"] = data
            result.member_names["techsupport"] = path.name
            return result
        raise TsfFormatError(f"{path.name} is neither a TSF archive nor a techsupport text file")

    with tarfile.open(path, mode="r:*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = _normalize(member.name)
            for key, pattern in WANTED:
                if key in result.files or not pattern.match(name):
                    continue
                if member.size > MAX_MEMBER_BYTES:
                    result.skipped_too_large.append(name)
                    break
                fh = tar.extractfile(member)
                if fh is not None:
                    result.files[key] = fh.read(MAX_MEMBER_BYTES + 1)[:MAX_MEMBER_BYTES]
                    result.member_names[key] = name
                break

    if "techsupport" not in result.files:
        raise TsfFormatError(
            "No tmp/cli/techsupport_*.txt found in the archive. Is this a Tech Support File?"
        )
    return result
