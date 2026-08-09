"""Tests for reading the recorder's own WAV files: the IC-9700 title tag."""

import struct
import wave
from datetime import datetime

from timeline import (
    Segment,
    read_wav_metadata,
)
from wav import parse_wav_title


def _write_wav_with_title(path: str, title: str) -> None:
    """A minimal WAV file carrying an IC-9700-style LIST/INFO/INAM title tag,
    for testing read_wav_metadata without needing a real recording."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 100)
    raw = title.encode("ascii") + b"\x00"
    pad = b"\x00" if len(raw) % 2 else b""
    inam = b"INAM" + struct.pack("<I", len(raw)) + raw + pad
    list_data = b"INFO" + inam
    list_pad = b"\x00" if len(list_data) % 2 else b""
    list_chunk = b"LIST" + struct.pack("<I", len(list_data)) + list_data + list_pad
    data = bytearray(open(path, "rb").read())
    data.extend(list_chunk)
    data[4:8] = struct.pack("<I", len(data) - 8)
    with open(path, "wb") as f:
        f.write(data)


class TestWavMetadata:
    def test_parse_ssb(self):
        title = (
            "IC-9700 Voice Recorder Data   144.299.84 USB    "
            "----.---.-- ------ -- TX 2026-07-06 16:00:37"
        )
        assert parse_wav_title(title) == (144299840, "SSB", True)

    def test_parse_cw(self):
        title = (
            "IC-9700 Voice Recorder Data   144.080.00 CW     "
            "----.---.-- ------ -- TX 2026-07-06 16:03:24"
        )
        assert parse_wav_title(title) == (144080000, "CW", True)

    def test_parse_fm_rx(self):
        title = (
            "IC-9700 Voice Recorder Data   145.350.00 FM     "
            "----.---.-- ------ -- RX 2026-07-06 16:49:24"
        )
        assert parse_wav_title(title) == (145350000, "FM", False)

    def test_parse_lsb_normalizes_to_ssb(self):
        title = (
            "IC-9700 Voice Recorder Data   432.109.75 LSB    "
            "----.---.-- ------ -- RX 2026-07-06 16:37:24"
        )
        freq_hz, mode, ptt = parse_wav_title(title)
        assert mode == "SSB"

    def test_parse_returns_none_for_unrecognized_format(self):
        assert parse_wav_title("not an IC-9700 title at all") is None
        assert parse_wav_title("") is None

    def test_read_wav_metadata_populates_segment(self, tmp_path):
        path = tmp_path / "seg.wav"
        _write_wav_with_title(
            path,
            "IC-9700 Voice Recorder Data   144.080.00 CW     "
            "----.---.-- ------ -- TX 2026-07-06 16:03:24",
        )
        segs = [Segment(str(path), datetime(2026, 7, 6, 16, 3, 24), 4.361, 0.0)]
        read_wav_metadata(segs)
        assert segs[0].freq_hz == 144080000
        assert segs[0].mode == "CW"
        assert segs[0].ptt is True

    def test_read_wav_metadata_leaves_none_without_a_tag(self, tmp_path):
        path = tmp_path / "plain.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 100)
        segs = [Segment(str(path), datetime(2026, 7, 6, 16, 3, 24), 4.361, 0.0)]
        read_wav_metadata(segs)
        assert segs[0].freq_hz is None
        assert segs[0].mode is None
        assert segs[0].ptt is None
