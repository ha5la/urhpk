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
    CIV_CONTROLLER_ADDR,
    RX_CODEC_LPCM16_MONO,
    RX_CODEC_LPCM16_STEREO,
    IcomNetError,
    IcomNetRig,
    _load_netrc_credentials,
    bcd_encode_freq,
    civ_mode_name,
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


async def calibrate(
    host: str, rate: int, steps: int, dwell: float, quiet_hz: int = 0
) -> int:
    """How long a sample takes to reach the laptop after it was digitised.

    The rate figures measure pacing and the continuity check measures the
    stream's internal consistency; neither sees a constant pipeline delay,
    because a fixed queue delays every packet equally. Measuring it needs an
    event whose instant the laptop already knows.

    A CI-V mode change is one, and it transmits nothing. The radio echoes our
    own frames back, so the echo says when it processed the command, to within
    the return leg of a sub-millisecond LAN. The demodulator's noise character
    changes at that same instant, and shows up in the audio however long the
    pipeline is. The difference is the delay. The radio's mode is read first
    and restored at the end.
    """
    import numpy as np

    user, password = _load_netrc_credentials(host)
    rig = IcomNetRig(host, user, password, rx_sample=rate)

    packets: list[tuple[float, bytes]] = []
    rig.on_audio(lambda seq, pcm, at: packets.append((at, pcm)))

    echoes: list[tuple[int, float]] = []

    def on_frame(frame: bytes) -> None:
        # Our own frames come back with the addresses the other way round;
        # the radio's replies are the ones we must not time against.
        if len(frame) >= 3 and frame[0] != CIV_CONTROLLER_ADDR:
            if frame[2] == CIV_CMD_SET_MODE:
                echoes.append((frame[3] if len(frame) > 3 else -1, time.time()))

    rig.on_civ_frame(on_frame)
    await rig.connect()
    # connect() only *sends* the priming mode query; the reply lands later, so
    # reading rig.mode straight away gets None and the restore below silently
    # leaves the radio in whatever this run last set.
    await asyncio.sleep(1.0)
    original = rig.mode
    original_hz = rig.freq_hz
    print(
        f"connected to {host}; rig is {original} on "
        f"{(original_hz or 0) / 1e6:.4f} MHz, restoring both at the end"
    )
    if quiet_hz:
        # 144.800 is APRS: random stations key up at random times, and a burst
        # is a far bigger step in the hiss band than any mode change. Measure
        # somewhere nobody is transmitting. Receive only -- nothing here keys
        # the radio.
        print(f"listening on {quiet_hz / 1e6:.4f} MHz for the measurement")
        rig._send_civ_command(CIV_CMD_SET_FREQ, bcd_encode_freq(quiet_hz))
        await asyncio.sleep(1.0)

    marks: list[tuple[float, int]] = []  # (command processed at, mode)
    rtts: list[float] = []
    try:
        for i in range(steps):
            mode = CIV_MODE_USB if i % 2 == 0 else CIV_MODE_FM
            before = len(echoes)
            sent = time.time()
            rig._send_civ_command(CIV_CMD_SET_MODE, bytes([mode, 0x01]))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if len(echoes) > before:
                    break
            if len(echoes) > before:
                marks.append((echoes[before][1], mode))
                rtts.append(echoes[before][1] - sent)
            else:
                print(f"  step {i}: no echo, skipped")
                marks.append((sent, mode))
            await asyncio.sleep(dwell)
    finally:
        if quiet_hz and original_hz:
            rig._send_civ_command(CIV_CMD_SET_FREQ, bcd_encode_freq(original_hz))
            await asyncio.sleep(0.3)
        if original:
            code = next((c for c in range(0x18) if civ_mode_name(c) == original), None)
            if code is not None:
                rig._send_civ_command(CIV_CMD_SET_MODE, bytes([code, 0x01]))
                await asyncio.sleep(0.5)
        await rig.close()

    if not packets:
        print("no audio arrived")
        return 1

    # Sample index of each packet's first sample, and when that packet landed.
    arrivals = np.array([p[0] for p in packets])
    counts = np.cumsum([0] + [len(p[1]) // 2 for p in packets[:-1]])
    audio = np.frombuffer(b"".join(p[1] for p in packets), "<i2").astype(np.float32)

    delays = []
    for at, mode in marks:
        # The change cannot precede the command, and cannot lag it by anything
        # like the gap to the next one -- a window wide enough to hold a
        # neighbouring transition is a window that will sometimes pick it.
        j = int(np.searchsorted(arrivals, at))
        if j < 5 or j >= len(packets) - 20:
            continue
        lo = counts[j - 5]  # 100 ms before, to catch a negative delay
        hi = counts[j + 20]  # 400 ms after
        level, hop = hiss_band(audio[lo:hi], rate)
        # Sign-agnostic on purpose: switching to USB narrows the noise and
        # drops this band, but switching to FM can *also* drop it, whenever
        # the squelch closes on the way. Assuming the direction lost half the
        # transitions rather than protecting the measurement.
        off = step_index(level, hop)
        if off is None:
            continue
        onset = lo + off  # sample index of the change
        # Which packet carries it, and when its own last sample was digitised
        k = int(np.searchsorted(counts, onset, side="right")) - 1
        after = counts[k] + len(packets[k][1]) // 2 - onset  # samples still to come
        delays.append((arrivals[k] - after / rate - at, mode))

    if len(delays) < 3:
        print(f"only {len(delays)} usable transitions -- is the squelch open?")
        return 1
    d = np.array([x[0] for x in delays]) * 1000
    modes = np.array([x[1] for x in delays])
    print(f"{len(d)} transitions of {len(marks)}")
    if rtts:
        r = np.array(rtts) * 1000
        print(
            f"  CI-V echo round trip: median {np.median(r):.1f} ms, min {r.min():.1f}"
        )
    for name, code in (("-> USB", CIV_MODE_USB), ("-> FM ", CIV_MODE_FM)):
        v = np.sort(d[modes == code])
        if len(v):
            print(
                f"  {name}: median {np.median(v):+7.1f} ms  "
                + " ".join(f"{x:.1f}" for x in v)
            )
    print(
        f"  overall median {np.median(d):.1f} ms, IQR {np.percentile(d, 75) - np.percentile(d, 25):.1f} ms"
    )
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


CIV_CMD_SET_MODE = 0x06
CIV_CMD_SET_FREQ = 0x05
CIV_MODE_FM = 0x05
CIV_MODE_USB = 0x01


def hiss_band(x, rate: int, hop_ms: float = 1.0):
    """Energy in 2.8-3.9 kHz per hop -- the top of the passband, where FM's
    wider noise sits and SSB's does not. A mode change moves it by tens of dB
    within one hop, which is what makes it a usable instant rather than a
    fade."""
    import numpy as np

    n = 256
    hop = max(1, int(rate * hop_ms / 1000))
    frames = (len(x) - n) // hop
    if frames < 2:
        return np.zeros(0), hop
    # Windows start at i*hop rather than being centred on it, which measures
    # unbiased against a real step -- verified, because the arithmetic argues
    # the other way and is wrong: centring the window puts the answer 8 ms
    # late, half a window, which is the size of the delay being measured.
    view = np.lib.stride_tricks.sliding_window_view(x, n)[::hop][:frames]
    spec = np.abs(np.fft.rfft(view * np.hanning(n), axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / rate)
    band = spec[:, (freqs > 2800) & (freqs < 3900)].sum(1)
    return 10 * np.log10(band + 1e-20), hop


def step_index(level, hop: int, expect_sign: int = 0) -> int | None:
    """Where a level series steps, as a sample offset from its own start.

    Located by the largest single-hop change rather than a threshold: the two
    sides differ by tens of dB and their absolute values vary between runs, so
    a fixed threshold would need retuning and a maximum does not.

    `expect_sign` restricts it to changes in the known direction -- switching
    to USB narrows the noise and must drop this band, switching to FM must
    raise it. Without that, a large enough fluctuation anywhere in the window
    outranks the real edge, which on real audio put outliers of several
    hundred ms either side of a 4 ms answer."""
    import numpy as np

    w = 50  # hops either side: a real mode change is sustained, a spike is not
    if len(level) < 3 * w:
        return None
    # Mean either side of each candidate, not a single-hop difference: where
    # the band sits near the noise floor a change of nothing in absolute terms
    # is tens of dB, and single-hop differencing chases those spikes.
    c = np.concatenate([[0.0], np.cumsum(level)])
    means = (c[w:] - c[:-w]) / w
    score = means[w:] - means[:-w]  # after minus before, at each boundary
    scored = score * expect_sign if expect_sign else np.abs(score)
    k = int(np.argmax(scored))
    if scored[k] < 6.0:  # nothing that looks like a mode change
        return None

    # The averaging that makes the edge findable also blurs where it is, by
    # about its own width -- useless for timing a delay of the same order. So
    # refine: the sharpest single hop, of the direction the coarse pass found,
    # within one averaging width of it.
    sign = 1 if score[k] > 0 else -1
    centre = k + w
    d = np.diff(level) * sign
    lo = max(0, centre - w)
    hi = min(len(d), centre + w)
    return (lo + int(np.argmax(d[lo:hi])) + 1) * hop


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


def coarse_lag(lan, sd, rate: int, win_s: float = 2.0) -> tuple[int, float]:
    """Where sd's midpoint window sits in lan, searching the whole capture.

    The two recordings are started by hand seconds apart, and the SD card
    stamps its filename on the radio's clock rather than the laptop's, so
    nothing about their start times can be assumed. One full-length
    correlation settles it; the per-probe search then only has to cover the
    slips it is looking for."""
    import numpy as np

    win = int(rate * win_s)
    s = max(0, (len(sd) - win) // 2)
    ref = sd[s : s + win]
    ref = ref - ref.mean()
    if len(lan) < win + 1 or not ref.any():
        return 0, 0.0
    c = _normalised_correlation(lan, ref, win)
    k = int(np.argmax(c))
    return k - s, float(c[k])


def lag_profile(
    lan,
    sd,
    rate: int,
    win_s: float = 1.0,
    search_s: float = 3.0,
    probes: int = 40,
    base_lag: int = 0,
) -> list[tuple[float, int, float]]:
    """(time, lag in samples, correlation) at evenly spaced points through sd.

    The lag is where each window of sd is found in lan, reported relative to
    base_lag. Constant lag means the two streams agree sample for sample; a
    step means one of them gained or lost samples at that moment, which is the
    whole point of measuring it.
    """
    import numpy as np

    win = int(rate * win_s)
    search = int(rate * search_s)
    rows = []
    for s in np.linspace(0, len(sd) - win, probes).astype(int):
        ref = sd[s : s + win]
        ref = ref - ref.mean()
        centre = s + base_lag
        # Clamped explicitly, never by slicing: a probe whose window falls
        # before lan starts -- which every probe does for as long as the SD
        # card was recording before the capture began -- makes the stop index
        # negative, and the slice then wraps and matches noise at the far end.
        lo = max(0, centre - search)
        hi = min(len(lan), centre + win + search)
        hay = lan[lo:hi] if hi > lo else lan[:0]
        if len(hay) < win + 1 or not ref.any():
            continue
        c = _normalised_correlation(hay, ref, win)
        k = int(np.argmax(c))
        rows.append((s / rate, lo + k - centre, float(c[k])))
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
    base, base_c = coarse_lag(lan, sd, rate)
    print(f"coarse alignment: {base / rate:+.3f}s, correlation {base_c:.3f}")
    if base_c < 0.5:
        print("the two recordings do not appear to contain the same audio")
        return 1
    rows = lag_profile(lan, sd, rate, search_s=0.5, base_lag=base)
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
        "--quiet-hz",
        type=int,
        default=0,
        dest="quiet_hz",
        help="retune here for --calibrate and restore afterwards (receive only)",
    )
    ap.add_argument(
        "--calibrate",
        action="store_true",
        help="measure the radio's send buffer by timing CI-V mode changes "
        "against the audio (changes RX mode briefly, restores it, no TX)",
    )
    ap.add_argument(
        "--continuity",
        metavar="WAV",
        help="check an existing capture's samples against the SD card's own "
        "recording of the same minutes (one continuous, no-TX WAV)",
    )
    args = ap.parse_args()

    if args.calibrate:
        try:
            return asyncio.run(
                calibrate(args.target, args.rate, 24, 1.2, args.quiet_hz)
            )
        except IcomNetError as exc:
            print(f"radio: {exc}", file=sys.stderr)
            return 1
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
