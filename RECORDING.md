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
own clock display.

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
