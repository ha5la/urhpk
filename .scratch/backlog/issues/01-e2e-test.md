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
