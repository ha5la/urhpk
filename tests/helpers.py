"""Shared test helpers: mock servers and IRC client."""
from __future__ import annotations

import asyncio
import socket

from on4kst_irc_bridge import Bridge, IRCSession, ON4KSTClient

CALLSIGN   = "HA5LA"
PASSWORD   = "testpass"
# A minimal chat-prompt line that satisfies RE_CHAT and contains a locator
CHAT_PROMPT = b"1234Z HA5LA HA5LA JN97MX chat >\r\n"


# ============================================================
# Mock ON4KST server
# ============================================================

class MockKSTServer:
    """Minimal ON4KST server for integration tests."""

    def __init__(self):
        self._server = None
        self.port: int = 0
        self._writer: asyncio.StreamWriter | None = None
        self.received: list[str] = []
        self._logged_in = asyncio.Event()

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter):
        self._writer = writer
        try:
            writer.write(b"Login: ")
            await writer.drain()
            await reader.readline()

            writer.write(b"Password: ")
            await writer.drain()
            await reader.readline()

            writer.write(b"Your choice> ")
            await writer.drain()
            await reader.readline()

            writer.write(CHAT_PROMPT)
            await writer.drain()
            self._logged_in.set()

            while True:
                line = await reader.readline()
                if not line:
                    break
                cmd = line.decode(errors="replace").strip()
                self.received.append(cmd)
                await self._auto_respond(cmd, writer)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _auto_respond(self, cmd: str, writer: asyncio.StreamWriter):
        upper = cmd.upper()
        if "/SHOW CONF" in upper:
            # Include callsign + locator so fetch_locator() succeeds
            writer.write(b"HA5LA JN97MX config\r\n")
            writer.write(CHAT_PROMPT)
            await writer.drain()
        elif "/SHOW USER" in upper:
            writer.write(CHAT_PROMPT)
            await writer.drain()

    async def wait_ready(self, timeout: float = 5.0):
        async with asyncio.timeout(timeout):
            await self._logged_in.wait()

    async def inject(self, text: str):
        """Send a line to the bridge as if from ON4KST."""
        if self._writer:
            self._writer.write((text + "\r\n").encode())
            await self._writer.drain()

    def was_sent(self, fragment: str) -> bool:
        return any(fragment in cmd for cmd in self.received)


# ============================================================
# Mock Bridge / KST proxy  (for IRCSession unit tests)
# ============================================================

class MockKSTProxy:
    def __init__(self):
        self.sent: list[str] = []
        self.online_users: dict[str, dict] = {}

    async def send(self, text: str):
        self.sent.append(text)


class MockBridge:
    """Minimal Bridge substitute — lets IRCSession tests run without KST."""
    callsign = CALLSIGN

    def __init__(self):
        self.kst            = MockKSTProxy()
        self.my_locator     = ""
        self.irc_messages:   list[tuple[str, str]] = []
        self.connected:      list = []
        self.disconnected:   list = []

    async def irc_connected(self, session):
        self.connected.append(session)

    async def irc_disconnected(self, session):
        self.disconnected.append(session)

    async def irc_message(self, target: str, text: str):
        self.irc_messages.append((target, text))


# ============================================================
# IRC client helper
# ============================================================

class IRCClientHelper:
    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._buf    = ""

    async def send(self, line: str):
        self._writer.write((line + "\r\n").encode())
        await self._writer.drain()

    async def recv(self, timeout: float = 2.0) -> str:
        async with asyncio.timeout(timeout):
            while "\n" not in self._buf:
                data = await self._reader.read(4096)
                if not data:
                    raise EOFError("Connection closed")
                self._buf += data.decode(errors="replace")
            line, self._buf = self._buf.split("\n", 1)
            return line.rstrip("\r")

    async def drain(self, n: int = 30, timeout: float = 0.3) -> list[str]:
        """Read up to n lines until quiet for timeout seconds."""
        lines = []
        for _ in range(n):
            try:
                lines.append(await self.recv(timeout=timeout))
            except TimeoutError:
                break
        return lines

    async def recv_until(self, pattern: str, timeout: float = 5.0) -> list[str]:
        lines = []
        async with asyncio.timeout(timeout):
            while True:
                line = await self.recv()
                lines.append(line)
                if pattern in line:
                    return lines

    async def register(self, nick: str = "TESTNICK") -> list[str]:
        """Complete IRC registration and return lines up to and including 376."""
        await self.send("CAP LS 302")
        await self.send(f"NICK {nick}")
        await self.send("USER test 0 * :Test User")
        await self.send("CAP END")
        return await self.recv_until("376")


# ============================================================
# Stream pair helper
# ============================================================

async def make_stream_pair():
    """Two connected (reader, writer) pairs via socketpair."""
    s1, s2 = socket.socketpair()
    r1, w1 = await asyncio.open_connection(sock=s1)
    r2, w2 = await asyncio.open_connection(sock=s2)
    return (r1, w1), (r2, w2)


async def make_irc_pair(bridge: MockBridge):
    """Return (IRCSession, IRCClientHelper) wired together."""
    (sr, sw), (cr, cw) = await make_stream_pair()
    session = IRCSession(sr, sw, bridge)
    client  = IRCClientHelper(cr, cw)
    return session, client
