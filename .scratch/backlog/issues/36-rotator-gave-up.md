# 36 — The rotator gave up eleven minutes in

Status: needs-info

The October round has azimuth for its first 11 minutes only; the operator
switched to the old controller, which has no position feedback, for the rest.

## What telemetry says

- 16:09–16:16: normal. Moves run ~6°/s (26° at 16:14:27 → 304° at 16:14:43, one
  reading per second).
- **From 16:16:40 it is not normal.** Readings jump (`280, 190, 234`), then
  creep at ~1° per 2–4 s, reverse, and throw single outliers far off the trend
  (123° and 108° at 16:18:16–17 among ~190°; 106° at 16:19:35 among ~113°;
  123° at 16:20:05 among ~143°). That is a position sensor or a motor in trouble, not the
  software misreading a healthy one — rotctld kept answering throughout.
- **16:20:51** — `az None`: rotctld stops answering. `ROT: ---` from here.

## Needs from the operator

- What "gave up" looked like at the controller: error, stall, power?
- Was it commanded to 144° at 16:20, or drifting on its own?
- Has it been on the bench since?

Until it is known whether this is hardware, there is nothing here for the
code to fix. If it is, the useful software change is a toolbar warning when
the bearing moves while nothing commanded it.

## Comments
