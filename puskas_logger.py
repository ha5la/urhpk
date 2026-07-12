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
Input:  CALL RST NR LOC
          HA7NS 59 015 JN97WM    → locator required
          HA7NS 599 014 JN97WM   → CW with locator
Commands: !undo  !help
Ctrl-D at empty prompt → save EDI files and exit
"""

import json
import math
import netrc
import os
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_completions
from prompt_toolkit.formatted_text import HTML, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import DynamicStyle, Style

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────
RIGCTLD_HOST   = "localhost"
RIGCTLD_PORT   = 4532
ROTCTLD_HOST   = "localhost"
ROTCTLD_PORT   = 4533
RIGCTLD_POLL_S = 1
MY_LOGS_DIR    = Path("my-logs")
PUSKAS_DIR     = Path.home() / ".puskas"
SEEN_STATIONS  = PUSKAS_DIR / "puskas-seen-stations.json"
ON4KST_SEEN    = PUSKAS_DIR / "on4kst-seen-stations.json"
_BANDS         = ("2M", "70CM", "23CM")
_MODES         = ("SSB", "CW", "FM")
WEBCAM_DEVICE       = "/dev/video0"  # find with: v4l2-ctl --list-devices
WEBCAM_AUDIO_SOURCE = "default"      # find with: pactl list short sources

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

def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    φ1, λ1, φ2, λ2 = map(math.radians, (lat1, lon1, lat2, lon2))
    x = math.sin(λ2 - λ1) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(λ2 - λ1)
    return math.degrees(math.atan2(x, y)) % 360

_BEARING_ARROWS = "↑↗→↘↓↙←↖"

def _bearing_arrow(degrees: int) -> str:
    return _BEARING_ARROWS[int((degrees + 22.5) / 45) % 8]

def _format_open_combos(by_band: dict[str, list[str]]) -> str:
    """Compact 'BAND:MODE,MODE' listing for the rprompt, e.g.
    '70CM:CW,FM 23CM:SSB,CW,FM' -- see LogBook.open_combos."""
    return ' '.join(f"{b}:{','.join(ms)}" for b, ms in by_band.items())

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
                        cache[call] = loc
        except Exception:
            pass
    return cache

def _parse_seen_file(path: Path) -> dict[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, list[str]] = {}
    for call, v in data.items():
        wwls = v.get("wwls") or ([v["wwl"]] if v.get("wwl") else [])
        if wwls:
            result[call] = list(wwls)
    return result

def _merge_loc_sources(*sources: dict[str, list[str]]) -> dict[str, list[str]]:
    """Merge locator sources in priority order (highest-priority source first).

    Each locator appears at most once, at the position of the highest-priority
    source that contains it.  Sources listed later only contribute locs not
    already present from an earlier (higher-priority) source.
    """
    result: dict[str, list[str]] = {}
    for source in sources:
        for call, locs in source.items():
            existing = result.setdefault(call, [])
            for loc in locs:
                if loc not in existing:
                    existing.append(loc)
    return result

def load_loc_cache() -> dict[str, list[str]]:
    # Priority order, highest first: edi > on4kst > puskas.
    # QSO-entered locs are inserted at the front later via _update_loc_cache.
    edi_raw = _parse_edi_files()
    edi: dict[str, list[str]] = {call: [loc] for call, loc in edi_raw.items()}
    if edi:
        print(f"  {len(edi)} stations from my-logs/")

    on4kst: dict[str, list[str]] = {}
    if ON4KST_SEEN.exists():
        try:
            on4kst = _parse_seen_file(ON4KST_SEEN)
            print(f"  {len(on4kst)} stations from {ON4KST_SEEN.name}")
        except Exception:
            pass

    puskas: dict[str, list[str]] = {}
    if SEEN_STATIONS.exists():
        try:
            puskas = _parse_seen_file(SEEN_STATIONS)
            print(f"  {len(puskas)} stations from {SEEN_STATIONS.name}")
        except Exception:
            pass

    cache = _merge_loc_sources(edi, on4kst, puskas)
    if not cache:
        print("  No locator cache (run puskas_harvester.py to build one)")
    return cache

# ──────────────────────────────────────────────────────────────
# rigctld — background daemon thread
# ──────────────────────────────────────────────────────────────
_rig: dict        = {"band": "", "mode": "", "qrg": "", "online": False}
_rig_lock         = threading.Lock()
_rig_manual: dict = {"band": "", "mode": ""}

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
    """Query freq ('f' -> 1 line) and mode ('m' -> mode + passband, 2 lines)
    in one round trip. 3 lines total.

    PTT used to be queried here too ('t') and recorded into telemetry, but
    the WAV recordings themselves turned out to carry the same information
    straight from the rig (IC-9700 'Voice Recorder' metadata, read by
    contest_video.py's read_wav_metadata) with zero polling lag -- telemetry
    was in practice used to *reconstruct* something already recorded
    losslessly elsewhere. Removed rather than kept for redundancy per Kent
    Beck's rule: dead weight, not a safety net.
    """
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
                if len(buf.decode(errors="replace").splitlines()) >= 3:
                    break
            lines = buf.decode(errors="replace").splitlines()
            qrg = f"{float(lines[0]) / 1e6:.3f}"
            mode = lines[1].strip() if len(lines) > 1 else ""
            return qrg, mode
    except Exception:
        return "", ""

def _rig_thread():
    while True:
        try:
            qrg, raw = _read_rig()
            with _rig_lock:
                if qrg:
                    _rig.update(band=_band_from_qrg(float(qrg)),
                                mode=_mode_str(raw), qrg=qrg, online=True)
                else:
                    _rig.update(band="", mode="", qrg="", online=False)
        except Exception:
            pass
        time.sleep(RIGCTLD_POLL_S)

def current_rig() -> tuple[str, str, str, bool]:
    """(band, mode, qrg, online) — falls back to manual override if offline."""
    with _rig_lock:
        if _rig["online"]:
            return _rig["band"], _rig["mode"], _rig["qrg"], True
    return _rig_manual["band"], _rig_manual["mode"], "", False

# ──────────────────────────────────────────────────────────────
# rotctld — background daemon thread
# ──────────────────────────────────────────────────────────────
_rot: dict  = {"az": 0.0, "online": False}
_rot_lock   = threading.Lock()

def _read_rot() -> float | None:
    with socket.create_connection((ROTCTLD_HOST, ROTCTLD_PORT), timeout=2.0) as s:
        s.sendall(b"p\n")
        buf = b""
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.0:
            s.settimeout(2.0 - (time.monotonic() - t0))
            chunk = s.recv(64)
            if not chunk:
                break
            buf += chunk
            if len(buf.splitlines()) >= 1:
                break
        return float(buf.decode(errors="replace").splitlines()[0])

def _rot_thread():
    while True:
        try:
            az = _read_rot()
            with _rot_lock:
                _rot.update(az=az, online=True)
        except Exception:
            with _rot_lock:
                _rot.update(az=0.0, online=False)
        time.sleep(RIGCTLD_POLL_S)

def current_rot() -> tuple[float, bool]:
    """(azimuth_degrees, online)."""
    with _rot_lock:
        return _rot["az"], _rot["online"]

def _rot_set(az: int) -> None:
    def _do():
        try:
            with socket.create_connection((ROTCTLD_HOST, ROTCTLD_PORT), timeout=2.0) as s:
                s.sendall(f"P {az:.1f} 0\n".encode())
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()

_clock_sync_notice: dict = {"msg": "", "until": 0.0}
_clock_sync_lock = threading.Lock()

def _clock_sync() -> None:
    """Sleep to the next minute boundary, then push UTC time to rigctld.

    The IC-9700 ignores the seconds field, so we sync on :00 for reliability.
    Shows "waiting…" immediately so the operator knows the key was registered,
    then the result for 5 s once the sync fires."""
    def _do():
        with _clock_sync_lock:
            _clock_sync_notice["msg"]   = "clock sync: waiting for :00…"
            _clock_sync_notice["until"] = time.monotonic() + 120.0
        now = datetime.now(timezone.utc)
        secs_to_next_minute = 60 - now.second - now.microsecond / 1e6
        time.sleep(secs_to_next_minute)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
        try:
            with socket.create_connection((RIGCTLD_HOST, RIGCTLD_PORT), timeout=3.0) as s:
                s.sendall(f"\\set_clock {ts}\n".encode())
                resp = s.recv(64).decode(errors="replace").strip()
                msg = f"clock synced {ts[11:16]}Z" if resp == "RPRT 0" else f"clock sync failed: {resp}"
        except Exception as exc:
            msg = f"clock sync failed: {exc}"
        with _clock_sync_lock:
            _clock_sync_notice["msg"]   = msg
            _clock_sync_notice["until"] = time.monotonic() + 5.0
    threading.Thread(target=_do, daemon=True).start()

# ──────────────────────────────────────────────────────────────
# Telemetry recorder — one JSON line per second to CWD
# ──────────────────────────────────────────────────────────────

def _telemetry_record(now: datetime) -> str:
    """Build one telemetry JSON line from the current rig/rotator state.

    No ptt field: the WAV recordings' own IC-9700 metadata already carries
    it, straight from the rig with zero polling lag (see contest_video.py's
    read_wav_metadata) -- telemetry only needs to cover what the WAV
    metadata *can't*: freq/mode drift within a long segment, and az, which
    has no on-air equivalent at all.
    """
    _, mode, qrg, rig_online = current_rig()
    az, rot_online = current_rot()
    return json.dumps({
        "t":       now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "freq_hz": round(float(qrg) * 1e6) if rig_online and qrg else None,
        "mode":    mode                     if rig_online          else None,
        "az":      round(az, 1)             if rot_online          else None,
    })

def _telemetry_thread(path: Path) -> None:
    with open(path, "a") as fh:
        while True:
            try:
                fh.write(_telemetry_record(datetime.now(timezone.utc)) + "\n")
                fh.flush()
            except Exception:
                pass
            time.sleep(1.0)

# ──────────────────────────────────────────────────────────────
# Input-box recorder — event-triggered (one line per keystroke), not
# polled: a 1 Hz sample like telemetry would blur or entirely miss fast
# typing, and the buffer only changes on a keypress in the first place, so
# there's nothing to poll. Feeds contest_video.py's typewriter overlay.
#
# Two event kinds share the file: "text" (one per keystroke, the full
# current buffer contents) and "qso" (one per QSO actually appended to the
# log, microsecond-precise). The EDI format only stores QSO time to the
# minute, which is what makes contest_video.py's QSO-panel timing an
# audio-structure-snapping guess rather than exact — this file's "qso"
# events give it an exact submit timestamp to use instead, when available.
# Note this deliberately does *not* try to distinguish a submit from an
# abort (Enter vs Ctrl+U/Escape) at the "text" level — both just clear the
# buffer the same way, and even Enter's own clear only happens at the start
# of the *next* prompt, not at keypress time. Trying to infer "QSO logged"
# from that stream is unreliable; the explicit "qso" event below, written
# from the one place in the code that actually knows a QSO was appended, is
# the unambiguous alternative.
# ──────────────────────────────────────────────────────────────

_input_log_fh = None

def _input_log_open(path: Path) -> None:
    global _input_log_fh
    _input_log_fh = open(path, "a")

def _log_input_event(rec: dict) -> None:
    if _input_log_fh is None:
        return
    try:
        _input_log_fh.write(json.dumps(rec) + "\n")
        _input_log_fh.flush()
    except Exception:
        pass

def _on_buffer_changed(buf) -> None:
    _log_input_event({
        "t":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event": "text",
        "text":  buf.text,
    })

# ──────────────────────────────────────────────────────────────
# Webcam capture (Alt+V toggles start/stop) -- unlike the phone recording
# contest_video.py had to sync via audio cross-correlation (two independent,
# unrelated clocks), this runs on the same machine as the logger: start/stop
# is logged through the exact same _log_input_event/datetime.now(timezone.utc)
# used for QSOs and keystrokes, so the recording's real start time is known
# precisely with no separate device clock to reconcile at all.
# ──────────────────────────────────────────────────────────────

_webcam_proc = None
_webcam_log_fh = None

def _webcam_capture_cmd(device: str, audio_source: str, out_path: str) -> list[str]:
    """The ffmpeg command to capture the local webcam + mic to `out_path`.
    -preset ultrafast keeps this cheap enough to run alongside the logger
    for a multi-hour session without competing for CPU with rigctld polling
    or the UI itself."""
    return ["ffmpeg", "-y", "-f", "v4l2", "-i", device,
           "-f", "pulse", "-i", audio_source,
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
           "-c:a", "aac", out_path]

def _webcam_toggle(path_prefix: str) -> str | None:
    """Start or stop webcam capture; returns a status message for the
    toolbar/notice area, or None if nothing changed (e.g. ffmpeg missing)."""
    global _webcam_proc, _webcam_log_fh
    now = datetime.now(timezone.utc)
    if _webcam_proc is None:
        out_path = f"{path_prefix}-webcam.mp4"
        try:
            _webcam_log_fh = open(f"{path_prefix}-webcam.log", "a")
            _webcam_proc = subprocess.Popen(
                _webcam_capture_cmd(WEBCAM_DEVICE, WEBCAM_AUDIO_SOURCE, out_path),
                stdin=subprocess.DEVNULL, stdout=_webcam_log_fh, stderr=subprocess.STDOUT)
        except Exception as e:
            _webcam_proc = None
            return f"webcam start failed: {e}"
        _log_input_event({"t": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "event": "webcam_start"})
        return f"recording {out_path}"
    else:
        _webcam_proc.send_signal(signal.SIGINT)
        try:
            _webcam_proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            _webcam_proc.terminate()
        _webcam_proc = None
        if _webcam_log_fh:
            _webcam_log_fh.close()
            _webcam_log_fh = None
        _log_input_event({"t": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "event": "webcam_stop"})
        return "recording stopped"

def _webcam_stop_if_running() -> None:
    """Called on exit so a still-running capture is stopped cleanly (SIGINT,
    not killed) rather than left orphaned or the mp4 left unfinalized."""
    if _webcam_proc is not None:
        _webcam_toggle("")  # path_prefix unused on the stop branch

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
    def __init__(self, my_call: str, my_loc: str, loc_cache: dict[str, list[str]]):
        self.my_call   = my_call
        self.my_loc    = my_loc
        self.loc_cache = loc_cache
        self.qsos:   list[QSO]                 = []
        self.worked: set[tuple[str, str, str]] = set()   # (call, band, mode)

    def next_nr(self, band: str) -> int:
        if band:
            return sum(1 for q in self.qsos if q.band == band) + 1
        return len(self.qsos) + 1

    def is_dup(self, call: str, band: str, mode: str) -> bool:
        return (call, band, mode) in self.worked

    def open_combos(self, call: str) -> dict[str, list[str]]:
        """Band -> list of modes not yet worked with `call` this round, for
        bands that have at least one open mode.

        Empty if `call` hasn't been worked in *any* band/mode yet -- every
        one of the 9 combos is trivially open on a brand-new callsign,
        which isn't useful information to show. Only meaningful once
        there's at least one worked combo to compare the rest against."""
        if not any((call, b, m) in self.worked for b in _BANDS for m in _MODES):
            return {}
        out: dict[str, list[str]] = {}
        for b in _BANDS:
            missing = [m for m in _MODES if (call, b, m) not in self.worked]
            if missing:
                out[b] = missing
        return out

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

    def bearing(self, loc: str) -> int:
        if not (self.my_loc and loc):
            return 0
        try:
            return int(initial_bearing(*maidenhead_to_latlon(self.my_loc),
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

    path = out_dir / f"{date_6}-{lb.my_call}-{band}.edi"
    stale = path.with_suffix(".EDI")   # remove uppercase sibling from pre-1.6 saves
    if stale.exists():
        stale.unlink()
    path.write_text("\n".join(hdr + records) + "\n", encoding="utf-8")
    return path

def save_all(lb: LogBook, tname: str) -> list[Path]:
    return [p for band in lb.bands()
            if (p := write_edi(lb, band, tname, Path("."))) is not None]

# ──────────────────────────────────────────────────────────────
# EDI crash recovery
# ──────────────────────────────────────────────────────────────
_BAND_FROM_FREQ = {"145 MHz": "2M", "435 MHz": "70CM", "1296 MHz": "23CM"}
_MODE_FROM_CODE = {"1": "SSB", "2": "CW", "6": "FM"}

def load_from_edi(paths: list[Path],
                  loc_cache: dict[str, list[str]]) -> tuple[LogBook, str] | None:
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

    my_call = my_loc = tname = ""

    for path in paths:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("PCall=") and not my_call:
                    my_call = line[6:].strip().upper()
                elif line.startswith("PWWLo=") and not my_loc:
                    my_loc = line[6:].strip().upper()
                elif line.startswith("TName=") and not tname:
                    tname = line[6:].strip()
        except Exception:
            pass
        if my_call:
            break

    if not my_call:
        return None

    lb = LogBook(my_call, my_loc, loc_cache)

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            band = ""
            in_qso = False
            for line in text.splitlines():
                if line.startswith("PBand="):
                    band = _BAND_FROM_FREQ.get(line[6:].strip(), "")
                elif line.startswith("[QSORecords"):
                    in_qso = True
                elif in_qso and ";" in line:
                    f = line.split(";")
                    if len(f) < 10:
                        continue
                    try:
                        dt = datetime.strptime(
                            f[0].strip() + f[1].strip(), "%y%m%d%H%M"
                        ).replace(tzinfo=timezone.utc)
                        call    = f[2].strip().upper()
                        mode    = _MODE_FROM_CODE.get(f[3].strip(), "SSB")
                        rst_s   = f[4].strip()
                        nr_s    = int(f[5].strip())
                        rst_r   = f[6].strip()
                        nr_r    = int(f[7].strip())
                        loc     = f[9].strip().upper()
                        dist_km = int(f[10].strip()) if len(f) > 10 and f[10].strip().isdigit() else 0
                        if not dist_km and loc and RE_LOC.match(loc):
                            dist_km = lb.dist(loc)
                        if call and band and RE_LOC.match(loc):
                            lb.add(QSO(dt=dt, band=band, mode=mode, call=call,
                                       rst_s=rst_s, nr_s=nr_s, rst_r=rst_r, nr_r=nr_r,
                                       loc=loc, dist_km=dist_km))
                    except (ValueError, IndexError):
                        pass
        except Exception:
            pass

    lb.qsos.sort(key=lambda q: (q.dt, q.nr_s))
    return lb, tname

# ──────────────────────────────────────────────────────────────
# Input parser
# ──────────────────────────────────────────────────────────────
RE_CALL = re.compile(r'^(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,}(/[A-Z0-9P/]+)?$')

def parse_input(line: str) -> dict | str:
    """Parse 'CALL RST NR LOC'. Returns dict or error string."""
    tokens = line.upper().split()
    if not tokens:
        return ""
    if len(tokens) < 3:
        return "Usage: CALL RST NR LOC   e.g.  HA7NS 59 015 JN97WM"
    call = tokens[0]
    if not RE_CALL.match(call):
        return f"Invalid callsign: {call!r}"
    rst_r = tokens[1]
    try:
        nr_r = int(tokens[2])
        if not (0 < nr_r < 10000):
            raise ValueError
    except ValueError:
        return f"Expected serial number as third token, got {tokens[2]!r}"
    loc = ""
    for tok in tokens[3:]:
        if RE_LOC.match(tok):
            loc = tok[:6]
            break
    if not loc:
        return "Usage: CALL RST NR LOC   e.g.  HA7NS 59 015 JN97WM"
    return dict(call=call, rst_r=rst_r, nr_r=nr_r, loc=loc)

# ──────────────────────────────────────────────────────────────
# Received-NR prediction
# ──────────────────────────────────────────────────────────────
_NR_PREDICT_MAX_AGE = 5 * 60  # seconds

def _predict_nr(lb: LogBook, call: str, band: str, mode: str,
                now: datetime | None = None) -> int | None:
    """Return last_nr_r + 1 if there is a recent cross-mode QSO for call on band.

    The other station's serial counter is per-band; a recent QSO on the same band
    in a different mode gives us a close estimate of their current serial.
    `now` is injectable for testing; defaults to the real wall clock.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    for q in reversed(lb.qsos):
        if q.call == call and q.band == band and q.mode != mode:
            if (now - q.dt).total_seconds() <= _NR_PREDICT_MAX_AGE:
                return q.nr_r + 1
            return None  # found but too old
    return None

# ──────────────────────────────────────────────────────────────
# Callsign autocomplete
# ──────────────────────────────────────────────────────────────
class CallCompleter(Completer):
    def __init__(self, loc_cache: dict[str, list[str]]):
        self._calls = sorted(loc_cache.keys())
        self._locs  = loc_cache  # call → [most_recent, ...]

    def get_completions(self, document, complete_event):
        text   = document.text_before_cursor
        tokens = text.split()
        if not tokens:
            return
        trailing = text[-1] == ' '

        # Callsign: first token being typed
        if len(tokens) == 1 and not trailing:
            prefix = tokens[0]
            for call in self._calls:
                if call.startswith(prefix.upper()):
                    yield Completion(call, start_position=-len(prefix))

        # Locator: after "CALL RST NR " (3 complete tokens + cursor past space)
        elif (len(tokens) == 3 and trailing) or len(tokens) == 4:
            call   = tokens[0].upper()
            prefix = tokens[3] if len(tokens) == 4 else ""
            for loc in self._locs.get(call, []):
                if loc.startswith(prefix.upper()):
                    yield Completion(loc, start_position=-len(prefix))

# ──────────────────────────────────────────────────────────────
# Display helpers
# ──────────────────────────────────────────────────────────────
W = 80
_REDRAW = object()  # sentinel: exit prompt to force a full screen refresh

# CW macros bound to F1–F7.  Placeholders: <MYCALL> <HISCALL> <NUMBER> <LOCATOR>
CW_MACROS = [
    "CQ <MYCALL> <MYCALL> TEST",                               # F1
    "<MYCALL>",                                                # F2
    "5NN <NUMBER> <LOCATOR>",  # F3
    "TU",                                               # F4
    "<HISCALL>",                                               # F5
    "DE <MYCALL>",                                             # F6
    "?",                                                       # F7
    "282 282 SSB",                                             # F8
]

def _expand_cw(template: str, lb: LogBook, hiscall: str, band: str) -> str:
    nr = lb.next_nr(band)
    nr_cw = f"{nr:03d}".replace("0", "T").replace("9", "N")
    return (template
            .replace("<MYCALL>",  lb.my_call)
            .replace("<HISCALL>", hiscall or "?")
            .replace("<NUMBER>",  nr_cw)
            .replace("<LOCATOR>", lb.my_loc))

def _cw_send(message: str) -> None:
    def _do():
        try:
            with socket.create_connection((RIGCTLD_HOST, RIGCTLD_PORT), timeout=2.0) as s:
                s.sendall(f"b{message}\n".encode())
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()

def _cw_stop() -> None:
    def _do():
        try:
            with socket.create_connection((RIGCTLD_HOST, RIGCTLD_PORT), timeout=2.0) as s:
                s.sendall(b"\xbb")
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()

def _band_summary(lb: LogBook) -> str:
    parts = []
    for b in ("2M", "70CM", "23CM"):
        qsos = [q for q in lb.qsos if q.band == b]
        if not qsos:
            continue
        valid = [q for q in qsos if not _is_dup_in_log(qsos, q)]
        pts = sum(q.dist_km for q in valid)
        parts.append(f"{b}:{len(qsos)}q/{pts}pt")
    return "  ".join(parts) or "no QSOs yet"

_CW_LEGEND = "  F1:CQ  F2:MY  F3:EXCH  F4:TU  F5:HIS  F6:DE  F7:?  F8:QSY  ESC:STOP"

def _print_header(lb: LogBook):
    bar = "━" * W
    print(f"\n\033[1m{bar}\033[0m")
    print(f" PUSKÁS LOGGER  │  {_band_summary(lb)}")
    print(f"\033[2m{_CW_LEGEND}\033[0m")
    print(f"\033[1m{bar}\033[0m")

def _print_recent(lb: LogBook, n: int = 8, focus: int | None = None):
    qsos = lb.qsos
    if focus is not None:
        before = n // 2
        start  = max(0, min(focus - before, len(qsos) - n))
        window = qsos[start:start + n]
    else:
        start  = max(0, len(qsos) - n)
        window = qsos[-n:]
    for abs_idx, q in enumerate(window, start=start):
        dup    = _is_dup_in_log(qsos, q)
        bear   = lb.bearing(q.loc)
        dist   = f"  {lb.dist(q.loc):4d} km  {bear:3d}° {_bearing_arrow(bear)}"
        marker = "  \033[31mDUP\033[0m" if dup else ""
        row    = (f"{q.dt.strftime('%H:%M')}  {q.call:<10}  {q.band:<5} {q.mode:<4}"
                  f"  ↑{q.rst_s:<3} {q.nr_s:03d} ↓{q.rst_r:<3} {q.nr_r:03d}  {q.loc:<6}{dist}{marker}")
        if abs_idx == focus:
            print(f"\033[1m> {row}\033[0m")
        else:
            print(f"  {row}")
    print("─" * W)


# ──────────────────────────────────────────────────────────────
# Command handler
# ──────────────────────────────────────────────────────────────
def _handle_command(line: str, lb: LogBook, tname: str):
    parts = line.split()
    cmd   = parts[0].lower()

    if cmd == "!undo":
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
        print("  CALL RST NR LOC          — log a QSO (locator required)")
        print("  !undo                    — remove last QSO")
        print("  Alt+B                    — cycle band (rig offline)")
        print("  Alt+M                    — cycle mode (rig offline)")
        print("  Alt+R                    — point rotator at selected bearing")
        print("  Alt+T                    — sync radio clock to system UTC")
        print("  Alt+V                    — start/stop webcam recording")
        print("  !help                    — this help")
        print("  Ctrl-D                   — save and exit")

    else:
        print(f"  Unknown command: {cmd}  (try !help)")

    input("  [Enter to continue]")

def _update_loc_cache(loc_cache: dict[str, list[str]], call: str, loc: str) -> None:
    """Insert loc at the front of loc_cache[call], maintaining most-recent-first order."""
    if not loc:
        return
    locs = loc_cache.setdefault(call, [])
    if loc in locs:
        locs.remove(loc)
    locs.insert(0, loc)

# ──────────────────────────────────────────────────────────────
# Offline setup wizard
# ──────────────────────────────────────────────────────────────
def _offline_setup():
    """Ask for band and mode interactively when rig is offline at startup.
    Raises EOFError / KeyboardInterrupt if the user wants to quit.
    """
    band, mode, _, online = current_rig()
    if online or (band and mode):
        return
    bar = "━" * W
    print(f"\n\033[1m{bar}\033[0m")
    print("  RIG OFFLINE — set band and mode to start logging")
    print("\033[2m  (start rigctld for automatic control, or enter values below)\033[0m")
    print(f"\033[1m{bar}\033[0m")
    while True:
        band, mode, _, online = current_rig()
        if online or (band and mode):
            return
        if not band:
            raw = input(f"  Band [{' / '.join(_BANDS)}]: ").strip().upper()
            if raw in _BANDS:
                _rig_manual["band"] = raw
            else:
                print(f"  \033[31m{raw!r} — choose {', '.join(_BANDS)}\033[0m")
        elif not mode:
            raw = input(f"  Mode [{' / '.join(_MODES)}]: ").strip().upper()
            if raw in _MODES:
                _rig_manual["mode"] = raw
            else:
                print(f"  \033[31m{raw!r} — choose {', '.join(_MODES)}\033[0m")


_CET = ZoneInfo("Europe/Budapest")


def _is_contest_time(now: datetime | None = None) -> bool:
    """True during Puskás URH Kupa: first Monday of month, 18:00–20:00 CET/CEST."""
    if now is None:
        now = datetime.now(timezone.utc)
    local = now.astimezone(_CET)
    return local.weekday() == 0 and local.day <= 7 and 18 <= local.hour < 20


# ──────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────
def run(lb: LogBook, tname: str):
    # 0 = last QSO selected for edit, 1 = second-to-last, None = no edit in progress
    _state: dict = {
        'edit_idx': None, 'restore_text': '', 'warn_until': 0.0,
        'prev_band': None, 'prev_mode': None, 'webcam_notice': ('', 0.0),
    }
    _webcam_path_prefix = f"{datetime.now(timezone.utc).strftime('%y%m%d')}-{lb.my_call}"

    def _toolbar() -> FormattedText:
        band, mode, qrg, online = current_rig()
        now = datetime.now(timezone.utc)
        t   = now.strftime("%H:%M:%S")

        # Trigger a full REDRAW when band or mode changes so the TX line stays accurate.
        # Suppressed during edit mode: a rig change must not clear the operator's input.
        # _toolbar() runs on the event-loop thread, making get_app().exit() safe here.
        if _state['prev_band'] is not None:
            if band != _state['prev_band'] or mode != _state['prev_mode']:
                _state['prev_band'] = band
                _state['prev_mode'] = mode
                if _state['edit_idx'] is None:
                    try:
                        get_app().exit(result=_REDRAW)
                    except Exception:
                        pass
        else:
            _state['prev_band'] = band
            _state['prev_mode'] = mode

        parts: list[tuple[str, str]] = []

        # During edit, warn when the rig is on a different band/mode than the QSO.
        if _state['edit_idx'] is not None and band:
            real_idx = len(lb.qsos) - 1 - _state['edit_idx']
            if 0 <= real_idx < len(lb.qsos):
                q = lb.qsos[real_idx]
                if band != q.band or mode != q.mode:
                    parts.append(("bg:ansiyellow fg:black",
                                  f"  RIG→{band} {mode}  │  "))

        if time.monotonic() < _state['warn_until']:
            parts.append(("bg:ansiyellow fg:black", "  rig online — Alt+B/M ignored  │  "))
        elif online:
            parts.append(("", f"  {qrg} MHz  │  "))
        else:
            parts.append(("", "  offline  │  "))

        rot_az, rot_online = current_rot()
        rot_str = f"{rot_az:.0f}°" if rot_online else "---"
        parts.append(("", f"  ROT: {rot_str}  │  "))

        if _webcam_proc is not None:
            parts.append(("bg:ansired fg:white", "  ● REC  │  "))

        with _clock_sync_lock:
            sync_msg   = _clock_sync_notice["msg"]
            sync_until = _clock_sync_notice["until"]
        if time.monotonic() < sync_until:
            parts.append(("bg:ansigreen fg:black", f"  {sync_msg}  │  "))

        webcam_msg, webcam_until = _state['webcam_notice']
        if time.monotonic() < webcam_until:
            parts.append(("bg:ansigreen fg:black", f"  {webcam_msg}  │  "))

        time_style = "bg:ansigreen fg:black" if _is_contest_time(now) else "bg:ansired fg:white"
        parts.append((time_style, f" {t}Z "))
        return FormattedText(parts)

    def _qso_to_input(q: QSO) -> str:
        parts = [q.call, q.rst_r, f"{q.nr_r:03d}"]
        if q.loc:
            parts.append(q.loc)
        return ' '.join(parts)

    def _cache_loc(call: str, loc: str) -> None:
        _update_loc_cache(lb.loc_cache, call, loc)

    def _enter_edit(idx: int) -> None:
        """Set edit_idx and queue a REDRAW with the QSO's data in the buffer."""
        real_idx = len(lb.qsos) - 1 - idx
        if real_idx < 0 or real_idx >= len(lb.qsos):
            return
        _state['edit_idx']    = idx
        _state['restore_text'] = _qso_to_input(lb.qsos[real_idx])
        get_app().exit(result=_REDRAW)

    def _rprompt() -> HTML | str:
        if _state['edit_idx'] is not None:
            idx      = _state['edit_idx']
            real_idx = len(lb.qsos) - 1 - idx
            if 0 <= real_idx < len(lb.qsos):
                nr_s = lb.qsos[real_idx].nr_s
                return HTML(f"<ansiblue><b>  EDIT #{nr_s:03d}  </b></ansiblue>")
        try:
            text = get_app().current_buffer.text
        except Exception:
            return ""
        tokens = text.upper().split()
        if not tokens:
            return ""
        first = tokens[0]
        if RE_LOC.match(first) and len(tokens) == 1:
            dist = lb.dist(first)
            bear = lb.bearing(first)
            if dist:
                return HTML(f"<ansigreen>  {dist} km  {bear}° {_bearing_arrow(bear)}  </ansigreen>")
            return ""
        if not RE_CALL.match(first):
            return ""
        call = first
        band, mode, *_ = current_rig()
        locs = lb.loc_cache.get(call, [])
        geo = ""
        if locs:
            dist = lb.dist(locs[0])
            bear = lb.bearing(locs[0])
            if dist:
                geo = f"  {locs[0]}  {dist} km  {bear}° {_bearing_arrow(bear)}"
        open_str = _format_open_combos(lb.open_combos(call))
        tail = f"  <ansiyellow>{open_str}</ansiyellow>" if open_str else ""
        if band and mode and lb.is_dup(call, band, mode):
            return HTML(f"<ansired><b>  DUP  </b></ansired><ansigreen>{geo}  </ansigreen>{tail}")
        if geo or tail:
            return HTML(f"<ansigreen>{geo}  </ansigreen>{tail}")
        return ""

    def _get_input_style() -> Style:
        if _state['edit_idx'] is not None:
            return Style.from_dict({})
        try:
            text = get_app().current_buffer.text.upper().split()
            if text and RE_CALL.match(text[0]):
                band, mode, *_ = current_rig()
                if band and mode and lb.is_dup(text[0], band, mode):
                    return Style.from_dict({'': 'bg:ansired fg:white'})
        except Exception:
            pass
        return Style.from_dict({})

    kb = KeyBindings()

    @kb.add(' ')
    def _on_space(event):
        buf = event.app.current_buffer
        if buf.cursor_position != len(buf.text):
            buf.insert_text(' ')
            return
        buf.insert_text(' ')
        tokens = buf.text.strip().split()
        if len(tokens) == 1:
            call = tokens[0].upper()
            if not RE_CALL.match(call):
                return
            band, mode, *_ = current_rig()
            rst = "599" if mode == "CW" else "59"
            predicted = _predict_nr(lb, call, band, mode)
            if predicted is not None:
                buf.insert_text(f"{rst} {predicted:03d}")
            else:
                buf.insert_text(rst + ' ')
        elif len(tokens) == 3:
            locs = lb.loc_cache.get(tokens[0].upper(), [])
            if len(locs) == 1:
                buf.insert_text(locs[0])      # only one known — insert directly
            elif locs:
                buf.start_completion(select_first=True)  # multiple — show choice

    @kb.add('backspace')
    def _on_backspace(event):
        buf = event.app.current_buffer
        if buf.text:
            buf.delete_before_cursor()

    @kb.add('up')
    def _on_up(event):
        buf = event.app.current_buffer
        if buf.complete_state:
            buf.complete_previous()
            return
        if _state['edit_idx'] is None and buf.text:
            buf.history_backward()
            return
        n = len(lb.qsos)
        if n == 0:
            return
        new_idx = 0 if _state['edit_idx'] is None else min(_state['edit_idx'] + 1, n - 1)
        _enter_edit(new_idx)

    @kb.add('down')
    def _on_down(event):
        buf = event.app.current_buffer
        if buf.complete_state:
            buf.complete_next()
            return
        if _state['edit_idx'] is None:
            if buf.text:
                buf.history_forward()
            return
        if _state['edit_idx'] > 0:
            _enter_edit(_state['edit_idx'] - 1)
        else:
            _state['edit_idx'] = None
            _state['restore_text'] = ''
            buf.set_document(Document(''))
            get_app().exit(result=_REDRAW)

    @kb.add('escape')
    def _on_escape(event):
        buf = event.app.current_buffer
        _cw_stop()
        if buf.complete_state:
            buf.cancel_completion()
            return
        if _state['edit_idx'] is not None:
            _state['edit_idx'] = None
            _state['restore_text'] = ''
            buf.set_document(Document(''))
            get_app().exit(result=_REDRAW)
        else:
            buf.set_document(Document(''))

    for _fn_idx, _macro in enumerate(CW_MACROS, 1):
        @kb.add(f'f{_fn_idx}')
        def _fn_key(event, _tmpl=_macro):
            buf = event.app.current_buffer
            tokens = buf.text.strip().split()
            hiscall = tokens[0].upper() if tokens else ''
            band, *_ = current_rig()
            _cw_send(_expand_cw(_tmpl, lb, hiscall, band))

    @kb.add('escape', 'b')
    def _on_alt_b(event):
        if _rig["online"]:
            _state['warn_until'] = time.monotonic() + 2.0
        else:
            cur = _rig_manual.get("band", "")
            _rig_manual["band"] = _BANDS[(_BANDS.index(cur) + 1) % len(_BANDS)] if cur in _BANDS else _BANDS[0]
        event.app.invalidate()

    @kb.add('escape', 'm')
    def _on_alt_m(event):
        if _rig["online"]:
            _state['warn_until'] = time.monotonic() + 2.0
        else:
            cur = _rig_manual.get("mode", "")
            _rig_manual["mode"] = _MODES[(_MODES.index(cur) + 1) % len(_MODES)] if cur in _MODES else _MODES[0]
        event.app.invalidate()

    @kb.add('escape', 'r')
    def _on_alt_r(event):
        _, rot_online = current_rot()
        if not rot_online:
            return
        loc = None
        if _state['edit_idx'] is not None:
            real_idx = len(lb.qsos) - 1 - _state['edit_idx']
            if 0 <= real_idx < len(lb.qsos):
                loc = lb.qsos[real_idx].loc
        else:
            try:
                tokens = event.app.current_buffer.text.upper().split()
                if tokens:
                    first = tokens[0]
                    if RE_LOC.match(first):
                        loc = first
                    elif RE_CALL.match(first):
                        locs = lb.loc_cache.get(first, [])
                        if locs:
                            loc = locs[0]
            except Exception:
                pass
        if loc:
            _rot_set(lb.bearing(loc))

    @kb.add('escape', 't')
    def _on_alt_t(_event):
        _clock_sync()

    @kb.add('escape', 'v')
    def _on_alt_v(_event):
        msg = _webcam_toggle(_webcam_path_prefix)
        if msg:
            _state['webcam_notice'] = (msg, time.monotonic() + 5.0)

    @kb.add('enter', filter=has_completions)
    def _on_enter_completion(event):
        buf = event.app.current_buffer
        state = buf.complete_state
        if state and state.current_completion:
            buf.apply_completion(state.current_completion)
        else:
            buf.cancel_completion()

    session = PromptSession(
        completer=CallCompleter(lb.loc_cache),
        key_bindings=kb,
        complete_while_typing=False,
        enable_history_search=False,
    )
    session.default_buffer.on_text_changed += _on_buffer_changed

    try:
        _offline_setup()
    except (EOFError, KeyboardInterrupt):
        return

    while True:
        band, mode, qrg, online = current_rig()
        os.write(1, b"\033[2J\033[H")
        _print_header(lb)
        focus = (len(lb.qsos) - 1 - _state['edit_idx']
                 if _state['edit_idx'] is not None else None)
        try:
            rows = os.get_terminal_size().lines
        except OSError:
            rows = 24
        _print_recent(lb, n=max(3, rows - 9), focus=focus)

        band, mode, _, _ = current_rig()
        nr  = lb.next_nr(band)
        rst = "599" if mode == "CW" else "59"
        print(f"\033[1;92m  TX ► {lb.my_call}  {rst}  {nr:03d}  {lb.my_loc}\033[0m")

        default = _state.pop('restore_text', '') or ''
        try:
            def _prompt_msg() -> str:
                if _state['edit_idx'] is not None:
                    real_idx = len(lb.qsos) - 1 - _state['edit_idx']
                    if 0 <= real_idx < len(lb.qsos):
                        q = lb.qsos[real_idx]
                        return f"{q.band} {q.mode}  RX ► "
                b, m, *_ = current_rig()
                return f"{b or '?'} {m or '?'}  RX ► "

            result = session.prompt(_prompt_msg, bottom_toolbar=_toolbar,
                                    rprompt=_rprompt,
                                    style=DynamicStyle(_get_input_style),
                                    refresh_interval=0.1,
                                    default=default,
                                    pre_run=lambda: setattr(get_app(), 'ttimeoutlen', 0.05))
        except KeyboardInterrupt:
            _state['edit_idx'] = None
            continue
        except EOFError:
            break
        if result is _REDRAW:
            continue
        line = result.strip()

        if not line:
            _state['edit_idx'] = None
            continue

        if line.startswith("!"):
            _state['edit_idx'] = None
            _handle_command(line, lb, tname)
            continue

        parsed = parse_input(line)
        if isinstance(parsed, str):
            _state['edit_idx'] = None
            if parsed:
                print(f"\033[31m  {parsed}\033[0m")
                input("  [Enter to continue]")
            continue

        edit_idx = _state['edit_idx']
        _state['edit_idx'] = None

        if edit_idx is not None:
            # Replace an existing QSO; preserve dt, band, mode, nr_s, rst_s
            real_idx = len(lb.qsos) - 1 - edit_idx
            if 0 <= real_idx < len(lb.qsos):
                old = lb.qsos[real_idx]
                loc = parsed["loc"]
                lb.qsos[real_idx] = QSO(
                    dt=old.dt, band=old.band, mode=old.mode,
                    call=parsed["call"], rst_s=old.rst_s, nr_s=old.nr_s,
                    rst_r=parsed["rst_r"], nr_r=parsed["nr_r"],
                    loc=loc, dist_km=lb.dist(loc),
                )
                lb.worked = {(q.call, q.band, q.mode) for q in lb.qsos}
                _cache_loc(parsed["call"], loc)
                save_all(lb, tname)
            continue

        # New QSO — re-read rig at the moment Enter is pressed
        band, mode, qrg, online = current_rig()

        if not band:
            print("\033[31m  Cannot log: band unknown. Set with !band 2M\033[0m")
            input("  [Enter to continue]")
            continue

        call    = parsed["call"]
        nr_r    = parsed["nr_r"]
        loc     = parsed["loc"]
        rst_def = "599" if mode == "CW" else "59"
        rst_s   = rst_def
        rst_r   = parsed["rst_r"]
        nr_s    = lb.next_nr(band)
        dist_km = lb.dist(loc)

        now = datetime.now(timezone.utc)
        qso = QSO(
            dt=now.replace(second=0, microsecond=0),
            band=band, mode=mode or "SSB", call=call,
            rst_s=rst_s, nr_s=nr_s, rst_r=rst_r, nr_r=nr_r,
            loc=loc, dist_km=dist_km,
        )

        dup = lb.add(qso)
        if dup:
            print(f"\033[31m  *** DUP *** {call} already in log for {band} {mode}\033[0m")
            input("  [Enter to continue]")

        _log_input_event({
            "t":     now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "event": "qso",
            "call":  call,
            "band":  band,
            "mode":  qso.mode,
            "nr_s":  nr_s,
            "dup":   dup,
        })

        _cache_loc(call, loc)
        save_all(lb, tname)

    _webcam_stop_if_running()
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

def _edi_qso_count(path: Path) -> int:
    try:
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("[QSORecords;"):
                return int(line.split(";")[1].rstrip("]"))
    except Exception:
        pass
    return 0

def main():
    print("Puskás URH Kupa Logger")
    print("─" * 40)

    lb: LogBook | None = None
    tname: str = ""

    edi_files = sorted(Path(".").glob("*.[Ee][Dd][Ii]"))
    if edi_files:
        summary = ", ".join(
            f"{p.name} ({_edi_qso_count(p)} QSOs)" for p in edi_files
        )
        print(f"Found existing logs: {summary}")
        ans = input("Resume? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            print("Building locator cache...")
            loc_cache = load_loc_cache()
            result = load_from_edi(edi_files, loc_cache)
            if result:
                lb, tname = result
                for q in lb.qsos:
                    _update_loc_cache(lb.loc_cache, q.call, q.loc)
                print(f"Callsign: {lb.my_call}")
                print(f"Locator:  {lb.my_loc}")
                print(f"Contest:  {tname}")
                print(f"Loaded {len(lb.qsos)} QSOs")

    if lb is None:
        my_call = _load_callsign()
        print(f"Callsign: {my_call}")
        my_loc = input("Your locator [JN97TF]: ").strip().upper() or "JN97TF"
        if not RE_LOC.match(my_loc):
            print(f"Warning: {my_loc!r} doesn't look like a valid Maidenhead locator")
        now = datetime.now(timezone.utc)
        default_tname = tname_for(now)
        tname = input(f"Contest name [{default_tname}]: ").strip() or default_tname
        print("Building locator cache...")
        loc_cache = load_loc_cache()
        lb = LogBook(my_call, my_loc, loc_cache)

    t = threading.Thread(target=_rig_thread, daemon=True)
    t.start()
    threading.Thread(target=_rot_thread, daemon=True).start()
    _telem_path = Path(f"{datetime.now(timezone.utc).strftime('%y%m%d')}-{lb.my_call}-telemetry.jsonl")
    threading.Thread(target=_telemetry_thread, args=(_telem_path,), daemon=True).start()
    print(f"Telemetry: {_telem_path}")
    _input_log_path = Path(f"{datetime.now(timezone.utc).strftime('%y%m%d')}-{lb.my_call}-input.jsonl")
    _input_log_open(_input_log_path)
    print(f"Input log: {_input_log_path}")
    print("Webcam:    Alt+V to start/stop recording")

    print()
    print("Input: CALL RST NR [LOC]   e.g.  HA7NS 59 015   or  HA7NS 59 015 JN97WM")
    print("Tab-complete callsigns  │  Space after callsign fills RST  │  Space after NR fills locator")
    print("!help for commands  │  Ctrl-D to save and exit")
    print()
    input("[Enter to start]")

    try:
        run(lb, tname)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        _webcam_stop_if_running()
        save_all(lb, tname)
        raise

if __name__ == "__main__":
    main()
