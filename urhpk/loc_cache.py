"""The locator cache: which locator a callsign was last heard using.

Three sources merge, most-trusted first — this operator's own past EDI logs,
then the harvester's database, then whatever the ON4KST bridge has seen. A
callsign can appear in all three with different locators; the order decides
which one the logger offers.

Locators typed during a round go to the front of their callsign's list, so the
most recent observation always wins.
"""

from __future__ import annotations

import json
from pathlib import Path

from urhpk import edi
from urhpk.geo import is_locator
from urhpk.logbook import MY_LOGS_DIR
from urhpk.wiring import ON4KST_SEEN, SEEN_STATIONS


def _from_my_logs() -> dict[str, str]:
    """The locator this operator last logged for each callsign, from their own
    past EDI logs in `my-logs/`. Files are read oldest-first by name, so a
    later round's locator overwrites an earlier one."""
    cache: dict[str, str] = {}
    if not MY_LOGS_DIR.exists():
        return cache
    for path in sorted(MY_LOGS_DIR.glob("*.[Ee][Dd][Ii]")):
        for record in edi.read(path).records:
            callsign, loc = record.callsign.upper(), record.loc.upper()
            if callsign and is_locator(loc):
                cache[callsign] = loc
    return cache


def _from_seen_file(path: Path) -> dict[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, list[str]] = {}
    for callsign, v in data.items():
        wwls = v.get("wwls") or ([v["wwl"]] if v.get("wwl") else [])
        if wwls:
            result[callsign] = list(wwls)
    return result


def merge_sources(*sources: dict[str, list[str]]) -> dict[str, list[str]]:
    """Merge locator sources in priority order (highest-priority source first).

    Each locator appears at most once, at the position of the highest-priority
    source that contains it.  Sources listed later only contribute locs not
    already present from an earlier (higher-priority) source.
    """
    result: dict[str, list[str]] = {}
    for source in sources:
        for callsign, locs in source.items():
            existing = result.setdefault(callsign, [])
            for loc in locs:
                if loc not in existing:
                    existing.append(loc)
    return result


def load() -> dict[str, list[str]]:
    # Priority order, highest first: edi > on4kst > puskas.
    # QSO-entered locs are inserted at the front later via remember.
    edi_raw = _from_my_logs()
    edi: dict[str, list[str]] = {callsign: [loc] for callsign, loc in edi_raw.items()}
    if edi:
        print(f"  {len(edi)} stations from my-logs/")

    on4kst: dict[str, list[str]] = {}
    if ON4KST_SEEN.exists():
        try:
            on4kst = _from_seen_file(ON4KST_SEEN)
            print(f"  {len(on4kst)} stations from {ON4KST_SEEN.name}")
        except Exception:
            pass

    puskas: dict[str, list[str]] = {}
    if SEEN_STATIONS.exists():
        try:
            puskas = _from_seen_file(SEEN_STATIONS)
            print(f"  {len(puskas)} stations from {SEEN_STATIONS.name}")
        except Exception:
            pass

    cache = merge_sources(edi, on4kst, puskas)
    if not cache:
        print("  No locator cache (run puskas_harvester.py to build one)")
    return cache


def remember(loc_cache: dict[str, list[str]], callsign: str, loc: str) -> None:
    """Insert loc at the front of loc_cache[call], maintaining most-recent-first order."""
    if not loc:
        return
    locs = loc_cache.setdefault(callsign, [])
    if loc in locs:
        locs.remove(loc)
    locs.insert(0, loc)
