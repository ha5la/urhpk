# 02 — Constrain pytest duration

Status: ready-for-agent

Set and enforce a limit on suite runtime. Today it is ~10 s for 502 tests
(with coverage on). Is 1 s feasible? 4?

Note the history: adopting `wait_until`/`recv_until` instead of guessed sleeps
already cut the suite from ~29 s to ~3.5 s and exposed a real race the old
slack had been hiding. Coverage reporting is now a visible share of what is
left, so measure with and without it before picking a number.

A budget is only useful if something enforces it — decide whether that is
`--durations` review, a hard `--timeout`, or a CI check.
