# 21 — Capture the radio's LAN audio during a round

Status: needs-triage

`lan_audio_probe.py` proves the stream out end to end against the real radio.
This is the ticket that puts it in `puskas_logger.py` so a round records it
without anyone remembering to.

## Why, honestly

The original case was timing — a laptop-clocked copy of the round's audio, to
fix the radio-vs-laptop offset. **That case is now weak.** Measuring it is what
found the 5.6 ms-per-segment excess, and `compensate_split_excess` already fixes
that from the SD card alone; the sub-band ruler put the radio's own split timing
at ~5 ms, and the filename quantisation that remains is ±0.5 s either way.

What survives is **insurance**. The Voice Recorder is started by hand and cannot
be interrogated (FINDINGS.md, "the radio cannot say whether it is recording"), so
forgetting REC costs the entire round's audio and nothing during the round says
so. Alt+S is an acknowledgement, not a measurement. A LAN capture is the only
thing that would make that survivable — and by the measurements below it is a
genuinely good recording, not merely a fallback.

Decide whether that is worth the code before building it.

## What is already known (FINDINGS.md, all measured against the radio)

- LPCM 16 kHz mono, the same bandlimited AF stage the SD card records; 48 kHz
  buys only the quantisation floor.
- −0.056 ±0.035 ppm against `CLOCK_BOOTTIME` over ten minutes. No drift to fit,
  unlike the webcam.
- Sample-continuous: 4.8 M samples against the SD card's own recording, not one
  gained or lost.
- 30,000 datagrams with no gap in the sequence. Retransmit compliance stays
  unnecessary on this LAN.
- ~10 ms pipeline delay, mode-dependent by 18 ms, absolute value unresolved —
  and mostly common with the SD card, so it cancels for joining the two.

## Settled by the grilling session

- **Always on**, no flag.
- **Non-fatal, always.** A failed or dying audio socket is logged and shown in
  the toolbar; the round continues on CI-V alone. CI-V is the thing that cannot
  be lost.
- **One file**, no rotation.
- **Status-bar warning under 5 GB free.** Deliberately a warning and not a hard
  stop — the operator's call. Residual risk stated and accepted: a full disk
  takes down the logger, and this is what fills it.
- 16 kHz mono ≈ 230 MB for a two-hour round.

## Open

- **Format.** The probe writes one JSON line per datagram with wall clock,
  `CLOCK_BOOTTIME` and the sequence number. That was designed when the stream's
  timing was the thing under suspicion. It isn't any more, and a plain WAV is
  smaller, previewable and needs no tooling. Timestamps still buy: an NTP step
  visible as the two clocks diverging, and a *hole* that is provable rather than
  silently concatenated — which matters precisely in the case this feature
  exists for, a crash mid-round. A middle option is a WAV plus a small sidecar
  of (sample index, wall, boot, seq) at each gap and every N seconds.
- Where it goes in `contest_video.py`, if anywhere, when the SD card is missing.
- Whether `icom_net.py`'s audio path needs the retransmit-request compliance it
  currently skips, if the round's LAN is ever less clean than this one.

## Constraints an implementation must not break

- The radio holds one session, so the capture goes through the logger's existing
  `IcomNetRig` — never a second connect.
- `rx_sample=0` must stay the default, leaving `conninfo` byte-identical for any
  consumer that doesn't want audio.
- Audio comes up after CI-V is confirmed ready, and gets its own disconnect in
  `close()`.
