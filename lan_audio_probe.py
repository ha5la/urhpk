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
import wave
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
    payloads = [base64.b64decode(r["pcm"]) for r in recs]
    x = np.frombuffer(b"".join(payloads), "<i2").astype(float) / 32768.0
    span = recs[-1]["boot"] - recs[0]["boot"]

    # Rate by least squares over every datagram, not len(x)/span: the naive
    # form counts the last packet's samples against a span that ends when it
    # *arrived*, and pins the whole answer on two jittery endpoints. The fit
    # uses all of them, and its residual is the arrival jitter itself -- which
    # is what says whether any ppm figure here is signal.
    counts = np.cumsum([0] + [len(p) // 2 for p in payloads[:-1]], dtype=float)
    t = np.array([r["boot"] for r in recs]) - recs[0]["boot"]
    coef = np.polyfit(t, counts, 1)
    rate = coef[0]
    resid = counts - np.polyval(coef, t)
    jitter_ms = resid.std() / rate * 1000
    se = resid.std() / (t.std() * len(t) ** 0.5)  # standard error of the slope
    nominal = min((8000, 16000, 32000, 48000, 96000), key=lambda n: abs(n - rate))
    ppm = (rate / nominal - 1) * 1e6
    print(f"{len(recs)} datagrams, {len(x)} samples over {span:.2f}s")
    print(
        f"  rate {rate:.5f} Hz vs {nominal} nominal -> {ppm:+.3f} ppm "
        f"+-{se / nominal * 1e6:.3f}"
    )
    print(f"  arrival jitter {jitter_ms:.2f} ms rms")
    rate = round(rate)

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


def _normalised_correlation(hay, ref, win: int):
    """Correlation of a mean-subtracted `ref` against every window of `hay`.

    Via FFT, not a sliding window: at 16 kHz a one-second reference against a
    seven-second haystack is ~10^9 multiply-adds done directly, per probe. The
    numerator needs no mean subtraction on the haystack side because `ref`
    already sums to zero, so the window's own mean cancels out of the product;
    the denominator gets each window's variance from running sums."""
    import numpy as np

    n = len(hay)
    size = 1 << (n + win).bit_length()
    conv = np.fft.irfft(np.fft.rfft(hay, size) * np.fft.rfft(ref[::-1], size), size)
    num = conv[win - 1 : n]

    c1 = np.concatenate([[0.0], np.cumsum(hay, dtype=np.float64)])
    c2 = np.concatenate([[0.0], np.cumsum(np.square(hay, dtype=np.float64))])
    s1 = c1[win:] - c1[:-win]
    s2 = c2[win:] - c2[:-win]
    var = np.maximum(s2 - s1 * s1 / win, 0.0)
    return num / (np.sqrt(var * np.square(ref, dtype=np.float64).sum()) + 1e-12)


def lag_profile(
    lan, sd, rate: int, win_s: float = 1.0, search_s: float = 3.0, probes: int = 40
) -> list[tuple[float, int, float]]:
    """(time, lag in samples, correlation) at evenly spaced points through sd.

    The lag is where each window of sd is found in lan. Constant lag means the
    two streams agree sample for sample; a step means one of them gained or
    lost samples at that moment, which is the whole point of measuring it.
    """
    import numpy as np

    win = int(rate * win_s)
    search = int(rate * search_s)
    rows = []
    for s in np.linspace(0, len(sd) - win, probes).astype(int):
        ref = sd[s : s + win]
        ref = ref - ref.mean()
        lo = max(0, s - search)
        hay = lan[lo : min(len(lan), s + win + search)]
        if len(hay) < win + 1 or not ref.any():
            continue
        c = _normalised_correlation(hay, ref, win)
        k = int(np.argmax(c))
        rows.append((s / rate, lo + k - s, float(c[k])))
    return rows


def continuity(path: Path, wav_path: Path) -> int:
    """Does the LAN stream's sample timeline actually hold?

    The rate fit in --spectrum measures packet *pacing*: a radio slipping the
    odd sample while pacing off a network timer would read perfectly clean.
    This measures the samples instead, against the SD card recording the same
    AF stage over the same minutes. Both are 16-bit LPCM of one source, so a
    window of one lines up with the other to the sample -- and a lag that walks
    is exactly the slip the pacing figure cannot see.

    Wants one continuous WAV, i.e. a recording made without transmitting: on TX
    the SD card records the microphone while the LAN stream carries the muted
    receiver, and the two have nothing in common to correlate.
    """
    import numpy as np

    recs = [json.loads(line) for line in path.read_text().splitlines() if line]
    lan = np.frombuffer(
        b"".join(base64.b64decode(r["pcm"]) for r in recs), "<i2"
    ).astype(np.float32)

    with wave.open(str(wav_path)) as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        sd = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32)
    if channels != 1:
        sys.exit("expected a mono WAV")

    print(f"LAN {len(lan) / rate:.1f}s vs WAV {len(sd) / rate:.1f}s at {rate} Hz")
    rows = lag_profile(lan, sd, rate)
    good = [r for r in rows if r[2] > 0.5]
    print(f"{len(good)} of {len(rows)} windows matched above 0.5")
    if len(good) < 2:
        print("not enough correlation -- is this the same audio, same minutes?")
        return 1
    lags = np.array([r[1] for r in good], float)
    ts = np.array([r[0] for r in good])
    drift = np.polyfit(ts, lags, 1)[0]
    print(
        f"  lag {lags.min():.0f}..{lags.max():.0f} samples, spread {np.ptp(lags):.0f}"
    )
    print(f"  correlation {min(r[2] for r in good):.3f}..{max(r[2] for r in good):.3f}")
    print(f"  drift {drift * 1e6 / rate:+.3f} ppm over the overlap")
    if np.ptp(lags) == 0:
        print("  CONTINUOUS: not one sample gained or lost")
    else:
        print(f"  {np.ptp(lags):.0f} samples of movement -- listing every change:")
        prev = lags[0]
        for t, lag in zip(ts, lags):
            if lag != prev:
                print(f"    {t:8.1f}s  lag {prev:+.0f} -> {lag:+.0f}")
                prev = lag
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", help="radio IP to capture from, or a .jsonl to analyse")
    ap.add_argument("--seconds", type=float, default=60.0)
    # 16 kHz matches the SD card and is twice the radio's own 4 kHz passband;
    # 48 kHz was measured to add only the quantisation floor (FINDINGS.md).
    ap.add_argument("--rate", type=int, default=16000)
    ap.add_argument("--stereo", action="store_true", help="main and sub as L/R")
    ap.add_argument(
        "--spectrum",
        action="store_true",
        help="analyse an existing capture instead of making one",
    )
    ap.add_argument(
        "--continuity",
        metavar="WAV",
        help="check an existing capture's samples against the SD card's own "
        "recording of the same minutes (one continuous, no-TX WAV)",
    )
    args = ap.parse_args()

    if args.continuity:
        return continuity(Path(args.target), Path(args.continuity))
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
