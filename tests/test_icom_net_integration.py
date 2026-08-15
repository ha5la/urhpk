"""End-to-end test of IcomNetRig.connect() against an in-process fake radio.

Simulates just enough of the real IC-9700's network-control wire protocol
(control-socket login/token/capabilities/conninfo handshake, then a
CI-V-socket open handshake) to drive every branch of connect() the same
way the real hardware would, without needing the radio itself. Verifies
the actual point of this module: a CI-V Transceive frame the "radio"
pushes unsolicited shows up on IcomNetRig.freq_hz/.mode/.band and fires
on_update() with no polling involved.
"""

from __future__ import annotations

import asyncio
import os
import struct

import pytest

from tests.helpers import wait_until
from urhpk.icom_net import (
    CIV_CONTROLLER_ADDR,
    CIV_IC9700_ADDR,
    CIV_PARAM_FILE_SPLIT,
    CIV_PARAM_NTP_SERVER,
    CIV_PARAM_RX_REC_CONDITION,
    CIV_PARAM_TIME,
    IDLE_PERIOD_S,
    IcomNetRig,
    bcd_encode_freq,
    civ_data_packet,
    civ_frame,
    control_packet,
    parse_civ_data_packet,
    split_civ_frames,
)


def _envelope(
    buf: bytearray, length: int, ptype: int, seq: int, sentid: bytes, rcvdid: bytes
) -> None:
    struct.pack_into("<IHH", buf, 0, length, ptype, seq)
    buf[8:12] = sentid
    buf[12:16] = rcvdid


def _login_response(sentid: bytes, rcvdid: bytes, tok: bytes) -> bytes:
    buf = bytearray(0x60)
    _envelope(buf, 0x60, 0, 0, sentid, rcvdid)
    buf[0x14] = 0x02  # requestreply: success
    buf[0x1A:0x20] = tok
    return bytes(
        buf
    )  # error field (0x30) left zero -> not the ff ff ff fe failure marker


def _token_response(
    sentid: bytes, rcvdid: bytes, requesttype: int, tok: bytes
) -> bytes:
    buf = bytearray(0x40)
    _envelope(buf, 0x40, 0, 0, sentid, rcvdid)
    buf[0x14] = 0x02
    buf[0x15] = requesttype
    buf[0x1A:0x20] = tok
    return bytes(buf)  # response field (0x30) left zero -> success


def _capabilities(
    sentid: bytes, rcvdid: bytes, guid: bytes, name: str, civ_addr: int
) -> bytes:
    header = bytearray(0x42)
    _envelope(header, 0x42 + 0x66, 0, 0, sentid, rcvdid)
    struct.pack_into("<H", header, 0x40, 1)
    entry = bytearray(0x66)
    entry[0:16] = guid
    nm = name.encode("ascii")
    entry[0x10 : 0x10 + len(nm)] = nm
    entry[0x52] = civ_addr
    return bytes(header) + bytes(entry)


def _conninfo_response(sentid: bytes, rcvdid: bytes) -> bytes:
    buf = bytearray(0x90)
    _envelope(buf, 0x90, 0, 0, sentid, rcvdid)
    return bytes(buf)


class _FakeEndpoint(asyncio.DatagramProtocol):
    """One of the radio's listening sockets. Unlike the client's channels
    these are unconnected — the radio replies to whoever wrote to it."""

    def __init__(self, handler):
        self._handler = handler
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self._handler(data, addr)


class FakeIcomRadio:
    """Just enough server-side protocol to satisfy IcomNetRig.connect()."""

    def __init__(self):
        self._my_ctrl_id = os.urandom(4)
        self._my_civ_id = os.urandom(4)
        self._client_ctrl_id = b"\x00\x00\x00\x00"
        self._client_civ_id = b"\x00\x00\x00\x00"
        self._client_civ_addr = None
        self._tok = os.urandom(6)
        self._civ_inner_seq = 0
        self.civ_opened = asyncio.Event()
        self.civ_closed = asyncio.Event()
        self.received_civ: list[bytes] = []
        self.token_deregistered = asyncio.Event()
        self.ctrl_disconnected = asyncio.Event()
        self.civ_disconnected = asyncio.Event()
        self._cur_freq = 144_174_000
        self._cur_mode = 0x01  # USB
        # Numbered settings (CI-V 0x1A 0x05), keyed by their two BCD bytes.
        # Values are one byte, or several for a setting like the clock.
        self.params: dict[bytes, int | bytes] = {}
        self._ctrl_transport = None
        self._civ_transport = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._ctrl_transport, _ = await loop.create_datagram_endpoint(
            lambda: _FakeEndpoint(self._handle_ctrl), local_addr=("127.0.0.1", 0)
        )
        self._civ_transport, _ = await loop.create_datagram_endpoint(
            lambda: _FakeEndpoint(self._handle_civ), local_addr=("127.0.0.1", 0)
        )
        self.control_port = self._ctrl_transport.get_extra_info("sockname")[1]
        self.civ_port = self._civ_transport.get_extra_info("sockname")[1]

    def stop(self) -> None:
        """Idempotent: one test kills the radio mid-session, then the fixture
        tears it down again."""
        for transport in (self._ctrl_transport, self._civ_transport):
            if transport is not None:
                transport.close()
        self._ctrl_transport = self._civ_transport = None

    def _send_ctrl(self, data: bytes, addr) -> None:
        if self._ctrl_transport is not None:
            self._ctrl_transport.sendto(data, addr)

    def _handle_ctrl(self, data: bytes, addr) -> None:
        if len(data) < 6:
            return
        length, ptype = struct.unpack_from("<IH", data, 0)
        if length == 0x10 and ptype == 3:  # are-you-there
            self._client_ctrl_id = bytes(data[8:12])
            self._send_ctrl(
                control_packet(4, 0, self._my_ctrl_id, self._client_ctrl_id), addr
            )
        elif length == 0x10 and ptype == 6:  # are-you-ready
            self._send_ctrl(data, addr)
        elif length == 0x10 and ptype == 5:  # disconnect
            self.ctrl_disconnected.set()
        elif length == 0x10:
            pass  # idle
        elif length == 0x80:  # login_packet
            self._tok = data[0x1A:0x1C] + self._tok[2:]
            self._send_ctrl(
                _login_response(self._my_ctrl_id, self._client_ctrl_id, self._tok), addr
            )
        elif length == 0x40:  # token_packet
            requesttype = data[0x15]
            if requesttype == 0x01:
                self.token_deregistered.set()
            self._send_ctrl(
                _token_response(
                    self._my_ctrl_id, self._client_ctrl_id, requesttype, self._tok
                ),
                addr,
            )
            if requesttype == 0x02:
                caps = _capabilities(
                    self._my_ctrl_id,
                    self._client_ctrl_id,
                    os.urandom(16),
                    "IC-9700",
                    CIV_IC9700_ADDR,
                )
                self._send_ctrl(caps, addr)
        elif length == 0x90:  # conninfo_packet
            self._send_ctrl(
                _conninfo_response(self._my_ctrl_id, self._client_ctrl_id), addr
            )

    def _send_civ_to(self, data: bytes, addr) -> None:
        if self._civ_transport is not None:
            self._civ_transport.sendto(data, addr)

    def _handle_civ(self, data: bytes, addr) -> None:
        self._client_civ_addr = addr
        if len(data) < 6:
            return
        length, ptype = struct.unpack_from("<IH", data, 0)
        if length == 0x10 and ptype == 3:  # are-you-there
            self._client_civ_id = bytes(data[8:12])
            self._send_civ_to(
                control_packet(4, 0, self._my_civ_id, self._client_civ_id), addr
            )
        elif length == 0x10 and ptype == 6:  # are-you-ready
            self._send_civ_to(data, addr)
        elif length == 0x10 and ptype == 5:  # disconnect
            self.civ_disconnected.set()
        elif length == 0x16:  # openclose_packet
            magic = data[0x15]
            if magic == 0x00:
                self.civ_closed.set()
            if magic in (0x04, 0x05) and not self.civ_opened.is_set():
                self.civ_opened.set()
                # Just needs to prove data is flowing -- connect()'s own open-wait
                # loop consumes this one directly, it never reaches IcomNetRig's
                # steady-state receive loop. Real initial state recovery happens
                # via the read-freq/read-mode queries handled below, exactly as
                # it would against real hardware.
                self.push_freq_mode(self._cur_freq, self._cur_mode)
        elif length >= 0x15 and data[0x10] == 0xC1:  # civ_data_packet
            payload = parse_civ_data_packet(data)
            if payload:
                for frame in split_civ_frames(payload):
                    self.received_civ.append(frame)
                    if len(frame) >= 3 and frame[2] == 0x03:  # read freq
                        reply = civ_frame(
                            CIV_CONTROLLER_ADDR,
                            CIV_IC9700_ADDR,
                            0x03,
                            bcd_encode_freq(self._cur_freq),
                        )
                        self._send_civ(reply)
                    elif len(frame) >= 3 and frame[2] == 0x04:  # read mode
                        reply = civ_frame(
                            CIV_CONTROLLER_ADDR,
                            CIV_IC9700_ADDR,
                            0x04,
                            bytes([self._cur_mode]),
                        )
                        self._send_civ(reply)
                    elif len(frame) == 6 and frame[2:4] == b"\x1a\x05":  # read setting
                        value = self.params.get(bytes(frame[4:6]))
                        if value is not None:
                            data = bytes([value]) if isinstance(value, int) else value
                            self._send_civ(
                                civ_frame(
                                    CIV_CONTROLLER_ADDR,
                                    CIV_IC9700_ADDR,
                                    0x1A,
                                    bytes([0x05]) + frame[4:6] + data,
                                )
                            )

    def _send_civ(self, frame: bytes) -> None:
        pkt = civ_data_packet(
            0, self._my_civ_id, self._client_civ_id, self._civ_inner_seq, frame
        )
        self._civ_inner_seq += 1
        if self._client_civ_addr:
            self._send_civ_to(pkt, self._client_civ_addr)

    def push_freq_mode(self, hz: int, mode_code: int) -> None:
        """Simulate the radio pushing a CI-V Transceive update -- exactly
        what a real front-panel band/frequency change looks like on the
        wire, unsolicited."""
        self._cur_freq, self._cur_mode = hz, mode_code
        payload = civ_frame(
            CIV_CONTROLLER_ADDR, CIV_IC9700_ADDR, 0x00, bcd_encode_freq(hz)
        )
        payload += civ_frame(
            CIV_CONTROLLER_ADDR, CIV_IC9700_ADDR, 0x01, bytes([mode_code])
        )
        pkt = civ_data_packet(
            0, self._my_civ_id, self._client_civ_id, self._civ_inner_seq, payload
        )
        self._civ_inner_seq += 1
        if self._client_civ_addr:
            self._send_civ_to(pkt, self._client_civ_addr)


@pytest.fixture
async def fake_radio():
    radio = FakeIcomRadio()
    await radio.start()
    yield radio
    radio.stop()


async def test_connect_and_receive_unsolicited_transceive_update(fake_radio):
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    updates = []
    rig.on_update(lambda freq_hz, mode, band: updates.append((freq_hz, mode, band)))

    try:
        await rig.connect(timeout=5.0)

        # connect()'s priming read-freq/read-mode queries recover initial state --
        # each answer is its own UDP packet, so wait for both, not just one.
        assert await wait_until(
            lambda: rig.freq_hz == 144_174_000 and rig.mode == "USB", timeout=2.0
        )
        assert rig.band == "2M"

        # Now simulate a live front-panel change -- e.g. QSY to 70cm CW --
        # and confirm it arrives with no polling, i.e. purely event-driven.
        fake_radio.push_freq_mode(432_500_000, 0x03)
        assert await wait_until(
            lambda: rig.freq_hz == 432_500_000 and rig.mode == "CW", timeout=2.0
        )
        assert rig.band == "70CM"

        assert (432_500_000, "CW", "70CM") in updates
    finally:
        await rig.close()


async def test_the_civ_stream_is_opened_without_waiting_out_an_idle_period(fake_radio):
    # The radio has nothing to say on the CI-V socket until the open request
    # has gone out, so listening before speaking only delays every connect by
    # a whole idle period.
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    connecting = asyncio.create_task(rig.connect(timeout=5.0))
    try:
        assert await wait_until(fake_radio.civ_opened.is_set, timeout=IDLE_PERIOD_S / 2)
        await connecting
    finally:
        await rig.close()


async def test_send_cw_and_stop_cw_reach_the_radio_as_civ_frames(fake_radio):
    # The two rigctld operations puskas_logger uses besides freq/mode reads:
    # CW macro send ('b') and CW abort (0xBB). Each must land on the radio as
    # the exact CI-V frame Hamlib would send.
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    try:
        await rig.connect(timeout=5.0)
        assert await wait_until(lambda: rig.freq_hz is not None, timeout=2.0)

        rig.send_cw("TU")
        rig.stop_cw()

        to_radio = bytes([CIV_IC9700_ADDR, CIV_CONTROLLER_ADDR])
        expected = [
            to_radio + bytes([0x17]) + b"TU",
            to_radio + bytes([0x17, 0xFF]),
        ]
        assert await wait_until(
            lambda: all(f in fake_radio.received_civ for f in expected), timeout=2.0
        )
    finally:
        await rig.close()


async def test_read_param_returns_the_radios_recorder_settings(fake_radio):
    # Nothing in the IC-9700's CI-V command table reports whether the Voice
    # Recorder is running, but its settings are readable -- and those decide
    # whether the segments it writes are usable at all.
    fake_radio.params = {CIV_PARAM_FILE_SPLIT: 0x01, CIV_PARAM_RX_REC_CONDITION: 0x01}
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    try:
        await rig.connect(timeout=5.0)
        assert await rig.read_param(CIV_PARAM_FILE_SPLIT) == 1
        assert await rig.read_param(CIV_PARAM_RX_REC_CONDITION) == 1

        query = bytes([CIV_IC9700_ADDR, CIV_CONTROLLER_ADDR, 0x1A, 0x05, 0x02, 0x44])
        assert query in fake_radio.received_civ
    finally:
        await rig.close()


async def test_read_clock_returns_the_radios_hours_and_minutes(fake_radio):
    # The clock is the one setting whose reply is two bytes rather than one,
    # which is why it does not go through read_param.
    fake_radio.params = {CIV_PARAM_TIME: bytes([0x18, 0x05])}
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    try:
        await rig.connect(timeout=5.0)
        assert await rig.read_clock() == (18, 5)
    finally:
        await rig.close()


async def test_read_ntp_server_and_local_addr(fake_radio):
    # The address the radio must be pointed at is the one this end of the
    # CI-V socket actually has -- read from the socket, never configured.
    fake_radio.params = {CIV_PARAM_NTP_SERVER: b"time.nist.gov".ljust(64, b" ")}
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    try:
        await rig.connect(timeout=5.0)
        assert await rig.read_ntp_server() == "time.nist.gov"
        assert rig.local_addr == "127.0.0.1"
    finally:
        await rig.close()


async def test_read_param_gives_up_rather_than_hanging_on_a_silent_radio(fake_radio):
    # A radio that answers nothing must not wedge the check -- it runs on the
    # UI's own event loop.
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    try:
        await rig.connect(timeout=5.0)
        assert await rig.read_param(CIV_PARAM_FILE_SPLIT, timeout=0.2) is None
        # and the one-shot listener is gone again, not left accumulating
        assert not any(
            "_read_setting" in getattr(cb, "__qualname__", "")
            for cb in rig._frame_listeners
        )
    finally:
        await rig.close()


async def test_on_civ_frame_sees_raw_inbound_frames(fake_radio):
    # ACK frames (FB/FA) and replies this client doesn't interpret itself
    # must still be observable -- the logger's clock-sync feedback needs to
    # know whether the radio accepted a set command.
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    frames = []
    rig.on_civ_frame(frames.append)
    try:
        await rig.connect(timeout=5.0)
        # connect()'s priming read-freq query is answered by the fake radio --
        # that reply must show up raw, addressing bytes and all.
        freq_reply = bytes([CIV_CONTROLLER_ADDR, CIV_IC9700_ADDR, 0x03])
        assert await wait_until(
            lambda: any(f.startswith(freq_reply) for f in frames), timeout=2.0
        )
    finally:
        await rig.close()


async def test_close_says_goodbye_on_both_sockets(fake_radio):
    # Deregistering the token alone is not the whole goodbye: without a
    # disconnect (0x05) on each socket the radio keeps the session on its
    # books and goes on streaming to the dead sockets -- measured at ~50
    # packets/s against the real IC-9700, for minutes, while refusing new
    # sessions. wfview sends stream-close, then disconnect, on every socket
    # it opened.
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    await rig.connect(timeout=5.0)
    await rig.close()
    async with asyncio.timeout(2.0):
        await fake_radio.civ_closed.wait()
        await fake_radio.civ_disconnected.wait()
        await fake_radio.ctrl_disconnected.wait()


async def test_close_deregisters_the_session_token(fake_radio):
    # Without this the radio holds the abandoned session for tens of
    # seconds (observed on real hardware) and refuses the next connect --
    # fatal for a logger restart mid-round.
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    await rig.connect(timeout=5.0)
    assert not fake_radio.token_deregistered.is_set()
    await rig.close()
    assert fake_radio.token_deregistered.is_set()


async def test_last_rx_age_is_fresh_while_connected_and_grows_when_radio_dies(
    fake_radio,
):
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    try:
        await rig.connect(timeout=5.0)
        # connect() only returns once CI-V data has been seen, so the age
        # must already be finite and recent.
        assert rig.last_rx_age() < 2.0
        fake_radio.stop()
        assert await wait_until(lambda: rig.last_rx_age() > 0.5, timeout=5.0)
    finally:
        await rig.close()


async def test_close_cancels_the_meter_poller_before_tearing_down_sockets(fake_radio):
    # The meter poller sends a burst of four queries per cycle with no await
    # between them, so a cancel that is not awaited leaves it able to finish
    # that burst while close() is already sending its goodbye and
    # token-deregister packets -- putting meter queries on the wire *after*
    # the disconnect, which is exactly what leaves the radio refusing the next
    # session. close() must await it, which it only does for tasks registered
    # in _tasks.
    rig = IcomNetRig(
        "127.0.0.1",
        "testuser",
        "testpass",
        control_port=fake_radio.control_port,
        civ_port=fake_radio.civ_port,
    )
    await rig.connect(timeout=5.0)
    rig.enable_meters(interval=0.05)
    meter_task = rig._meter_task
    assert meter_task in rig._tasks
    await rig.close()
    assert meter_task.done()
