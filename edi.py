"""The EDI contest log format.

EDI is the current source of truth for a round, not a permanent one. Nothing
outside this module should mention `PCall=` or a field index, so that replacing
the format is a change here and nowhere else.

Reading lives here. Writing does not: emitting a log needs the logbook and the
scoring, which are the logger's business — it borrows the tables below for the
two fields whose spelling this format owns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

BAND_BY_HEADER = {"145 MHz": "2M", "435 MHz": "70CM", "1296 MHz": "23CM"}
HEADER_BY_BAND = {v: k for k, v in BAND_BY_HEADER.items()}

MODE_BY_CODE = {"1": "SSB", "2": "CW", "6": "FM"}
CODE_BY_MODE = {v: k for k, v in MODE_BY_CODE.items()}


@dataclass
class Record:
    """One line of a `[QSORecords]` section.

    `dt` is naive: the format carries no zone, and the two readers disagreed
    about attaching one. Callers that want an aware datetime attach UTC
    themselves, which is what the values mean.
    """

    dt: datetime
    callsign: str
    mode: str
    rst_s: str
    nr_s: str
    rst_r: str
    nr_r: str
    loc: str
    points: int
    duplicate: bool


@dataclass
class Log:
    my_callsign: str = ""
    my_locator: str = ""
    band: str = ""
    contest_name: str = ""
    records: list[Record] = field(default_factory=list)


_HEADERS = {
    "PCall=": "my_callsign",
    "PWWLo=": "my_locator",
    "TName=": "contest_name",
}


def _parse_record(line: str) -> Record | None:
    f = line.split(";")
    if len(f) < 10:
        return None
    try:
        dt = datetime.strptime(f[0].strip() + f[1].strip(), "%y%m%d%H%M")
    except ValueError:
        return None
    points = int(f[10].strip()) if len(f) > 10 and f[10].strip().isdigit() else 0
    return Record(
        dt=dt,
        callsign=f[2].strip(),
        mode=MODE_BY_CODE.get(f[3].strip(), ""),
        rst_s=f[4].strip(),
        nr_s=f[5].strip(),
        rst_r=f[6].strip(),
        nr_r=f[7].strip(),
        loc=f[9].strip(),
        points=points,
        duplicate=len(f) > 13 and f[13].strip().upper() == "D",
    )


def read(path) -> Log:
    """Parse one EDI file. Unreadable files and unparseable records are
    skipped rather than raised on — these come from disk, mid-round."""
    log = Log()
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return log
    in_records = False
    for line in text.splitlines():
        for prefix, attr in _HEADERS.items():
            if line.startswith(prefix):
                setattr(log, attr, line.split("=", 1)[1].strip())
                break
        else:
            if line.startswith("PBand="):
                log.band = BAND_BY_HEADER.get(line.split("=", 1)[1].strip(), "")
            elif line.startswith("[QSORecords"):
                in_records = True
            elif line.startswith("["):
                in_records = False
            elif in_records and line:
                record = _parse_record(line)
                if record is not None:
                    log.records.append(record)
    return log
