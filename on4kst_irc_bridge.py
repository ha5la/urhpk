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
  • Messages received while no IRC client is connected are buffered (up to
    HISTORY_MAX) and replayed with their original timestamp when a client
    connects

Prerequisites:
  ~/.netrc must contain:
    machine www.on4kst.info login <callsign> password <password>

Usage:
    uv run on4kst_irc_bridge.py

irssi quick-start:
    /server add -auto -network on4kst localhost 6667
    /channel add -auto #on4kst on4kst
    /save
    /connect on4kst
"""
from __future__ import annotations

import asyncio
import html
import netrc
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

# ============================================================
# Configuration
# ============================================================
KST_HOST    = "www.on4kst.info"
KST_PORT    = 23000
CHAT_CHOICE = "2"           # 144/432 MHz
IRC_HOST    = "127.0.0.1"
IRC_PORT    = 6667
SERVER_NAME = "on4kst.bridge"
CHANNEL     = "#on4kst"
REFRESH_SEC = 120
RECONNECT_S = 30
HISTORY_MAX = 500           # buffered messages replayed on reconnect

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
    r"^\(?([A-Z0-9]{3,}(?:/[A-Z0-9]+)?)\)?\s{2,}([A-Z]{2}\d{2}[A-Z]{2})",
    re.I,
)
RE_PROMPT    = re.compile(r"(Login|Password|choice|chat)\s*[>:]\s*$", re.I)
RE_LOCATOR   = re.compile(r"\b([A-R]{2}\d{2}[A-X]{2})\b", re.I)

# ============================================================
# Buffered message (history replay)
# ============================================================

@dataclass
class BufferedMsg:
    utc:       str   # e.g. "0712Z"
    from_call: str
    target:    str   # CHANNEL or a callsign (for PMs addressed to us)
    text:      str


# ============================================================
# Bridge  (coordinates ON4KST client ↔ IRC sessions)
# ============================================================

class Bridge:
    def __init__(self, callsign: str):
        self.callsign    = callsign
        self.kst: ON4KSTClient | None = None
        self._sessions: set[IRCSession] = set()
        self._history:  deque[BufferedMsg] = deque(maxlen=HISTORY_MAX)

    # ----------------------------------------------------------
    # IRC session lifecycle
    # ----------------------------------------------------------

    async def irc_connected(self, session: IRCSession):
        self._sessions.add(session)
        if self.kst:
            await self.kst.send("/SET HERE")
        if self._history:
            await session.send_notice(
                f"--- replaying {len(self._history)} buffered message(s) ---"
            )
            for msg in self._history:
                await session.send_privmsg(
                    msg.from_call, msg.target, f"[{msg.utc}] {msg.text}"
                )
            await session.send_notice("--- end of buffer ---")

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
            await self.kst.send(text)
        else:
            await self.kst.send(f"/CQ {target.upper()} {text}")

    # ----------------------------------------------------------
    # Messages from ON4KST → IRC clients
    # ----------------------------------------------------------

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

        msg = BufferedMsg(utc, from_call, target, text)

        if self._sessions:
            for s in list(self._sessions):
                try:
                    await s.send_privmsg(from_call, target, text)
                except Exception:
                    pass
        else:
            self._history.append(msg)

    async def kst_userlist(self, old: dict[str, str], new: dict[str, str]):
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
        self._user         = ""
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

    async def send_notice(self, text: str):
        await self._send(f":{SERVER_NAME} NOTICE {self.nick or '*'} :{text}")

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
        await self._num(1,   f"Welcome to the ON4KST IRC Bridge, {self.nick}")
        await self._num(2,   f"Your host is {SERVER_NAME}")
        await self._num(3,   "This server was created today")
        await self._num(4,   SERVER_NAME, "on4kst-bridge-1.0", "o", "o")
        await self._num(375, f"- {SERVER_NAME} Message of the Day -")
        await self._num(372, f"- ON4KST 144/432 MHz IRC bridge")
        await self._num(372, f"- Connected as: {self._bridge.callsign}")
        await self._num(372, f"- Join {CHANNEL} to enter the chat")
        await self._num(376, "End of MOTD command.")
        self._reg = True
        await self._bridge.irc_connected(self)

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
                if self.nick and self._user and not self._reg:
                    await self._welcome()

        elif cmd == "NICK":
            self.nick = parts[1].strip() if len(parts) > 1 else "?"
            if self._user and not self._reg and not self._cap_pending:
                await self._welcome()

        elif cmd == "USER":
            self._user = parts[1].strip() if len(parts) > 1 else "?"
            if self.nick and not self._reg and not self._cap_pending:
                await self._welcome()

        elif cmd == "PING":
            token = (parts[1] if len(parts) > 1 else SERVER_NAME).lstrip(":")
            await self._send(f"PONG :{token}")

        elif cmd == "JOIN":
            channel = parts[1].split(",")[0].strip() if len(parts) > 1 else ""
            if channel.lower() == CHANNEL.lower():
                await self._send(
                    f":{self.nick}!{self.nick}@localhost JOIN {CHANNEL}"
                )
                await self._num(332, CHANNEL, "ON4KST 144/432 MHz chat bridge")
                await self._num(333, CHANNEL, SERVER_NAME, "0")
                await self._send_names()

        elif cmd == "PRIVMSG":
            if len(parts) >= 3:
                target = parts[1]
                text   = parts[2].lstrip(":")
                await self._bridge.irc_message(target, text)

        elif cmd == "WHO":
            target = parts[1].strip() if len(parts) > 1 else CHANNEL
            if target.lower() == CHANNEL.lower() and self._bridge.kst:
                for call, loc in self._bridge.kst.online_users.items():
                    await self._send(
                        f":{SERVER_NAME} 352 {self.nick} {CHANNEL} {call} on4kst "
                        f"{SERVER_NAME} {call} H :0 {loc}"
                    )
            await self._num(315, CHANNEL, "End of WHO list.")

        elif cmd == "WHOIS":
            target = (parts[1].strip() if len(parts) > 1 else "").upper()
            kst    = self._bridge.kst
            loc    = (kst.online_users.get(target, "") if kst else "")
            if loc:
                await self._num(311, target, target, "on4kst", "*", target)
                await self._num(319, target, CHANNEL)
                await self._num(312, target, SERVER_NAME, f"ON4KST {loc}")
            await self._num(318, target, "End of WHOIS list.")

        elif cmd == "MODE":
            ch = parts[1].strip() if len(parts) > 1 else ""
            if ch.lower() == CHANNEL.lower():
                await self._num(324, CHANNEL, "+")

        elif cmd == "QUIT":
            self._writer.close()

        # Silently ignore: CAP, PASS, AWAY, LIST, NAMES, TOPIC, ISON, …

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
        self._new_users: dict[str, str] = {}
        self.online_users: dict[str, str] = {}
        self.first_userlist = asyncio.Event()
        self.locator  = ""

    async def connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port
            )
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
            call = m.group(1).upper().strip("()")
            loc  = m.group(2).upper()
            self._new_users[call] = loc
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
        self.first_userlist.set()
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
            if not chunk:
                print("[KST] Connection closed by server.")
                break
            self._process_chunk(chunk)


# ============================================================
# Entry point
# ============================================================

async def _run_kst(bridge: Bridge, callsign: str, password: str):
    """Keep ON4KST connected, reconnecting as needed."""
    while True:
        kst = ON4KSTClient(KST_HOST, KST_PORT, callsign, password, bridge)
        bridge.kst = kst
        print(f"[KST] Connecting to {KST_HOST}:{KST_PORT} ...")
        if await kst.connect():
            print("[KST] Logging in ...")
            if await kst.login():
                loc = await kst.fetch_locator()
                if loc:
                    kst.locator = loc
                    print(f"[KST] Locator: {loc}")
                # Mirror presence state: HERE if any IRC client is connected
                if bridge._sessions:
                    await kst.send("/SET HERE")
                else:
                    await kst.send("/UNSET HERE")
                await kst.read_loop()
            else:
                print("[KST] Login failed.")
        bridge.kst = None
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
    print(f"[IRC] irssi: /server localhost {IRC_PORT}  then  /join {CHANNEL}")

    async with server:
        await asyncio.gather(
            server.serve_forever(),
            _run_kst(bridge, callsign, password),
        )


def main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n73!")


if __name__ == "__main__":
    main()
