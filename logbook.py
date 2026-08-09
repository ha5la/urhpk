"""The logbook: the round's QSOs, what counts as a duplicate, and the EDI log.

One place holds the contest's rules about a QSO — which band/mode combinations
a station can be worked on, when a second contact with it is a duplicate, and
how points are scored. The EDI export and the crash-recovery read are here too,
because emitting a log needs the logbook and the scoring; the format itself is
edi.py's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import edi
from geo import bearing_between, distance_between, is_locator

BANDS = ("2M", "70CM", "23CM")
MODES = ("SSB", "CW", "FM")

MY_LOGS_DIR = Path("my-logs")


@dataclass
class QSO:
    dt: datetime
    band: str
    mode: str
    callsign: str
    rst_s: str
    nr_s: int
    rst_r: str
    nr_r: int
    loc: str
    dist_km: int


class LogBook:
    def __init__(self, my_callsign: str, my_loc: str, loc_cache: dict[str, list[str]]):
        self.my_callsign = my_callsign
        self.my_loc = my_loc
        self.loc_cache = loc_cache
        self.qsos: list[QSO] = []
        self.worked: set[tuple[str, str, str]] = set()  # (call, band, mode)

    def next_nr(self, band: str) -> int:
        if band:
            return sum(1 for q in self.qsos if q.band == band) + 1
        return len(self.qsos) + 1

    def is_dup(self, callsign: str, band: str, mode: str) -> bool:
        return (callsign, band, mode) in self.worked

    def worked_combos(self, callsign: str) -> dict[str, list[str]]:
        """Band -> list of modes already worked with `call` this round, for
        bands that have at least one.

        Shown in red in the rprompt so the operator sees at a glance which
        band/mode combos are already in the log (i.e. which would be dups).
        Naturally empty for a brand-new callsign -- nothing worked yet -- so
        nothing is shown until there's something to warn about."""
        out: dict[str, list[str]] = {}
        for b in BANDS:
            worked = [m for m in MODES if (callsign, b, m) in self.worked]
            if worked:
                out[b] = worked
        return out

    def add(self, qso: QSO) -> bool:
        """Append QSO; returns True if duplicate."""
        dup = self.is_dup(qso.callsign, qso.band, qso.mode)
        self.qsos.append(qso)
        if not dup:
            self.worked.add((qso.callsign, qso.band, qso.mode))
        return dup

    def undo(self) -> QSO | None:
        if not self.qsos:
            return None
        q = self.qsos.pop()
        self.worked = {(x.callsign, x.band, x.mode) for x in self.qsos}
        return q

    def dist(self, loc: str) -> int:
        km = distance_between(self.my_loc, loc)
        return 0 if km is None else int(km)

    def bearing(self, loc: str) -> int:
        deg = bearing_between(self.my_loc, loc)
        return 0 if deg is None else int(deg)

    def bands(self) -> list[str]:
        seen: list[str] = []
        for q in self.qsos:
            if q.band not in seen:
                seen.append(q.band)
        return seen


def _is_dup_in_log(qsos: list[QSO], target: QSO) -> bool:
    seen: set[tuple[str, str, str]] = set()
    for q in qsos:
        k = (q.callsign, q.band, q.mode)
        if q is target:
            return k in seen
        seen.add(k)
    return False


# ──────────────────────────────────────────────────────────────
# EDI export
# ──────────────────────────────────────────────────────────────
_MONTH_HU = [
    "",
    "JANUAR",
    "FEBRUAR",
    "MARCIUS",
    "APRILIS",
    "MAJUS",
    "JUNIUS",
    "JULIUS",
    "AUGUSZTUS",
    "SZEPTEMBER",
    "OKTOBER",
    "NOVEMBER",
    "DECEMBER",
]


def tname_for(dt: datetime) -> str:
    return f"PUSKAS{dt.year}{_MONTH_HU[dt.month]}"


def write_edi(lb: LogBook, band: str, tname: str, out_dir: Path) -> Path | None:
    qsos = [q for q in lb.qsos if q.band == band]
    if not qsos:
        return None
    date_long = qsos[0].dt.strftime("%Y%m%d")
    date_6 = qsos[0].dt.strftime("%y%m%d")
    valid_qsos = [q for q in qsos if not _is_dup_in_log(qsos, q)]
    valid_pts = sum(q.dist_km for q in valid_qsos)
    unique_locs = len({q.loc for q in valid_qsos if q.loc})

    hdr = [
        "[REG1TEST;1]",
        f"TName={tname}",
        f"TDate={date_long};{date_long}",
        f"PCall={lb.my_callsign}",
        f"PWWLo={lb.my_loc}",
        f"PExch={lb.my_loc}",
        "PAdr1=",
        "PAdr2=",
        "PSect=SINGLE-OP",
        f"PBand={edi.HEADER_BY_BAND.get(band, '145 MHz')}",
        "PClub=",
        "RName=",
        "RCall=",
        "RAdr1=",
        "RAdr2=",
        "RPoCo=",
        "RCity=",
        "RCoun=Hungary",
        "RPhon=",
        "RHBBS=",
        f"MOpe1={lb.my_callsign}",
        "MOpe2=",
        "STXEq=",
        "SPowe=0",
        "SRXEq=",
        "SAnte=",
        "SAntH=0;0",
        f"CQSOs={len(qsos)};1",
        f"CQSOP={valid_pts}",
        f"CWWLs={unique_locs};0;1",
        "CWWLB=0",
        "CExcs=0;0;1",
        "CExcB=0",
        "CDXCs=1;0;1",
        "CDXCB=0",
        f"CToSc={valid_pts}",
        "CODXC=;;0",
        "[Remarks]",
        f"[QSORecords;{len(qsos)}]",
    ]

    records = []
    seen: set[tuple[str, str, str]] = set()
    for q in qsos:
        k = (q.callsign, q.band, q.mode)
        dup = k in seen
        seen.add(k)
        records.append(
            f"{date_6};{q.dt.strftime('%H%M')};{q.callsign};"
            f"{edi.CODE_BY_MODE.get(q.mode, '1')};"
            f"{q.rst_s};{q.nr_s:03d};{q.rst_r};{q.nr_r:03d};;"
            f"{q.loc};{0 if dup else q.dist_km};;;{'D' if dup else ''};"
        )

    path = out_dir / f"{date_6}-{lb.my_callsign}-{band}.edi"
    path.write_text("\n".join(hdr + records) + "\n", encoding="utf-8")
    return path


def save_all(lb: LogBook, tname: str) -> list[Path]:
    return [
        p
        for band in lb.bands()
        if (p := write_edi(lb, band, tname, Path("."))) is not None
    ]


# ──────────────────────────────────────────────────────────────
# EDI crash recovery
# ──────────────────────────────────────────────────────────────


def load_from_edi(
    paths: list[Path], loc_cache: dict[str, list[str]]
) -> tuple[LogBook, str] | None:
    """Parse EDI files and return (logbook, tname), or None on failure."""
    # Deduplicate by stem (case-insensitive) — guards against foo.EDI + foo.edi coexisting
    seen_stems: set[str] = set()
    unique: list[Path] = []
    for p in paths:
        key = p.stem.lower()
        if key not in seen_stems:
            seen_stems.add(key)
            unique.append(p)
    paths = unique

    my_callsign = my_loc = tname = ""

    logs = [edi.read(p) for p in paths]

    for log in logs:
        my_callsign = my_callsign or log.my_callsign.upper()
        my_loc = my_loc or log.my_locator.upper()
        tname = tname or log.contest_name
        if my_callsign:
            break

    if not my_callsign:
        return None

    lb = LogBook(my_callsign, my_loc, loc_cache)

    for log in logs:
        for r in log.records:
            callsign = r.callsign.upper()
            loc = r.loc.upper()
            if not (callsign and log.band and is_locator(loc)):
                continue
            try:
                nr_s, nr_r = int(r.nr_s), int(r.nr_r)
            except ValueError:
                continue
            lb.add(
                QSO(
                    dt=r.dt.replace(tzinfo=timezone.utc),
                    band=log.band,
                    mode=r.mode or "SSB",
                    callsign=callsign,
                    rst_s=r.rst_s,
                    nr_s=nr_s,
                    rst_r=r.rst_r,
                    nr_r=nr_r,
                    loc=loc,
                    dist_km=r.points or lb.dist(loc),
                )
            )

    lb.qsos.sort(key=lambda q: (q.dt, q.nr_s))
    return lb, tname
