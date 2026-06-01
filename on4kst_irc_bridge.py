#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
ON4KST IRC Bridge
=================
Connects to the ON4KST 144/432 MHz chat server and presents it as a
minimal IRC server on localhost, so any IRC client (irssi, weechat, …)
can be used to participate in the chat.

IRC server:  127.0.0.1:6667  (override with IRC_HOST / IRC_PORT env vars)
Channel:     #on4kst

  • Public chat  ↔  PRIVMSG #on4kst
  • Private msg  ↔  /CQ CALLSIGN  (mapped to IRC PRIVMSG <callsign>)
  • /SET HERE when first IRC client connects; /UNSET HERE when last disconnects
  • ON4KST connection is kept alive and reconnects automatically

Prerequisites:
  ~/.netrc must contain:
    machine www.on4kst.info login <callsign> password <password>

Usage:
    uv run on4kst_irc_bridge.py

irssi quick-start:
    /server add -auto -network on4kst localhost 6667
    /save
    /connect on4kst
"""
from __future__ import annotations

import asyncio
import html
import math
import netrc
import re
import socket
import sys
import time
import urllib.request
import json
from pathlib import Path

# ============================================================
# Configuration
# ============================================================
KST_HOST     = "www.on4kst.info"
KST_PORT     = 23000
CHAT_CHOICE  = "2"           # 144/432 MHz
IRC_HOST     = "127.0.0.1"
IRC_PORT     = 6667
SERVER_NAME  = "on4kst.bridge"
CHANNEL      = "#on4kst"
REFRESH_SEC  = 120
RECONNECT_S  = 30
RIGCTLD_HOST   = "localhost"
RIGCTLD_PORT   = 4532
RIGCTLD_POLL_S = 5

# ============================================================
# Credentials
# ============================================================

def load_credentials() -> tuple[str, str]:
    try:
        n     = netrc.netrc()
        entry = n.authenticators(KST_HOST)
        if entry:
            login, _, password = entry
            return login.upper(), password
    except FileNotFoundError:
        pass
    except netrc.NetrcParseError as e:
        print(f"[error] .netrc parse error: {e}")
        sys.exit(1)
    print("[error] No credentials found in ~/.netrc.")
    print(f"  Add: machine {KST_HOST} login <callsign> password SECRET")
    sys.exit(1)

# ============================================================
# Locator math (Maidenhead → lat/lon, haversine, bearing)
# ============================================================

def maidenhead_to_latlon(loc: str) -> tuple[float, float]:
    loc = loc.upper().strip()
    lon = (ord(loc[0]) - ord('A')) * 20 - 180 + int(loc[2]) * 2
    lat = (ord(loc[1]) - ord('A')) * 10 - 90  + int(loc[3]) * 1
    if len(loc) >= 6:
        lon += (ord(loc[4]) - ord('A')) * (2 / 24) + (1 / 24)
        lat += (ord(loc[5]) - ord('A')) * (1 / 24) + (1 / 48)
    else:
        lon += 1.0
        lat += 0.5
    return lat, lon

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p)
         * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))

def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    la1, la2 = lat1 * p, lat2 * p
    dlon = (lon2 - lon1) * p
    x = math.sin(dlon) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def _loc_distance_str(my_loc: str, their_loc: str) -> str:
    """Returns ' | dist km bear°' or '' if either locator is missing/invalid."""
    if not my_loc or not their_loc:
        return ""
    try:
        lat1, lon1 = maidenhead_to_latlon(my_loc)
        lat2, lon2 = maidenhead_to_latlon(their_loc)
        dist = int(haversine_km(lat1, lon1, lat2, lon2))
        bear = int(initial_bearing(lat1, lon1, lat2, lon2))
        return f" | {dist} km {bear}°"
    except Exception:
        return ""

# ============================================================
# Airplane scatter
# ============================================================

SCATTER_MIN_KM  = 200
SCATTER_MAX_KM  = 1500
SCATTER_RADIUS_KM = 50   # aircraft search radius around path midpoint
OPENSKY_URL     = "https://opensky-network.org/api/states/all"

def latlon_to_maidenhead(lat: float, lon: float) -> str:
    lon += 180
    lat += 90
    loc  = chr(ord('A') + int(lon / 20))
    loc += chr(ord('A') + int(lat / 10))
    loc += str(int((lon % 20) / 2))
    loc += str(int(lat % 10))
    loc += chr(ord('A') + int((lon % 2) / (2 / 24)))
    loc += chr(ord('A') + int((lat % 1) / (1 / 24)))
    return loc

def great_circle_midpoint(lat1: float, lon1: float,
                           lat2: float, lon2: float) -> tuple[float, float]:
    p  = math.pi / 180
    la1, la2 = lat1 * p, lat2 * p
    lo1, lo2 = lon1 * p, lon2 * p
    Bx = math.cos(la2) * math.cos(lo2 - lo1)
    By = math.cos(la2) * math.sin(lo2 - lo1)
    lat_m = math.atan2(math.sin(la1) + math.sin(la2),
                       math.sqrt((math.cos(la1) + Bx) ** 2 + By ** 2))
    lon_m = lo1 + math.atan2(By, math.cos(la1) + Bx)
    return lat_m / p, lon_m / p

def fetch_aircraft_near(lat: float, lon: float,
                         radius_km: float) -> list[dict]:
    """Query OpenSky Network for aircraft within radius_km of (lat, lon).
    Returns a list of dicts with keys: callsign, lat, lon, altitude_m, distance_km.
    Returns [] on any error (network, rate-limit, etc.).
    """
    deg = radius_km / 111.0
    params = (f"?lamin={lat - deg:.4f}&lomin={lon - deg:.4f}"
              f"&lamax={lat + deg:.4f}&lomax={lon + deg:.4f}")
    try:
        with urllib.request.urlopen(OPENSKY_URL + params, timeout=5) as r:
            data = json.loads(r.read())
    except Exception:
        return []

    aircraft = []
    for s in (data.get("states") or []):
        # state vector: [icao, callsign, origin, ?, ?, lon, lat, baro_alt, ...]
        if s[5] is None or s[6] is None or s[7] is None:
            continue
        ac_lat, ac_lon, alt = s[6], s[5], s[7]
        dist = haversine_km(lat, lon, ac_lat, ac_lon)
        if dist <= radius_km:
            aircraft.append({
                "callsign":    (s[1] or "").strip() or s[0],
                "lat":         ac_lat,
                "lon":         ac_lon,
                "altitude_m":  alt,
                "distance_km": dist,
            })
    aircraft.sort(key=lambda a: a["distance_km"])
    return aircraft

async def scatter_candidates(my_loc: str,
                              online_users: dict[str, dict]) -> list[dict]:
    """Return scatter-feasible online stations with nearest aircraft info."""
    if not my_loc:
        return []
    my_lat, my_lon = maidenhead_to_latlon(my_loc)
    candidates = []
    for call, user in online_users.items():
        loc = user.get("loc", "")
        if not loc:
            continue
        try:
            th_lat, th_lon = maidenhead_to_latlon(loc)
        except Exception:
            continue
        dist = haversine_km(my_lat, my_lon, th_lat, th_lon)
        if not (SCATTER_MIN_KM <= dist <= SCATTER_MAX_KM):
            continue
        mid_lat, mid_lon = great_circle_midpoint(my_lat, my_lon, th_lat, th_lon)
        bear_to_mid = initial_bearing(my_lat, my_lon, mid_lat, mid_lon)
        mid_loc = latlon_to_maidenhead(mid_lat, mid_lon)
        # aircraft query runs in a thread to avoid blocking the event loop
        aircraft = await asyncio.get_event_loop().run_in_executor(
            None, fetch_aircraft_near, mid_lat, mid_lon, SCATTER_RADIUS_KM
        )
        candidates.append({
            "call":       call,
            "loc":        loc,
            "dist_km":    dist,
            "bear_to_mid": bear_to_mid,
            "mid_loc":    mid_loc,
            "aircraft":   aircraft,
        })
    candidates.sort(key=lambda c: c["dist_km"])
    return candidates

# ============================================================
# Rig control (rigctld TCP client)
# ============================================================

async def fetch_rig_info() -> tuple[str, str]:
    """Returns (freq_mhz_str, mode) from rigctld, or ('', '') if unavailable."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(RIGCTLD_HOST, RIGCTLD_PORT), timeout=2.0
        )
        writer.write(b"f\nm\n")
        await writer.drain()
        freq_line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().strip()
        mode_line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode().strip()
        writer.close()
        return f"{float(freq_line) / 1e6:.3f}", mode_line
    except Exception:
        return "", ""

def sked_text(call: str, my_call: str, my_loc: str,
              their_loc: str,
              qrg: str = "", mode: str = "") -> str:
    msg = f"Hi {call}, sked? Puskás URH Kupa"
    if my_loc and their_loc:
        try:
            lat1, lon1 = maidenhead_to_latlon(my_loc)
            lat2, lon2 = maidenhead_to_latlon(their_loc)
            dist = int(haversine_km(lat1, lon1, lat2, lon2))
            bear = int(initial_bearing(lat1, lon1, lat2, lon2))
            msg += f" – {dist} km, {bear}°"
        except Exception:
            pass
    if qrg:
        msg += f" – {qrg} MHz"
        if mode:
            msg += f" {mode}"
    if my_loc:
        msg += f" ({my_loc})"
    msg += f". 73 {my_call}"
    return msg

# ============================================================
# Telnet IAC filter
# ============================================================

def strip_iac(data: bytes) -> bytes:
    out = bytearray()
    i   = 0
    while i < len(data):
        if data[i] == 0xFF and i + 2 < len(data):
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)

# ============================================================
# Regexes
# ============================================================
RE_LOGIN     = re.compile(r"Login\s*:",    re.I)
RE_PASSWORD  = re.compile(r"Password\s*:", re.I)
RE_CHOICE    = re.compile(r"Your choice",  re.I)
RE_CHAT      = re.compile(r"\d{4}Z\s+\S+\s+.+chat\s*>", re.I)
RE_CHAT_MSG  = re.compile(r"^(\d{4}Z)\s+([A-Z0-9/]+)\s+.*?>\s+(.+)", re.I)
RE_RECIPIENT = re.compile(r"^\(([A-Z0-9/]+)\)\s+(.*)", re.I)
RE_USR       = re.compile(
    r"^(\(?)([A-Z0-9]{3,}(?:/[A-Z0-9]+)?)\)?\s{2,}([A-Z]{2}\d{2}[A-Z]{2})\s*(.*)",
    re.I,
)
# groups: 1=open-paren (away marker), 2=callsign, 3=locator, 4=name+equipment
RE_PROMPT    = re.compile(r"(Login|Password|choice|chat)\s*[>:]\s*$", re.I)
RE_LOCATOR   = re.compile(r"\b([A-R]{2}\d{2}[A-X]{2})\b", re.I)

# ============================================================
# Bridge  (coordinates ON4KST client ↔ IRC sessions)
# ============================================================

class Bridge:
    def __init__(self, callsign: str):
        self.callsign    = callsign
        self.my_locator  = ""
        self.rig_qrg     = ""
        self.rig_mode    = ""
        self.kst: ON4KSTClient | None = None
        self._sessions: set[IRCSession] = set()

    # ----------------------------------------------------------
    # IRC session lifecycle
    # ----------------------------------------------------------

    async def irc_connected(self, session: IRCSession):
        self._sessions.add(session)
        if self.kst:
            await self.kst.send("/SET HERE")

    async def irc_disconnected(self, session: IRCSession):
        self._sessions.discard(session)
        if not self._sessions and self.kst:
            await self.kst.send("/UNSET HERE")

    # ----------------------------------------------------------
    # Messages from IRC client → ON4KST
    # ----------------------------------------------------------

    async def irc_message(self, target: str, text: str):
        if not self.kst:
            return
        if target.lower() == CHANNEL.lower():
            if text.startswith("!"):
                await self._handle_local_command(text.split()[0].lower())
            else:
                await self.kst.send(text)
        else:
            if text.strip().lower() == "sked":
                call = target.upper()
                user = self.kst.online_users.get(call) or {}
                msg  = sked_text(call, self.callsign, self.my_locator,
                                 user.get("loc", ""),
                                 self.rig_qrg, self.rig_mode)
                await self.kst.send(f"/CQ {call} {msg}")
                await self._notify(f"→ /CQ {call}: {msg}")
            else:
                await self.kst.send(f"/CQ {target.upper()} {text}")

    # ----------------------------------------------------------
    # Messages from ON4KST → IRC clients
    # ----------------------------------------------------------

    async def _notify(self, text: str):
        for s in list(self._sessions):
            try:
                await s._send(f":{SERVER_NAME} NOTICE {CHANNEL} :{text}")
            except Exception:
                pass

    async def _notify_status(self, text: str):
        for s in list(self._sessions):
            try:
                await s._send(f":{SERVER_NAME} NOTICE {self.callsign} :{text}")
            except Exception:
                pass

    async def _handle_local_command(self, cmd: str):
        if cmd == "!scatter":
            asyncio.create_task(self._run_scatter())
        elif cmd == "!list":
            await self._run_list()
        elif cmd == "!help":
            await self._run_help()
        else:
            await self._notify(f"Unknown command: {cmd}  –  try !help")

    async def _run_help(self):
        await self._notify("Local commands (not sent to the channel):")
        await self._notify("  !list     – online stations sorted by distance and bearing")
        await self._notify("  !scatter  – airplane scatter paths with live aircraft data")
        await self._notify("  !help     – this help")
        await self._notify("  /msg CALL sked  – send contest sked proposal via /CQ")
        if self.rig_qrg:
            await self._notify(f"  Rig: {self.rig_qrg} MHz {self.rig_mode} (included in sked automatically)")
        else:
            await self._notify("  Rig: rigctld not connected (start rigctld to include QRG in sked)")

    async def _run_list(self):
        if not self.kst:
            return
        if not self.my_locator:
            await self._notify("Own locator not yet known, try again in a moment.")
            return
        my_lat, my_lon = maidenhead_to_latlon(self.my_locator)
        rows = []
        for call, user in self.kst.online_users.items():
            if call == self.callsign:
                continue
            loc = user.get("loc", "")
            if not loc:
                continue
            try:
                th_lat, th_lon = maidenhead_to_latlon(loc)
                dist = haversine_km(my_lat, my_lon, th_lat, th_lon)
                bear = initial_bearing(my_lat, my_lon, th_lat, th_lon)
                rows.append((call, loc, dist, bear, user.get("away", False)))
            except Exception:
                continue
        rows.sort(key=lambda r: r[2])
        if not rows:
            await self._notify("No other stations online.")
            return
        await self._notify(f"Online stations ({len(rows)}), sorted by distance:")
        for call, loc, dist, bear, away in rows:
            away_str = " (away)" if away else ""
            await self._notify(
                f"  {call:<10} {loc:<8} {int(dist):>5} km  {int(bear):>3}°{away_str}"
            )

    async def _run_scatter(self):
        if not self.kst:
            return
        await self._notify("Querying aircraft positions, please wait…")
        candidates = await scatter_candidates(self.my_locator, self.kst.online_users)
        if not candidates:
            await self._notify(
                f"No scatter candidates online ({SCATTER_MIN_KM}–{SCATTER_MAX_KM} km)."
            )
            return
        await self._notify(
            f"Scatter candidates ({SCATTER_MIN_KM}–{SCATTER_MAX_KM} km), "
            f"aircraft within {SCATTER_RADIUS_KM} km of midpoint:"
        )
        for c in candidates:
            ac = c["aircraft"]
            if ac:
                a = ac[0]
                ac_str = (f"✈ {a['callsign']} "
                          f"{int(a['altitude_m'])} m, "
                          f"{int(a['distance_km'])} km off midpoint")
            else:
                ac_str = "no aircraft near midpoint"
            await self._notify(
                f"  {c['call']:<10} {c['loc']:<8} "
                f"{int(c['dist_km']):>5} km  "
                f"aim {int(c['bear_to_mid']):>3}° → {c['mid_loc']}  "
                f"{ac_str}"
            )

    async def kst_message(self, utc: str, from_call: str,
                          recipient: str | None, text: str):
        if from_call == self.callsign:
            return  # suppress echo of our own messages

        if recipient and recipient == self.callsign:
            target = self.callsign   # PM addressed to me → query window
        elif recipient:
            target = CHANNEL         # addressed to someone else → channel
            text   = f"({recipient}) {text}"
        else:
            target = CHANNEL

        for s in list(self._sessions):
            try:
                await s.send_privmsg(from_call, target, text)
            except Exception:
                pass

    async def kst_userlist(self, old: dict[str, dict], new: dict[str, dict]):
        joined = set(new) - set(old)
        parted = set(old) - set(new)
        for s in list(self._sessions):
            try:
                for call in joined:
                    if call != self.callsign:
                        await s.send_join(call)
                for call in parted:
                    if call != self.callsign:
                        await s.send_part(call)
            except Exception:
                pass


# ============================================================
# IRC session  (one connected IRC client)
# ============================================================

class IRCSession:
    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, bridge: Bridge):
        self._reader = reader
        self._writer = writer
        self._bridge = bridge
        self.nick          = ""
        self._got_user     = False
        self._reg          = False   # registration complete
        self._cap_pending  = False   # True between CAP LS and CAP END

    # ----------------------------------------------------------
    # Low-level send helpers
    # ----------------------------------------------------------

    async def _send(self, line: str):
        self._writer.write((line + "\r\n").encode("utf-8", errors="replace"))
        await self._writer.drain()

    async def _num(self, code: int, *params: str):
        target   = self.nick or "*"
        parts    = list(params)
        parts[-1] = f":{parts[-1]}"
        await self._send(f":{SERVER_NAME} {code:03d} {target} {' '.join(parts)}")

    # ----------------------------------------------------------
    # Server-to-client message helpers
    # ----------------------------------------------------------

    async def send_privmsg(self, from_call: str, target: str, text: str):
        await self._send(f":{from_call}!{from_call}@on4kst PRIVMSG {target} :{text}")

    async def send_join(self, callsign: str):
        await self._send(f":{callsign}!{callsign}@on4kst JOIN {CHANNEL}")

    async def send_part(self, callsign: str):
        await self._send(f":{callsign}!{callsign}@on4kst PART {CHANNEL}")

    async def _send_names(self):
        kst     = self._bridge.kst
        users   = list(kst.online_users.keys()) if kst else []
        me      = self._bridge.callsign
        nicks   = [me] + [u for u in users if u != me]
        for i in range(0, max(1, len(nicks)), 20):
            chunk = " ".join(nicks[i:i + 20])
            await self._send(f":{SERVER_NAME} 353 {self.nick} = {CHANNEL} :{chunk}")
        await self._num(366, CHANNEL, "End of /NAMES list.")

    # ----------------------------------------------------------
    # Registration
    # ----------------------------------------------------------

    async def _welcome(self):
        callsign = self._bridge.callsign
        await self._num(1,   f"Welcome to the ON4KST IRC Bridge, {self.nick}")
        await self._num(2,   f"Your host is {SERVER_NAME}")
        await self._num(3,   "This server was created today")
        await self._num(4,   SERVER_NAME, "on4kst-bridge-1.0", "o", "o")
        await self._num(375, f"- {SERVER_NAME} Message of the Day -")
        await self._num(372, f"- ON4KST 144/432 MHz IRC bridge")
        await self._num(372, f"- Connected as: {callsign}")
        await self._num(372, f"- Join {CHANNEL} to enter the chat")
        await self._num(376, "End of MOTD command.")
        if self.nick.upper() != callsign.upper():
            await self._send(f":{self.nick}!{self.nick}@localhost NICK {callsign}")
            self.nick = callsign
        self._reg = True
        await self._bridge.irc_connected(self)
        await self._do_join()  # auto-join #on4kst immediately after welcome

    async def _do_join(self):
        await self._send(f":{self.nick}!{self.nick}@localhost JOIN {CHANNEL}")
        await self._num(332, CHANNEL, "ON4KST 144/432 MHz chat bridge")
        await self._num(333, CHANNEL, SERVER_NAME, "0")
        await self._num(324, CHANNEL, "+")
        await self._send_names()

    # ----------------------------------------------------------
    # Inbound IRC command handling
    # ----------------------------------------------------------

    async def _handle_line(self, line: str):
        if not line:
            return
        # Strip leading colon (rare in client→server, but be safe)
        if line.startswith(":"):
            line = line.split(" ", 1)[-1]
        parts = line.split(" ", 2)
        cmd   = parts[0].upper()

        if cmd == "CAP":
            subcmd = parts[1].upper() if len(parts) > 1 else ""
            if subcmd == "LS":
                self._cap_pending = True
                await self._send(f":{SERVER_NAME} CAP * LS :")
            elif subcmd == "REQ":
                caps = parts[2].lstrip(":") if len(parts) > 2 else ""
                await self._send(f":{SERVER_NAME} CAP * NAK :{caps}")
            elif subcmd == "END":
                self._cap_pending = False
                if self.nick and self._got_user and not self._reg:
                    await self._welcome()

        elif cmd == "NICK":
            self.nick = parts[1].strip() if len(parts) > 1 else "?"
            if self._got_user and not self._reg and not self._cap_pending:
                await self._welcome()

        elif cmd == "USER":
            self._got_user = True
            if self.nick and not self._reg and not self._cap_pending:
                await self._welcome()

        elif cmd == "PING":
            token = (parts[1] if len(parts) > 1 else SERVER_NAME).lstrip(":")
            await self._send(f"PONG :{token}")

        elif cmd == "JOIN":
            channel = parts[1].split(",")[0].strip() if len(parts) > 1 else ""
            if channel.lower() == CHANNEL.lower():
                await self._do_join()

        elif cmd == "PRIVMSG":
            if len(parts) >= 3:
                target = parts[1]
                text   = parts[2].lstrip(":")
                await self._bridge.irc_message(target, text)

        elif cmd == "AWAY":
            going_away = len(parts) > 1
            if going_away:
                await self._num(306, "You have been marked as being away")
                if self._bridge.kst:
                    await self._bridge.kst.send("/UNSET HERE")
            else:
                await self._num(305, "You are no longer marked as being away")
                if self._bridge.kst:
                    await self._bridge.kst.send("/SET HERE")

        elif cmd == "WHO":
            target = parts[1].strip() if len(parts) > 1 else CHANNEL
            if target.lower() == CHANNEL.lower() and self._bridge.kst:
                for call, user in self._bridge.kst.online_users.items():
                    flag  = "G" if user.get("away") else "H"
                    gecos = user.get("info") or user["loc"]
                    await self._send(
                        f":{SERVER_NAME} 352 {self.nick} {CHANNEL} {call} on4kst "
                        f"{SERVER_NAME} {call} {flag} :0 {gecos} [{user['loc']}]"
                    )
            await self._num(315, CHANNEL, "End of WHO list.")

        elif cmd == "WHOIS":
            target = (parts[1].strip() if len(parts) > 1 else "").upper()
            kst    = self._bridge.kst
            user   = kst.online_users.get(target) if kst else None
            if user:
                gecos    = user.get("info") or user["loc"]
                loc      = user["loc"]
                dist_str = _loc_distance_str(self._bridge.my_locator, loc)
                await self._num(311, target, target, "on4kst", "*",
                                f"{gecos} [{loc}]{dist_str}")
                await self._num(319, target, CHANNEL)
                await self._num(312, target, SERVER_NAME, f"ON4KST {loc}")
                if user.get("away"):
                    await self._num(301, target, "Away (UNSET HERE)")
            await self._num(318, target, "End of WHOIS list.")

        elif cmd == "MODE":
            ch   = parts[1].strip() if len(parts) > 1 else ""
            flag = parts[2].strip() if len(parts) > 2 else ""
            if ch.lower() == CHANNEL.lower():
                if flag == "b":
                    await self._num(368, CHANNEL, "End of channel ban list.")
                elif flag == "e":
                    await self._num(349, CHANNEL, "End of channel exception list.")
                elif flag == "I":
                    await self._num(347, CHANNEL, "End of channel invite list.")
                else:
                    await self._num(324, CHANNEL, "+")

        elif cmd == "QUIT":
            self._writer.close()

    async def handle_loop(self):
        buf = ""
        try:
            while True:
                data = await self._reader.read(4096)
                if not data:
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    raw, buf = buf.split("\n", 1)
                    await self._handle_line(raw.rstrip("\r"))
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            try:
                self._writer.close()
            except Exception:
                pass
            await self._bridge.irc_disconnected(self)
            print("[IRC] Client disconnected.")


# ============================================================
# ON4KST client
# ============================================================

class ON4KSTClient:
    def __init__(self, host: str, port: int,
                 callsign: str, password: str, bridge: Bridge):
        self.host     = host
        self.port     = port
        self.callsign = callsign.upper()
        self.password = password
        self._bridge  = bridge
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buf         = b""
        self._collecting  = False
        self._new_users: dict[str, dict] = {}
        self.online_users: dict[str, dict] = {}

    async def connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port
            )
            sock = self._writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                except AttributeError:
                    pass  # Linux-specific; silently skip elsewhere
            return True
        except Exception as e:
            print(f"[KST] Connection error: {e}")
            return False

    async def send(self, text: str):
        try:
            self._writer.write((text + "\n").encode("utf-8", errors="replace"))
            await self._writer.drain()
        except Exception:
            pass

    async def _read_until(self, pattern: re.Pattern, timeout: float = 15.0) -> bool:
        accumulated = ""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    chunk = await self._reader.read(4096)
                    if not chunk:
                        return False
                    accumulated += strip_iac(chunk).decode("utf-8", errors="replace")
                    if pattern.search(accumulated):
                        return True
        except TimeoutError:
            return False

    async def login(self) -> bool:
        if not await self._read_until(RE_LOGIN):
            return False
        await self.send(self.callsign.lower())
        if not await self._read_until(RE_PASSWORD):
            return False
        await self.send(self.password)
        if not await self._read_until(RE_CHOICE):
            return False
        await self.send(CHAT_CHOICE)
        if not await self._read_until(RE_CHAT, timeout=20):
            return False
        self._buf = b""
        return True

    async def fetch_locator(self) -> str:
        await self.send("/SHow CONFig")
        buf = b""
        try:
            async with asyncio.timeout(5.0):
                while True:
                    chunk = await self._reader.read(4096)
                    if not chunk:
                        break
                    buf += strip_iac(chunk)
                    text = buf.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        if self.callsign in line.upper():
                            m = RE_LOCATOR.search(line)
                            if m:
                                return m.group(1).upper()
                    if RE_CHAT.search(text):
                        break
        except TimeoutError:
            pass
        return ""

    def _process_chunk(self, chunk: bytes):
        self._buf += strip_iac(chunk)
        decoded    = self._buf.decode("utf-8", errors="replace")
        lines      = re.split(r"\r?\n", decoded)
        self._buf  = lines[-1].encode("utf-8", errors="replace")
        for line in lines[:-1]:
            self._process_line(line)
        tail = lines[-1]
        if tail and RE_PROMPT.search(tail):
            self._process_line(tail)
            self._buf = b""

    def _process_line(self, line: str):
        line     = html.unescape(line)
        stripped = line.strip()

        # --- user list accumulation ---
        m = RE_USR.match(stripped)
        if m:
            away = m.group(1) == "("
            call = m.group(2).upper()
            loc  = m.group(3).upper()
            info = m.group(4).strip()
            self._new_users[call] = {"loc": loc, "info": info, "away": away}
            self._collecting = True
            return

        if self._collecting:
            if not stripped or RE_CHAT.search(line):
                self._flush_userlist()
                self._collecting = False
                if not stripped:
                    return
            else:
                self._flush_userlist()
                self._collecting = False
            # fall through: process current line as chat/notice

        # --- skip chat prompt lines ---
        if RE_CHAT.search(stripped):
            return

        # --- chat messages ---
        m = RE_CHAT_MSG.match(stripped)
        if m:
            utc       = m.group(1)
            from_call = m.group(2).upper()
            rest      = m.group(3)
            r = RE_RECIPIENT.match(rest)
            if r:
                recipient = r.group(1).upper()
                text      = r.group(2)
            else:
                recipient = None
                text      = rest
            asyncio.create_task(
                self._bridge.kst_message(utc, from_call, recipient, text)
            )

    def _flush_userlist(self):
        if not self._new_users:
            return
        old = self.online_users
        self.online_users = dict(self._new_users)
        self._new_users   = {}
        asyncio.create_task(self._bridge.kst_userlist(old, self.online_users))

    async def read_loop(self):
        last_refresh = time.monotonic()
        await self.send("/SHow USer")
        while True:
            timeout = max(0.1, last_refresh + REFRESH_SEC - time.monotonic())
            try:
                chunk = await asyncio.wait_for(
                    self._reader.read(4096), timeout=timeout
                )
            except asyncio.TimeoutError:
                await self.send("/SHow USer")
                last_refresh = time.monotonic()
                continue
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                print(f"[KST] Connection lost: {e}")
                break
            if not chunk:
                print("[KST] Connection closed by server.")
                break
            self._process_chunk(chunk)


# ============================================================
# Entry point
# ============================================================

async def _rig_poller(bridge: Bridge):
    """Poll rigctld every RIGCTLD_POLL_S seconds and cache freq/mode on bridge."""
    was_connected = False
    while True:
        qrg, mode = await fetch_rig_info()
        if qrg:
            if not was_connected:
                await bridge._notify_status(f"[rig] Connected – {qrg} MHz {mode}")
                was_connected = True
            bridge.rig_qrg  = qrg
            bridge.rig_mode = mode
        else:
            if was_connected:
                await bridge._notify_status("[rig] Disconnected – rig info unavailable")
                was_connected = False
            bridge.rig_qrg  = ""
            bridge.rig_mode = ""
        await asyncio.sleep(RIGCTLD_POLL_S)

async def _run_kst(bridge: Bridge, callsign: str, password: str):
    """Keep ON4KST connected, reconnecting as needed."""
    was_connected = False
    while True:
        kst = ON4KSTClient(KST_HOST, KST_PORT, callsign, password, bridge)
        bridge.kst = kst
        print(f"[KST] Connecting to {KST_HOST}:{KST_PORT} ...")
        if await kst.connect():
            print("[KST] Logging in ...")
            if await kst.login():
                loc = await kst.fetch_locator()
                if loc:
                    print(f"[KST] Locator: {loc}")
                    bridge.my_locator = loc
                # Mirror presence state: HERE if any IRC client is connected
                if bridge._sessions:
                    await kst.send("/SET HERE")
                else:
                    await kst.send("/UNSET HERE")
                await bridge._notify_status("[kst] Connected to ON4KST")
                was_connected = True
                await kst.read_loop()
                await bridge._notify_status(f"[kst] Disconnected – reconnecting in {RECONNECT_S} s")
            else:
                print("[KST] Login failed.")
                await bridge._notify_status(f"[kst] Login failed – reconnecting in {RECONNECT_S} s")
        else:
            if was_connected:
                await bridge._notify_status(f"[kst] Connection lost – reconnecting in {RECONNECT_S} s")
        bridge.kst = None
        was_connected = False
        print(f"[KST] Reconnecting in {RECONNECT_S} s ...")
        await asyncio.sleep(RECONNECT_S)


async def _main():
    callsign, password = load_credentials()
    print(f"[bridge] Callsign: {callsign}")

    bridge = Bridge(callsign)

    async def _irc_handler(reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        print(f"[IRC] Client connected from {addr}")
        session = IRCSession(reader, writer, bridge)
        await session.handle_loop()

    server = await asyncio.start_server(_irc_handler, IRC_HOST, IRC_PORT)
    addrs  = ", ".join(str(s.getsockname()) for s in server.sockets)
    print(f"[IRC] Listening on {addrs}")
    print(f"[IRC] irssi: /server localhost {IRC_PORT}  (auto-joins {CHANNEL})")

    async with server:
        await asyncio.gather(
            server.serve_forever(),
            _run_kst(bridge, callsign, password),
            _rig_poller(bridge),
        )


def main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n73!")


if __name__ == "__main__":
    main()
