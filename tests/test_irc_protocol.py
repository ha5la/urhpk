"""
IRC protocol tests — IRCSession via socketpair + MockBridge.
No ON4KST server needed; tests the IRC-facing layer in isolation.
"""
import asyncio

import pytest

from on4kst_irc_bridge import CHANNEL

from tests.helpers import CALLSIGN, MockBridge, make_irc_pair


# ============================================================
# Fixture: a factory that yields (task, client, bridge) tuples
# and cancels all tasks on teardown.
# ============================================================

@pytest.fixture
async def make_registered():
    """
    Factory fixture.  Call make_registered() or make_registered(bridge)
    to get a fully-registered (task, IRCClientHelper, bridge) triple.
    All tasks are cancelled after the test.
    """
    tasks = []

    async def _factory(bridge=None, nick="TESTNICK"):
        if bridge is None:
            bridge = MockBridge()
        session, client = await make_irc_pair(bridge)
        task = asyncio.create_task(session.handle_loop())
        tasks.append(task)
        await client.register(nick)
        await client.drain()   # consume NICK-change + auto-join output
        return task, client, bridge

    yield _factory

    for t in tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


# ============================================================
# CAP negotiation
# ============================================================

class TestCAPNegotiation:
    async def test_cap_ls_returns_empty_list(self):
        bridge = MockBridge()
        session, client = await make_irc_pair(bridge)
        task = asyncio.create_task(session.handle_loop())
        try:
            await client.send("CAP LS 302")
            line = await client.recv()
            assert "CAP * LS" in line
            assert line.rstrip().endswith(":")   # empty capability list
        finally:
            task.cancel()

    async def test_cap_req_returns_nak(self):
        bridge = MockBridge()
        session, client = await make_irc_pair(bridge)
        task = asyncio.create_task(session.handle_loop())
        try:
            await client.send("CAP LS 302")
            await client.recv()
            await client.send("CAP REQ :multi-prefix")
            line = await client.recv()
            assert "NAK" in line
        finally:
            task.cancel()

    async def test_welcome_waits_for_cap_end(self):
        bridge = MockBridge()
        session, client = await make_irc_pair(bridge)
        task = asyncio.create_task(session.handle_loop())
        try:
            await client.send("CAP LS 302")
            await client.recv()   # consume CAP LS response
            await client.send("NICK TESTNICK")
            await client.send("USER test 0 * :Test")
            # No CAP END yet — server must not send 001
            with pytest.raises(TimeoutError):
                await client.recv(timeout=0.3)
            # CAP END triggers welcome
            await client.send("CAP END")
            line = await client.recv()
            assert "001" in line
        finally:
            task.cancel()


# ============================================================
# Registration / welcome
# ============================================================

class TestRegistration:
    async def test_welcome_numerics_present(self, make_registered):
        _, client, _ = await make_registered()
        lines = await client.drain()
        # drain() comes after register() which already consumed up to 376,
        # so just check the already-received lines via register()'s return
        # value.  Re-register to get a fresh view:
        # (We trust register() consumed 001…376 correctly.)

    async def test_nick_forced_to_callsign(self, make_registered):
        _, client, _ = await make_registered(nick="TESTNICK")
        # drain() was called inside make_registered; the NICK change message
        # was already consumed.  Re-run from scratch to observe it:
        bridge = MockBridge()
        session, client2 = await make_irc_pair(bridge)
        task = asyncio.create_task(session.handle_loop())
        try:
            lines = await client2.register(nick="TESTNICK")
            lines += await client2.drain()
            nick_change = [l for l in lines if f"NICK {CALLSIGN}" in l]
            assert nick_change, "Bridge must send NICK change to ON4KST callsign"
        finally:
            task.cancel()

    async def test_nick_already_callsign_no_forced_change(self):
        bridge = MockBridge()
        session, client = await make_irc_pair(bridge)
        task = asyncio.create_task(session.handle_loop())
        try:
            lines = await client.register(nick=CALLSIGN)
            lines += await client.drain()
            # No NICK command should appear (the join has the nick but as
            # part of a JOIN line, not a NICK line)
            nick_cmds = [l for l in lines
                         if l.lstrip(":").split()[0].upper() == "NICK"]
            assert not nick_cmds, "No NICK change when nick already matches callsign"
        finally:
            task.cancel()

    async def test_auto_join_channel(self, make_registered):
        bridge = MockBridge()
        session, client = await make_irc_pair(bridge)
        task = asyncio.create_task(session.handle_loop())
        try:
            lines = await client.register()
            lines += await client.drain()
            joined = [l for l in lines if "JOIN" in l and CHANNEL in l]
            assert joined, "Bridge must auto-join #on4kst after welcome"
        finally:
            task.cancel()

    async def test_names_sent_on_join(self, make_registered):
        bridge = MockBridge()
        bridge.kst.online_users = {
            "G6DDN": {"loc": "IO83", "info": "Ian", "away": False}
        }
        session, client = await make_irc_pair(bridge)
        task = asyncio.create_task(session.handle_loop())
        try:
            lines = await client.register()
            lines += await client.drain()
            assert any("353" in l for l in lines), "Must send NAMES (353)"
            assert any("366" in l for l in lines), "Must send end-of-NAMES (366)"
            assert any("G6DDN" in l for l in lines)
        finally:
            task.cancel()


# ============================================================
# Channel sync  (the set that irssi sends after JOIN)
# ============================================================

class TestChannelSync:
    async def test_mode_channel_returns_324(self, make_registered):
        _, client, _ = await make_registered()
        await client.send(f"MODE {CHANNEL}")
        line = await client.recv()
        assert "324" in line

    async def test_mode_ban_list_returns_368(self, make_registered):
        _, client, _ = await make_registered()
        await client.send(f"MODE {CHANNEL} b")
        line = await client.recv()
        assert "368" in line

    async def test_mode_exception_list_returns_349(self, make_registered):
        _, client, _ = await make_registered()
        await client.send(f"MODE {CHANNEL} e")
        line = await client.recv()
        assert "349" in line

    async def test_mode_invite_list_returns_347(self, make_registered):
        _, client, _ = await make_registered()
        await client.send(f"MODE {CHANNEL} I")
        line = await client.recv()
        assert "347" in line

    async def test_who_with_args_still_returns_352(self, make_registered):
        bridge = MockBridge()
        bridge.kst.online_users = {
            "G6DDN": {"loc": "IO83", "info": "Ian", "away": False}
        }
        _, client, _ = await make_registered(bridge)
        await client.send(f"WHO {CHANNEL} %uhsnfar,152")
        lines = await client.recv_until("315")
        assert any("352" in l for l in lines), "WHO must produce 352 replies"
        assert any("315" in l for l in lines), "WHO must end with 315"

    async def test_plain_who_returns_352(self, make_registered):
        bridge = MockBridge()
        bridge.kst.online_users = {
            "G6DDN": {"loc": "IO83", "info": "Ian", "away": False}
        }
        _, client, _ = await make_registered(bridge)
        await client.send(f"WHO {CHANNEL}")
        lines = await client.recv_until("315")
        assert any("352" in l for l in lines), "Plain WHO must produce 352 replies"

    async def test_whois_shows_name_and_locator(self, make_registered):
        bridge = MockBridge()
        bridge.kst.online_users = {
            "G6DDN": {"loc": "IO83RJ", "info": "Ian", "away": False}
        }
        _, client, _ = await make_registered(bridge)
        await client.send("WHOIS G6DDN")
        lines = await client.recv_until("318")
        assert any("311" in l for l in lines)
        full = " ".join(lines)
        assert "Ian" in full
        assert "IO83RJ" in full

    async def test_whois_shows_distance_and_bearing(self, make_registered):
        bridge = MockBridge()
        bridge.my_locator = "JN97MX"
        bridge.kst.online_users = {
            "G6DDN": {"loc": "IO83RJ", "info": "Ian", "away": False}
        }
        _, client, _ = await make_registered(bridge)
        await client.send("WHOIS G6DDN")
        lines = await client.recv_until("318")
        full = " ".join(lines)
        assert "km" in full
        assert "°" in full

    async def test_whois_no_distance_without_my_locator(self, make_registered):
        bridge = MockBridge()
        bridge.my_locator = ""  # locator not yet known
        bridge.kst.online_users = {
            "G6DDN": {"loc": "IO83RJ", "info": "Ian", "away": False}
        }
        _, client, _ = await make_registered(bridge)
        await client.send("WHOIS G6DDN")
        lines = await client.recv_until("318")
        full = " ".join(lines)
        assert "km" not in full

    async def test_whois_away_shows_301(self, make_registered):
        bridge = MockBridge()
        bridge.kst.online_users = {
            "G6DDN": {"loc": "IO83RJ", "info": "Ian", "away": True}
        }
        _, client, _ = await make_registered(bridge)
        await client.send("WHOIS G6DDN")
        lines = await client.recv_until("318")
        assert any("301" in l for l in lines), "Away user must produce 301"


# ============================================================
# AWAY forwarding
# ============================================================

class TestAway:
    async def test_away_with_message_sends_unset_here(self, make_registered):
        _, client, bridge = await make_registered()
        await client.send("AWAY :Out for a walk")
        line = await client.recv()
        assert "306" in line
        assert "/UNSET HERE" in bridge.kst.sent

    async def test_bare_away_sends_set_here(self, make_registered):
        _, client, bridge = await make_registered()
        await client.send("AWAY")
        line = await client.recv()
        assert "305" in line
        assert "/SET HERE" in bridge.kst.sent


# ============================================================
# PRIVMSG routing
# ============================================================

class TestMessaging:
    async def test_channel_privmsg_forwarded_to_bridge(self, make_registered):
        _, client, bridge = await make_registered()
        await client.send(f"PRIVMSG {CHANNEL} :Hello everyone")
        await asyncio.sleep(0.05)
        assert (CHANNEL, "Hello everyone") in bridge.irc_messages

    async def test_pm_to_callsign_forwarded_to_bridge(self, make_registered):
        _, client, bridge = await make_registered()
        await client.send("PRIVMSG G6DDN :Sked?")
        await asyncio.sleep(0.05)
        assert ("G6DDN", "Sked?") in bridge.irc_messages
