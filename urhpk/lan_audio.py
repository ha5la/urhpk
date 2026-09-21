"""The radio's LAN audio stream, written to a WAV the round can fall back on.

The Voice Recorder is started by hand and cannot be interrogated (FINDINGS.md),
so a forgotten REC costs the round its entire audio and nothing says so at the
time. This capture is the insurance: the same bandlimited AF stage the SD card
records, off the LAN session the logger already holds.

Two files, because a plain WAV cannot say what it is missing:

  * `<round>.wav` -- 16 kHz mono LPCM, exactly the samples that arrived.
  * `<round>.wav.jsonl` -- a checkpoint per interval and one per sequence gap,
    each pinning a sample index to the wall clock, CLOCK_BOOTTIME and the
    radio's own datagram sequence number. A lost datagram is concatenated over
    rather than padded -- inventing silence would put a fabricated duration
    into the one recording kept for when the real one is missing -- so the
    sidecar is what makes the hole provable. Two clocks rather than one because
    an NTP step shows up as the pair diverging instead of as a silently bent
    timeline.

The header's size fields are rewritten after every datagram, so the failure this
exists for -- the logger dying mid-round, with nothing getting to run at the
end -- leaves a WAV that plays whole and without repair. Tying that to the
checkpoint interval instead was measurably worse: ffprobe reads an unclosed
file as exactly as long as its last rewrite, so a minute's interval is a
minute of the backup recording thrown away in the one case it is for.
"""

from __future__ import annotations

import json
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

CHECKPOINT_S = 60.0

_RIFF_SIZE_OFFSET = 4
_DATA_SIZE_OFFSET = 40


def _wav_header(rate: int, data_bytes: int) -> bytes:
    """The canonical 44-byte mono 16-bit PCM header."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        rate,
        rate * 2,
        2,  # block align
        16,  # bits per sample
        b"data",
        data_bytes,
    )


class LanAudioWriter:
    def __init__(
        self,
        path: Path,
        rate: int,
        checkpoint_s: float = CHECKPOINT_S,
        boot_clock=None,
    ):
        self._rate = rate
        self._checkpoint_s = checkpoint_s
        self._boot = boot_clock or (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
        self._wav = open(path, "wb")
        self._wav.write(_wav_header(rate, 0))
        self._sidecar = open(str(path) + ".jsonl", "w")
        self._samples = 0
        self._last_seq: int | None = None
        self._last_checkpoint: float | None = None

    def write(self, seq: int, pcm: bytes, received_at: float) -> None:
        gap = 0
        if self._last_seq is not None:
            gap = (seq - self._last_seq - 1) & 0xFFFF
        self._last_seq = seq
        boot = self._boot()
        due = (
            self._last_checkpoint is None
            or boot - self._last_checkpoint >= self._checkpoint_s
        )
        if gap or due:
            # Before the write: a checkpoint pins the sample index this
            # datagram *starts* at, which is what joins seq to a position.
            self._checkpoint(seq, boot, received_at, gap)
            if due:
                self._last_checkpoint = boot
        self._wav.write(pcm)
        self._samples += len(pcm) // 2
        self._fix_sizes()

    def _checkpoint(self, seq: int, boot: float, wall: float, gap: int) -> None:
        record = {
            "sample": self._samples,
            "wall": datetime.fromtimestamp(wall, timezone.utc).isoformat(),
            "boot": round(boot, 6),
            "seq": seq,
        }
        if gap:
            record["gap"] = gap
        self._sidecar.write(json.dumps(record) + "\n")
        self._sidecar.flush()

    def _fix_sizes(self) -> None:
        data_bytes = self._samples * 2
        end = self._wav.tell()
        self._wav.seek(_RIFF_SIZE_OFFSET)
        self._wav.write(struct.pack("<I", 36 + data_bytes))
        self._wav.seek(_DATA_SIZE_OFFSET)
        self._wav.write(struct.pack("<I", data_bytes))
        self._wav.seek(end)
        self._wav.flush()

    def close(self) -> None:
        self._fix_sizes()
        self._wav.close()
        self._sidecar.close()
