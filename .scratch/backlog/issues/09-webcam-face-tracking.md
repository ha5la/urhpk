# 09 — Frame the webcam PiP on the face

Status: resolved

The webcam PiP is centred on a fixed crop, which is wrong for a capture the
operator cannot see while it happens. Derive the crop from where the face
actually is.

The ticket originally asked for *tracking*. Measurement says the problem is a
constant horizontal offset, not motion, and not zoom — see below. The filename
keeps the old slug so the number stays stable.

## What the measurement says

YuNet run over the whole August round (2 h, laptop Alt+V capture, 1280×720,
sampled every 5 s, 1440 samples, **98.8 % hit rate**, longest miss run 10 s):

| | source px (1280 wide) |
|---|---|
| face centre x, median | **782** (0.61 of width) — the crop is centred on 640 |
| p5 → p95 | 689 → 938 (**±124 px**) |
| centre y, p5 → p95 | 68 px of 720 — vertically nailed down |
| box height, median | 46 % of frame height; whole head with headset ≈ 64 % |
| movement per 5 s | median 28 px, p90 149 px, max 427 px |

How far the face centre strays from the crop centre (half-width 353 px):

| | > ½ half-width | > 0.8 | worst |
|---|---|---|---|
| today, centred on 640 | **36.8 %** of the round | 7.8 % | **491 px — outside the crop; the operator was partly out of the PiP** |
| static crop on median (782) | 4.2 % | **0.14 %** | 350 px |

That 0.14 % is a single 10 s excursion in two hours, which is what rules
tracking out: it would buy those 10 s at the cost of smoothing constants, a
lost-face policy, `sendcmd` keyframing and a jitter failure mode that the
project treats as a bug.

Zoom is ruled out separately: the head already fills ~64 % of the frame height,
so the existing full-height crop already puts it at ~60 % of the recess.

**Why the problem exists**: the July round was shot on a phone front camera, so
the operator could see the framing while recording. The Alt+V laptop capture
that replaced it gives no such feedback. Every future round is the second kind.

## Decisions

| | |
|---|---|
| Geometry | **Pan only.** The crop's width and height formula is unchanged; only x is derived. Clamp to source bounds |
| Dynamics | **One static crop per clip**, from the median of the face centres |
| Detector | YuNet via `opencv-python-headless`; vendor `face_detection_yunet_2023mar.onnx` (233 KB) beside the module. Score ≥ 0.6, largest face per frame |
| Dependency | An optional extra selected by `contest_video.py`'s shebang: `#!/usr/bin/env -S uv run --extra render`, so the logger's env stays free of a 50 MB render-only wheel |
| Module | New `urhpk/webcam_face.py`, plus its row in ARCHITECTURE.md's module table |
| CLI | Always on, no flag. Falls back to today's centre crop |
| Cost | ~7.4 min per 2 h clip (6.2 decode + 1.2 detect) against a ~3 h render — 4 %. No cache, no sidecar, no keyframe-only shortcut |
| Reporting | One line per clip, in the early sync phase before any frame is drawn; `urhpk/progress.py` `stage_bar` for the scan itself |

Verified about the extra: `uv run --extra render` installs opencv on first
render, and a later plain `uv run puskas_logger.py` does **not** prune it — no
churn on alternating runs. A plain `uv sync` is exact and does drop it, so a
fresh clone's first render needs network.

## Implementation

The crop today is
`crop=min(iw\,ih*{fw}/{fh}):min(ih\,iw*{fh}/{fw})` in `contest_video.py`, which
centres both axes. For a 245×250 recess and a 1280×720 source that is
706×720 at x=287; the height already equals the source height, so y stays 0 and
only x moves:

    x = clamp(median_cx - crop_w / 2, 0, iw - crop_w)

For the August clip that is x = 429, no clamping needed. Clamping rather than
shrinking the zoom is deliberate: a crop that changes size because the operator
leaned is the jitter this ticket avoids.

The seam is a pure function — `face_crop(detections, src_size, recess_size)
-> (x, y, w, h)` — with cv2 behind a thin adapter, so the detector is never
imported from a test.

Regenerate the scan (needs `/tmp/yunet.onnx` from opencv_zoo):

    ffmpeg -v error -i CLIP.mp4 -vf "fps=1/5,scale=640:360" -f rawvideo -pix_fmt bgr24 -
    # piped into cv2.FaceDetectorYN.create(model, "", (640, 360), 0.6, 0.3, 5000)

## Acceptance

Unit tests on `face_crop` with fixtures cut from the August scan, in
`tests/fixtures/` alongside the two `mrasz-*.json`.

Then a reality check, run manually and not in the 12 s suite — re-detect on a
rendered PiP and measure the face centre against the recess centre, in recess
pixels (245 wide):

- **median ≤ 25 px and p95 ≤ 65 px.** Measured envelope for a static crop is
  median 18.7, p95 55.5; today's centre crop is 49.1 and 103.5
- the face centre never leaves the crop, which today's does
- a synthesised well-framed clip (a centred window cut out of the August
  footage) must come back a near no-op — the algorithm must not damage footage
  that was already right

## Docs

FINDINGS.md gets a subsection with the two tables above and the
self-monitored-phone vs unmonitored-Alt+V explanation, so the static-vs-tracking
decision is not re-argued from scratch. RECORDING.md gets one sentence in the
`--webcam` section. ARCHITECTURE.md gets the module row.

## Comments

Noticed while checking whether `webcam_sync.py`'s charter still holds — *"the
one stream with no trustworthy clock: its start, and its drift"*. For an Alt+V
capture the **start** is now exact (`exact timestamp in filename, same-machine
clock, no cross-correlation needed`), so that half is stale. The August render
did fit a +2.487 s/hour drift over 16 anchors, but that round ran with
`systemd-timesyncd` restarted by hand shortly before the start and no chrony, so
the laptop clock was probably still slewing — the rate is not evidence of an
intrinsic two-clock mismatch. Worth re-measuring on the next round, now that
chrony disciplines the laptop and the radio runs NTP. Not this ticket's work.

Implemented in 0a52037 (the module and its geometry) and 5e8d922 (the wiring
and the docs). The acceptance measurement, on the rendered PiP rather than on
the scan: median 18.5 px, p95 57.1 px against the criterion's 25 and 65; the
centred crop measures 48.9 and 93.0 on the same footage.

One thing deliberately left alone: a `--cut` preview scans whatever clip
survives the cut in full, so a 20-minute preview still pays the whole clip's
~7 minutes. Worth a `-t` on the scan's ffmpeg if previews start to feel slow.
