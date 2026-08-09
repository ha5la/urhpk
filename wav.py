"""The recorder's WAV files: what a segment file carries, and how to read it.

The IC-9700's Voice Recorder writes one WAV per RX/TX switch, with the rig's
own frequency/mode/direction stamped into the RIFF title tag. This module is
the boundary to that format — everything above it talks about segments, not
about chunk headers.
"""

from __future__ import annotations

import re
import wave

import numpy as np

import edi

_WAV_TITLE_RE = re.compile(
    r"(\d+)\.(\d+)\.(\d+)\s+(\S+)\s+.*?(RX|TX)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*$"
)


def parse_wav_title(title: str) -> tuple[int, str, bool] | None:
    """Parse an IC-9700 'Voice Recorder' title tag, e.g.
    'IC-9700 Voice Recorder Data   144.299.84 USB    ----.---.-- ------ -- '
    'TX 2026-07-06 16:00:37' -> (144299840, 'SSB', True).

    This is ground truth straight from the radio at the exact instant it
    started recording the file -- unlike telemetry (a separate 1 Hz poll,
    not synced to the WAV split at all), there is no possible lag here.
    Returns None if the title doesn't match this format (not an IC-9700
    recording, or a future firmware changing it)."""
    m = _WAV_TITLE_RE.search(title)
    if not m:
        return None
    mhz, khz, h10, mode, rxtx = m.groups()
    freq_hz = int(mhz) * 1_000_000 + int(khz) * 1_000 + int(h10) * 10
    return freq_hz, edi.mode_from_radio(mode), rxtx == "TX"


def read_wav_title(path: str) -> str | None:
    """Read the LIST/INFO/INAM ('title') tag directly from a WAV file's own
    RIFF chunk structure -- no subprocess. ffprobe can read the same tag
    but spawning it once per file doesn't scale: measured 707 files at
    ~112s via ffprobe vs. ~0.02s reading the raw chunk headers directly."""
    with open(path, "rb") as f:
        header = f.read(12)
        if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None
        while True:
            chunk_header = f.read(8)
            if len(chunk_header) < 8:
                return None
            chunk_id = chunk_header[0:4]
            chunk_size = int.from_bytes(chunk_header[4:8], "little")
            if chunk_id == b"LIST":
                data = f.read(chunk_size)
                if chunk_size % 2:
                    f.read(1)  # chunks are padded to an even size
                if data[0:4] == b"INFO":
                    pos = 4
                    while pos + 8 <= len(data):
                        sub_id = data[pos : pos + 4]
                        sub_size = int.from_bytes(data[pos + 4 : pos + 8], "little")
                        sub_data = data[pos + 8 : pos + 8 + sub_size]
                        if sub_id == b"INAM":
                            return sub_data.rstrip(b"\x00").decode(
                                "ascii", errors="replace"
                            )
                        pos += 8 + sub_size + (sub_size % 2)
            else:
                f.seek(chunk_size + (chunk_size % 2), 1)


def read_wav_range(path: str, t0: float, t1: float) -> tuple[np.ndarray, int]:
    """Read samples in [t0, t1) seconds from a WAV file without loading the
    whole file -- for extracting one sub-range out of a long segment (see
    cw_decode.decode_long_segment). t0/t1 are clamped to the file's own bounds."""
    w = wave.open(path)
    sr = w.getframerate()
    n_frames = w.getnframes()
    f0 = max(0, min(n_frames, int(t0 * sr)))
    f1 = max(f0, min(n_frames, int(t1 * sr)))
    w.setpos(f0)
    x = np.frombuffer(w.readframes(f1 - f0), dtype=np.int16).astype(float)
    w.close()
    return x, sr
