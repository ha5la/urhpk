#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["prompt_toolkit"]
# ///
"""
Puskás URH Kupa – Contest QSO Logger
=====================================
Usage:  uv run puskas_logger.py
Input:  CALL NR_R [LOC] [RST_R]
          HA7NS 015           → locator from cache
          HA7NS 015 JN97WM    → explicit locator
          HA7NS 015 JN97WM 58 → also override received RST
Commands: !save  !undo  !band 2M|70CM|23CM  !mode SSB|CW|FM  !help
Ctrl-D at empty prompt → save EDI files and exit
"""

import json
import math
import netrc
import os
import re
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────
RIGCTLD_HOST   = "localhost"
RIGCTLD_PORT   = 4532
RIGCTLD_POLL_S = 5
MY_LOGS_DIR    = Path(__file__).parent / "my-logs"
BASE_URL       = "https://bb.mrasz.hu/nest"
EVENT_IDS: list[str] = [
    "69f763f0e0f63251aa32f0f1",  # 2026-05
    # Add new round event IDs here as they become available
]
REQUEST_TIMEOUT = 10
REQUEST_DELAY   = 0.3

# ──────────────────────────────────────────────────────────────
# Geo helpers
# ──────────────────────────────────────────────────────────────
def maidenhead_to_latlon(loc: str) -> tuple[float, float]:
    loc = loc.upper()
    lon = (ord(loc[0]) - 65) * 20 - 180
    lat = (ord(loc[1]) - 65) * 10 - 90
    lon += int(loc[2]) * 2
    lat += int(loc[3])
    if len(loc) >= 6:
        lon += (ord(loc[4]) - 65) * (5 / 60)
        lat += (ord(loc[5]) - 65) * (2.5 / 60)
        lon += 2.5 / 60
        lat += 1.25 / 60
    else:
        lon += 1.0
        lat += 0.5
    return lat, lon

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    φ1, λ1, φ2, λ2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((φ2-φ1)/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin((λ2-λ1)/2)**2
    return R * 2 * math.asin(math.sqrt(a))

# ──────────────────────────────────────────────────────────────
# Locator cache
# ──────────────────────────────────────────────────────────────
RE_LOC = re.compile(r'^[A-R]{2}[0-9]{2}([A-X]{2})?$', re.IGNORECASE)

def _parse_edi_files() -> dict[str, str]:
    cache: dict[str, str] = {}
    if not MY_LOGS_DIR.exists():
        return cache
    for path in sorted(MY_LOGS_DIR.glob("*.[Ee][Dd][Ii]")):
        try:
            in_qso = False
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("[QSORecords"):
                    in_qso = True
                    continue
                if not in_qso:
                    continue
                f = line.split(";")
                if len(f) >= 10:
                    call = f[2].strip().upper()
                    loc  = f[9].strip().upper()
                    if call and RE_LOC.match(loc):
                        cache[call] = loc   # later file (sorted by name) wins
        except Exception:
            pass
    return cache

def _fetch_api_locators() -> dict[str, str]:
    cache: dict[str, str] = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PuskasLogger/1.0)",
        "Accept":     "application/json",
    }
    for event_id in EVENT_IDS:
        try:
            url = f"{BASE_URL}/claimed?eventId={event_id}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read())
            for cat in data:
                for log in cat.get("logs", []):
                    call = log.get("_id", {}).get("callsign", "").upper().strip()
                    wwl  = log.get("_id", {}).get("WWL", "").upper().strip()
                    if call and RE_LOC.match(wwl):
                        cache[call] = wwl
            time.sleep(REQUEST_DELAY)
        except Exception:
            pass
    return cache

def build_loc_cache() -> dict[str, str]:
    print("Building locator cache...")
    cache = _parse_edi_files()
    print(f"  {len(cache)} callsigns from local EDI files in {MY_LOGS_DIR}")
    if EVENT_IDS:
        print(f"  Querying bb.mrasz.hu ({len(EVENT_IDS)} event(s))...", flush=True)
        api = _fetch_api_locators()
        new = sum(1 for k in api if k not in cache)
        cache.update(api)
        print(f"  {len(api)} from API, {new} new — total {len(cache)}")
    return cache

# ──────────────────────────────────────────────────────────────
# rigctld — background daemon thread
# ──────────────────────────────────────────────────────────────
_rig: dict       = {"band": "", "mode": "", "qrg": "", "online": False}
_rig_lock        = threading.Lock()
_rig_manual: dict = {"band": "", "mode": ""}   # manual override when rig offline

def _mode_str(raw: str) -> str:
    r = raw.upper()
    if r in ("USB", "LSB", "AM", "DSB", "SAM"): return "SSB"
    if r in ("CW",  "CWR"):                      return "CW"
    if r in ("FM",  "FMN", "WFM", "NFM"):       return "FM"
    return r or "SSB"

def _band_from_qrg(mhz: float) -> str:
    if mhz < 300:  return "2M"
    if mhz < 1000: return "70CM"
    return "23CM"

def _read_rig() -> tuple[str, str]:
    try:
        with socket.create_connection((RIGCTLD_HOST, RIGCTLD_PORT), timeout=2.0) as s:
            s.sendall(b"f\nm\n")
            buf, t0 = b"", time.monotonic()
            while time.monotonic() - t0 < 2.0:
                s.settimeout(2.0 - (time.monotonic() - t0))
                chunk = s.recv(256)
                if not chunk:
                    break
                buf += chunk
                if len(buf.decode(errors="replace").splitlines()) >= 2:
                    break
            lines = buf.decode(errors="replace").splitlines()
            return f"{float(lines[0]) / 1e6:.3f}", lines[1].strip() if len(lines) > 1 else ""
    except Exception:
        return "", ""

def _rig_thread():
    while True:
        qrg, raw = _read_rig()
        with _rig_lock:
            if qrg:
                _rig.update(band=_band_from_qrg(float(qrg)),
                            mode=_mode_str(raw), qrg=qrg, online=True)
            else:
                _rig.update(band="", mode="", qrg="", online=False)
        time.sleep(RIGCTLD_POLL_S)

def current_rig() -> tuple[str, str, str, bool]:
    """(band, mode, qrg, online) — falls back to manual override if offline."""
    with _rig_lock:
        if _rig["online"]:
            return _rig["band"], _rig["mode"], _rig["qrg"], True
    return _rig_manual["band"], _rig_manual["mode"], "", False

# ──────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────
@dataclass
class QSO:
    dt:      datetime
    band:    str
    mode:    str
    call:    str
    rst_s:   str
    nr_s:    int
    rst_r:   str
    nr_r:    int
    loc:     str
    dist_km: int

class LogBook:
    def __init__(self, my_call: str, my_loc: str, loc_cache: dict[str, str]):
        self.my_call   = my_call
        self.my_loc    = my_loc
        self.loc_cache = loc_cache
        self.qsos:   list[QSO]                 = []
        self.worked: set[tuple[str, str, str]] = set()   # (call, band, mode)

    def next_nr(self, band: str) -> int:
        return sum(1 for q in self.qsos if q.band == band) + 1

    def is_dup(self, call: str, band: str, mode: str) -> bool:
        return (call, band, mode) in self.worked

    def add(self, qso: QSO) -> bool:
        """Append QSO; returns True if duplicate."""
        dup = self.is_dup(qso.call, qso.band, qso.mode)
        self.qsos.append(qso)
        if not dup:
            self.worked.add((qso.call, qso.band, qso.mode))
        return dup

    def undo(self) -> QSO | None:
        if not self.qsos:
            return None
        q = self.qsos.pop()
        self.worked = {(x.call, x.band, x.mode) for x in self.qsos}
        return q

    def dist(self, loc: str) -> int:
        if not (self.my_loc and loc):
            return 0
        try:
            return int(haversine_km(*maidenhead_to_latlon(self.my_loc),
                                    *maidenhead_to_latlon(loc)))
        except Exception:
            return 0

    def bands(self) -> list[str]:
        seen: list[str] = []
        for q in self.qsos:
            if q.band not in seen:
                seen.append(q.band)
        return seen

def _is_dup_in_log(qsos: list[QSO], target: QSO) -> bool:
    seen: set[tuple[str, str, str]] = set()
    for q in qsos:
        k = (q.call, q.band, q.mode)
        if q is target:
            return k in seen
        seen.add(k)
    return False

# ──────────────────────────────────────────────────────────────
# EDI export
# ──────────────────────────────────────────────────────────────
_BAND_FREQ = {"2M": "145 MHz", "70CM": "435 MHz", "23CM": "1296 MHz"}
_MODE_CODE = {"SSB": "1", "CW": "2", "FM": "6"}
_MONTH_HU  = ["","JANUAR","FEBRUAR","MARCIUS","APRILIS","MAJUS","JUNIUS",
               "JULIUS","AUGUSZTUS","SZEPTEMBER","OKTOBER","NOVEMBER","DECEMBER"]

def tname_for(dt: datetime) -> str:
    return f"PUSKAS{dt.year}{_MONTH_HU[dt.month]}"

def write_edi(lb: LogBook, band: str, tname: str, out_dir: Path) -> Path | None:
    qsos = [q for q in lb.qsos if q.band == band]
    if not qsos:
        return None
    date_long = qsos[0].dt.strftime("%Y%m%d")
    date_6    = qsos[0].dt.strftime("%y%m%d")
    valid_qsos = [q for q in qsos if not _is_dup_in_log(qsos, q)]
    valid_pts  = sum(q.dist_km for q in valid_qsos)
    unique_locs = len({q.loc for q in valid_qsos if q.loc})

    hdr = [
        "[REG1TEST;1]",
        f"TName={tname}",
        f"TDate={date_long};{date_long}",
        f"PCall={lb.my_call}",
        f"PWWLo={lb.my_loc}",
        f"PExch={lb.my_loc}",
        "PAdr1=", "PAdr2=",
        "PSect=SINGLE-OP",
        f"PBand={_BAND_FREQ.get(band, '145 MHz')}",
        "PClub=", "RName=", "RCall=",
        "RAdr1=", "RAdr2=", "RPoCo=", "RCity=",
        "RCoun=Hungary", "RPhon=", "RHBBS=",
        f"MOpe1={lb.my_call}", "MOpe2=",
        "STXEq=", "SPowe=0", "SRXEq=", "SAnte=", "SAntH=0;0",
        f"CQSOs={len(qsos)};1",
        f"CQSOP={valid_pts}",
        f"CWWLs={unique_locs};0;1",
        "CWWLB=0", "CExcs=0;0;1", "CExcB=0",
        "CDXCs=1;0;1", "CDXCB=0",
        f"CToSc={valid_pts}",
        "CODXC=;;0",
        "[Remarks]",
        f"[QSORecords;{len(qsos)}]",
    ]

    records = []
    seen: set[tuple[str, str, str]] = set()
    for q in qsos:
        k = (q.call, q.band, q.mode)
        dup = k in seen
        seen.add(k)
        records.append(
            f"{date_6};{q.dt.strftime('%H%M')};{q.call};"
            f"{_MODE_CODE.get(q.mode, '1')};"
            f"{q.rst_s};{q.nr_s:03d};{q.rst_r};{q.nr_r:03d};;"
            f"{q.loc};{0 if dup else q.dist_km};;;{'D' if dup else ''};"
        )

    path = out_dir / f"{date_6}-{lb.my_call}-{band}.EDI"
    path.write_text("\n".join(hdr + records) + "\n", encoding="utf-8")
    return path

def save_all(lb: LogBook, tname: str) -> list[Path]:
    return [p for band in lb.bands()
            if (p := write_edi(lb, band, tname, Path("."))) is not None]

# ──────────────────────────────────────────────────────────────
# Input parser
# ──────────────────────────────────────────────────────────────
RE_CALL = re.compile(r'^[A-Z0-9]{2,}(/[A-Z0-9P/]+)?$')

def parse_input(line: str) -> dict | str:
    """Parse 'CALL NR_R [LOC] [RST_R]'. Returns dict or error string."""
    tokens = line.upper().split()
    if not tokens:
        return ""
    if len(tokens) < 2:
        return "Usage: CALL NR_R [LOC]   e.g.  HA7NS 015"
    call = tokens[0]
    if not RE_CALL.match(call):
        return f"Invalid callsign: {call!r}"
    try:
        nr_r = int(tokens[1])
        if not (0 < nr_r < 10000):
            raise ValueError
    except ValueError:
        return f"Expected serial number as second token, got {tokens[1]!r}"
    loc = rst_r = ""
    for tok in tokens[2:]:
        if not loc and RE_LOC.match(tok):
            loc = tok[:6]
        elif not rst_r and tok.isdigit():
            rst_r = tok
    return dict(call=call, nr_r=nr_r, loc=loc, rst_r=rst_r)

# ──────────────────────────────────────────────────────────────
# Display helpers
# ──────────────────────────────────────────────────────────────
W = 56

def _band_summary(lb: LogBook) -> str:
    parts = [f"{b}:{sum(1 for q in lb.qsos if q.band == b)}"
             for b in ("2M", "70CM", "23CM")
             if any(q.band == b for q in lb.qsos)]
    return "  ".join(parts) or "no QSOs yet"

def _print_header(lb: LogBook, band: str, mode: str, qrg: str, online: bool):
    now     = datetime.now(timezone.utc).strftime("%H:%M UTC")
    rig_str = f"{qrg} MHz {mode}" if online else "(rig offline)"
    nr      = lb.next_nr(band) if band else 1
    bar     = "━" * W
    print(f"\n\033[1m{bar}\033[0m")
    print(f" PUSKÁS LOGGER  {now}  │  {band or '?'}  {rig_str}  │  Next: {nr:03d}")
    print(f" {_band_summary(lb)}")
    print(f"\033[1m{bar}\033[0m")

def _print_recent(lb: LogBook, n: int = 8):
    for q in lb.qsos[-n:]:
        dup    = _is_dup_in_log(lb.qsos, q)
        dist   = f"  {q.dist_km} km" if q.dist_km else ""
        marker = "  \033[31mDUP\033[0m" if dup else ""
        print(f"  {q.dt.strftime('%H:%M')}  {q.call:<10}  {q.mode:<4}"
              f"  {q.rst_s} {q.nr_s:03d}  {q.rst_r} {q.nr_r:03d}  {q.loc:<6}{dist}{marker}")
    print("─" * W)

def _toolbar() -> HTML:
    band, mode, qrg, online = current_rig()
    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if online:
        return HTML(f"  <b>Rig:</b> {qrg} MHz {mode}  │  {t} UTC")
    else:
        return HTML(f"  <b>Rig:</b> <ansired>offline</ansired>  │  {t} UTC")

# ──────────────────────────────────────────────────────────────
# Command handler
# ──────────────────────────────────────────────────────────────
def _handle_command(line: str, lb: LogBook, tname: str):
    parts = line.split()
    cmd   = parts[0].lower()

    if cmd == "!save":
        paths = save_all(lb, tname)
        for p in paths:
            print(f"  Saved: {p}")

    elif cmd == "!undo":
        q = lb.undo()
        if q:
            print(f"  Undone: {q.dt.strftime('%H:%M')} {q.call} {q.band} {q.mode}")
            save_all(lb, tname)
        else:
            print("  Nothing to undo.")

    elif cmd == "!band":
        if len(parts) < 2 or parts[1].upper() not in ("2M", "70CM", "23CM"):
            print("  Usage: !band 2M | 70CM | 23CM")
        else:
            _rig_manual["band"] = parts[1].upper()
            print(f"  Band override: {_rig_manual['band']}")

    elif cmd == "!mode":
        if len(parts) < 2 or parts[1].upper() not in ("SSB", "CW", "FM"):
            print("  Usage: !mode SSB | CW | FM")
        else:
            _rig_manual["mode"] = parts[1].upper()
            print(f"  Mode override: {_rig_manual['mode']}")

    elif cmd == "!help":
        print("  CALL NR_R [LOC] [RST_R]  — log a QSO")
        print("  !save                    — write EDI files now")
        print("  !undo                    — remove last QSO")
        print("  !band 2M|70CM|23CM       — set band manually (rig offline)")
        print("  !mode SSB|CW|FM          — set mode manually (rig offline)")
        print("  !help                    — this help")
        print("  Ctrl-D                   — save and exit")

    else:
        print(f"  Unknown command: {cmd}  (try !help)")

    input("  [Enter to continue]")

# ──────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────
def run(lb: LogBook, tname: str):
    session = PromptSession()
    while True:
        band, mode, qrg, online = current_rig()
        os.write(1, b"\033[2J\033[H")
        _print_header(lb, band, mode, qrg, online)
        _print_recent(lb)
        if not band:
            print("\033[33m  No band — use !band 2M or !band 70CM or !band 23CM\033[0m")

        try:
            line = session.prompt("> ", bottom_toolbar=_toolbar).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break

        if not line:
            continue

        if line.startswith("!"):
            _handle_command(line, lb, tname)
            continue

        parsed = parse_input(line)
        if isinstance(parsed, str):
            if parsed:
                print(f"\033[31m  {parsed}\033[0m")
                input("  [Enter to continue]")
            continue

        if not band:
            print("\033[31m  Cannot log: band unknown. Set with !band 2M\033[0m")
            input("  [Enter to continue]")
            continue

        call     = parsed["call"]
        nr_r     = parsed["nr_r"]
        loc      = parsed["loc"] or lb.loc_cache.get(call, "")
        rst_def  = "599" if mode == "CW" else "59"
        rst_s    = rst_def
        rst_r    = parsed["rst_r"] or rst_def
        nr_s     = lb.next_nr(band)
        dist_km  = lb.dist(loc)

        qso = QSO(
            dt=datetime.now(timezone.utc).replace(second=0, microsecond=0),
            band=band, mode=mode or "SSB", call=call,
            rst_s=rst_s, nr_s=nr_s, rst_r=rst_r, nr_r=nr_r,
            loc=loc, dist_km=dist_km,
        )

        dup = lb.add(qso)
        if dup:
            print(f"\033[31m  *** DUP *** {call} already in log for {band} {mode}\033[0m")
            input("  [Enter to continue]")

        save_all(lb, tname)   # auto-save after every QSO

    # Ctrl-D — final save
    print("\nSaving EDI files...")
    paths = save_all(lb, tname)
    if paths:
        for p in paths:
            print(f"  {p}")
    else:
        print("  (no QSOs logged)")

# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────
def _load_callsign() -> str:
    try:
        auth = netrc.netrc().authenticators("www.on4kst.info")
        if auth:
            return auth[0].upper()
    except Exception:
        pass
    return "HA5LA"

def main():
    print("Puskás URH Kupa Logger")
    print("─" * 40)
    my_call = _load_callsign()
    print(f"Callsign: {my_call}")

    my_loc = input("Your locator [JN97TF]: ").strip().upper() or "JN97TF"
    if not RE_LOC.match(my_loc):
        print(f"Warning: {my_loc!r} doesn't look like a valid Maidenhead locator")

    now = datetime.now(timezone.utc)
    default_tname = tname_for(now)
    tname = input(f"Contest name [{default_tname}]: ").strip() or default_tname

    loc_cache = build_loc_cache()

    t = threading.Thread(target=_rig_thread, daemon=True)
    t.start()

    lb = LogBook(my_call, my_loc, loc_cache)

    print()
    print("Input: CALL NR_R [LOC]   e.g.  HA7NS 015   or  HA7NS 015 JN97WM")
    print("!help for commands  │  Ctrl-D to save and exit")
    print()
    input("[Enter to start]")

    try:
        run(lb, tname)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        save_all(lb, tname)
        raise

if __name__ == "__main__":
    main()
