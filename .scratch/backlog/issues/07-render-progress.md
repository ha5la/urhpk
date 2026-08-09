# 07 — Progress reporting for the render

Status: ready-for-agent

`contest_video.py` runs for hours unattended with little indication of where
it is. Add progress reporting (tqdm was the suggestion).

Worth knowing before implementing: the render is several sequential stages
(cast replay, HUD frames, scope waterfall, the ffmpeg pass) with very
different rates — the cast replay is the slowest, and a 20-minute cut of a
121-minute round spent ~40 minutes in it. A single undifferentiated bar would
mislead; per-stage progress is the useful thing.
