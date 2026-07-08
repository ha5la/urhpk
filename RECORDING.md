# Contest recording and video production

Notes from the URH Országos Bajnokság 2026-07-04 session (first test run).

## Recording setup

- **Radio**: Icom IC-9700
- **Software**: something that splits audio on PTT/RX-TX switching and names
  segments `YYYYMMDD_HHMMSS*.wav` in local time
- **Format**: 16 kHz mono PCM WAV, one file per transmission (RX or TX)
- **Segments are contiguous**: sub-second gaps between files; total duration of
  all WAVs equals the session length

One recording directory per round (e.g. `urhob2026cw/recording/`).
The matching EDI log lives next to it (`urhob2026cw/260704-HA5LA-2M.edi`).

## Video production

```
uv run contest_video.py RECORDING_DIR EDI_FILE [-o OUT.mp4] [--skip-gaps] [--res 720p|1080p]
```

| Option | Effect |
|---|---|
| `--skip-gaps` | Trim listening/CQ gaps between QSOs to 3 s each (42 min → 7.6 min for this session) |
| `--res 720p` | Render at 1280×720 instead of 1920×1080 — ~2.5× faster, good for preview |
| `--pitch HZ` | CW tone frequency (default 600 Hz, matches IC-9700 sidetone default) |
| `--keep-ass` | Keep intermediate `.ass` and `.concat.wav` for inspection |
| `--telemetry FILE` | `*-telemetry.jsonl` from `puskas_logger.py` — adds a top-left RX/TX indicator |

Render speed at 720p with `--skip-gaps`: ~1.4× realtime (7.6 min video in ~5 min).
Render speed at 1080p without `--skip-gaps`: ~0.28× realtime (~2.5 h for 42 min).

### CW decoder behaviour

- Works per-segment: each WAV is one over at one speed — adaptive dit estimation
  is robust per file.
- **CW tone is auto-detected per segment**, not assumed to be 600 Hz (IC-9700
  sidetone default) for the whole session — `--pitch` is now only a fallback
  for the rare case `_detect_pitch` finds nothing (e.g. true silence). Found
  from real received-signal segments the user transcribed by ear: one RX
  segment's true tone was ~1296 Hz against the 600 Hz default, a 695 Hz gap
  entirely outside the envelope lowpass's passband (`LOWPASS_CUTOFF_HZ=120`)
  — not a decode-quality problem but a near-total loss of the actual signal
  before decoding even started (SNR measured near 0). The operator's own TX
  sidetone auto-detects to within ~1 Hz of 600 Hz regardless (verified
  across several real TX segments from two different QSOs), so always
  auto-detecting is strictly better than only doing it conditionally.
- **Trust gate**: a segment's decode is shown in the ticker only if it is
  short (< `MAX_OVER_S`, 35 s), has high SNR (≥ 20 dB), word-shaped text
  (≥ 50% multi-char tokens), and not a chopped carrier (no single character
  > 40% of all chars *once there's enough text for that pattern to mean
  anything* — see `MIN_CHARS_FOR_DOMINANCE` below). This keeps all real
  exchanges and drops band noise / listening stretches.
  - `MAX_OVER_S` was raised from 30s to 35s after a real, correctly
    transcribable 32.5-second exchange (a full report + locator handoff)
    was being skipped before decoding even started. There's no clean
    statistical gap between "long real over" and "genuine listening
    period" the way there is for e.g. `FREQ_MATCH_TOLERANCE_HZ` — real
    segment durations form a continuum from 30s past 100s — so this is a
    modest, evidence-backed nudge for one confirmed case, not a broad
    guess; the other three gates still guard genuinely long listening
    periods that happen to fall in the 30-35s range.
  - `MIN_CHARS_FOR_DOMINANCE` (5): any 2-character decode has dominance
    ≥ 0.5 by construction (the two characters either match, giving 1.0, or
    don't, giving exactly 1/2 — never less), so `MAX_DOMINANCE=0.4` was
    structurally impossible to pass for *any* two-letter contest word ("TU",
    "R", "K"...) independent of content. Found from real, correctly-decoded
    "TU" and "73 EE" being silently dropped from the ticker. Text shorter
    than this length skips the dominance check entirely — the "chopped
    carrier" pattern it guards against only shows up over many characters
    in practice anyway.
- Dah-heavy CW (e.g. "CQ TEST") needs the min/max-midpoint dit estimator — a
  plain median collapses when dahs dominate.
- My own transmissions decode cleanly. Received signals from third parties
  on the band are filtered by the trust gate. Direct partner reports
  (received CW from the actual QSO partner, not third-party QRM) turned out
  *not* to decode cleanly by default — see debounce below, found from a
  real received segment the user transcribed by ear.
- **Debounce**: `_debounce_on` merges any on/off run under `DEBOUNCE_DIT_FRAC`
  (0.5) of the segment's own preliminary dit estimate into its neighbour.
  The operator's own TX sidetone is a clean, locally-generated tone; a real
  received signal has near-threshold noise/QSB the sidetone never does, and
  it was fragmenting single dits/dahs into several pieces even at a
  respectable 33 dB SNR (SNR is average loudness, not edge cleanliness).
  Relative to the segment's own dit, not a fixed time, because a fixed
  threshold tuned against one real file (30 ms) turned out to silently eat
  *all* decode at 45 WPM in the synthesized-WPM regression test, where a
  dit is only ~27 ms. `THR_HI_FRAC`/`THR_LO_FRAC` (0.5/0.3 → 0.35/0.15) were
  lowered in the same tuning pass. Grid-searched by edit distance against
  the one real segment with known ground truth; net effect on the first 20
  minutes of that recording: 187 characters from 13 trusted overs → 500
  from 30, no regressions in the WPM sweep or on previously-good TX segments.
- **Envelope filter**: a windowed-sinc lowpass (`LOWPASS_CUTOFF_HZ=120`,
  `LOWPASS_NTAPS=321`) replaced a plain boxcar average of the same cutoff.
  Verified against both real recordings before adopting: it measurably
  raises SNR for interference roughly 150 Hz+ away from the CW pitch (14.6dB
  → 17.0dB in one measured case), with zero effect on the 3 genuine CW QSOs
  in the "mix" round (identical decoded text in every filter/threshold
  combination tried). Interference closer than ~100 Hz genuinely overlaps
  the wanted signal's own keying spectrum — no linear filter, however
  sharp, can separate that without also cutting real fast keying; that's a
  hard limit, not a tuning problem.
- **Hysteresis thresholding**: `_hysteresis_on` (two thresholds, `THR_HI_FRAC`/
  `THR_LO_FRAC`) replaced a single static level, so noise sitting right at
  the old threshold can no longer make the on/off detection chatter.
  Synthetic Gaussian-noise sweeps didn't show a measurable difference from
  this alone; it's included because it's theoretically sound and was part of
  the combination that gave the best real-recording result, not because it
  was independently proven to matter.
- **Efficiency**: `decode_segment` now checks duration before doing any
  signal processing and returns immediately for anything longer than
  `MAX_OVER_S` — `gate_events` would reject it on duration alone regardless
  of decode quality, so there's no point running the filter/threshold
  pipeline over what can be several minutes of "listening" audio. Net effect
  across both recordings: ~2x faster overall (13.2s → 6.7s for 297 segments)
  despite the new filter needing 4x more taps than the old boxcar.

### Timing: audio structure, not the EDI clock

The EDI contest log format only stores QSO time to the minute (no seconds
field exists in the format) — a QSO logged at `09:17:43` is written as
`1117` and read back as `09:17:00`. Early versions of this tool used that
truncated time (minus a fixed pre-show margin) to decide when to flush the
CW ticker and switch QSO panels, which could land several seconds *into*
the next real over — the new over's opening characters got appended onto
the previous QSO's leftover ticker text instead of starting fresh, and the
panel/chapters/captions all switched a few seconds late for the same
reason (verified against this session's actual recording: QSO 2's real
over started at t=520.03s in the audio, but the EDI-time calculation
landed at t=527.31s — 7.3s into the over).

The fix doesn't need a better clock at all: `cluster_starts()` scans the
already-decoded WAV segments and finds every real over that immediately
follows a genuine listening gap (no trusted events, `dur > MAX_OVER_S`) —
that's the true, sub-second-precise start of a fresh burst of activity,
straight from the audio. The ticker flushes exactly there, and
`qso_windows()` snaps each QSO's approximate EDI-derived position onto one
of those bursts via `_snap_to_cluster`, so the panel, chapters, and captions
all switch at that same instant. The old `LEAD` (fixed pre-show) constant is
gone — once timing is snapped to the real over, showing the panel exactly
when it starts already gives a natural few-seconds lead (the over itself
takes a few seconds), so an artificial margin was no longer needed.

Two follow-up bugs turned up once this was checked against real recordings
(both now covered by regression tests, found test-first where practical):

- **Snap to the *latest* burst at or before the approximate time, not the
  *nearest* one.** A QSO's own over always starts before it gets logged, so
  "nearest" can jump ahead onto the *next* contact's burst if the current
  QSO took a while (calling, retries) to complete first. Caught by the user
  noticing a QSO's panel showing the timestamp of the *following* contact
  instead of its own.
- **Falling back to the first cluster when none qualify was itself a bug.**
  If a QSO's approximate time is before *every* detected burst — the very
  first QSO, or any QSO on a recording where little or no CW has been
  decoded yet — there's nothing to snap to. The old fallback jumped to the
  first cluster in the whole recording, which could be minutes away. It now
  just uses the approximate time as-is in that case (no worse than before
  this whole timing feature existed). Caught by the user on the "mix"
  session: a QSO they could hear starting at 0:26 in the video was
  chaptered at 9:28, because that session is mostly voice and the first CW
  ever decoded doesn't happen until minutes in.

That last point led to a fourth bug, also fixed: **`cluster_starts()`
originally required a segment to have decoded CW events to count as a burst
start.** A voice-mode over never carries decodable CW, so on the "mix"
session (27 voice QSOs, 3 CW) this found only 5 bursts across the whole
51-minute recording — nearly every QSO got no audio-precision benefit at
all. The fix: key on segment duration alone (`dur <= MAX_OVER_S`) instead of
requiring events. A WAV segment boundary is a precise real-world RX/TX
transition no matter what's being transmitted — CW and voice are equally
real switches. After the fix, the same session has 27 clusters, and QSO 1
(logged at 0:48 after this fix, was jumping to 9:28 before the third bug fix
above) is at least in the right *burst* now.

One tempting further idea, tried and rejected: make *every* real-over
segment a snap candidate, not just the first one per coalesced burst, to
pin down exactly which segment within a burst a specific voice QSO started
on. This actually made the CW round *worse* — QSO 2's panel, independently
verified earlier against the real audio at 520.03s, shifted to a wrong
579.14s, because a single QSO's own exchange spans several segments and
"latest candidate at or before the logged time" then lands on some later
point inside that same exchange rather than its start. Coalescing to one
candidate per burst is precisely what makes "latest cluster" mean "the
start of this exchange" — necessary, not incidental.

That gap closed without needing telemetry, from an idea of the user's:
**a burst's own first segment isn't always where a QSO starts**, if the
operator was listening (RX) before their own initiating call -- e.g. the
very first burst of the "mix" session starts mid-listen. `_tx_start` finds
the real start within a burst by exploiting two things that hold without
any PTT data: RX and TX strictly alternate (the recorder splits on every
switch), and a TX segment -- a brief call or report -- is consistently
shorter than the RX either side of it. Whichever alternating phase has the
shorter median duration is TX; its first occurrence is the real start.
Verified against the exact real burst the user identified by ear (RX
26.11s, TX 2.13s, RX 5.54s, TX 5.41s) -- QSO 1 now starts at 26.11s, not
0:00. Checked against the CW round too: it's byte-for-byte unchanged, since
every one of its bursts already happened to start on TX -- the heuristic
only ever moves a snap point *later* within its own burst, never earlier or
into a different burst.

**The user's own caveat, left unsolved**: this breaks down while calling
CQ. A stretch of many brief TX calls with only short listening gaps in
between has no single "real" start to find this way, and an earlier
fruitless call looks identical to the one that finally got answered.
Falls back to the burst's own first segment when the two phases aren't
distinguishable (equal medians, or fewer than one of each) -- no better
answer for the CQ case than that right now.

Bottom line: a CW-heavy recording ("cw", all 8 QSOs CW) gets tight,
audio-precise timing on essentially every QSO. A mostly-voice recording
("mix") now gets the operator's own real TX start for most QSOs too,
purely from segment durations -- except during CQ-calling stretches, which
remain an open problem.

This also makes the pipeline far more tolerant of clock skew between the
radio and the PC. The WAV filenames' timestamps come from the **radio's own
clock** (the IC-9700 records straight to its SD card; the WAVs are copied
off after the contest), while the EDI timestamp comes from the **PC's**
clock, via `puskas_logger` — two independent clocks, which is exactly why
`Alt+T` (radio clock sync, see below) exists. Snapping to the nearest
`cluster_starts()` burst only needs the EDI time to land closer to the
*right* real over than to any other one — comfortably true even with
several seconds, or low tens of seconds, of drift, since QSOs in a contest
are normally well over a minute apart. `Alt+T` is still worth pressing
periodically to keep that margin comfortable (and for the radio's own
displayed clock to be correct), but this timing fix no longer depends on
the radio and PC agreeing to the second the way the old EDI-time-minus-lead
calculation implicitly did.

### RX/TX + rig/rotator overlay

`--telemetry 260704-HA5LA-telemetry.jsonl` adds, top-left:

```
● TX
144.174 MHz  CW  ROT 135°
```

(`● TX` red, `● RX` green). It needs the telemetry fields `puskas_logger.py`
has been recording since it started polling rigctld's `get_ptt` alongside
freq/mode/az — recordings made before that change won't have `ptt`, and the
whole overlay is simply omitted for any segment with no `ptt` in its aligned
state rather than guessing. `freq_hz`/`mode` are shown if known; a missing
`az` falls back to `ROT ---`, same as the logger's own toolbar.

The interesting part is reconciling two different precisions: telemetry is
sampled once a second, but the WAV segment splits happen *exactly* on the
real PTT transitions (that's what triggers a new file). So the overlay's
on/off times in the video are the segment boundaries, not the telemetry
timestamps — `align_telemetry_to_segments` only uses the telemetry to decide
*which* state each already-precisely-bounded segment is in: `ptt`/`freq_hz`/
`mode` by majority vote of the samples that fall inside it (or the nearest
sample if the segment is shorter than 1 s), `az` by median.

### YouTube navigation: chapters + captions

Every run also writes `<out base>.chapters.txt` and `<out base>.srt` next to
the mp4, so you can find a QSO without scrubbing:

- **`.chapters.txt`** — paste into the YouTube video description. YouTube turns
  these into clickable seek-bar chapter markers. Format: `M:SS Title` per line,
  first line always `0:00 Start` (YouTube requires the first chapter at 0:00).
  QSOs less than 10 s after the previous chapter are dropped from this list
  (YouTube ignores chapters closer together than that) — they still get an SRT
  cue, just no separate marker.
- **`.srt`** — upload as a captions track (YouTube Studio → Subtitles). This
  gives a clickable, timestamped transcript in the sidebar — a second way to
  jump to a QSO, independent of chapters and of whether CC is toggled on. Each
  cue is capped to 8 s so it reads as a normal caption rather than persisting
  on screen until the next QSO.

Both are derived from the same start/end window used for the on-screen QSO
panel, via `qso_windows()`, so all three (panel, chapter, caption) agree on
timing.

## Telemetry file

`puskas_logger.py` writes `YYMMDD-CALL-telemetry.jsonl` to the contest CWD,
one JSON line per second:

```json
{"t": "2026-07-04T09:08:15Z", "freq_hz": 144174000, "mode": "CW", "az": 135.0}
```

| Field | Type | Notes |
|---|---|---|
| `t` | ISO 8601 UTC string | second precision |
| `freq_hz` | integer Hz | `null` when rigctld offline |
| `mode` | `"SSB"` / `"CW"` / `"FM"` | `null` when rigctld offline |
| `az` | float degrees | `null` when rotctld offline |

Size: ~70 bytes/line × 3600 lines/hour ≈ **250 KB/hour**. Keep it.

`contest_video.py` does not yet use this file; the video overlay for frequency,
mode, and rotator bearing is the next planned feature.

## IC-9700 clock sync via rigctld

**Hamlib model number**: `3081` (not 3730 as one might expect).

```
rigctl -m 3081 -r /dev/ttyUSB0 get_clock
# → 2026-07-04T20:47:00.000+00:00
```

**Quirk**: the radio ignores the seconds field when setting the clock. Always
sync on a minute boundary or the set has no effect.

**In the logger**: `Alt+T` sleeps to the next `:00` boundary, then sends:

```
\set_clock 2026-07-04T20:48:00.000+00:00
```

to rigctld and expects `RPRT 0` back. The toolbar shows
`clock sync: waiting for :00…` immediately (so you know the key registered),
then `clock synced 20:48Z` for 5 s on success.

Worst-case wait after pressing `Alt+T`: 59 s. Press it just before a minute
rolls over to minimise the wait.

**Verification**: after syncing, `get_clock` still shows `:00` seconds — that
field is always zero on read regardless. Cross-check by watching the radio's
own clock display (the menu, not `get_clock`, shows live seconds).

**Reliability quirk**: `\set_clock` over CAT is not reliable when the radio's
clock is already close to correct (only 2-3 s off) — the set silently doesn't
take. It worked fine when the clock had been deliberately desynced further via
the radio's own menu first. Needs watching before the contest: check the
radio's menu clock (which does show seconds) after pressing `Alt+T` rather
than trusting the toolbar's "synced" message alone.

## File layout for a contest session

```
~/contest-dir/
  recording/               ← WAV segments from the radio
    20260704_110713A.wav
    20260704_110716A.wav
    ...
  260704-HA5LA-2M.edi      ← QSO log (written by puskas_logger)
  260704-HA5LA-telemetry.jsonl  ← rig/rotator telemetry (written by puskas_logger)
  urhob2026cw_annotated.mp4    ← rendered video (written by contest_video)
```
