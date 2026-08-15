# Backlog

Items carried over from the hand-written `TODO` at the repo root. That file is
a brainstorm scratchpad in no particular order; items move here as tickets and
leave there.

This is not one feature — it is the standing list. Each ticket is independent
unless it says otherwise. The numbering is a suggested order, not a
dependency chain, and it follows one rule: **things that make later work
verifiable come before the work they would verify.** The e2e test and the
pytest budget are first for that reason, and the e2e test is explicitly wanted
in the window between the structure refactor and the next round.

The `structure/` effort (compressing the project) is tracked separately and is
finished; 17 is the piece of it that outlived it, deliberately deferred behind
01.

18 is the half of 01 that could not be built yet, and it is last on purpose:
it compares a render against the previous build, so it is only meaningful once
the render has stopped changing on purpose.

| # | Ticket | Kind |
|---|---|---|
| 01 | `e2e-test` | verification |
| 02 | `pytest-budget` | verification |
| 03 | `radio-clock-sync` | reliability |
| 04 | `score-mismatch` | correctness |
| 05 | `cw-decode-follows-radio-mode` | correctness |
| 06 | `esc-should-not-clear-input` | UX bug |
| 07 | `render-progress` | UX |
| 08 | `hud-chip-animation` | UX |
| 09 | `webcam-face-tracking` | UX |
| 10 | `translucent-map-pip` | feature |
| 11 | `waterfall-text` | fun, outside the round |
| 12 | `measure-the-cleanup` | verification |
| 13 | `error-handling-principle` | principle |
| 14 | `docs-layout` | structure |
| 15 | `thread-map` | structure |
| 16 | `logger-on-one-event-loop` | structure |
| 17 | `library-package` | structure |
| 18 | `differential-render` | verification |
