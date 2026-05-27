#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["prompt_toolkit"]
# ///
"""
Puskás URH Kupa – ON4KST chat helper
======================================
Connects to the ON4KST 144/432 MHz chat, monitors online stations,
and cross-references them against puskas_stations.csv.

Prerequisites:
  1. puskas_log_analyzer.py has been run → puskas_stations.csv exists
  2. ~/.netrc contains login credentials:
       machine www.on4kst.info login ha5la password SECRET

Usage:
    uv run puskas_kst.py
"""

import asyncio
import csv
import html
import netrc
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from prompt_toolkit import print_formatted_text, PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.patch_stdout import patch_stdout

# ============================================================
# Configuration  (callsign + locator are set at startup from
# .netrc / server, not hardcoded here)
# ============================================================
MY_CALLSIGN  = ""
MY_LOCATOR   = ""
KST_HOST     = "www.on4kst.info"
KST_PORT     = 23000
CHAT_CHOICE  = "2"          # 144/432 MHz
STATIONS_CSV = "puskas_stations.csv"
REFRESH_SEC  = 120
SEND_DELAY   = 1.5

_online_count = 0

def current_prompt() -> str:
    ts = datetime.now(timezone.utc).strftime("%H%MZ")
    if _online_count:
        return f"{ts} [online:{_online_count}] {MY_CALLSIGN}> "
    return f"{ts} {MY_CALLSIGN}> "

# ============================================================

BANNER = """
╔══════════════════════════════════════════════════════════╗
║          Puskás URH Kupa – ON4KST chat helper            ║
║                                                          ║
║  Commands:                                               ║
║    l           – list online + known stations            ║
║    s CALL      – preview sked message                    ║
║    send CALL   – send sked message (public chat)         ║
║    pm CALL     – send sked message (/CQ)                 ║
║    raw TEXT    – send arbitrary text                     ║
║    q           – quit (/QUIT)                            ║
╚══════════════════════════════════════════════════════════╝
"""

# ============================================================
# Message highlighting (ANSI, work fine through patch_stdout)
# ============================================================
_RESET      = "\033[0m"
_TO_ME      = "\033[1;93m"   # bold bright-yellow  – addressed to me
_BROADCAST  = "\033[96m"     # bright cyan          – no explicit recipient
_SERVER     = "\033[2m"      # dim                  – server notices

RE_ADDRESSED = re.compile(r"^\d{4}Z\s+\S+.*?>\s+\(([A-Z0-9/]+)\)", re.I)

def colored_chat(line: str) -> str:
    m = RE_ADDRESSED.match(line)
    if m:
        return f"{_TO_ME}{line}{_RESET}" if m.group(1).upper() == MY_CALLSIGN \
               else line                  # to someone else: plain
    return f"{_BROADCAST}{line}{_RESET}"  # CQ / general broadcast


# ============================================================
# TAB completion
# ============================================================
COMMANDS = ["l", "s", "send", "pm", "raw", "q"]

class KSTCompleter(Completer):
    def __init__(self, known_calls: list[str], get_online):
        self.known_calls = known_calls
        self.get_online  = get_online

    def get_completions(self, document, complete_event):
        text  = document.text_before_cursor.lstrip()
        words = text.split()

        if not words:
            for cmd in COMMANDS:
                yield Completion(cmd)
            return

        first = words[0].lower()
        if first in ("send", "pm", "s") and (len(words) > 1 or text.endswith(" ")):
            partial   = words[1] if len(words) > 1 else ""
            all_calls = list(dict.fromkeys(self.known_calls + list(self.get_online())))
            for call in all_calls:
                if call.upper().startswith(partial.upper()):
                    yield Completion(call, start_position=-len(partial))
        elif len(words) == 1 and not text.endswith(" "):
            for cmd in COMMANDS:
                if cmd.startswith(first) and cmd != first:
                    yield Completion(cmd, start_position=-len(first))


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
# CSV loading
# ============================================================

def load_stations(csv_path: str) -> dict[str, dict]:
    path = Path(csv_path)
    if not path.exists():
        print(f"[error] Not found: {csv_path}")
        print("        Run puskas_log_analyzer.py first!")
        sys.exit(1)

    stations: dict[str, dict] = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            call = row["callsign"].upper().strip()
            if call == MY_CALLSIGN:
                continue
            if call not in stations:
                stations[call] = {
                    "wwl":         row["wwl"],
                    "bearing":     float(row["bearing"]),
                    "distance_km": float(row["distance_km"]),
                    "sector":      row["sector"],
                    "bands":       [],
                }
            band = row["band"].strip()
            if band and band != "?" and band not in stations[call]["bands"]:
                stations[call]["bands"].append(band)

    print(f"[OK] {len(stations)} stations loaded: {csv_path}")
    return stations


# ============================================================
# Telnet IAC filter
# ============================================================

def strip_iac(data: bytes) -> bytes:
    out = bytearray()
    i = 0
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
RE_LOGIN    = re.compile(r"Login\s*:",    re.I)
RE_PASSWORD = re.compile(r"Password\s*:", re.I)
RE_CHOICE   = re.compile(r"Your choice",  re.I)
RE_CHAT     = re.compile(r"\d{4}Z\s+\S+\s+.+chat\s*>", re.I)
RE_CHAT_MSG = re.compile(r"^\d{4}Z\s+\S+.*>\s+\S")
RE_USR      = re.compile(
    r"^\(?([A-Z0-9]{3,}(?:/[A-Z0-9]+)?)\)?\s{2,}([A-Z]{2}\d{2}[A-Z]{2})",
    re.I,
)
RE_PROMPT   = re.compile(r"(Login|Password|choice|chat)\s*[>:]\s*$", re.I)
RE_LOCATOR  = re.compile(r"\b([A-R]{2}\d{2}[A-X]{2})\b", re.I)


# ============================================================
# ON4KST client – pure asyncio, no threads
# ============================================================

class KSTClient:
    def __init__(self, host: str, port: int, callsign: str, password: str):
        self.host         = host
        self.port         = port
        self.callsign     = callsign.upper()
        self.password     = password
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.online_users: dict[str, str] = {}
        self._buf         = b""
        self._collecting  = False
        self._new_users: dict[str, str] = {}
        self.first_userlist = asyncio.Event()

    async def connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
            print(f"[KST] Connected: {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"[KST] Connection error: {e}")
            return False

    async def _send(self, text: str):
        self._writer.write((text + "\n").encode("utf-8", errors="replace"))
        await self._writer.drain()

    # ----------------------------------------------------------
    # Login phase: sequential async reads
    # ----------------------------------------------------------

    async def _read_until(self, pattern: re.Pattern, timeout: float = 15.0) -> bool:
        accumulated = ""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    chunk = await self._reader.read(4096)
                    if not chunk:
                        return False
                    text = strip_iac(chunk).decode("utf-8", errors="replace")
                    for line in re.split(r"\r?\n", text):
                        if line.strip():
                            print(f"  {line}")
                    accumulated += text
                    if pattern.search(accumulated):
                        return True
        except TimeoutError:
            return False

    async def login(self) -> bool:
        print("[KST] Waiting for Login: prompt...")
        if not await self._read_until(RE_LOGIN):
            print("[KST] Login: prompt not received.")
            return False

        print(f"[KST] Logging in as: {self.callsign}")
        await self._send(self.callsign.lower())

        print("[KST] Waiting for Password: prompt...")
        if not await self._read_until(RE_PASSWORD):
            print("[KST] Password: prompt not received.")
            return False

        await self._send(self.password)

        print("[KST] Waiting for chat selection menu...")
        if not await self._read_until(RE_CHOICE):
            print("[KST] Chat selection menu not received.")
            return False

        print(f"[KST] Selecting chat: {CHAT_CHOICE} (144/432 MHz)")
        await self._send(CHAT_CHOICE)

        print("[KST] Waiting for chat prompt...")
        if not await self._read_until(RE_CHAT, timeout=20):
            print("[KST] Chat prompt not received – wrong password?")
            return False

        self._buf = b""
        print("[KST] Login successful!\n")
        return True

    async def fetch_locator(self) -> str:
        """Send /SHow CONFig after login and extract our grid locator."""
        await self._send("/SHow CONFig")
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

    # ----------------------------------------------------------
    # Post-login: event-driven line processing
    # ----------------------------------------------------------

    def _process_chunk(self, chunk: bytes):
        self._buf += strip_iac(chunk)
        decoded = self._buf.decode("utf-8", errors="replace")
        lines = re.split(r"\r?\n", decoded)
        self._buf = lines[-1].encode("utf-8", errors="replace")
        for line in lines[:-1]:
            self._process_line(line)
        tail = lines[-1]
        if tail and RE_PROMPT.search(tail):
            self._process_line(tail)
            self._buf = b""

    def _process_line(self, line: str):
        global _online_count
        line = html.unescape(line)
        stripped = line.strip()

        m = RE_USR.match(stripped)
        if m:
            call = m.group(1).upper().strip("()")
            loc  = m.group(2).upper()
            self._new_users[call] = loc
            self._collecting = True
            return

        if self._collecting:
            if not stripped or RE_CHAT.search(line):
                if self._new_users:
                    self.online_users = dict(self._new_users)
                    _online_count = len(self._new_users)
                    self.first_userlist.set()
                self._new_users = {}
                self._collecting = False
                return
            if self._new_users:
                self.online_users = dict(self._new_users)
                _online_count = len(self._new_users)
                self.first_userlist.set()
            self._new_users = {}
            self._collecting = False

        if RE_CHAT_MSG.search(line):
            print_formatted_text(ANSI(colored_chat(line)))
        elif stripped and not RE_CHAT.search(line):
            print_formatted_text(ANSI(f"{_SERVER}  [server] {line}{_RESET}"))

    async def read_loop(self):
        last_refresh = time.monotonic()
        await self._send("/SHow USer")
        while True:
            timeout = max(0.1, last_refresh + REFRESH_SEC - time.monotonic())
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=timeout)
            except asyncio.TimeoutError:
                await self._send("/SHow USer")
                last_refresh = time.monotonic()
                continue
            if not chunk:
                print("[KST] Connection lost.")
                break
            self._process_chunk(chunk)

    async def send_chat(self, text: str):
        await self._send(text)
        await asyncio.sleep(SEND_DELAY)

    async def send_cq(self, callsign: str, text: str):
        await self._send(f"/CQ {callsign.upper()} {text}")
        await asyncio.sleep(SEND_DELAY)

    async def quit(self):
        await self._send("/QUIT")
        await asyncio.sleep(0.5)
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass


# ============================================================
# Display
# ============================================================

def fmt_bands(bands: list[str]) -> str:
    return ", ".join(sorted(bands)) if bands else "?"


def list_interesting(online: dict[str, str], known: dict[str, dict]):
    matches = []
    for call, loc in online.items():
        if call.upper() == MY_CALLSIGN:
            continue
        info = known.get(call.upper())
        if info:
            matches.append((call, loc, info))

    matches.sort(key=lambda x: x[2]["distance_km"], reverse=True)
    now = datetime.now(timezone.utc).strftime("%H:%MZ")

    print(f"\n{'─'*68}")
    print(f"  KST online: {len(online)}  |  {now}  |  Known: {len(matches)}")
    print(f"{'─'*68}")

    if matches:
        print(f"  {'Callsign':<12} {'KST loc':<8} {'CSV loc':<8}"
              f" {'Bear':>6} {'Dist':>7}  Bands")
        print(f"  {'─'*63}")
        for call, kst_loc, info in matches:
            warn = "  ⚠ loc≠" if kst_loc.upper() != info["wwl"].upper() else ""
            print(f"  {call:<12} {kst_loc:<8} {info['wwl']:<8}"
                  f" {info['bearing']:>5.1f}°  {info['distance_km']:>6.1f} km"
                  f"  {fmt_bands(info['bands'])}{warn}")
    else:
        print("  (No known stations online)")

    unknown = sorted(
        c for c in online
        if c.upper() not in known and c.upper() != MY_CALLSIGN
    )
    if unknown:
        print(f"\n  Unknown (not active in previous round):")
        for i in range(0, len(unknown), 5):
            print(f"    {', '.join(unknown[i:i+5])}")
    print(f"{'─'*68}\n")


def sked_text(call: str, known: dict[str, dict]) -> str:
    info = known.get(call.upper())
    if info:
        return (
            f"Hi {call.upper()}, sked? Puskás URH Kupa – "
            f"{fmt_bands(info['bands'])} – "
            f"{int(info['distance_km'])} km, {int(info['bearing'])}° "
            f"({MY_LOCATOR}). 73 {MY_CALLSIGN}"
        )
    return f"Hi {call.upper()}, sked? Puskás URH Kupa – ({MY_LOCATOR}). 73 {MY_CALLSIGN}"


# ============================================================
# Command handler
# ============================================================

async def handle_command(cmd: str, client: KSTClient, known: dict[str, dict]) -> bool:
    """Returns False to quit."""
    if not cmd or cmd.lower() in ("l", "ls", "list", "?"):
        list_interesting(client.online_users, known)

    elif cmd.lower() == "q":
        return False

    elif cmd.lower().startswith("s "):
        call = cmd.split(None, 1)[1].strip().upper()
        text = sked_text(call, known)
        print(f"\n  Preview → {text}")
        print(f"  Send with: send {call}  or  pm {call}\n")

    elif cmd.lower().startswith("send "):
        call = cmd.split(None, 1)[1].strip().upper()
        text = sked_text(call, known)
        if call not in client.online_users:
            print(f"  ⚠ {call} not seen online – not sending.")
            print(f"  To send anyway: raw {text}")
        else:
            await client.send_chat(text)
            print(f"  [sent] {text}")

    elif cmd.lower().startswith("pm "):
        call = cmd.split(None, 1)[1].strip().upper()
        text = sked_text(call, known)
        if call not in client.online_users:
            print(f"  ⚠ {call} not seen online – not sending.")
            print(f"  To send anyway: raw /CQ {call} {text}")
        else:
            await client.send_cq(call, text)
            print(f"  [/CQ sent] {text}")

    elif cmd.lower().startswith("raw "):
        await client.send_chat(cmd[4:].strip())

    else:
        print("  Unknown command: l, s CALL, send CALL, pm CALL, raw TEXT, q")

    return True


# ============================================================
# Minute-boundary prompt refresh
# ============================================================

async def minute_ticker(session: PromptSession):
    """Invalidates the prompt display at each UTC minute transition."""
    while True:
        now = datetime.now(timezone.utc)
        wait = 60.0 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(wait)
        try:
            session.app.invalidate()
        except AttributeError:
            pass  # prompt_async not yet active


# ============================================================
# Entry point
# ============================================================

async def _main():
    global MY_CALLSIGN, MY_LOCATOR

    print(BANNER)

    callsign, password = load_credentials()
    MY_CALLSIGN = callsign

    known  = load_stations(STATIONS_CSV)
    client = KSTClient(KST_HOST, KST_PORT, callsign, password)

    if not await client.connect():
        sys.exit(1)
    if not await client.login():
        print("[error] Login failed.")
        sys.exit(1)

    locator = await client.fetch_locator()
    if locator:
        MY_LOCATOR = locator
        print(f"[OK] Locator from server: {MY_LOCATOR}")
    else:
        print("[warning] Could not fetch locator – sked messages will omit it.")

    session = PromptSession(
        completer=KSTCompleter(list(known.keys()), lambda: client.online_users.keys()),
    )

    print("Commands: l, s CALL, send CALL, pm CALL, raw TEXT, q")
    print("TAB completion available. Ctrl-C to quit.\n")

    with patch_stdout():
        reader_task = asyncio.create_task(client.read_loop())
        asyncio.create_task(minute_ticker(session))

        # Wait for first userlist so the prompt shows a count immediately
        try:
            await asyncio.wait_for(client.first_userlist.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        try:
            while not reader_task.done():
                try:
                    cmd = await session.prompt_async(current_prompt, refresh_interval=0)
                except EOFError:
                    break
                except KeyboardInterrupt:
                    break
                if not await handle_command(cmd.strip(), client, known):
                    break
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
            print("\nQuitting...")
            await client.quit()
            print("73!")


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
