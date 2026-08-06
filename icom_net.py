#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Icom Network CI-V Client
=========================
Direct Ethernet control of an Icom radio (IC-9700 etc.) that exposes the
network-remote-control protocol used by RS-BA1 / wfview, bypassing rigctld
and its polling model entirely. Once connected, CI-V Transceive frames
(frequency/mode changes) arrive unsolicited over the wire the instant they
happen on the radio — no polling loop needed.

Protocol reverse-engineered from wfview (github.com/eliggett/wfview,
mirror of gitlab.com/eliggett/wfview) and kappanhang
(github.com/nonoo/kappanhang); see docstrings below for citations.
Pure stdlib, no external dependencies.

Prerequisites (set on the radio itself):
  SET > Network > Network Control: ON
  SET > Network > Network User1/2: a username + password
  SET > Connectors > CI-V > CI-V Transceive: ON  (for unsolicited updates)
  ~/.netrc must contain:
    machine <radio IP> login <username> password <password>

Usage:
    uv run icom_net.py <radio-ip>
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone

# ============================================================
# Credential scrambling
#
# Not a real cipher -- a fixed substitution table keyed by
# (character_value + position_index), wrapping into the printable-ASCII
# range. Byte-identical in wfview (include/icomudpbase.h) and kappanhang
# (passcode.go, credited there to W6EL).
# ============================================================

_SEQUENCE = {
    32: 0x47, 33: 0x5D, 34: 0x4C, 35: 0x42, 36: 0x66, 37: 0x20, 38: 0x23, 39: 0x46,
    40: 0x4E, 41: 0x57, 42: 0x45, 43: 0x3D, 44: 0x67, 45: 0x76, 46: 0x60, 47: 0x41,
    48: 0x62, 49: 0x39, 50: 0x59, 51: 0x2D, 52: 0x68, 53: 0x7E, 54: 0x7C, 55: 0x65,
    56: 0x7D, 57: 0x49, 58: 0x29, 59: 0x72, 60: 0x73, 61: 0x78, 62: 0x21, 63: 0x6E,
    64: 0x5A, 65: 0x5E, 66: 0x4A, 67: 0x3E, 68: 0x71, 69: 0x2C, 70: 0x2A, 71: 0x54,
    72: 0x3C, 73: 0x3A, 74: 0x63, 75: 0x4F, 76: 0x43, 77: 0x75, 78: 0x27, 79: 0x79,
    80: 0x5B, 81: 0x35, 82: 0x70, 83: 0x48, 84: 0x6B, 85: 0x56, 86: 0x6F, 87: 0x34,
    88: 0x32, 89: 0x6C, 90: 0x30, 91: 0x61, 92: 0x6D, 93: 0x7B, 94: 0x2F, 95: 0x4B,
    96: 0x64, 97: 0x38, 98: 0x2B, 99: 0x2E, 100: 0x50, 101: 0x40, 102: 0x3F, 103: 0x55,
    104: 0x33, 105: 0x37, 106: 0x25, 107: 0x77, 108: 0x24, 109: 0x26, 110: 0x74, 111: 0x6A,
    112: 0x28, 113: 0x53, 114: 0x4D, 115: 0x69, 116: 0x22, 117: 0x5C, 118: 0x44, 119: 0x31,
    120: 0x36, 121: 0x58, 122: 0x3B, 123: 0x7A, 124: 0x51, 125: 0x5F, 126: 0x52,
}  # fmt: skip


def passcode(s: str) -> bytes:
    """Scramble a username/password into the 16-byte field Icom's login/
    conninfo packets expect. Same algorithm both places on the wire."""
    out = bytearray(16)
    b = s.encode("ascii")
    for i in range(min(len(b), 16)):
        p = b[i] + i
        if p > 126:
            p = 32 + p % 127
        out[i] = _SEQUENCE[p]
    return bytes(out)


# ============================================================
# BCD frequency encode/decode
#
# Standard Icom CI-V 5-byte little-endian BCD, each byte two decimal
# digits, least-significant digit pair first (byte 0 = 1s/10s of Hz).
# ============================================================


def bcd_encode_freq(hz: int) -> bytes:
    digits = f"{hz:010d}"[::-1]  # 10 digits, least significant first
    return bytes(int(digits[i + 1] + digits[i], 16) for i in range(0, 10, 2))


def bcd_decode_freq(data: bytes) -> int:
    if len(data) != 5:
        raise ValueError(f"expected 5 BCD bytes, got {len(data)}")
    digits = ""
    for byte in data:
        digits = f"{(byte >> 4) & 0x0F}{byte & 0x0F}" + digits
    return int(digits)


# ============================================================
# CI-V frame framing:  FE FE <to> <from> <cmd> [<data...>] FD
# ============================================================

CIV_CONTROLLER_ADDR = 0xE0
CIV_IC9700_ADDR = 0xA2


def civ_frame(to_addr: int, from_addr: int, cmd: int, data: bytes = b"") -> bytes:
    return bytes([0xFE, 0xFE, to_addr, from_addr, cmd]) + data + bytes([0xFD])


def split_civ_frames(buf: bytes) -> list[bytes]:
    """Split a byte stream that may contain several concatenated
    FE FE ... FD CI-V frames back-to-back into individual frames
    (each returned without the FE FE prefix or FD suffix)."""
    frames = []
    i = 0
    n = len(buf)
    while i < n - 1:
        if buf[i] == 0xFE and buf[i + 1] == 0xFE:
            end = buf.find(0xFD, i + 2)
            if end == -1:
                break
            frames.append(buf[i + 2 : end])
            i = end + 1
        else:
            i += 1
    return frames


def _mode_str(code: int) -> str:
    return {
        0x00: "LSB",
        0x01: "USB",
        0x02: "AM",
        0x03: "CW",
        0x04: "RTTY",
        0x05: "FM",
        0x06: "WFM",
        0x07: "CW-R",
        0x08: "RTTY-R",
        0x17: "DV",
    }.get(code, f"0x{code:02x}")


def parse_civ_update(frame: bytes) -> tuple[str, object] | None:
    """Parse one CI-V frame body (as returned by split_civ_frames, i.e. sans
    FE FE/FD) into ('freq', hz) or ('mode', mode_str), or None if it's not
    a frequency/mode frame we care about. Handles both unsolicited
    transceive frames (cmd 0x00 freq, 0x01 mode) and query-reply frames
    (cmd 0x03 freq, 0x04 mode) -- same payload layout either way.
    """
    if len(frame) < 3:
        return None
    cmd = frame[2]
    payload = frame[3:]
    if cmd in (0x00, 0x03) and len(payload) >= 5:
        return ("freq", bcd_decode_freq(payload[:5]))
    if cmd in (0x01, 0x04) and len(payload) >= 1:
        return ("mode", _mode_str(payload[0]))
    return None


def band_from_hz(hz: int) -> str:
    mhz = hz / 1_000_000
    if mhz < 300:
        return "2M"
    if mhz < 1000:
        return "70CM"
    return "23CM"


# ============================================================
# CW keying + clock -- the remaining CI-V commands puskas_logger needs
# to drop rigctld entirely. Byte layouts transcribed from Hamlib's
# icom_send_morse/icom_stop_morse/icom_set_clock (rigs/icom/icom.c) and
# the IC-9700's parameter numbers (ic9700_clock_cmds in rigs/icom/ic7300.c),
# the exact code paths rigctld's 'b' / 0xBB / \set_clock commands take.
# ============================================================

CIV_CMD_SEND_CW = 0x17
CIV_CW_STOP = 0xFF  # as the message payload: abort the in-progress message
CIV_CW_MAX_CHARS = 30
CIV_CMD_SET_PARAM = 0x1A  # with subcommand 0x05: numbered radio setting
CIV_PARAM_SUBCMD = 0x05
CIV_PARAM_DATE = bytes([0x01, 0x79])
CIV_PARAM_TIME = bytes([0x01, 0x80])
CIV_PARAM_UTC_OFFSET = bytes([0x01, 0x84])


def _bcd2(v: int) -> int:
    return ((v // 10) << 4) | (v % 10)


def civ_cw_payload(message: str) -> bytes:
    return message[:CIV_CW_MAX_CHARS].encode("ascii")


def civ_clock_payloads(now: datetime) -> list[bytes]:
    """Data payloads (for CI-V command 0x1A) that set the radio's clock.

    Always sets UTC (offset 0000), matching the logger's existing
    `\\set_clock ...+00:00` rigctld usage; the IC-9700 ignores seconds.
    Hamlib's own send order: UTC offset, time, date.
    """
    now = now.astimezone(timezone.utc)
    sub = bytes([CIV_PARAM_SUBCMD])
    return [
        sub + CIV_PARAM_UTC_OFFSET + bytes([0x00, 0x00, 0x00]),
        sub + CIV_PARAM_TIME + bytes([_bcd2(now.hour), _bcd2(now.minute)]),
        sub
        + CIV_PARAM_DATE
        + bytes(
            [
                _bcd2(now.year // 100),
                _bcd2(now.year % 100),
                _bcd2(now.month),
                _bcd2(now.day),
            ]
        ),
    ]


# ============================================================
# Scope (spectrum waterfall) frames
#
# Not a separate socket/port -- confirmed against real hardware that the
# IC-9700's own SET > Network menu only exposes three LAN ports (Control/
# CI-V/Audio; ScopeLANPort=50004 in wfview's client config is a vestige of
# its Yaesu support, never used for Icom radios). Scope sweeps are CI-V
# command 0x27 frames on the same CI-V socket already used for freq/mode,
# turned on with two ordinary CI-V "set" commands (see enable_scope below)
# rather than any connection-level negotiation.
# ============================================================

CIV_CMD_SCOPE = 0x27
CIV_SCOPE_ON_OFF = 0x10
CIV_SCOPE_DATA_OUTPUT = 0x11
SCOPE_MODE_CENTER = 0x00


def _bcd_byte(b: int) -> int:
    return (b >> 4) * 10 + (b & 0x0F)


def parse_scope_frame(frame: bytes) -> dict | None:
    """Parse one CI-V scope-wave-data frame body (cmd 0x27, sub 0x00 0x00;
    frame is as returned by split_civ_frames, i.e. sans FE FE/FD). Always
    returns 'sequence'/'sequence_max'/'pixels' (this frame's own slice);
    the first frame of a sweep (sequence==1) additionally carries
    'start_hz'/'end_hz'/'out_of_range'. None if not a scope-wave frame.

    Byte layout confirmed directly against a real IC-9700 packet capture
    (not just from source reading): frame[7]=mode, frame[8:13]/[13:18]=
    start/end frequency as 5-byte BCD, frame[18]=out-of-range flag,
    frame[19:]=pixel bytes -- decoding a real captured frame with this
    layout gave a sane 145.11-146.11 MHz sweep (2M band, 1 MHz span); an
    earlier reading with oor before the frequency fields instead of after
    decoded to a nonsensical ~1.45 MHz "center" and was wrong.
    """
    if (
        len(frame) < 7
        or frame[2] != CIV_CMD_SCOPE
        or frame[3] != 0x00
        or frame[4] != 0x00
    ):
        return None
    sequence = _bcd_byte(frame[5])
    sequence_max = _bcd_byte(frame[6])
    if sequence == 1:
        if len(frame) < 19:
            return None
        mode = frame[7]
        raw_start = bcd_decode_freq(frame[8:13])
        raw_end = bcd_decode_freq(frame[13:18])
        out_of_range = bool(frame[18])
        if mode == SCOPE_MODE_CENTER:
            # Transmitted fields are center frequency and half-span, not edges.
            start_hz = raw_start - raw_end
            end_hz = start_hz + 2 * raw_end
        else:
            start_hz, end_hz = raw_start, raw_end
        return {
            "sequence": sequence,
            "sequence_max": sequence_max,
            "mode": mode,
            "out_of_range": out_of_range,
            "start_hz": start_hz,
            "end_hz": end_hz,
            "pixels": frame[19:],
        }
    return {"sequence": sequence, "sequence_max": sequence_max, "pixels": frame[7:]}


# ============================================================
# Wire packet layer
#
# All packets share a 16-byte outer envelope (len/type/seq LE, then
# sentid/rcvdid -- 4 opaque bytes each, echoed verbatim, never
# interpreted numerically). Byte offsets below are transcribed directly
# from wfview's include/packettypes.h (struct-exact, cross-checked
# against icomudphandler.cpp's actual field assignments) and kappanhang's
# independent Go reimplementation. Fields are BE/LE as noted per field --
# this protocol mixes both within one packet, there is no single blanket
# endianness.
# ============================================================

CONTROL_PORT = 50001
CIV_PORT = 50002

PT_RETRANSMIT = 0x01
PT_ARE_YOU_THERE = 0x03
PT_I_AM_HERE = 0x04
PT_ARE_YOU_READY = 0x06
PT_PING = 0x07


def _envelope(
    buf: bytearray, length: int, ptype: int, seq: int, sentid: bytes, rcvdid: bytes
) -> None:
    struct.pack_into("<IHH", buf, 0, length, ptype, seq)
    buf[8:12] = sentid
    buf[12:16] = rcvdid


def control_packet(ptype: int, seq: int, sentid: bytes, rcvdid: bytes) -> bytes:
    buf = bytearray(0x10)
    _envelope(buf, 0x10, ptype, seq, sentid, rcvdid)
    return bytes(buf)


def login_packet(
    seq: int,
    sentid: bytes,
    rcvdid: bytes,
    innerseq: int,
    tokrequest: bytes,
    username: str,
    password: str,
    client_name: str,
) -> bytes:
    buf = bytearray(0x80)
    _envelope(buf, 0x80, 0x00, seq, sentid, rcvdid)
    struct.pack_into(">I", buf, 0x10, 0x70)
    buf[0x14] = 0x01
    buf[0x15] = 0x00
    struct.pack_into(">H", buf, 0x16, innerseq)
    buf[0x1A:0x1C] = tokrequest
    name = client_name.encode("ascii")[:16]
    buf[0x40:0x50] = passcode(username)
    buf[0x50:0x60] = passcode(password)
    buf[0x60 : 0x60 + len(name)] = name
    return bytes(buf)


def parse_login_response(data: bytes) -> dict:
    return {
        "ok": len(data) >= 0x34 and data[0x30:0x34] != b"\xff\xff\xff\xfe",
        "tok": data[0x1A:0x20],  # tokrequest(2)+token(4), echoed verbatim from here on
    }


def token_packet(
    seq: int, sentid: bytes, rcvdid: bytes, innerseq: int, requesttype: int, tok: bytes
) -> bytes:
    buf = bytearray(0x40)
    _envelope(buf, 0x40, 0x00, seq, sentid, rcvdid)
    struct.pack_into(">I", buf, 0x10, 0x30)
    buf[0x14] = 0x01
    buf[0x15] = requesttype
    struct.pack_into(">H", buf, 0x16, innerseq)
    buf[0x1A:0x20] = tok
    struct.pack_into(">H", buf, 0x24, 0x0798)
    return bytes(buf)


def parse_token_response(data: bytes) -> bool:
    """True if the radio accepted the token_packet request."""
    return len(data) >= 0x40 and data[0x30:0x34] == b"\x00\x00\x00\x00"


def parse_capabilities(data: bytes) -> list[dict]:
    """A capabilities_packet is pushed by the radio, unprompted, right
    after the register (requesttype=0x02) token exchange. Recognize one
    by length: header (0x42) + N * one 0x66-byte radio_cap_packet entry."""
    radios = []
    off = 0x42
    while off + 0x66 <= len(data):
        entry = data[off : off + 0x66]
        radios.append(
            {
                "guid": entry[0:16],
                "name": entry[0x10:0x30].rstrip(b"\x00").decode("ascii", "replace"),
                "civ_addr": entry[0x52],
            }
        )
        off += 0x66
    return radios


def is_capabilities_packet(data: bytes) -> bool:
    return len(data) >= 0x42 + 0x66 and (len(data) - 0x42) % 0x66 == 0


def conninfo_packet(
    seq: int,
    sentid: bytes,
    rcvdid: bytes,
    innerseq: int,
    tok: bytes,
    guid: bytes,
    radio_name: str,
    username: str,
    civ_port: int,
) -> bytes:
    """Stream-open request: registers our local civ_port with the radio so
    it starts sending CI-V-over-UDP traffic there. Audio is intentionally
    disabled (rxenable=txenable=0) -- this client only wants CI-V; audio
    streaming is a separate future step."""
    buf = bytearray(0x90)
    _envelope(buf, 0x90, 0x00, seq, sentid, rcvdid)
    struct.pack_into(">I", buf, 0x10, 0x80)
    buf[0x14] = 0x01
    buf[0x15] = 0x03
    struct.pack_into(">H", buf, 0x16, innerseq)
    buf[0x1A:0x20] = tok
    buf[0x20:0x30] = guid[:16].ljust(16, b"\x00")
    name = radio_name.encode("ascii", "replace")[:32]
    buf[0x40 : 0x40 + len(name)] = name
    buf[0x60:0x70] = passcode(username)
    buf[0x70] = 0  # rxenable
    buf[0x71] = 0  # txenable
    struct.pack_into(">I", buf, 0x7C, civ_port)
    struct.pack_into(">I", buf, 0x80, 0)  # audioport: unused, audio disabled
    buf[0x88] = 1  # convert
    return bytes(buf)


def openclose_packet(
    seq: int, sentid: bytes, rcvdid: bytes, inner_seq: int, magic: int
) -> bytes:
    buf = bytearray(0x16)
    _envelope(buf, 0x16, 0x00, seq, sentid, rcvdid)
    struct.pack_into("<H", buf, 0x10, 0x01C0)
    struct.pack_into(">H", buf, 0x13, inner_seq)
    buf[0x15] = magic
    return bytes(buf)


def civ_data_packet(
    seq: int, sentid: bytes, rcvdid: bytes, inner_seq: int, payload: bytes
) -> bytes:
    buf = bytearray(0x15 + len(payload))
    _envelope(buf, len(buf), 0x00, seq, sentid, rcvdid)
    buf[0x10] = 0xC1
    struct.pack_into("<H", buf, 0x11, len(payload))
    struct.pack_into(">H", buf, 0x13, inner_seq)
    buf[0x15:] = payload
    return bytes(buf)


def parse_civ_data_packet(data: bytes) -> bytes | None:
    if len(data) < 0x15 or data[0x10] != 0xC1:
        return None
    datalen = struct.unpack_from("<H", data, 0x11)[0]
    if len(data) - 0x15 != datalen:
        return None
    return data[0x15:]


def is_ping_request(data: bytes) -> bool:
    return (
        len(data) == 0x15
        and struct.unpack_from("<H", data, 0x04)[0] == PT_PING
        and data[0x10] == 0x00
    )


def ping_reply(seq: int, sentid: bytes, rcvdid: bytes, echo: bytes) -> bytes:
    buf = bytearray(0x15)
    _envelope(buf, 0x15, PT_PING, seq, sentid, rcvdid)
    buf[0x10] = 0x01
    buf[0x11:0x15] = echo
    return bytes(buf)


# ============================================================
# Connection
# ============================================================

IDLE_PERIOD_S = 0.1
TOKEN_RENEWAL_S = 60.0
CIV_STALE_S = 2.0
CLIENT_NAME = "urhpk"


class IcomNetError(Exception):
    pass


def _rendezvous(
    sock: socket.socket, addr: tuple[str, int], local_id: bytes, timeout: float
) -> bytes:
    """Are-you-there / I-am-here / are-you-ready handshake, shared by the
    control socket and the CI-V socket (each gets its own fresh local_id /
    remote_id pair). Returns the radio's remote_id for this socket."""
    deadline = time.monotonic() + timeout
    hello = control_packet(PT_ARE_YOU_THERE, 0, local_id, b"\x00\x00\x00\x00")
    remote_id = None
    while remote_id is None:
        if time.monotonic() > deadline:
            raise IcomNetError(f"no response from {addr} (are-you-there)")
        sock.sendto(hello, addr)
        try:
            sock.settimeout(0.5)
            data, _ = sock.recvfrom(2048)
        except TimeoutError:
            continue
        if (
            len(data) >= 0x10
            and struct.unpack_from("<H", data, 0x04)[0] == PT_I_AM_HERE
        ):
            remote_id = bytes(data[8:12])

    ready = control_packet(PT_ARE_YOU_READY, 1, local_id, remote_id)
    while True:
        if time.monotonic() > deadline:
            raise IcomNetError(f"no response from {addr} (are-you-ready)")
        sock.sendto(ready, addr)
        try:
            sock.settimeout(0.5)
            data, _ = sock.recvfrom(2048)
        except TimeoutError:
            continue
        if (
            len(data) >= 0x10
            and struct.unpack_from("<H", data, 0x04)[0] == PT_ARE_YOU_READY
        ):
            return remote_id


def _local_udp_port() -> int:
    """OS-assigned free UDP port: bind a throwaway socket, read the port,
    close it, and hand the number back to the caller to reuse -- the same
    trick wfview itself uses, since the conninfo request needs to name the
    civ_port *before* the real CI-V socket is opened on it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class IcomNetRig:
    """Direct-Ethernet Icom CI-V client. connect() performs the full
    login/token/conninfo/civ-open handshake; after that, freq_hz/mode/band
    update themselves the instant the radio pushes a CI-V Transceive frame
    -- no polling required."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        civ_addr: int = CIV_IC9700_ADDR,
        control_port: int = CONTROL_PORT,
        civ_port: int = CIV_PORT,
    ):
        self.host = host
        self._username = username
        self._password = password
        self._civ_addr = civ_addr
        self._radio_control_port = control_port
        self._radio_civ_port = civ_port

        # RLock: _apply_update holds this while reading self.band, whose own
        # getter re-acquires the same lock -- a plain Lock would self-deadlock.
        self._lock = threading.RLock()
        self.freq_hz: int | None = None
        self.mode: str | None = None
        self._listeners: list = []
        self._frame_listeners: list = []

        self.scope_start_hz: int | None = None
        self.scope_end_hz: int | None = None
        self.scope_pixels: bytes | None = None
        self._scope_listeners: list = []
        self._scope_pending_range: tuple[int, int] | None = None
        self._scope_buf = bytearray()

        self._stop = threading.Event()
        self._ctrl_sock: socket.socket | None = None
        self._civ_sock: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    @property
    def band(self) -> str | None:
        with self._lock:
            return band_from_hz(self.freq_hz) if self.freq_hz is not None else None

    def on_update(self, callback) -> None:
        """callback(freq_hz, mode, band) fires whenever either changes."""
        self._listeners.append(callback)

    def _apply_update(self, kind: str, value) -> None:
        with self._lock:
            if kind == "freq":
                if value == self.freq_hz:
                    return
                self.freq_hz = value
            else:
                if value == self.mode:
                    return
                self.mode = value
            freq_hz, mode, band = self.freq_hz, self.mode, self.band
        for cb in self._listeners:
            try:
                cb(freq_hz, mode, band)
            except Exception:
                pass

    def on_civ_frame(self, callback) -> None:
        """callback(frame: bytes) fires for every inbound CI-V frame (sans
        FE FE/FD), before any interpretation -- lets callers observe ACKs
        (FB ok / FA rejected) and replies this client doesn't parse itself."""
        self._frame_listeners.append(callback)

    def send_cw(self, message: str) -> None:
        """Key the radio's own CW message sender (CI-V 0x17) -- the same
        command rigctld's 'b' (send_morse) issues. Truncated to the radio's
        30-char limit, exactly as Hamlib's icom_send_morse does."""
        self._send_civ_command(CIV_CMD_SEND_CW, civ_cw_payload(message))

    def stop_cw(self) -> None:
        """Abort an in-progress CW message (0x17 with payload 0xFF) -- the
        same command rigctld's 0xBB (stop_morse) issues."""
        self._send_civ_command(CIV_CMD_SEND_CW, bytes([CIV_CW_STOP]))

    def set_clock(self, now: datetime) -> None:
        """Set the radio's clock + UTC offset -- what rigctld's \\set_clock
        does. The IC-9700 ignores seconds, so callers should fire this on a
        minute boundary (as puskas_logger's Alt+T already does)."""
        for payload in civ_clock_payloads(now):
            self._send_civ_command(CIV_CMD_SET_PARAM, payload)

    def on_scope(self, callback) -> None:
        """callback(start_hz, end_hz, pixels: bytes) fires on each complete
        scope sweep. pixels is one raw byte per bin, 0-160 (Icom's own linear
        scope unit, not dBm) -- already a ready-to-colormap waterfall row."""
        self._scope_listeners.append(callback)

    def enable_scope(self) -> None:
        """Turn on the radio's scope sweep + unsolicited data output (CI-V
        0x27 0x10/0x11). Off by default -- a much heavier stream than plain
        freq/mode transceive frames, so only requested if the caller wants
        it, unlike connect()'s automatic freq/mode priming."""
        self._send_civ_command(CIV_CMD_SCOPE, bytes([CIV_SCOPE_ON_OFF, 0x01]))
        self._send_civ_command(CIV_CMD_SCOPE, bytes([CIV_SCOPE_DATA_OUTPUT, 0x01]))

    def _apply_scope_frame(self, parsed: dict) -> None:
        if parsed["sequence"] == 1:
            self._scope_pending_range = (parsed["start_hz"], parsed["end_hz"])
            self._scope_buf = bytearray(parsed["pixels"])
        elif self._scope_pending_range is not None:
            self._scope_buf += parsed["pixels"]
        else:
            return  # sequence 1 of this sweep was lost -- nothing usable yet
        if parsed["sequence"] != parsed["sequence_max"]:
            return
        start_hz, end_hz = self._scope_pending_range
        pixels = bytes(self._scope_buf)
        with self._lock:
            self.scope_start_hz, self.scope_end_hz, self.scope_pixels = (
                start_hz,
                end_hz,
                pixels,
            )
        self._scope_pending_range = None
        self._scope_buf = bytearray()
        for cb in self._scope_listeners:
            try:
                cb(start_hz, end_hz, pixels)
            except Exception:
                pass

    def connect(self, timeout: float = 5.0) -> None:
        """On any failure partway through, log out and close sockets rather
        than leaving a half-registered session on the radio -- confirmed
        against real hardware that repeated failed connection attempts
        without this left the radio unable to complete a login/conninfo
        handshake at all until the abandoned sessions aged out on their own."""
        try:
            self._connect_impl(timeout)
        except BaseException:
            self._best_effort_logout()
            raise

    def _best_effort_logout(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        if getattr(self, "_tok", None) is not None and self._ctrl_sock is not None:
            try:
                pkt = token_packet(
                    getattr(self, "_ctrl_seq", 100),
                    self._ctrl_local_id,
                    self._ctrl_remote_id,
                    getattr(self, "_auth_seq", 100),
                    0x01,  # deregister
                    self._tok,
                )
                self._ctrl_sock.sendto(pkt, self._ctrl_addr)
            except OSError:
                pass
        if self._civ_sock is not None:
            self._civ_sock.close()
            self._civ_sock = None
        if self._ctrl_sock is not None:
            self._ctrl_sock.close()
            self._ctrl_sock = None

    def _connect_impl(self, timeout: float = 5.0) -> None:
        self._ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ctrl_addr = (self.host, self._radio_control_port)
        ctrl_local_id = os.urandom(4)
        ctrl_remote_id = _rendezvous(self._ctrl_sock, ctrl_addr, ctrl_local_id, timeout)
        # Stashed early (not just at the end) so _best_effort_logout can send a
        # deregister if a later step in this method fails.
        self._ctrl_local_id, self._ctrl_remote_id, self._ctrl_addr = (
            ctrl_local_id,
            ctrl_remote_id,
            ctrl_addr,
        )

        auth_seq = 1
        # The are-you-there(seq=0)/are-you-ready(seq=1) handshake is untracked and
        # lives in its own namespace -- confirmed against real hardware, which
        # requested a retransmit of seq=1 when the first tracked send (login) here
        # started at 2. The tracked-send counter starts fresh at 1.
        ctrl_seq = 1

        tokrequest = os.urandom(2)
        pkt = login_packet(
            ctrl_seq,
            ctrl_local_id,
            ctrl_remote_id,
            auth_seq,
            tokrequest,
            self._username,
            self._password,
            CLIENT_NAME,
        )
        ctrl_seq += 1
        auth_seq += 1
        self._ctrl_sock.sendto(pkt, ctrl_addr)
        resp = self._recv_len(self._ctrl_sock, 0x60, timeout)
        login = parse_login_response(resp)
        if not login["ok"]:
            raise IcomNetError(
                "login rejected -- check username/password on the radio's SET > Network menu"
            )
        tok = self._tok = login["tok"]

        pkt = token_packet(ctrl_seq, ctrl_local_id, ctrl_remote_id, auth_seq, 0x02, tok)
        ctrl_seq += 1
        auth_seq += 1
        self._ctrl_sock.sendto(pkt, ctrl_addr)

        capabilities = None
        deadline = time.monotonic() + timeout
        while capabilities is None:
            if time.monotonic() > deadline:
                raise IcomNetError(
                    "no capabilities packet from radio after token register"
                )
            self._ctrl_sock.settimeout(0.5)
            try:
                data, _ = self._ctrl_sock.recvfrom(4096)
            except TimeoutError:
                continue
            if is_capabilities_packet(data):
                capabilities = parse_capabilities(data)
        if not capabilities:
            raise IcomNetError("capabilities packet listed no radios")
        radio = capabilities[0]

        # No token(0x05) renewal here -- captured directly from a real wfview
        # session (packet trace against this same radio): its outer seq goes
        # login=1, token(register)=2, conninfo=3, with no second token request
        # in between. The radio's own strict seq-continuity tracking (see the
        # ctrl_seq comment above) means sending one anyway just puts our seq
        # numbering out of step with what a real client's sequence looks like.

        civ_port = _local_udp_port()
        pkt = conninfo_packet(
            ctrl_seq,
            ctrl_local_id,
            ctrl_remote_id,
            auth_seq,
            tok,
            radio["guid"],
            radio["name"] or "radio",
            self._username,
            civ_port,
        )
        ctrl_seq += 1
        auth_seq += 1
        self._ctrl_sock.sendto(pkt, ctrl_addr)
        self._recv_len(
            self._ctrl_sock, 0x90, timeout
        )  # confirms the radio accepted the stream-open request

        self._ctrl_seq, self._auth_seq = ctrl_seq, auth_seq

        # Start the control-socket keepalive loop right away. The CI-V-socket
        # open handshake below can take a couple of seconds against real hardware
        # (see _civ_loop) -- confirmed against a real IC-9700 that if the control
        # socket goes that long without an idle packet, the radio silently drops
        # the just-registered conninfo session and never opens CI-V data at all,
        # even though the low-level are-you-there/ready responder (which is
        # session-independent) still answers normally the whole time.
        self._stop.clear()
        self._threads = [threading.Thread(target=self._ctrl_loop, daemon=True)]
        self._threads[0].start()

        self._civ_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._civ_sock.bind(("0.0.0.0", civ_port))
        civ_net_addr = (self.host, self._radio_civ_port)
        civ_local_id = os.urandom(4)
        civ_remote_id = _rendezvous(self._civ_sock, civ_net_addr, civ_local_id, timeout)
        self._civ_local_id, self._civ_remote_id, self._civ_addr_net = (
            civ_local_id,
            civ_remote_id,
            civ_net_addr,
        )
        # same untracked-handshake-vs-tracked-counter split as ctrl_seq above
        self._civ_seq = 1
        self._civ_inner = 0
        # wfview uses magic=0x04 to open the IC-9700's CI-V stream; kappanhang uses
        # 0x05 for the IC-705 -- _civ_loop tries 0x04 first and falls back to 0x05.
        self._civ_open_magic = 0x04
        self._civ_ready = threading.Event()

        civ_thread = threading.Thread(target=self._civ_loop, daemon=True)
        self._threads.append(civ_thread)
        civ_thread.start()
        if not self._civ_ready.wait(timeout=10.0):
            raise IcomNetError("radio never sent CI-V data after opening the stream")

        # Prime current state rather than waiting for the next front-panel change.
        self._send_civ_command(0x03)
        self._send_civ_command(0x04)

    def _recv_len(self, sock: socket.socket, length: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(0.5)
            try:
                data, _ = sock.recvfrom(4096)
            except TimeoutError:
                continue
            if len(data) == length:
                return data
        raise IcomNetError(f"no {length}-byte reply from radio within {timeout}s")

    def _send_civ_command(self, cmd: int, data: bytes = b"") -> None:
        frame = civ_frame(self._civ_addr, CIV_CONTROLLER_ADDR, cmd, data)
        pkt = civ_data_packet(
            self._civ_seq,
            self._civ_local_id,
            self._civ_remote_id,
            self._civ_inner,
            frame,
        )
        self._civ_seq += 1
        self._civ_inner += 1
        self._civ_sock.sendto(pkt, self._civ_addr_net)

    def _ctrl_loop(self) -> None:
        last_idle = 0.0
        last_reauth = time.monotonic()
        self._ctrl_sock.settimeout(IDLE_PERIOD_S)
        while not self._stop.is_set():
            try:
                data, _ = self._ctrl_sock.recvfrom(4096)
                if is_ping_request(data):
                    reply = ping_reply(
                        struct.unpack_from("<H", data, 0x06)[0],
                        self._ctrl_local_id,
                        self._ctrl_remote_id,
                        data[0x11:0x15],
                    )
                    self._ctrl_sock.sendto(reply, self._ctrl_addr)
            except (TimeoutError, OSError):
                pass

            now = time.monotonic()
            if now - last_idle >= IDLE_PERIOD_S:
                idle = control_packet(
                    0x00, 0, self._ctrl_local_id, self._ctrl_remote_id
                )
                try:
                    self._ctrl_sock.sendto(idle, self._ctrl_addr)
                except OSError:
                    pass
                last_idle = now
            if now - last_reauth >= TOKEN_RENEWAL_S:
                pkt = token_packet(
                    self._ctrl_seq,
                    self._ctrl_local_id,
                    self._ctrl_remote_id,
                    self._auth_seq,
                    0x05,
                    self._tok,
                )
                self._ctrl_seq += 1
                self._auth_seq += 1
                try:
                    self._ctrl_sock.sendto(pkt, self._ctrl_addr)
                except OSError:
                    pass
                last_reauth = now

    def _civ_loop(self) -> None:
        last_idle = 0.0
        last_data = 0.0  # 0.0 means "never yet" -- distinguishes initial open from a later stall
        last_reopen = 0.0
        opened_once = False
        self._civ_sock.settimeout(IDLE_PERIOD_S)
        while not self._stop.is_set():
            try:
                data, _ = self._civ_sock.recvfrom(4096)
                payload = parse_civ_data_packet(data)
                if payload is not None:
                    if last_data == 0.0:
                        self._civ_ready.set()
                    last_data = time.monotonic()
                    for frame in split_civ_frames(payload):
                        for cb in self._frame_listeners:
                            try:
                                cb(frame)
                            except Exception:
                                pass
                        scope = parse_scope_frame(frame)
                        if scope is not None:
                            self._apply_scope_frame(scope)
                            continue
                        update = parse_civ_update(frame)
                        if update is not None:
                            self._apply_update(*update)
                elif is_ping_request(data):
                    reply = ping_reply(
                        struct.unpack_from("<H", data, 0x06)[0],
                        self._civ_local_id,
                        self._civ_remote_id,
                        data[0x11:0x15],
                    )
                    self._civ_sock.sendto(reply, self._civ_addr_net)
            except (TimeoutError, OSError):
                pass

            now = time.monotonic()
            if now - last_idle >= IDLE_PERIOD_S:
                idle = control_packet(0x00, 0, self._civ_local_id, self._civ_remote_id)
                try:
                    self._civ_sock.sendto(idle, self._civ_addr_net)
                except OSError:
                    pass
                last_idle = now

            # Retry cadence intentionally conservative, not wfview's documented
            # 100ms watchdog: against real hardware, resending the open request
            # every 100ms (each with a new tracked seq, since we don't implement
            # retransmit-request compliance) made the radio's own sequence
            # tracker lose sync entirely -- it started demanding retransmission
            # of a huge, nonsensical seq range. Spacing retries by CIV_STALE_S
            # (2s) gives each attempt time to land before the next is sent.
            if last_data == 0.0 or now - last_data >= CIV_STALE_S:
                due = not opened_once or now - last_reopen >= CIV_STALE_S
                if due:
                    if opened_once and self._civ_open_magic == 0x04:
                        self._civ_open_magic = (
                            0x05  # first magic got no data -- try the other one
                        )
                    pkt = openclose_packet(
                        self._civ_seq,
                        self._civ_local_id,
                        self._civ_remote_id,
                        self._civ_inner,
                        self._civ_open_magic,
                    )
                    self._civ_seq += 1
                    self._civ_inner += 1
                    try:
                        self._civ_sock.sendto(pkt, self._civ_addr_net)
                    except OSError:
                        pass
                    # The radio never sends CI-V data unprompted just because the
                    # stream is "open" -- confirmed from a real wfview session
                    # trace: its first actual CI-V traffic is a client-sent query
                    # (a broadcast "read transceiver address"), not anything
                    # radio-initiated. Waiting to hear something before speaking
                    # first was a deadlock. Send a real query alongside every
                    # open attempt so the radio always has something to answer.
                    self._send_civ_command(0x03)
                    opened_once = True
                    last_reopen = now

    def close(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        if self._civ_sock is not None:
            try:
                pkt = openclose_packet(
                    self._civ_seq,
                    self._civ_local_id,
                    self._civ_remote_id,
                    self._civ_inner,
                    0x00,
                )
                self._civ_sock.sendto(pkt, self._civ_addr_net)
            except OSError:
                pass
            self._civ_sock.close()
        if self._ctrl_sock is not None:
            self._ctrl_sock.close()


def _load_netrc_credentials(host: str) -> tuple[str, str]:
    import netrc

    n = netrc.netrc()
    entry = n.authenticators(host)
    if not entry:
        raise IcomNetError(f"no credentials for {host} in ~/.netrc")
    login, _, password = entry
    return login, password


def write_scope_record(f, ts: float, start_hz: int, end_hz: int, pixels: bytes) -> None:
    """Append one sweep to a scope recording: <f8 ts><u4 start_hz><u4 end_hz>
    <u2 npixels><npixels raw bytes>, repeated per sweep. A simple, appendable
    binary format sized for a multi-hour contest session -- JSON per sweep
    would balloon file size at scope refresh rates for no benefit, since
    pixel values are already byte-quantized 0-160 with nothing gained from
    text encoding (same reasoning as the WAV recorder storing raw samples
    rather than a JSON array per sample)."""
    f.write(struct.pack("<dIIH", ts, start_hz, end_hz, len(pixels)))
    f.write(pixels)


def read_scope_records(path) -> list[tuple[float, int, int, bytes]]:
    """Read back a recording written by write_scope_record: a list of
    (timestamp, start_hz, end_hz, pixels) tuples, one per sweep. The sole
    reader for the format -- contest_video.py and scope_preview.py both
    import this rather than each re-parsing the binary layout themselves."""
    records = []
    with open(path, "rb") as f:
        while True:
            header = f.read(18)
            if len(header) < 18:
                break
            ts, start_hz, end_hz, npix = struct.unpack("<dIIH", header)
            pixels = f.read(npix)
            if len(pixels) < npix:
                break
            records.append((ts, start_hz, end_hz, pixels))
    return records


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <radio-ip> [--scope [outfile.scope]]")
        sys.exit(1)
    host = sys.argv[1]
    username, password = _load_netrc_credentials(host)

    scope_enabled = "--scope" in sys.argv
    scope_out = None
    if scope_enabled:
        idx = sys.argv.index("--scope")
        if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("-"):
            scope_out = sys.argv[idx + 1]

    rig = IcomNetRig(host, username, password)

    def on_change(freq_hz, mode, band):
        f = f"{freq_hz / 1e6:.6f} MHz" if freq_hz is not None else "----"
        print(f"[icom] {band or '--'}  {f}  {mode or '--'}", flush=True)

    rig.on_update(on_change)

    scope_file = open(scope_out, "wb") if scope_out else None
    if scope_enabled:

        def on_scope(start_hz, end_hz, pixels):
            print(
                f"[icom] scope {start_hz / 1e6:.6f}-{end_hz / 1e6:.6f} MHz  "
                f"{len(pixels)}px  peak={max(pixels)}",
                flush=True,
            )
            if scope_file is not None:
                write_scope_record(scope_file, time.time(), start_hz, end_hz, pixels)

        rig.on_scope(on_scope)

    print(f"[icom] connecting to {host} ...", flush=True)
    rig.connect()
    if scope_enabled:
        rig.enable_scope()
    print("[icom] connected -- waiting for live updates (Ctrl-C to quit)", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        rig.close()
        if scope_file is not None:
            scope_file.close()


if __name__ == "__main__":
    main()
