# 01 — End-to-end test

Status: ready-for-agent

Manually simulate a round, collect all the data it produces, render a video,
check the result. Wanted specifically **in the window between the structure
refactor and the next round** — the refactor moves nearly every file, and the
suite does not cover how ffmpeg filter branches combine or how a drawn frame
actually looks.

This is the highest-value item on this list, and everything else that touches
the render is safer once it exists.

## The open questions, settled

**What "end to end" means**: not the radio, the webcam and the SD card. The
seam is the files a round leaves behind, and each half is driven from its own
side of that seam.

**The oracle**: `chapters.txt` and `.srt`. They are exact, and on a real round
they are ready about a second in — `--no-video` stops there. Golden frames were
never needed.

**CI**: no. The fixture is a recorded round kept outside git, and a full render
is minutes on a machine whose ffmpeg build pins the bytes. `-m smoke` locally,
plus the pre-round manual run.

## What exists

**The logger half** — `tests/test_logger_smoke.py`. Drives `puskas_logger.py`
through a pty: startup prompts, offline band/mode wizard, one QSO, Ctrl-D, then
asserts on the captured screen and the files left behind, plus the rig server,
SIGTERM and SIGHUP-with-the-terminal-gone. It caught a real `UnboundLocalError`
in `main()` that 584 unit tests did not, because nothing in the suite ever calls
`main()`.

**The render half** — `tests/test_render_smoke.py`. Runs `contest_video.py
--no-video` over `test/`, a recorded 169-second round with three QSOs across FM,
SSB and CW, and compares the chapters and captions against the manual run's own
output. Cast, scope, telemetry and input log are all passed — none can move a
caption, but a reader that no longer parses a real recording is a regression
nothing else would catch. Webcams are left out: their audio cross-correlation is
the slow part and cannot affect the oracle. ~1 s per run; skips when `test/` is
absent.

Both are `-m smoke`, 9 s together, deselected from the default run.

## What is left

**The differential render — `git worktree` + `md5sum`.** Rendering the same
3-minute cut of the August round from a pre-refactor worktree and from the
working tree produced a byte-identical MP4: no frame comparison, no tolerance,
no fixtures to maintain. It only answers "did this change move the output",
which is exactly the question a refactor asks and exactly the wrong question
while the render is being changed on purpose. Deferred until the render settles.

Measured cost, for whenever that is: a full 720p render of `test/` with every
side input is 5m42s on four cores, ~14 min at 1080p.

Nothing here judges whether the picture is *right*. That stays with the manual
run before the round.
