# Round recording and video production

Started as notes from the URH Országos Bajnokság 2026-07-04 round (first
test run) — kept up to date since as `contest_video.py` gained features.
ARCHITECTURE.md holds the pipeline's own constraints and FINDINGS.md the
measurements and dead ends behind them; this file is the practical "how to
actually use it" companion, with real numbers from real rounds.

## Recording setup

- **Radio**: Icom IC-9700, using its built-in "Voice Recorder" mode — the radio itself
  splits audio on every RX/TX switch and names segments `YYYYMMDD_HHMMSS*.wav` in local
  time, no separate recording software involved. Each file also carries the radio's own
  frequency/mode/RX-TX metadata in its WAV `title` tag (see `contest_video.py`'s
  `parse_wav_title`/`read_wav_metadata` in ARCHITECTURE.md).
- **Format**: 16 kHz mono PCM WAV, one file per transmission (RX or TX)
- **Segments are contiguous**: sub-second gaps between files; total duration of
  all WAVs equals the round length
- **Two settings under `QSO RECORDER > Recorder Set` must be right**, and the
  radio's own default for the second one is wrong:

  | Setting | Must be | Default | If wrong |
  |---|---|---|---|
  | `File Split` | `ON` | ON | one file per 2 GB instead of one per RX/TX switch — `qso_windows.py` loses every boundary it times QSOs from |
  | `RX REC Condition` | `Always` | **`Squelch Auto`** | quiet stretches missing entirely, and segments cut on squelch transitions too, so RX and TX stop strictly alternating |

  The logger reads both when it connects and flags `■ REC SET` in the toolbar if
  either is wrong; `Alt+S` says which. Neither failure is otherwise visible
  until render time, days later.
- **Press REC before the round, then `Alt+S` in the logger to confirm it.**
  Nothing on the CI-V bus reports whether the recorder is running (FINDINGS.md),
  so the logger cannot check this one — it shows a red `SD ✗` block from
  startup until you say so, and `SD ●` afterwards.

One recording directory per round (e.g. `urhob2026cw/recording/`).
The matching EDI log lives next to it (`urhob2026cw/260704-HA5LA-2M.edi`).

## Video production

```
uv run contest_video.py RECORDING_DIR EDI_FILE [EDI_FILE ...] [-o OUT.mp4] [options]
```
Pass more than one EDI file to merge multiple bands worked in one recording
(e.g. a 2M + 70CM round) into a single timeline — a WAV segment carries no
band field, so this only matters for merging QSO lists, not for rendering.

| Option | Effect |
|---|---|
| `-o/--out` | Output path (default `contest_video.mp4`) |
| `--res 720p\|1080p` | Render resolution (default 1080p) — 720p is ~2.5× faster, good for preview |
| `--pitch HZ` | CW tone fallback (default 600 Hz) — only used if `_detect_pitch` finds nothing at all in a segment; normally auto-detected per segment |
| `--skip-gaps` | Trim listening/CQ gaps between QSOs to `GAP_KEEP_S` (3 s) each |
| `--duration SECONDS` | Chronological preview: trim to the first `SECONDS` of real round time, skip CW-decoding past the cutoff (a 10-minute preview of a 2-hour round decodes ~12× less audio) |
| `--telemetry FILE` | `*-telemetry.jsonl` — optional; adds the compass needle and the meter panel, mode-gates the CW ticker, recovers CW from long segments, and catches freq/mode changes inside one. The WAV files themselves already give RX/TX and the starting QRG/mode |
| `--input-log FILE` | `*-input.jsonl` — optional; gives exact (not audio-structure-heuristic) QSO start/end times for chapters/captions where the operator logged the QSO during this recording |
| `--cast FILE` | asciinema `.cast` recording of the logger/irssi tmux session, shown as a large picture-in-picture |
| `--webcam FILE` | Webcam/selfie clip, shown as a small picture-in-picture bottom-right |
| `--webcam-offset SECONDS` | Manual fallback sync correction for `--webcam`, bypassing automatic sync entirely |
| `--scope FILE` | `.scope` recording (from `puskas_logger.py`) — replaces the audio-derived waterfall with the radio's own spectrum wherever it covers |
| `--hud-demo OUT.png` | Write one HUD bar with dummy values and exit; needs no recording at all |
| `--hud-preview OUT.png` | Write one HUD bar built from this recording at `--hud-preview-t SECONDS`, and exit |
| `--hud-theme DIR` | HUD theme directory (`artwork.png` + `theme.json`), default the `hud-theme/` beside the script |
| `--hud-theme-check [OUT.png]` | Draw every rect in the theme back onto its artwork and exit — the way to check a hand-edited theme |
| `--keep-intermediates` | Keep the intermediate `.wav`/`.cast.mp4`/`.scope.mp4`/`.hud.mp4` files for inspection |

Render speed, measured with everything on (HUD + cast + webcam) at 720p: a
20-minute cut of the August round took ~30 minutes, i.e. ~0.67× realtime. The
HUD stage itself is a small part of that — it drew 4,355 frames for 36,000 (8×
reuse; `hud_frame_key` redraws only when something visible changed). Older
numbers from the badge-and-ticker era, with no PiPs: 720p + `--skip-gaps` ~1.4×
realtime, 1080p without `--skip-gaps` ~0.28×.

**Iterate the HUD's layout with `--hud-demo`, not with renders** — it writes a
single PNG in about a second.

### CW decoder behaviour

- Works per-segment: each WAV is one over at one speed — adaptive dit estimation
  is robust per file. Segments longer than `MAX_OVER_S` (35 s) are skipped before
  any signal processing at all, since they'd be rejected on duration alone
  regardless of decode quality.
- **A segment we only listened to can still hide CW between *other* stations**
  if it runs past `MAX_OVER_S` (e.g. we followed someone else's whole
  exchange without ever transmitting). `decode_long_segment`/`cw_subranges`
  recover this: they find telemetry-confirmed CW-mode sub-ranges inside the
  long segment and decode just those, without the duration gate (the
  sub-range's own length isn't suspicious the way an unexplained long
  segment is — telemetry mode confirmation is already stronger evidence of
  genuine CW than length). Needs `--telemetry`; without it, nothing inside
  an over-length segment is ever recovered.
- **CW tone is auto-detected per segment**, not assumed to be 600 Hz (IC-9700
  sidetone default) for the whole round — `--pitch` is now only a fallback
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
  short (< `MAX_OVER_S`, 35 s), has high SNR (≥ `MIN_SNR_DB` = 20 dB), word-shaped text
  (`MIN_QUALITY` ≥ 0.5, i.e. ≥ 50% multi-char tokens), and not a chopped carrier (no single
  character > `MAX_DOMINANCE` = 40% of all chars *once there's enough text for that pattern to mean
  anything* — `MIN_CHARS_FOR_DOMINANCE` = 5, see below). This keeps all real
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
`1117` and read back as `09:17:00`. Early versions used that truncated time
(minus a fixed pre-show margin) to decide when a QSO began, which could land
several seconds *into* the next real over — chapters and captions all switched
late for the same reason (verified against a real recording: QSO 2's over
started at t=520.03s in the audio, but the EDI-time calculation landed at
t=527.31s, 7.3s in).

The fix doesn't need a better clock at all: `burst_starts()` scans the
already-decoded WAV segments and finds every real over that immediately
follows a genuine listening gap (no trusted events, `dur > MAX_OVER_S`) —
that's the true, sub-second-precise start of a fresh burst of activity,
straight from the audio. `qso_windows()` snaps each QSO's approximate
EDI-derived position onto one of those bursts via `_snap_to_burst`, so
chapters and captions land on the real over. (The CW ticker no longer takes
part in any of this: it scrolls on a clock, so a character's position comes
from when it was keyed and nothing can go stale on it.)

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
  round: a QSO they could hear starting at 0:26 in the video was
  chaptered at 9:28, because that round is mostly voice and the first CW
  ever decoded doesn't happen until minutes in.

That last point led to a fourth bug, also fixed: **`burst_starts()`
originally required a segment to have decoded CW events to count as a burst
start.** A voice-mode over never carries decodable CW, so on the "mix"
round (27 voice QSOs, 3 CW) this found only 5 bursts across the whole
51-minute recording — nearly every QSO got no audio-precision benefit at
all. The fix: key on segment duration alone (`dur <= MAX_OVER_S`) instead of
requiring events. A WAV segment boundary is a precise real-world RX/TX
transition no matter what's being transmitted — CW and voice are equally
real switches. After the fix, the same round has 27 clusters, and QSO 1
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
very first burst of the "mix" round starts mid-listen. `_tx_start` finds
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
off after the round), while the EDI timestamp comes from the **PC's**
clock, via `puskas_logger` — two independent clocks, which is exactly why
`Alt+T` (radio clock sync, see below) exists. Snapping to the nearest
`burst_starts()` burst only needs the EDI time to land closer to the
*right* real over than to any other one — comfortably true even with
several seconds, or low tens of seconds, of drift, since QSOs in a round
are normally well over a minute apart. `Alt+T` is still worth pressing
periodically to keep that margin comfortable (and for the radio's own
displayed clock to be correct), but this timing fix no longer depends on
the radio and PC agreeing to the second the way the old EDI-time-minus-lead
calculation implicitly did.

**`--input-log` removes the audio-structure guesswork where it's available**,
rather than replacing it entirely. `puskas_logger.py` writes one `'qso'`
event per logged QSO to `*-input.jsonl`, timestamped at the exact moment
the operator hit Enter — `match_qso_times` pairs each EDI QSO to its event
by callsign, in chronological order rather than by minute, so a log whose
timestamps disagree with the EDI's still matches. `qso_windows()` then uses that exact time instead of the EDI's
minute-truncated one as the anchor into `_snap_to_burst`, and uses it
directly (not the next QSO's start) as the window's *end* wherever known —
the moment logging finished, not whenever the next over happens to begin.
Falls back to the plain audio-structure heuristics above wherever a QSO has
no matching event (a `--duration` cut that excludes it, or a recording made
before the logger wrote these files).

### The HUD status bar

Everything the video used to draw as separate overlays — the RX/TX badge, the
CW ticker, QSO panels, a running score, a UTC clock, a typewriter of what was
typed — is now either in the HUD along the bottom or visible directly in the
terminal PiP. There is no subtitle/overlay stage left at all.

The bar is modelled on DOOM's status bar: the more important a value, the
bigger it is drawn. SCORE and QSOS take the health and ammo slots, the webcam
takes DOOMguy's face slot, and the rest of the panels carry QRG, band/mode,
an RX/TX lamp, an S-meter, a compass, UTC/rate/ODX and the CW ticker. It is a
piece of artwork (`hud-theme/`) with values drawn into its recesses — see
ARCHITECTURE.md's HUD section for what that means when editing, and
`hud-theme/artwork-prompt.md` for the artwork itself.

Where each value comes from:

| Panel | Source |
|---|---|
| SCORE / QSOS / rate / ODX | the EDI log(s) |
| QRG, band/mode chips, RX/TX lamp | the WAV files' own IC-9700 metadata, refined by `--telemetry` within long segments |
| compass: solid needle | `--telemetry` rotator azimuth, interpolated between samples |
| compass: hollow needle | bearing to the station being worked, computed from its EDI locator |
| S-meter | `--scope`, from the sweep's own centre bins — reads empty without one |
| Vd / A | `--telemetry` meter records — placeholders on every recording to date |
| CW ticker | the decoder, mode-gated by `--telemetry` |

RX/TX is the one thing the terminal PiP genuinely cannot show: `puskas_logger`
has no way to know the rig's real PTT state until the WAV files are pulled off
the SD card afterwards and their metadata read back.

`--telemetry` remains optional. Without it you lose the rotator needle, the
meter panel, mode-gating of the ticker, recovery of CW from long listened-to
segments, and any freq/mode change that happens *inside* a long segment — but
the bar itself works from the WAV files alone.

### YouTube navigation: chapters + captions

Every run also writes `<out base>.chapters.txt` and `<out base>.srt` next to
the mp4, so you can find a QSO without scrubbing:

- **`.chapters.txt`** — paste into the YouTube video description. YouTube turns
  these into clickable seek-bar chapter markers. Format: `M:SS Title` per line,
  first line always `0:00 Start` (YouTube requires the first chapter at 0:00).
  QSOs less than `MIN_CHAPTER_GAP_S` (10 s) after the previous chapter are dropped from this list
  (YouTube ignores chapters closer together than that) — they still get an SRT
  cue, just no separate marker.
- **`.srt`** — upload as a captions track (YouTube Studio → Subtitles). This
  gives a clickable, timestamped transcript in the sidebar — a second way to
  jump to a QSO, independent of chapters and of whether CC is toggled on. Each
  cue is capped to `CAPTION_DUR_S` (8 s) so it reads as a normal caption rather than persisting
  on screen until the next QSO.

Both are derived from the same start/end window used for the on-screen QSO
panel, via `qso_windows()`, so all three (panel, chapter, caption) agree on
timing — and both get the `--input-log` precision improvement described
above wherever a matching event exists, not just the audio-structure fallback.

## Terminal picture-in-picture (`--cast`)

`--cast FILE` takes an [asciinema](https://asciinema.org/) (cast v2)
recording of the tmux session running irssi + `puskas_logger.py` during the
round, and shows it as a large picture-in-picture — the dominant visual
element, since the terminal is most of what there is to watch. It
replaces what used to be separate QSO panels, running-score header, UTC
clock, and typewriter overlay, all of which are just visible directly in the
real logger UI now.

Sync is exact: the cast file's header carries a real Unix-epoch start
timestamp (`parse_cast_header`), so there's no filename-parsing or
whole-hour-rounding ambiguity the way there is for an independent webcam
device (below). Rendering the cast is its own pipeline stage
(`render_cast_video`, using `pyte` to replay the terminal escape codes and
Pillow to draw them), producing a standalone intermediate mp4 that the main
pass then composites — the same pattern the scope and HUD stages use.

See PIPELINE.md for how the recording is actually made
(`run-recorded-round.sh` does it automatically) and
ARCHITECTURE.md's `--cast` notes for the tmux/pyte implementation details (dirty-row-only redraw for render speed, the
DECSLRM/SU/SD terminal-emulation fixes needed because the recording is made
*inside* tmux, and the PiP's aspect-ratio/layout constants).

## Webcam picture-in-picture (`--webcam`)

`--webcam FILE` adds a small, muted picture-in-picture in the bottom-right
corner. Two different sync paths exist depending on how the clip was made:

- **Recorded via `puskas_logger.py`'s own Alt+V capture** (same machine as
  the logger, same `datetime.now(timezone.utc)` clock as every QSO/keystroke):
  exact sync, no cross-correlation needed. The file itself is renamed about
  a second in with a µs-precise UTC timestamp baked into the filename (e.g.
  `foo-webcam.mp4` -> `foo-webcam-20260706T160037.123456Z.mp4`) —
  `parse_webcam_precise_filename` reads it straight off the filename, no
  extra file needed. Renaming that early — the moment ffmpeg logs frame 0,
  with the capture still running — is what makes the timestamp survive a
  power cut or a `kill -9`, where nothing runs at the end to apply it. This
  was chosen over tagging the timestamp into the
  mp4's own container metadata after capture: that was tested against a
  real ~2h/3GB file and does work (a 15s stream-copy remux), but needs a
  full second copy of the file on disk at the same time — too risky right
  when a round ends and disk space is tightest. A rename needs none of
  that (verified: 0.006s on a 3GB file, a directory-entry update
  independent of size). Falls back to `webcam_start_wall` (the
  `*-input.jsonl` `webcam_start` event, ~1s early) for a recording made
  before the rename existed.
- **An independent recording (e.g. a phone propped up separately)**: the
  phone has its own clock convention, not necessarily the WAV recorder's —
  in the first real use of this path the WAV recorder stamped filenames in
  plain UTC while the phone stamped its own in local wall time.
  `sync_webcam_start` derives the phone's whole-hour offset from its
  filename timestamp; `refine_webcam_start` then corrects both a residual
  sub-hour offset *and* a linear clock-drift rate by cross-correlating the
  operator's own voice between the two devices' audio tracks (confirmed
  against a real ~2h round: 2.73s off with the coarse offset alone, 0.07s
  off after the rate correction). `--webcam-offset SECONDS` bypasses all of
  this with a fixed manual correction — for a clip with no audio track, or
  wherever cross-correlation finds no confident match.

Puskás Kupa rounds should prefer the Alt+V logger-recorded path now that
it exists — it's simpler and exactly synced by construction; the phone path
remains for older recordings or if Alt+V wasn't used.

## Telemetry file

`puskas_logger.py` writes `YYMMDD-CALL-telemetry.jsonl` to the round's CWD,
**one line per actual change**, with microsecond timestamps. Records are
*partial* by source — the rig writes one kind, the rotator another:

```json
{"t": "2026-08-03T16:05:52.174391Z", "freq_hz": 144174000, "mode": "CW"}
{"t": "2026-08-03T16:05:53.002118Z", "az": 358.0}
```

| Field | Type | Notes |
|---|---|---|
| `t` | ISO 8601 UTC string | microsecond precision (whole seconds in pre-2026-08 files) |
| `freq_hz` | integer Hz | the radio's exact value; `null` marks the radio going offline |
| `mode` | `"SSB"` / `"CW"` / `"FM"` | `null` marks the radio going offline |
| `az` | float degrees | `null` marks the rotator going offline |
| `vd` / `id` | integer raw | meter readings, stored raw and converted at render time |

A field a line doesn't mention simply didn't change, and carries forward. An
**absent** field and an explicit **`null`** mean opposite things: silence is
another source's record saying nothing, whereas a null is that device going
offline and ends the carry-forward.

No `ptt` field: the WAV files' own IC-9700 metadata already carries RX/TX with
zero polling lag (see Recording setup above), so a polled copy was just
reconstructing the same thing with more latency.

Being change-driven makes it small and sharp — an earlier 1 Hz sampler wrote
9313 lines for one round of which only 616 carried anything new, and blurred
any retune shorter than its interval. Keep the file: it is optional for
`contest_video.py` (see the HUD section above) but everything it adds is
unrecoverable afterwards.

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
the radio's own menu first. Needs watching before the round: check the
radio's menu clock (which does show seconds) after pressing `Alt+T` rather
than trusting the toolbar's "synced" message alone.

## File layout for a round

```
~/contest-dir/
  recording/                    ← WAV segments from the radio (IC-9700 Voice Recorder)
  260704-HA5LA-2M.edi           ← QSO log (written by puskas_logger)
  260704-HA5LA-telemetry.jsonl  ← rig/rotator telemetry (written by puskas_logger, optional input)
  260704-HA5LA-input.jsonl      ← keystroke + QSO + webcam start/stop events (written by puskas_logger)
  260704-HA5LA.cast             ← asciinema recording of the logger/irssi tmux session
  260704-HA5LA-webcam-…Z.mp4    ← Alt+V webcam capture (written by puskas_logger, optional)
  260704-HA5LA-webcam-…Z.log    ← ffmpeg capture log; the timestamp in that filename came from it
  260704-HA5LA.scope            ← radio sweeps (written by puskas_logger, optional input)
  urhob2026cw_annotated.mp4     ← rendered video (written by contest_video.py)
  urhob2026cw_annotated.mp4.chapters.txt  ← paste into the YouTube description
  urhob2026cw_annotated.mp4.srt           ← upload as a YouTube captions track
```
