# 01 — End-to-end test

Status: ready-for-agent

Manually simulate a round, collect all the data it produces, render a video,
check the result. Wanted specifically **in the window between the structure
refactor and the next round** — the refactor moves nearly every file, and the
suite does not cover how ffmpeg filter branches combine or how a drawn frame
actually looks.

This is the highest-value item on this list, and everything else that touches
the render is safer once it exists.

Open questions to settle first: what "end to end" means when the inputs are a
radio, a webcam and an SD card; what the oracle is (golden frames? decoded
timestamps? the `.srt`/`chapters.txt`, which are ready before the costly
render?); and whether it runs in CI at all given its runtime.

## Notes from the structure refactor

Two throwaway harnesses were written while splitting the two big files, and
both earned their keep. They are the concrete starting point this ticket's
open questions were asking for.

**The render side has a stronger oracle than golden frames: the old build.**
Rendering the same 3-minute cut of the August round from a pre-refactor
worktree and from the working tree produced a byte-identical MP4. No frame
comparison, no tolerance, no fixtures to maintain — `git worktree add` plus
`md5sum`. Both `chapters.txt` and `.srt` for the full round matched too, and
they are ready before the render starts, so they answer in seconds.

This only works for changes that are *supposed* to change nothing, which is
exactly what a refactor is. It says nothing about whether the output is
*correct* — only that it did not move.

**The logger side needs a pty, and that is where the bugs were.** A ~40-line
script drives `puskas_logger.py` through a pty: answer the startup prompts,
answer the offline band/mode wizard, type one QSO, Ctrl-D, then assert on the
captured screen and the files left behind. It caught a real
`UnboundLocalError` in `main()` that 584 unit tests did not, because nothing
in the suite ever calls `main()`.

Cost: about 30 s per run, so it is not a unit test. That is the real design
question for this ticket — a separate marker, a separate CI job, or a
pre-round checklist item.
