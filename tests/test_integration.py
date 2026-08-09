"""
Integration tests — full bridge with MockKSTServer and IRCClientHelper.
Covers the end-to-end message flows between ON4KST and IRC.
"""

import asyncio
import socket

import pytest

import on4kst_irc_bridge as bridge_module
from on4kst_irc_bridge import CHANNEL, Bridge, IRCSession, ON4KSTClient
from tests.helpers import (
    CALLSIGN,
    PASSWORD,
    IRCClientHelper,
    MockKSTServer,
    wait_until,
)

# ============================================================
# Fixture: full bridge environment
# ============================================================


@pytest.fixture
async def bridge_env():
    """
    Starts MockKSTServer, Bridge, and IRC server.
    Yields (bridge, kst_server, irc_port).
    """
    kst_server = MockKSTServer()
    await kst_server.start()

    # A dead port by default: nothing in these tests may accidentally reach
    # a real rig server (e.g. a live puskas_logger on 4532 on this machine).
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    orig_rig_port = bridge_module.RIG_SERVER_PORT
    bridge_module.RIG_SERVER_PORT = dead_port

    bridge = Bridge(CALLSIGN)

    async def handle_irc(r, w):
        await IRCSession(r, w, bridge).handle_loop()

    irc_server = await asyncio.start_server(handle_irc, "127.0.0.1", 0)
    irc_port = irc_server.sockets[0].getsockname()[1]

    kst_ref = []

    async def run_kst():
        kst = ON4KSTClient("127.0.0.1", kst_server.port, CALLSIGN, PASSWORD, bridge)
        kst_ref.append(kst)
        try:
            if await kst.connect() and await kst.login():
                await kst.fetch_locator()
                bridge.kst = kst
                await kst.read_loop()
        finally:
            if kst._writer:
                kst._writer.close()

    kst_task = asyncio.create_task(run_kst())
    await kst_server.wait_ready()
    # bridge.kst is set right after fetch_locator() returns, immediately
    # before read_loop() starts — that's the true readiness signal, not a
    # guessed duration for "fetch_locator + first /SHow USer to complete".
    assert await wait_until(lambda: bridge.kst is not None)

    yield bridge, kst_server, irc_port

    bridge_module.RIG_SERVER_PORT = orig_rig_port
    kst_task.cancel()
    try:
        await asyncio.wait_for(kst_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    irc_server.close()
    # Not gating an assertion (teardown only) — a short grace period for
    # any still-in-flight session write to finish before closing the mock
    # KST server out from under it, not a correctness wait.
    await asyncio.sleep(0.02)
    await kst_server.stop()


async def irc_connect(irc_port: int, nick: str = "TESTNICK"):
    """Connect an IRC client, register, and consume auto-join output.

    register() stops at 376 (end of MOTD); the bridge always follows with
    an optional NICK-change, JOIN, and NAMES, unconditionally terminated
    by numeric 366 — so recv_until("366") consumes exactly that, with no
    "wait for quiet" timeout guess involved.
    """
    r, w = await asyncio.open_connection("127.0.0.1", irc_port)
    client = IRCClientHelper(r, w)
    await client.register(nick)
    await client.recv_until("366")
    return client, w


# ============================================================
# ON4KST → IRC
# ============================================================


class TestKSTToIRC:
    async def test_public_message_forwarded(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await kst_server.inject("0712Z G6DDN Ian 2m14> Hello everyone")
            line = await client.recv()
            assert "PRIVMSG" in line and CHANNEL in line
            assert "G6DDN" in line
            assert "Hello everyone" in line
        finally:
            w.close()

    async def test_message_addressed_to_me_becomes_pm(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await kst_server.inject(f"0712Z G6DDN Ian 2m14> ({CALLSIGN}) Hey, sked?")
            line = await client.recv()
            assert f"PRIVMSG {CALLSIGN}" in line
            assert "G6DDN" in line
            assert "Hey, sked?" in line
        finally:
            w.close()

    async def test_message_addressed_to_other_stays_in_channel(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await kst_server.inject("0712Z G6DDN Ian 2m14> (DK5DV) See you on 2m!")
            line = await client.recv()
            assert f"PRIVMSG {CHANNEL}" in line
            assert "(DK5DV)" in line
        finally:
            w.close()

    async def test_own_message_not_echoed(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await kst_server.inject(f"0712Z {CALLSIGN} HA5LA JN97MX> Testing 1 2 3")
            # Proving a negative (nothing arrives) can't be turned into a
            # wait_until poll — there's no true condition to wait for. The
            # bound just needs to comfortably clear real loopback latency
            # (sub-ms), not "feel safe" as a guessed processing duration.
            with pytest.raises(TimeoutError):
                await client.recv(timeout=0.05)
        finally:
            w.close()

    async def test_html_entities_decoded(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            # Entities must be decoded in the message body before forwarding
            await kst_server.inject("0712Z G6DDN Ian 2m14> 6&amp;2m &#9889; sked?")
            line = await client.recv()
            assert "6&2m" in line  # &amp; → &
            assert "⚡" in line  # &#9889; → ⚡
        finally:
            w.close()


# ============================================================
# IRC → ON4KST
# ============================================================


class TestIRCToKST:
    async def test_channel_message_forwarded(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await client.send(f"PRIVMSG {CHANNEL} :CQ de {CALLSIGN}")
            assert await wait_until(lambda: kst_server.was_sent(f"CQ de {CALLSIGN}"))
        finally:
            w.close()

    async def test_pm_becomes_cq(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await client.send("PRIVMSG G6DDN :Sked?")
            assert await wait_until(lambda: kst_server.was_sent("/CQ G6DDN Sked?"))
        finally:
            w.close()


# ============================================================
# Presence (/SET HERE / /UNSET HERE)
# ============================================================


class TestPresence:
    async def test_set_here_on_irc_connect(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            assert await wait_until(lambda: kst_server.was_sent("/SET HERE"))
        finally:
            w.close()

    async def test_unset_here_on_irc_disconnect(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        w.close()
        assert await wait_until(lambda: kst_server.was_sent("/UNSET HERE"))

    async def test_away_command_sends_unset_here(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await client.send("AWAY :Eating dinner")
            assert await wait_until(lambda: kst_server.was_sent("/UNSET HERE"))
        finally:
            w.close()

    async def test_back_command_sends_set_here(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await client.send("AWAY :Gone")
            await client.send("AWAY")  # bare AWAY = back; TCP preserves order,
            # no delay needed between the two sends for the bridge to see them
            # in sequence

            def set_here_count():
                return sum(1 for c in kst_server.received if c == "/SET HERE")

            assert await wait_until(lambda: set_here_count() >= 2)  # connect + back
        finally:
            w.close()


# ============================================================
# User list → JOIN / PART
# ============================================================


class TestUserList:
    async def test_new_user_triggers_join(self, bridge_env):
        bridge, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            # Inject a user list that includes G6DDN
            await kst_server.inject("G6DDN           IO83RJ Ian")
            await kst_server.inject("1234Z HA5LA HA5LA JN97MX chat >")
            line = await client.recv()
            assert "JOIN" in line
            assert "G6DDN" in line
        finally:
            w.close()

    async def test_gone_user_triggers_part(self, bridge_env):
        bridge, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            # First establish G6DDN as online via a proper user list
            await kst_server.inject("G6DDN           IO83RJ Ian")
            await kst_server.inject("1234Z HA5LA HA5LA JN97MX chat >")
            await client.recv_until("G6DDN")  # wait for the JOIN

            # New user list without G6DDN → G6DDN should PART
            await kst_server.inject("DK5DV            JO30XS Gerd")
            await kst_server.inject("1234Z HA5LA HA5LA JN97MX chat >")
            lines = await client.drain()
            assert any("PART" in line and "G6DDN" in line for line in lines)
        finally:
            w.close()


# ============================================================
# Sked commands
# ============================================================


class TestSkedCommands:
    async def test_pm_sked_sends_cq_with_sked_text(self, bridge_env):
        bridge, kst_server, irc_port = bridge_env
        bridge.my_locator = "JN97MX"
        bridge.kst.online_users["G6DDN"] = {
            "loc": "IO83RJ",
            "info": "Ian",
            "away": False,
        }
        client, w = await irc_connect(irc_port)
        try:
            pre = len(kst_server.received)

            def new_sent():
                return " ".join(kst_server.received[pre:])

            await client.send("PRIVMSG G6DDN :sked")
            assert await wait_until(lambda: "/CQ G6DDN" in new_sent())
            assert "sked?" in new_sent()
        finally:
            w.close()

    async def test_pm_sked_echoes_notice(self, bridge_env):
        bridge, kst_server, irc_port = bridge_env
        bridge.my_locator = "JN97MX"
        bridge.kst.online_users["G6DDN"] = {
            "loc": "IO83RJ",
            "info": "Ian",
            "away": False,
        }
        client, w = await irc_connect(irc_port)
        try:
            await client.send("PRIVMSG G6DDN :sked")
            # Exactly one NOTICE line is echoed for a PM sked — no variable
            # trailing output to wait out, so a single recv() suffices.
            notice = await client.recv()
            assert "NOTICE" in notice
            assert "/CQ G6DDN" in notice
            assert "sked?" in notice
        finally:
            w.close()

    async def test_non_sked_pm_forwarded_unchanged(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            pre = len(kst_server.received)

            def new_sent():
                return " ".join(kst_server.received[pre:])

            await client.send("PRIVMSG G6DDN :Hello there")
            assert await wait_until(lambda: "/CQ G6DDN Hello there" in new_sent())
        finally:
            w.close()


# ============================================================
# Local channel commands (!list, !help, !scatter, unknown)
# ============================================================


class TestLocalCommands:
    async def test_exclamation_not_forwarded_to_kst(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            # irc_connect() only guarantees the IRC-facing registration
            # output; the connect-triggered "/SET HERE" to KST runs on a
            # separate task and can still be in flight. Wait for it before
            # taking the baseline, so the negative check below isn't racing
            # against the very connection setup it's trying to exclude.
            assert await wait_until(lambda: kst_server.was_sent("/SET HERE"))
            pre = len(kst_server.received)
            await client.send(f"PRIVMSG {CHANNEL} :!help")
            # Proving a negative — see test_own_message_not_echoed above.
            await asyncio.sleep(0.05)
            assert kst_server.received[pre:] == []
        finally:
            w.close()

    async def test_notice_targets_channel(self, bridge_env):
        bridge, kst_server, irc_port = bridge_env
        bridge.my_locator = "JN97MX"
        client, w = await irc_connect(irc_port)
        try:
            await client.send(f"PRIVMSG {CHANNEL} :!help")
            lines = await client.drain()
            notices = [line for line in lines if "NOTICE" in line]
            assert notices, "!help must produce NOTICE lines"
            assert all(CHANNEL in line for line in notices), (
                "NOTICEs must target the channel, not the status window"
            )
        finally:
            w.close()

    async def test_help_lists_commands(self, bridge_env):
        bridge, kst_server, irc_port = bridge_env
        bridge.my_locator = "JN97MX"
        client, w = await irc_connect(irc_port)
        try:
            await client.send(f"PRIVMSG {CHANNEL} :!help")
            lines = await client.drain()
            full = " ".join(lines)
            assert "!list" in full
            assert "!scatter" in full
        finally:
            w.close()

    async def test_list_shows_stations_by_distance(self, bridge_env):
        bridge, kst_server, irc_port = bridge_env
        bridge.my_locator = "JN97MX"
        bridge.kst.online_users["G6DDN"] = {
            "loc": "IO83RJ",
            "info": "Ian",
            "away": False,
        }
        client, w = await irc_connect(irc_port)
        try:
            await client.send(f"PRIVMSG {CHANNEL} :!list")
            lines = await client.drain()
            full = " ".join(lines)
            assert "G6DDN" in full
            assert "km" in full
            assert "°" in full
        finally:
            w.close()

    async def test_unknown_command_returns_notice(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await client.send(f"PRIVMSG {CHANNEL} :!bogus")
            # Exactly one NOTICE line for an unknown command.
            line = await client.recv()
            assert "NOTICE" in line and "bogus" in line
        finally:
            w.close()


# ============================================================
# rigctld integration
# ============================================================


class MockRigctld:
    """Minimal rigctld stub: responds to f\n+m\n with fixed freq and mode."""

    def __init__(self, freq_hz: str = "144174000", mode: str = "USB"):
        self._freq_hz = freq_hz
        self._mode = mode
        self._server = None
        self.port: int = 0

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                cmd = line.strip()
                if cmd == b"f":
                    writer.write(f"{self._freq_hz}\n".encode())
                elif cmd == b"m":
                    writer.write(f"{self._mode}\n2700\n".encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            writer.close()


class TestRigctld:
    """Rig state comes from one-shot queries to the rig server (normally
    puskas_logger's built-in 4532 server) at the moment it's needed -- no
    poller, no cache, no persistent connection."""

    async def _live_rig(self, freq_hz: str, mode: str):
        rig = MockRigctld(freq_hz=freq_hz, mode=mode)
        await rig.start()
        orig_host, orig_port = (
            bridge_module.RIG_SERVER_HOST,
            bridge_module.RIG_SERVER_PORT,
        )
        bridge_module.RIG_SERVER_HOST, bridge_module.RIG_SERVER_PORT = (
            "127.0.0.1",
            rig.port,
        )
        return rig, (orig_host, orig_port)

    async def test_sked_includes_qrg_from_live_server(self, bridge_env):
        bridge, kst_server, irc_port = bridge_env
        bridge.my_locator = "JN97MX"
        rig, orig = await self._live_rig("432500000", "CW")
        bridge.kst.online_users["G6DDN"] = {
            "loc": "IO83RJ",
            "info": "Ian",
            "away": False,
        }
        client, w = await irc_connect(irc_port)
        try:
            await client.send("PRIVMSG G6DDN :sked")
            assert await wait_until(
                lambda: "432.500 MHz CW" in " ".join(kst_server.received)
            )
        finally:
            w.close()
            bridge_module.RIG_SERVER_HOST, bridge_module.RIG_SERVER_PORT = orig
            await rig.stop()

    async def test_sked_omits_qrg_when_rig_server_unavailable(self, bridge_env):
        # bridge_env points RIG_SERVER_PORT at a dead port by default.
        bridge, kst_server, irc_port = bridge_env
        bridge.my_locator = "JN97MX"
        bridge.kst.online_users["G6DDN"] = {
            "loc": "IO83RJ",
            "info": "Ian",
            "away": False,
        }
        client, w = await irc_connect(irc_port)
        try:
            await client.send("PRIVMSG G6DDN :sked")
            assert await wait_until(lambda: "sked?" in " ".join(kst_server.received))
            assert "MHz" not in " ".join(kst_server.received)
        finally:
            w.close()

    async def test_help_shows_rig_line_from_live_server(self, bridge_env):
        # !help must reflect the rig's state right now, not a cache.
        bridge, kst_server, irc_port = bridge_env
        rig, orig = await self._live_rig("144174000", "USB")
        client, w = await irc_connect(irc_port)
        try:
            await client.send(f"PRIVMSG {CHANNEL} :!help")
            # recv_until raises TimeoutError if the rig line never appears.
            await client.recv_until("144.174 MHz USB")
        finally:
            w.close()
            bridge_module.RIG_SERVER_HOST, bridge_module.RIG_SERVER_PORT = orig
            await rig.stop()
