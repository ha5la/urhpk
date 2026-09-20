# 29 — The exact stamp is late by the capture latency

Status: resolved

Issue 28 stopped the correlation from moving an exact start, on the grounds
that a µs-precise stamp off this machine's own clock cannot be bettered by an
RMS-envelope correlation in 50 ms windows. The precision is real; the accuracy
is not. `-use_wallclock_as_timestamps` stamps a frame when ffmpeg *receives*
it, so the exposure, the USB transfer and the driver's buffers all sit between
the scene and the number in the filename.

Reported on the rendered September round as a PiP about a second behind, worse
at some points than others.

## The measurement

Re-ran the correlation anchor by anchor against the round's own audio, at
40-anchor sampling:

| clip | confident anchors | correction | residual std |
|---|---|---|---|
| 1 (4413 s) | 21 of 25 | −0.70 s → −0.40 s | 0.036 s |
| 2 (3973 s) | 18 of 19 | −0.40 s → −0.30 s | 0.042 s |

Both clips, recorded 75 minutes apart, agree on the sign at a residual std no
bigger than the correlation window. The render was discarding all of it and
printing it as a parenthetical: `the start stays exact at 48.72s (the fit
wanted -0.68s)`.

Issue 28's +4.09 s fit had a different cause — its anchors straddled issue 26's
17.6 s discontinuity and the least-squares line came out tilted. With that
fixed, the two clips' fits agree.

## What to do

Apply the fit's intercept to an exact clip when the fit is one a camera could
explain: negative (latency cannot deliver a frame before it was stamped) and
within `MAX_CAPTURE_LATENCY_S`. `Drift.intercept` stays 0 for an exact clip —
issue 28's real bug was the cast PiPs inheriting the webcam's correction, and
a cast shoots through no camera.

## Verification

A fit of −0.66 s on an exact clip moves it; +4.09 s does not. A 10-minute 720p
render with the correction applied by hand (`--webcam-offset -0.68`) was
judged "not perfect, but much better" against the same round rendered without
it.

## Resolved

September's clips are placed at 48.04 s and 4489.28 s, −0.68 s and −0.65 s off
their filename stamps; both casts still take intercept +0.00 s.

What is left, unfixed: the clip's own audio leads its own video by ~81 ms (the
capture log's two input headers are 0.081 s apart and both streams are written
starting at 0), and the correlation aligns the audio. See FINDINGS.md.
