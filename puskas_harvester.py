#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# ///
"""
Puskás URH Kupa – Pre-contest station harvester
================================================
Fetches all stations that have appeared in any Puskás URH Kupa round (as
submitters or QSO partners), with their locators and active bands.

Output: ~/puskas-seen-stations.json  (home directory, shared across contest rounds)

Usage:  uv run puskas_harvester.py
        Delete .puskas_cache/ to force a fresh fetch from the API.
"""

import json
import time
import urllib.request
from pathlib import Path

CONTEST_ID      = "67952021b55b621ae6619a4e"
BASE_URL        = "https://bb.mrasz.hu/nest"
LIST_URL        = "https://bb.mrasz.hu/nest/events/list?site=bb.mrasz.hu"
CACHE_DIR       = Path(".puskas_cache")
PUSKAS_DIR      = Path.home() / ".puskas"
OUTPUT          = PUSKAS_DIR / "puskas-seen-stations.json"
REQUEST_DELAY   = 0.3
REQUEST_TIMEOUT = 15
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PuskasHarvester/1.0)",
    "Accept":     "application/json",
    "Referer":    "https://bb.mrasz.hu/",
}


# ──────────────────────────────────────────────────────────────
# Cache
# ──────────────────────────────────────────────────────────────

def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    safe = key.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_")
    return CACHE_DIR / f"{safe}.json"


def _cached_get(url: str) -> dict | list | None:
    path = _cache_path(url.replace(BASE_URL, ""))
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(REQUEST_DELAY)
        return data
    except Exception as e:
        print(f"  [error] {url} → {e}")
        return None


# ──────────────────────────────────────────────────────────────
# Event discovery
# ──────────────────────────────────────────────────────────────

def fetch_event_ids() -> list[str]:
    CACHE_DIR.mkdir(exist_ok=True)
    list_cache = CACHE_DIR / "events_list.json"
    if list_cache.exists():
        data = json.loads(list_cache.read_text(encoding="utf-8"))
    else:
        print(f"  GET {LIST_URL}")
        try:
            req = urllib.request.Request(LIST_URL, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read())
            list_cache.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"  [error] {LIST_URL} → {e}")
            return []

    ids = [
        e["_id"] for e in data
        if e.get("isClaimed") and e.get("contest", {}).get("_id") == CONTEST_ID
    ]
    print(f"  → {len(ids)} claimed Puskás rounds")
    return ids


# ──────────────────────────────────────────────────────────────
# API
# ──────────────────────────────────────────────────────────────

def fetch_claimed(event_id: str) -> list[dict]:
    url  = f"{BASE_URL}/claimed?eventId={event_id}"
    data = _cached_get(url)
    if not data:
        return []
    stations: dict[str, str] = {}
    for category in data:
        for log in category.get("logs", []):
            call = log.get("_id", {}).get("callsign", "").upper().strip()
            wwl  = log.get("_id", {}).get("WWL", "").upper().strip()
            if call and wwl and call not in stations:
                stations[call] = wwl
    return [{"callsign": c, "wwl": w} for c, w in stations.items()]


def fetch_round_codes(event_id: str, callsign: str) -> list[str]:
    url  = f"{BASE_URL}/log?eventId={event_id}&skip=0&limit=25&callsign={callsign}"
    data = _cached_get(url)
    if not data:
        return []
    codes: list[str] = []
    for log in data.get("logs", []):
        for rnd in log.get("rounds", []):
            code = rnd.get("code", "")
            if code and code not in codes:
                codes.append(code)
    return codes


def fetch_qsos(event_id: str, callsign: str, round_code: str) -> list[dict]:
    url  = (f"{BASE_URL}/qso?eventId={event_id}"
            f"&callsign={callsign}&roundCode={round_code}&isClaimed=true")
    data = _cached_get(url)
    if not data:
        return []
    qsos = []
    for q in data.get("qsos", []):
        dx_call = q.get("callsign", "").upper().strip()
        dx_wwl  = q.get("rWWL", "").upper().strip()
        band    = q.get("band", "").strip()
        if dx_call and dx_wwl:
            qsos.append({"callsign": dx_call, "wwl": dx_wwl, "band": band})
    return qsos


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def main():
    print("Puskás URH Kupa – Station Harvester")
    print("─" * 40)
    if CACHE_DIR.exists():
        cached = list(CACHE_DIR.glob("*.json"))
        print(f"Cache: {len(cached)} files in {CACHE_DIR}/")

    print("Fetching event list...")
    event_ids = fetch_event_ids()
    if not event_ids:
        print("[!] No events found — check network or delete .puskas_cache/")
        return

    # call → {"wwls": list[str] most-recent-first, "bands": list[str]}
    stations: dict[str, dict] = {}

    def _record(call: str, wwl: str) -> None:
        """Add wwl to station's list; move to front if already seen (most recent = front)."""
        if call not in stations:
            stations[call] = {"wwls": [], "bands": []}
        if not wwl:
            return
        wwls = stations[call]["wwls"]
        if wwl in wwls:
            wwls.remove(wwl)
        wwls.insert(0, wwl)

    for i, event_id in enumerate(event_ids, 1):
        print(f"\n[{i}/{len(event_ids)}] {event_id}")
        claimed = fetch_claimed(event_id)
        print(f"  {len(claimed)} submitters")

        for j, s in enumerate(claimed, 1):
            call = s["callsign"]
            wwl  = s["wwl"]
            _record(call, wwl)

            for code in fetch_round_codes(event_id, call):
                for q in fetch_qsos(event_id, call, code):
                    _record(q["callsign"], q["wwl"])
                    band = q["band"]
                    if band and band not in stations[q["callsign"]]["bands"]:
                        stations[q["callsign"]]["bands"].append(band)

            if j % 10 == 0 or j == len(claimed):
                print(f"  {j}/{len(claimed)} processed — {len(stations)} total", flush=True)

    PUSKAS_DIR.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(stations, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] {len(stations)} stations → {OUTPUT}")
    print(f"     Delete {CACHE_DIR}/ to force a fresh fetch next time")


if __name__ == "__main__":
    main()
