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
- CW tone: 600 Hz (IC-9700 sidetone default). Pass `--pitch` if different.
- **Trust gate**: a segment's decode is shown in the ticker only if it is
  short (< 30 s), has high SNR (≥ 20 dB), word-shaped text (≥ 50% multi-char
  tokens), and not a chopped carrier (no single character > 40% of all chars).
  This keeps all real exchanges and drops band noise / listening stretches.
- Dah-heavy CW (e.g. "CQ TEST") needs the min/max-midpoint dit estimator — a
  plain median collapses when dahs dominate.
- My own transmissions and direct partner reports decode cleanly. Received
  signals from third parties on the band are filtered by the trust gate.

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
`qso_windows()` snaps each QSO's approximate EDI-derived position onto the
nearest such burst, so the panel, chapters, and captions all switch at that
same instant. The old `LEAD` (fixed pre-show) constant is gone — once
timing is snapped to the real over, showing the panel exactly when it
starts already gives a natural few-seconds lead (the over itself takes a
few seconds), so an artificial margin was no longer needed.

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
