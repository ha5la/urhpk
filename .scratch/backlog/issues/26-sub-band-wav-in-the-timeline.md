# 26 — The sub band's WAV is concatenated into the main timeline

Status: ready-for-agent

Dual watch was on for about eighteen seconds in the September round. The sub
band recorded those eighteen seconds to its own file, `scan_segments` picked it
up as if it were the next main-band segment, and every stream that is placed by
a constant offset — the cast PiP, the scope background, the webcam — ran 17.6 s
ahead of the HUD for the remaining 103 minutes of `contest_video.mp4`.

FINDINGS.md:299 called this a month before it happened, on a deliberate test
recording: *"So `contest_video.py` globbing all `*.wav` would interleave two
incompatible series"*. It still globs them. This round is the first with a B
file in it — 1,793 WAVs across the four rounds before it, all `A`.

## The measurement

`recording/20260907_163459B.wav`, 17.64 s, sub band, recorded while
`…163436A.wav` (172.29 s) was already covering that wall-clock time. The
telemetry shows dual watch coming on: at `16:35:03.885` a second `freq_hz`
stream appears (145.400 SSB) beside the main band's 432.200.

```
with the B file: 682 segs  wall span 8528.888 s  audio 8546.580 s  audio − wall = +17.692 s
A files only:    681 segs  wall span 8528.888 s  audio 8525.154 s  audio − wall =  −3.734 s
```

The −3.734 s is `compensate_split_excess` doing its job (681 × 5.57 ms =
3.79 s). So with the B file out, the assembled audio matches the wall clock it
spans to within 60 ms over 2 h 22 m. With it in, the audio is 17.7 s longer
than the round.

## Why that becomes visible skew

The HUD asks `audio_time_for` where a wall-clock moment lands, so the HUD
follows the audio wherever it goes. The cast, the scope and the webcam each get
*one* number from `stream_start`, computed once at their own first frame. The
moment the audio timeline gains 17.6 s the audio has no wall-clock claim on,
those two ways of telling the time part company and never come back.

Read off the rendered frames (HUD clock vs the logger's own toolbar clock in
the cast PiP):

| video t | HUD | cast | skew |
|---|---|---|---|
| 60 | 15:59:38 | 15:59:34 | −4 s |
| 2320 | 16:37:18 | 16:37:17 | −1 s |
| **2400** | 16:38:20 | 16:38:37 | **+17 s** |
| 4200 | 17:08:21 | 17:08:39 | +18 s |
| 4600 | 17:15:00 | 17:15:01 | +1 s |
| 8400 | 18:18:20 | 18:18:21 | +1 s |

It falls back to +1 s at 4600 only because the logger was restarted at 17:13 and
cast 2 was anchored *after* the step. The step is at video t=2330, 38:50.

## What to do

`scan_segments` takes the `A` series and nothing else. The band letter is the
16th character of the filename, right after the `YYYYMMDD_HHMMSS` stamp that
function already parses, and FINDINGS.md:328 has already established that the
suffix is where band identity lives — the title tag's second slot is not it.

Say so in the render's own output when a file is skipped, the same way the
split-excess line is printed: a round that had dual watch on should be able to
say so from its log.

Out of scope: doing anything *with* the sub band. It is a second audio stream
for a second band and the video has one. Skipping it is the whole fix.

## Verification

A directory with two A files and a B file overlapping them: `scan_segments`
returns the two A files, and the summed duration equals the wall-clock span.
Red against the current code, which returns three.
