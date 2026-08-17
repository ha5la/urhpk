#!/usr/bin/env -S uv run
"""Capture the radio's LAN audio stream to a timestamped log, and say what it is.

A standalone probe, not part of a round. It answers two questions that only the
real radio can:

  * does a 48 kHz LAN capture carry anything the SD card's 16 kHz recording
    doesn't -- run --spectrum on the result and compare against the passband
    table in FINDINGS.md
  * does the audio path disturb the CI-V session the logger depends on

**The radio holds exactly one session**, so this must not run while
`puskas_logger.py` is up: connecting here would silently kill the round's.

    uv run lan_audio_probe.py <radio-ip> --seconds 60
    uv run lan_audio_probe.py --spectrum lan-audio-<stamp>.jsonl

The log is one JSON line per datagram -- wall clock for joining to everything
else, CLOCK_BOOTTIME as a timeline no NTP step can bend (and which, unlike
CLOCK_MONOTONIC, keeps counting across a suspend), the radio's own sequence
number so a dropped datagram is provable, and the samples. A step shows up as
the two clocks diverging rather than as a silently bent log.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from urhpk.icom_net import (
    RX_CODEC_LPCM16_MONO,
    RX_CODEC_LPCM16_STEREO,
    IcomNetError,
    IcomNetRig,
    _load_netrc_credentials,
)


def _record(seq: int, pcm: bytes, received_at: float) -> dict:
    return {
        "wall": datetime.fromtimestamp(received_at, timezone.utc).isoformat(),
        "boot": round(time.clock_gettime(time.CLOCK_BOOTTIME), 6),
        "seq": seq,
        "pcm": base64.b64encode(pcm).decode("ascii"),
    }


async def capture(host: str, out: Path, seconds: float, rate: int, stereo: bool) -> int:
    user, password = _load_netrc_credentials(host)
    rig = IcomNetRig(
        host,
        user,
        password,
        rx_sample=rate,
        rx_codec=RX_CODEC_LPCM16_STEREO if stereo else RX_CODEC_LPCM16_MONO,
    )
    fh = out.open("w")
    count = 0
    dropped = 0
    last_seq = None

    def on_audio(seq: int, pcm: bytes, received_at: float) -> None:
        nonlocal count, dropped, last_seq
        if last_seq is not None:
            gap = (seq - last_seq - 1) & 0xFFFF
            if gap:
                dropped += gap
        last_seq = seq
        count += 1
        fh.write(json.dumps(_record(seq, pcm, received_at)) + "\n")

    rig.on_audio(on_audio)
    await rig.connect()
    print(f"connected to {host}; capturing {seconds:.0f}s to {out}")
    try:
        await asyncio.sleep(seconds)
    finally:
        await rig.close()
        fh.close()
    print(f"{count} datagrams, {dropped} missing by sequence")
    if not count:
        print("no audio arrived -- rxenable was set but the radio sent nothing")
        return 1
    return 0


def spectrum(path: Path) -> int:
    """Passband of a capture, in the same form as FINDINGS.md's table."""
    import numpy as np

    recs = [json.loads(line) for line in path.read_text().splitlines() if line]
    if not recs:
        sys.exit(f"{path} is empty")
    pcm = b"".join(base64.b64decode(r["pcm"]) for r in recs)
    x = np.frombuffer(pcm, "<i2").astype(float) / 32768.0
    span = recs[-1]["boot"] - recs[0]["boot"]
    rate = round(len(x) / span) if span else 0
    print(f"{len(recs)} datagrams, {len(x)} samples over {span:.2f}s -> {rate} Hz")

    n = 4096
    if len(x) < n * 2:
        sys.exit("too little audio to measure a spectrum")
    win = np.hanning(n)
    acc = np.zeros(n // 2 + 1)
    frames = 0
    for i in range(0, len(x) - n, n // 2):
        acc += np.abs(np.fft.rfft(x[i : i + n] * win)) ** 2
        frames += 1
    p = 10 * np.log10(acc / frames + 1e-20)
    p -= p.max()
    freqs = np.fft.rfftfreq(n, 1 / rate)
    for hz in (300, 1000, 2000, 3000, 3500, 4000, 6000, 8000, 12000, 20000):
        if hz < rate / 2:
            print(f"  {hz:6d} Hz  {p[np.argmin(abs(freqs - hz))]:7.1f} dB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", help="radio IP to capture from, or a .jsonl to analyse")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--stereo", action="store_true", help="main and sub as L/R")
    ap.add_argument(
        "--spectrum",
        action="store_true",
        help="analyse an existing capture instead of making one",
    )
    args = ap.parse_args()

    if args.spectrum:
        return spectrum(Path(args.target))

    stamp = datetime.now().strftime("%y%m%d-%H%M%S")
    out = Path(f"lan-audio-{stamp}.jsonl")
    try:
        return asyncio.run(
            capture(args.target, out, args.seconds, args.rate, args.stereo)
        )
    except IcomNetError as exc:
        print(f"radio: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
