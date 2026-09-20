# 30 — The scope waterfall runs ~800 ms behind the audio

Status: resolved

Reported after the webcam sync was fixed: the PiP lines up, the waterfall does
not. `.scope` sweeps carry `time.time()` from the moment they *arrived* over the
LAN — the radio swept earlier, and nothing accounted for the difference. The
scope was also the only stream with no clock-drift correction at all: the cast
borrows the webcam's fit, the scope borrowed nothing.

## The measurement

The scope's amplitude in the bins around a segment's own QRG against that
segment's audio envelope. A keyed CW signal makes the same shape in both.

| output time | scope late by | corr |
|---|---|---|
| 745 s | +0.80 s | 0.79 |
| 1322 s | +0.70 s | 0.82 |
| 3518 s | +0.65 s | 0.89 |
| 4761 s | +0.55 s | 0.83 |

21 segments, two outliers at corr <= 0.5. The constant is the arrival path; the
slope, −0.22 s/h, is the laptop-vs-radio clock the webcam already measures.

## What to do

`refine_scope_start`, in the shape `refine_webcam_start` already has: anchors,
`envelope_offset`, `robust_line_fit`, returning (start, rate, n). The render's
scope branch takes `setpts=PTS/(1-rate)` like the webcam branch, and the
corrected start goes into its `-itsoffset`.

## Verification

A synthetic round whose sweeps are stamped 0.8 s late: the placement the
renderer would use is +0.8 s, and the fit must pull it back to 0.

## Resolved

September: arrival 0.83 s behind the audio, rate +0.201 s/h from 17 anchors --
the third independent measurement of that clock pair on one round, against
+0.194 and +0.214 s/h from the two webcam captures.
