"""Tests for the pure, hardware-independent parts of icom_net.py:
credential scrambling, BCD frequency codec, and CI-V frame parsing.
"""

from __future__ import annotations

from icom_net import (
    bcd_decode_freq,
    bcd_encode_freq,
    civ_frame,
    parse_civ_update,
    parse_scope_frame,
    passcode,
    split_civ_frames,
)

# A real CI-V scope-wave-data payload (FE FE ... FD), captured with tcpdump
# against an actual IC-9700 (via a live wfview session) and extracted with a
# minimal pcap parser -- not synthesized. Ground truth for parse_scope_frame:
# decodes to a sane 145.11-146.11 MHz center-mode 2M sweep, 475 pixel bytes,
# each in the documented 0-160 range. Used as a fixture instead of only
# hand-built frames so the byte-offset layout is checked against real
# hardware, not just against our own possibly-mistaken assumptions.
_REAL_SCOPE_SWEEP_HEX = (
    "fefee1a227000001010000006145010000500000002f2a27242b2324182a2a272729291d1e26"
    "232b25282919230d25252c2c2a281a1a282314152c29272026302c161c18231d1e232025222e"
    "2c18171f25161c261f1f2a28202b2c2d272b1d1b2122181c25291c2129281d1e272317161920"
    "1f192b24232a221e2a21191b17191111161c1813212326222221242a281d2221282423282922"
    "261a26050d232312200f23291f1e221f222424242524222427241728271b1221291c0c252320"
    "1a2628271417251d1f2026201f20211d201a2b25211d211f1e232e252323281c211c28281e1f"
    "2423202222212b25181c25251523151e2127260d1b252529252528251f15301b1f1e221c1f23"
    "1f2920122218201a1e21262618271e241a20231e2b28241d1329221c1c1829201c202b2b1d10"
    "292b2323271c220e13191729221e1d19252117082b251c121417161c24231d2513221221232b"
    "24201c1c1c0f1b1b262a292b201f152a2a1f1c100e13151d250e271e22272821251d2118101a"
    "191b2a2d23171f1d1413242c1f1a1c22201e161f251c131d222212221b26211b292215242424"
    "182523081d2b291d15201d18171828191624230d1d180f1b2323242b1e0c261525201e2a2a22"
    "1127201c1f19212427262621282825211f23231d18221a21140f23231d2a2d25241c28281914"
    "2229fd"
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


def _real_scope_frame():
    civ_payload = bytes.fromhex(_REAL_SCOPE_SWEEP_HEX)
    (frame,) = split_civ_frames(civ_payload)
    return frame


def test_parse_scope_frame_real_capture_sequence_and_addressing():
    frame = _real_scope_frame()
    parsed = parse_scope_frame(frame)
    assert parsed is not None
    assert parsed["sequence"] == 1
    assert parsed["sequence_max"] == 1  # single LAN packet carries the whole sweep


def test_parse_scope_frame_real_capture_frequency_range():
    parsed = parse_scope_frame(_real_scope_frame())
    # 2M band, 1 MHz span, center mode -- matches the radio's actual dial
    # frequency at capture time, not a hand-picked/rounded test value.
    assert parsed["start_hz"] == 145_110_000
    assert parsed["end_hz"] == 146_110_000
    assert parsed["mode"] == 0x00
    assert parsed["out_of_range"] is False


def test_parse_scope_frame_real_capture_pixels():
    parsed = parse_scope_frame(_real_scope_frame())
    pixels = parsed["pixels"]
    assert len(pixels) == 475  # SpectrumLenMax for the IC-9700
    assert all(0 <= p <= 160 for p in pixels)


def test_parse_scope_frame_rejects_non_scope_command():
    frame = civ_frame(0xE1, 0xA2, 0x03, bcd_encode_freq(144_300_000))
    assert parse_scope_frame(frame) is None


def test_parse_scope_frame_rejects_too_short():
    assert parse_scope_frame(bytes([0xE1, 0xA2, 0x27, 0x00, 0x00])) is None


def _synthetic_scope_frame(
    sequence: int,
    sequence_max: int,
    pixels: bytes,
    *,
    start_hz: int = None,
    end_hz: int = None,
) -> bytes:
    seq_bcd = ((sequence // 10) << 4) | (sequence % 10)
    max_bcd = ((sequence_max // 10) << 4) | (sequence_max % 10)
    frame = bytes([0xE1, 0xA2, 0x27, 0x00, 0x00, seq_bcd, max_bcd])
    if sequence == 1:
        span = (end_hz - start_hz) // 2
        center = start_hz + span
        frame += (
            bytes([0x00])
            + bcd_encode_freq(center)
            + bcd_encode_freq(span)
            + bytes([0x00])
        )
    return frame + pixels


def test_parse_scope_frame_multi_sequence_reassembly():
    # IC-9700 over LAN always sends one packet per sweep in practice, but the
    # sequence/sequenceMax fields exist for the legacy multi-packet serial
    # framing too -- exercise that path since real hardware won't.
    first = _synthetic_scope_frame(
        1, 2, bytes(range(50)), start_hz=144_000_000, end_hz=146_000_000
    )
    second = _synthetic_scope_frame(2, 2, bytes(range(50, 60)))
    p1 = parse_scope_frame(first)
    p2 = parse_scope_frame(second)
    assert p1["sequence"] == 1 and p1["sequence_max"] == 2
    assert p2["sequence"] == 2 and p2["sequence_max"] == 2
    assert "start_hz" not in p2
