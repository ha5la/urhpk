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

import os
import socket
import struct
import threading

import pytest

from icom_net import (
    CIV_CONTROLLER_ADDR,
    CIV_IC9700_ADDR,
    IcomNetRig,
    bcd_encode_freq,
    civ_data_packet,
    civ_frame,
    control_packet,
    parse_civ_data_packet,
    split_civ_frames,
)
from tests.helpers import wait_until_sync


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


class FakeIcomRadio:
    """Just enough server-side protocol to satisfy IcomNetRig.connect()."""

    def __init__(self):
        self.ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctrl_sock.bind(("127.0.0.1", 0))
        self.civ_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.civ_sock.bind(("127.0.0.1", 0))
        self.control_port = self.ctrl_sock.getsockname()[1]
        self.civ_port = self.civ_sock.getsockname()[1]

        self._stop = threading.Event()
        self._my_ctrl_id = os.urandom(4)
        self._my_civ_id = os.urandom(4)
        self._client_ctrl_id = b"\x00\x00\x00\x00"
        self._client_civ_id = b"\x00\x00\x00\x00"
        self._client_ctrl_addr = None
        self._client_civ_addr = None
        self._tok = os.urandom(6)
        self._civ_inner_seq = 0
        self.civ_opened = threading.Event()
        self._cur_freq = 144_174_000
        self._cur_mode = 0x01  # USB

        self._threads = [
            threading.Thread(target=self._ctrl_loop, daemon=True),
            threading.Thread(target=self._civ_loop, daemon=True),
        ]

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self.ctrl_sock.close()
        self.civ_sock.close()

    def _ctrl_loop(self) -> None:
        self.ctrl_sock.settimeout(0.05)
        while not self._stop.is_set():
            try:
                data, addr = self.ctrl_sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            self._client_ctrl_addr = addr
            self._handle_ctrl(data, addr)

    def _handle_ctrl(self, data: bytes, addr) -> None:
        if len(data) < 6:
            return
        length, ptype = struct.unpack_from("<IH", data, 0)
        if length == 0x10 and ptype == 3:  # are-you-there
            self._client_ctrl_id = bytes(data[8:12])
            self.ctrl_sock.sendto(
                control_packet(4, 0, self._my_ctrl_id, self._client_ctrl_id), addr
            )
        elif length == 0x10 and ptype == 6:  # are-you-ready
            self.ctrl_sock.sendto(data, addr)
        elif length == 0x10:
            pass  # idle
        elif length == 0x80:  # login_packet
            self._tok = data[0x1A:0x1C] + self._tok[2:]
            self.ctrl_sock.sendto(
                _login_response(self._my_ctrl_id, self._client_ctrl_id, self._tok), addr
            )
        elif length == 0x40:  # token_packet
            requesttype = data[0x15]
            self.ctrl_sock.sendto(
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
                self.ctrl_sock.sendto(caps, addr)
        elif length == 0x90:  # conninfo_packet
            self.ctrl_sock.sendto(
                _conninfo_response(self._my_ctrl_id, self._client_ctrl_id), addr
            )

    def _civ_loop(self) -> None:
        self.civ_sock.settimeout(0.05)
        while not self._stop.is_set():
            try:
                data, addr = self.civ_sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            self._client_civ_addr = addr
            self._handle_civ(data, addr)

    def _handle_civ(self, data: bytes, addr) -> None:
        if len(data) < 6:
            return
        length, ptype = struct.unpack_from("<IH", data, 0)
        if length == 0x10 and ptype == 3:  # are-you-there
            self._client_civ_id = bytes(data[8:12])
            self.civ_sock.sendto(
                control_packet(4, 0, self._my_civ_id, self._client_civ_id), addr
            )
        elif length == 0x10 and ptype == 6:  # are-you-ready
            self.civ_sock.sendto(data, addr)
        elif length == 0x16:  # openclose_packet
            magic = data[0x15]
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

    def _send_civ(self, frame: bytes) -> None:
        pkt = civ_data_packet(
            0, self._my_civ_id, self._client_civ_id, self._civ_inner_seq, frame
        )
        self._civ_inner_seq += 1
        if self._client_civ_addr:
            self.civ_sock.sendto(pkt, self._client_civ_addr)

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
            self.civ_sock.sendto(pkt, self._client_civ_addr)


@pytest.fixture
def fake_radio():
    radio = FakeIcomRadio()
    radio.start()
    yield radio
    radio.stop()


def test_connect_and_receive_unsolicited_transceive_update(fake_radio):
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
        rig.connect(timeout=5.0)

        # connect()'s priming read-freq/read-mode queries recover initial state --
        # each answer is its own UDP packet, so wait for both, not just one.
        assert wait_until_sync(
            lambda: rig.freq_hz == 144_174_000 and rig.mode == "USB", timeout=2.0
        )
        assert rig.band == "2M"

        # Now simulate a live front-panel change -- e.g. QSY to 70cm CW --
        # and confirm it arrives with no polling, i.e. purely event-driven.
        fake_radio.push_freq_mode(432_500_000, 0x03)
        assert wait_until_sync(
            lambda: rig.freq_hz == 432_500_000 and rig.mode == "CW", timeout=2.0
        )
        assert rig.band == "70CM"

        assert (432_500_000, "CW", "70CM") in updates
    finally:
        rig.close()
