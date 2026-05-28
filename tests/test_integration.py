"""
Integration tests — full bridge with MockKSTServer and IRCClientHelper.
Covers the end-to-end message flows between ON4KST and IRC.
"""
import asyncio

import pytest

from on4kst_irc_bridge import Bridge, IRCSession, ON4KSTClient
from on4kst_irc_bridge import CHANNEL
from tests.helpers import (
    CALLSIGN, PASSWORD,
    IRCClientHelper, MockKSTServer,
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

    bridge = Bridge(CALLSIGN)

    async def handle_irc(r, w):
        await IRCSession(r, w, bridge).handle_loop()

    irc_server = await asyncio.start_server(handle_irc, "127.0.0.1", 0)
    irc_port   = irc_server.sockets[0].getsockname()[1]

    kst_ref = []

    async def run_kst():
        kst = ON4KSTClient(
            "127.0.0.1", kst_server.port, CALLSIGN, PASSWORD, bridge
        )
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
    # Give fetch_locator + first /SHow USer time to complete
    await asyncio.sleep(0.15)

    yield bridge, kst_server, irc_port

    kst_task.cancel()
    try:
        await asyncio.wait_for(kst_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    irc_server.close()
    await asyncio.sleep(0.1)   # let active sessions drain; skip wait_closed()
    await kst_server.stop()


async def irc_connect(irc_port: int, nick: str = "TESTNICK"):
    """Connect an IRC client, register, and drain auto-join output."""
    r, w = await asyncio.open_connection("127.0.0.1", irc_port)
    client = IRCClientHelper(r, w)
    await client.register(nick)
    await client.drain()
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
            await kst_server.inject(
                f"0712Z G6DDN Ian 2m14> ({CALLSIGN}) Hey, sked?"
            )
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
            await kst_server.inject(
                "0712Z G6DDN Ian 2m14> (DK5DV) See you on 2m!"
            )
            line = await client.recv()
            assert f"PRIVMSG {CHANNEL}" in line
            assert "(DK5DV)" in line
        finally:
            w.close()

    async def test_own_message_not_echoed(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await kst_server.inject(
                f"0712Z {CALLSIGN} HA5LA JN97MX> Testing 1 2 3"
            )
            with pytest.raises(TimeoutError):
                await client.recv(timeout=0.3)
        finally:
            w.close()

    async def test_html_entities_decoded(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            # Entities must be decoded in the message body before forwarding
            await kst_server.inject(
                "0712Z G6DDN Ian 2m14> 6&amp;2m &#9889; sked?"
            )
            line = await client.recv()
            assert "6&2m" in line    # &amp; → &
            assert "⚡" in line      # &#9889; → ⚡
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
            await asyncio.sleep(0.1)
            assert kst_server.was_sent(f"CQ de {CALLSIGN}")
        finally:
            w.close()

    async def test_pm_becomes_cq(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await client.send("PRIVMSG G6DDN :Sked?")
            await asyncio.sleep(0.1)
            assert kst_server.was_sent("/CQ G6DDN Sked?")
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
            await asyncio.sleep(0.1)
            assert kst_server.was_sent("/SET HERE")
        finally:
            w.close()

    async def test_unset_here_on_irc_disconnect(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        w.close()
        await asyncio.sleep(0.2)
        assert kst_server.was_sent("/UNSET HERE")

    async def test_away_command_sends_unset_here(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await client.send("AWAY :Eating dinner")
            await asyncio.sleep(0.1)
            assert kst_server.was_sent("/UNSET HERE")
        finally:
            w.close()

    async def test_back_command_sends_set_here(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            await client.send("AWAY :Gone")
            await asyncio.sleep(0.05)
            await client.send("AWAY")          # bare AWAY = back
            await asyncio.sleep(0.1)
            sent = kst_server.received
            set_here_count = sum(1 for c in sent if "/SET HERE" == c)
            assert set_here_count >= 2         # once on connect, once on back
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
            await client.recv_until("G6DDN")   # wait for the JOIN

            # New user list without G6DDN → G6DDN should PART
            await kst_server.inject("DK5DV            JO30XS Gerd")
            await kst_server.inject("1234Z HA5LA HA5LA JN97MX chat >")
            lines = await client.drain()
            assert any("PART" in l and "G6DDN" in l for l in lines)
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
            "loc": "IO83RJ", "info": "Ian", "away": False
        }
        client, w = await irc_connect(irc_port)
        try:
            pre = len(kst_server.received)
            await client.send("PRIVMSG G6DDN :sked")
            await asyncio.sleep(0.1)
            new_sent = " ".join(kst_server.received[pre:])
            assert "/CQ G6DDN" in new_sent
            assert "sked?" in new_sent
        finally:
            w.close()

    async def test_pm_sked_echoes_notice(self, bridge_env):
        bridge, kst_server, irc_port = bridge_env
        bridge.my_locator = "JN97MX"
        bridge.kst.online_users["G6DDN"] = {
            "loc": "IO83RJ", "info": "Ian", "away": False
        }
        client, w = await irc_connect(irc_port)
        try:
            await client.send("PRIVMSG G6DDN :sked")
            lines = await client.drain()
            notice = next((l for l in lines if "NOTICE" in l), None)
            assert notice is not None, "Bridge must echo a NOTICE after PM sked"
            assert "/CQ G6DDN" in notice
            assert "sked?" in notice
        finally:
            w.close()

    async def test_non_sked_pm_forwarded_unchanged(self, bridge_env):
        _, kst_server, irc_port = bridge_env
        client, w = await irc_connect(irc_port)
        try:
            pre = len(kst_server.received)
            await client.send("PRIVMSG G6DDN :Hello there")
            await asyncio.sleep(0.1)
            new_sent = " ".join(kst_server.received[pre:])
            assert "/CQ G6DDN Hello there" in new_sent
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
            pre = len(kst_server.received)
            await client.send(f"PRIVMSG {CHANNEL} :!help")
            await asyncio.sleep(0.1)
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
            notices = [l for l in lines if "NOTICE" in l]
            assert notices, "!help must produce NOTICE lines"
            assert all(CHANNEL in l for l in notices), \
                "NOTICEs must target the channel, not the status window"
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
            "loc": "IO83RJ", "info": "Ian", "away": False
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
            lines = await client.drain()
            assert any("NOTICE" in l and "bogus" in l for l in lines)
        finally:
            w.close()
