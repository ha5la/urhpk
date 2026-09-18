# 28 — Cross-correlation moves a start that is already exact

Status: resolved

`sync_webcams` places an Alt+V capture from the µs-precise UTC stamp in its own
filename — same machine, same clock as everything else in the round — prints
`exact ... no cross-correlation needed`, and then hands it to
`refine_webcam_start` anyway, which moves it.

The reasoning for running it regardless is sound and stated in the code: an
exact start fixes the constant offset, not the *rate*, and the laptop's clock
and the IC-9700's sample clock are different crystals. The rate is worth
fitting. The intercept is not: a cross-correlation of speech envelopes in 50 ms
windows cannot beat a timestamp read from the capture's own frame 0, and when
it disagrees it is the one that is wrong.

## The measurement

September round, the same fit applied to both:

```
webcam 1   intercept +4.09 s   rate −4.894 s/hour   7 anchors
webcam 2   intercept −0.65 s   rate +0.181 s/hour   9 anchors
```

Same laptop, same radio, fifteen minutes apart. A 5 s/hour disagreement between
two fits of one crystal pair is not a crystal pair. Webcam 1's anchors straddle
issue 26's 17.6 s discontinuity, so its least-squares line is fitted through two
groups of points 17.6 s apart and comes out tilted; webcam 2 starts after the
step and fits +0.181 s/hour, which is nothing.

Both intercepts are then also applied to the cast PiPs, which are placed from
their own exact asciinema header timestamps and have the same claim to being
left alone.

## What to do

Take the rate from the fit and keep the exact start. `sync_webcams` already
knows which clips are exact — `_webcam_start` returns the flag and it is already
printed. `Drift.intercept` becomes 0.0 for those, so the cast PiPs on the same
machine clock stop inheriting a correction measured against a start that did not
need correcting.

A coarse (phone-filename, whole-hour) start still takes the intercept: that is
what the correlation was written for and it has nothing better.

## Note for after this lands

With issue 26 fixed, webcam 1's anchors no longer straddle anything and its rate
should come out near webcam 2's. If it does, the round has measured the two
clocks against each other twice and agreed with itself — worth a line in
FINDINGS.md, where the +2.487 s/hour from August is still standing on a
laptop that was slewing at the time.

## Verification

A clip flagged exact keeps its start through `sync_webcams` and reports the
fitted rate; a clip not flagged exact still takes the intercept. Red against the
current code, which moves both.

## Resolved

`sync_webcams` keeps an exact start and takes only the rate; the fit's
disagreement with it is printed beside the rate. `Drift.intercept` is 0 for an
exact clip, so the cast PiPs stop inheriting it.

The note above came good. With ticket 26 fixed, September's two captures fit
**+0.233 s/h** and **+0.181 s/h** — same sign, 0.05 s/h apart, on the same laptop
and radio fifteen minutes apart, where the first previously read −4.894 s/h. Both
also wanted to move their own exact start by about the same amount (−0.68 s and
−0.65 s), which is the correlation's own bias rather than anything about the
clocks. In FINDINGS.md under the split-excess section, beside July's −0.296 s/h
and August's +0.272 s/h.
