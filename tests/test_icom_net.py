"""Tests for the pure, hardware-independent parts of icom_net.py:
credential scrambling, BCD frequency codec, and CI-V frame parsing.
"""

from __future__ import annotations

from icom_net import (
    bcd_decode_freq,
    bcd_encode_freq,
    civ_frame,
    parse_civ_update,
    passcode,
    split_civ_frames,
)


def test_passcode_is_16_bytes_and_deterministic():
    assert len(passcode("HA5LA")) == 16
    assert passcode("HA5LA") == passcode("HA5LA")


def test_passcode_differs_by_position_not_just_content():
    # Same characters, different order -> the +i position term must matter,
    # otherwise this "scramble" would just be a per-character substitution.
    assert passcode("AB") != passcode("BA")


def test_passcode_zero_pads_short_strings():
    out = passcode("HI")
    assert out[2:] == bytes(14)


def test_bcd_encode_matches_known_icom_example():
    # 144.300.000 Hz is the standard worked example in Icom's own CI-V
    # reference documentation: bytes 00 00 30 44 01.
    assert bcd_encode_freq(144_300_000) == bytes([0x00, 0x00, 0x30, 0x44, 0x01])


def test_bcd_decode_matches_known_icom_example():
    assert bcd_decode_freq(bytes([0x00, 0x00, 0x30, 0x44, 0x01])) == 144_300_000


def test_bcd_freq_round_trip():
    for hz in (144_174_000, 432_500_000, 1_296_000_000, 50_313_000, 0):
        assert bcd_decode_freq(bcd_encode_freq(hz)) == hz


def test_split_civ_frames_single():
    frame = civ_frame(0xE0, 0xA2, 0x03, bytes([1, 2, 3, 4, 5]))
    assert split_civ_frames(frame) == [bytes([0xE0, 0xA2, 0x03, 1, 2, 3, 4, 5])]


def test_split_civ_frames_body_excludes_fe_fe_and_fd():
    frame = civ_frame(0xE0, 0xA2, 0x00, bytes([0x01, 0x02]))
    (body,) = split_civ_frames(frame)
    assert body == bytes([0xE0, 0xA2, 0x00, 0x01, 0x02])
    assert 0xFD not in body
    assert body[:2] != bytes([0xFE, 0xFE])


def test_split_civ_frames_handles_several_concatenated_frames():
    f1 = civ_frame(0xE0, 0xA2, 0x00, bcd_encode_freq(144_174_000))
    f2 = civ_frame(0xE0, 0xA2, 0x01, bytes([0x03]))
    frames = split_civ_frames(f1 + f2)
    assert len(frames) == 2
    assert parse_civ_update(frames[0]) == ("freq", 144_174_000)
    assert parse_civ_update(frames[1]) == ("mode", "CW")


def test_split_civ_frames_ignores_noise_before_first_frame():
    # A lone 0xFE not itself followed by a second 0xFE isn't a valid frame
    # start and must be skipped over.
    noise = bytes([0x00, 0xFE, 0x11])
    frame = civ_frame(0xE0, 0xA2, 0x03, bcd_encode_freq(432_500_000))
    (body,) = split_civ_frames(noise + frame)
    assert parse_civ_update(body) == ("freq", 432_500_000)


def test_parse_civ_update_unsolicited_freq():
    frame = civ_frame(0xE0, 0xA2, 0x00, bcd_encode_freq(1_296_000_000))
    (body,) = split_civ_frames(frame)
    assert parse_civ_update(body) == ("freq", 1_296_000_000)


def test_parse_civ_update_unsolicited_mode():
    frame = civ_frame(0xE0, 0xA2, 0x01, bytes([0x05, 0x01]))  # FM + filter byte
    (body,) = split_civ_frames(frame)
    assert parse_civ_update(body) == ("mode", "FM")


def test_parse_civ_update_query_reply_freq_and_mode():
    freq_frame = civ_frame(0xE0, 0xA2, 0x03, bcd_encode_freq(50_313_000))
    mode_frame = civ_frame(0xE0, 0xA2, 0x04, bytes([0x01, 0x01]))  # USB
    assert parse_civ_update(split_civ_frames(freq_frame)[0]) == ("freq", 50_313_000)
    assert parse_civ_update(split_civ_frames(mode_frame)[0]) == ("mode", "USB")


def test_parse_civ_update_ignores_unrelated_commands():
    frame = civ_frame(0xE0, 0xA2, 0x1A, bytes([0x00, 0x01]))
    (body,) = split_civ_frames(frame)
    assert parse_civ_update(body) is None


def test_parse_civ_update_ignores_too_short_frame():
    assert parse_civ_update(bytes([0xE0, 0xA2])) is None
