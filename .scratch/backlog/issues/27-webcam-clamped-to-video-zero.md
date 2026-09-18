# 27 — A webcam that started before the audio is pinned to t=0

Status: resolved

`_webcam_start` places a clip with `audio_time_for`, which clamps anything
earlier than the first WAV segment to `segs[0].audio_t` — i.e. to video t=0.
`stream_start` exists for exactly this case and says so in its own docstring:

> *A cast or scope recording clamped to 0 gets its frame 0 pinned to video
> t=0, so everything in it reads late by the whole cast-to-WAV gap (25 s in
> the dry-run that caught it, as the cast PiP's clock lagging the round).
> This is the normal case, not an edge case.*

The cast branch and the scope branch call it. The webcam branch does not.

## The measurement

September round, webcam 1 (`…-webcam-20260907T155820.427445Z.mp4`):

```
first WAV segment      15:58:38.530
webcam frame 0         15:58:20.427      →  18.10 s before the audio
stream_start           −18.102555
audio_time_for           0.0             ← what the code used
```

Alt+V is pressed before the radio's recorder is switched on, every time — the
same reason the cast is always early. So the webcam PiP ran 18.1 s behind the
round, plus 4.09 s of cross-correlation "refinement" on top (issue 28), for a
total of 22.2 s. The operator timed the first TX period against a stopwatch at
about 23 s.

It stopped being visible 39 minutes in, when issue 26's 17.6 s step pushed the
HUD back by almost exactly the same amount and the two errors cancelled. That
is why this was reported as "the webcam is off at the start" rather than "the
webcam is off".

Webcam 2 started after the audio, so it was never clamped and was placed
correctly.

## What to do

`_webcam_start` uses `stream_start`. `render()` already turns a negative start
into an `-ss` seek into the clip (`_stream_input_args`), so the webcam branch
needs no other change — except that it currently builds its own input args
inline with a bare `-itsoffset` and must go through `_stream_input_args` too,
or a negative start becomes a negative `-itsoffset`.

## Verification

A clip whose wall-clock start is before `segs[0].wall` gets a negative start.
Red against the current code, which returns 0.0.

## Resolved

`_webcam_start` uses `stream_start`, and `render()` takes the webcam input
through `_stream_input_args` like the cast and scope branches. September's first
capture is placed at −18.10 s now, not 0.
