# Contest recording and video production

Started as notes from the URH Országos Bajnokság 2026-07-04 session (first
test run) — kept up to date since as `contest_video.py` gained features.
See CLAUDE.md's `contest_video.py` section for the full design history and
rationale; this file is the practical "how to actually use it" companion,
with real numbers from real sessions where available.

## Recording setup

- **Radio**: Icom IC-9700, using its built-in "Voice Recorder" mode — the radio itself
  splits audio on every RX/TX switch and names segments `YYYYMMDD_HHMMSS*.wav` in local
  time, no separate recording software involved. Each file also carries the radio's own
  frequency/mode/RX-TX metadata in its WAV `title` tag (see `contest_video.py`'s
  `parse_wav_title`/`read_wav_metadata` in CLAUDE.md).
- **Format**: 16 kHz mono PCM WAV, one file per transmission (RX or TX)
- **Segments are contiguous**: sub-second gaps between files; total duration of
  all WAVs equals the session length

One recording directory per round (e.g. `urhob2026cw/recording/`).
The matching EDI log lives next to it (`urhob2026cw/260704-HA5LA-2M.edi`).

## Video production

```
uv run contest_video.py RECORDING_DIR EDI_FILE [EDI_FILE ...] [-o OUT.mp4] [options]
```
Pass more than one EDI file to merge multiple bands worked in one recording
(e.g. a 2M + 70CM session) into a single timeline — a WAV segment carries no
band field, so this only matters for merging QSO lists, not for rendering.

| Option | Effect |
|---|---|
| `-o/--out` | Output path (default `contest_video.mp4`) |
| `--res 720p\|1080p` | Render resolution (default 1080p) — 720p is ~2.5× faster, good for preview |
| `--pitch HZ` | CW tone fallback (default 600 Hz) — only used if `_detect_pitch` finds nothing at all in a segment; normally auto-detected per segment |
| `--skip-gaps` | Trim listening/CQ gaps between QSOs to `GAP_KEEP_S` (3 s) each |
| `--duration SECONDS` | Chronological preview: trim to the first `SECONDS` of real session time, skip CW-decoding past the cutoff (a 10-minute preview of a 2-hour session decodes ~12× less audio) |
| `--telemetry FILE` | `*-telemetry.jsonl` — optional; the WAV files' own metadata already gives RX/TX plus starting QRG/mode, this only adds mode-gating for the CW ticker/long-segment recovery (see below) |
| `--input-log FILE` | `*-input.jsonl` — optional; gives exact (not audio-structure-heuristic) QSO start/end times for chapters/captions where the operator logged the QSO during this recording |
| `--seed-input-log OUT.jsonl` | Write a hand-editable QSO-timestamp skeleton from the EDI(s) and exit without rendering — for a recording made before `--input-log` existed |
| `--cast FILE` | asciinema `.cast` recording of the logger/irssi tmux session, shown as a large picture-in-picture |
| `--webcam FILE` | Webcam/selfie clip, shown as a small picture-in-picture bottom-right |
| `--webcam-offset SECONDS` | Manual fallback sync correction for `--webcam`, bypassing automatic sync entirely |
| `--keep-ass` | Keep intermediate `.ass`/`.wav` files for inspection |
| `--contest TEXT` | Contest name text (default `"URH OB 2026 - CW"`) |

Render speed measured on the first (badge+ticker only, no PiPs) session:
720p + `--skip-gaps`: ~1.4× realtime. 1080p, no `--skip-gaps`: ~0.28× realtime
(~2.5 h for a 42-minute session). Adding `--cast`/`--webcam` costs more —
compositing extra picture-in-picture streams in the same ffmpeg pass — but
hasn't been benchmarked separately from the above numbers.

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

**`--input-log` removes the audio-structure guesswork where it's available**,
rather than replacing it entirely. `puskas_logger.py` writes one `'qso'`
event per logged QSO to `*-input.jsonl`, timestamped at the exact moment
the operator hit Enter — `match_qso_times` pairs each EDI QSO to its event
by callsign, in chronological order (not by exact minute — an edited seed
log, see `--seed-input-log`, can freely move a timestamp across a minute
boundary). `qso_windows()` then uses that exact time instead of the EDI's
minute-truncated one as the anchor into `_snap_to_cluster`, and uses it
directly (not the next QSO's start) as the window's *end* wherever known —
the moment logging finished, not whenever the next over happens to begin.
Falls back to the plain audio-structure heuristics above wherever a QSO has
no matching event (no input log, an older recording, or a `--duration` cut
that excludes it). `--seed-input-log OUT.jsonl` bootstraps one for a
recording made before this file was ever produced: it writes one event per
EDI QSO with a placeholder minute-truncated timestamp, exits without
rendering, and you hand-edit each `t` against the audio before passing the
result back in as `--input-log`.

### RX/TX badge

The only overlay `contest_video.py` still burns into the video itself
(besides the CW ticker) is a small top-left `● TX` / `● RX` indicator —
everything the video used to render on its own (timestamp, QSO panels,
running score, band/mode/callsign text, what was typed) is now visible
directly in the terminal-session picture-in-picture (`--cast`, see below),
which shows the actual logger UI live rather than a reconstruction of it.
The badge used to also show a QRG/mode/rotator-bearing line underneath the
dot; that line was dropped as redundant once the terminal PiP existed (the
same info is legible in the logger's own toolbar there) and because its
second line overlapped the cast box at 720p.

RX/TX state comes from the WAV files' own IC-9700 metadata (see Recording
setup above) — the one thing the terminal PiP *can't* show, since
`puskas_logger` has no way to know the rig's actual PTT state until the WAV
files are pulled off the SD card and read back after the session.
`--telemetry` is no longer needed for the badge itself; its remaining jobs
are internal to the ticker rather than anything displayed:
- **Mode-gating**: a segment's decoded text is only trusted as CW in the
  ticker if telemetry's own mode for that stretch agrees (or telemetry
  wasn't available) — the decoder runs blind on every segment since there's
  no way to know the mode in advance, and a strong tone in voice audio can
  occasionally slip past the trust gate otherwise.
- **Recovering CW from long listened-to segments** — see
  `decode_long_segment` above, which needs telemetry to confirm a sub-range
  really was CW mode.

Rotator bearing (`az`) is still computed internally from telemetry (median
per segment) but isn't displayed anywhere in the current design — dead
weight kept around rather than actively used, now that the line it fed is
gone.

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

## Terminal-session picture-in-picture (`--cast`)

`--cast FILE` takes an [asciinema](https://asciinema.org/) (cast v2)
recording of the tmux session running irssi + `puskas_logger.py` during the
contest, and shows it as a large picture-in-picture — the dominant visual
element, since the terminal session is most of what there is to watch. It
replaces what used to be separate QSO panels, running-score header, UTC
clock, and typewriter overlay, all of which are just visible directly in the
real logger UI now.

Sync is exact: the cast file's header carries a real Unix-epoch start
timestamp (`parse_cast_header`), so there's no filename-parsing or
whole-hour-rounding ambiguity the way there is for an independent webcam
device (below). Rendering the cast is its own pipeline stage
(`render_cast_video`, using `pyte` to replay the terminal escape codes and
Pillow to draw them), producing a standalone intermediate mp4 before the
main waterfall/ASS pass.

See CLAUDE.md's "Recording the logger session" section for how to actually
make the recording (`run-recorded-contest-session.sh` does this
automatically now), and its `--cast` section for the tmux/pyte
implementation details (dirty-row-only redraw for render speed, the
DECSLRM/SU/SD terminal-emulation fixes needed because the recording is made
*inside* tmux, and the PiP's aspect-ratio/layout constants).

## Webcam picture-in-picture (`--webcam`)

`--webcam FILE` adds a small, muted picture-in-picture in the bottom-right
corner. Two different sync paths exist depending on how the clip was made:

- **Recorded via `puskas_logger.py`'s own Alt+V capture** (same machine as
  the logger, same `datetime.now(timezone.utc)` clock as every QSO/keystroke):
  exact sync, no cross-correlation needed. The file itself is renamed on
  stop with a µs-precise UTC timestamp baked into the filename (e.g.
  `foo-webcam.mp4` -> `foo-webcam-20260706T160037.123456Z.mp4`) —
  `parse_webcam_precise_filename` reads it straight off the filename, no
  extra file needed. This was chosen over tagging the timestamp into the
  mp4's own container metadata after capture: that was tested against a
  real ~2h/3GB file and does work (a 15s stream-copy remux), but needs a
  full second copy of the file on disk at the same time — too risky right
  when a session ends and disk space is tightest. A rename needs none of
  that (verified: 0.006s on a 3GB file, a directory-entry update
  independent of size). Falls back to `webcam_start_from_log` (the same
  precision, parsed from the `*-webcam.log` ffmpeg capture log) or
  `webcam_start_wall` (the `*-input.jsonl` `webcam_start` event, ~1s early)
  for a recording made before the rename existed.
- **An independent recording (e.g. a phone propped up separately)**: the
  phone has its own clock convention, not necessarily the WAV recorder's —
  in the first real use of this path the WAV recorder stamped filenames in
  plain UTC while the phone stamped its own in local wall time.
  `sync_webcam_start` derives the phone's whole-hour offset from its
  filename timestamp; `refine_webcam_start` then corrects both a residual
  sub-hour offset *and* a linear clock-drift rate by cross-correlating the
  operator's own voice between the two devices' audio tracks (confirmed
  against a real ~2h session: 2.73s off with the coarse offset alone, 0.07s
  off after the rate correction). `--webcam-offset SECONDS` bypasses all of
  this with a fixed manual correction — for a clip with no audio track, or
  wherever cross-correlation finds no confident match.

Puskás Kupa sessions should prefer the Alt+V logger-recorded path now that
it exists — it's simpler and exactly synced by construction; the phone path
remains for older recordings or if Alt+V wasn't used.

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

No `ptt` field: it used to be recorded here too, but the WAV files' own
IC-9700 metadata already carries RX/TX with zero polling lag (see Recording
setup above), so a separate 1 Hz-polled copy was just reconstructing the
same thing with more latency — removed rather than kept for redundancy.

Size: ~70 bytes/line × 3600 lines/hour ≈ **250 KB/hour**. Keep it — it's
optional for `contest_video.py` (mode-gating the ticker and recovering CW
from long listened-to segments, see "RX/TX badge" above), not required.

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
  recording/                    ← WAV segments from the radio (IC-9700 Voice Recorder)
  260704-HA5LA-2M.edi           ← QSO log (written by puskas_logger)
  260704-HA5LA-telemetry.jsonl  ← rig/rotator telemetry (written by puskas_logger, optional input)
  260704-HA5LA-input.jsonl      ← keystroke + QSO + webcam start/stop events (written by puskas_logger)
  260704-HA5LA.cast             ← asciinema recording of the logger/irssi tmux session
  260704-HA5LA-webcam.mp4       ← Alt+V webcam capture (written by puskas_logger, optional)
  260704-HA5LA-webcam.log       ← ffmpeg capture log for the above (exact sync timestamp)
  urhob2026cw_annotated.mp4     ← rendered video (written by contest_video.py)
  urhob2026cw_annotated.mp4.chapters.txt  ← paste into the YouTube description
  urhob2026cw_annotated.mp4.srt           ← upload as a YouTube captions track
```
