# 37 — Face framing scans whole clips even for a short preview

Status: needs-triage

A `--duration 150` preview of the October round spent 3 min 31 s in face
framing — the longest stage of the whole run — scanning 892 samples across
both webcam clips, of which the preview shows the first ~100 s of one.

`contest_video.py` already drops clips that start after the cut, before the
scan, but `scan_faces(clip.path)` then samples every clip it keeps end to end
(one frame per `SAMPLE_S`). It is told nothing about the cut.

## The decision to make first

The crop is one per clip, from the median face position over the whole clip
(ARCHITECTURE.md). Scanning only what the preview shows makes the preview
fast, but then its framing is a different median from the full render's — and
a preview exists to show what the full render will look like.

Two ways out:

- Scan only `[clip.start, total)` of each clip and accept the preview may frame
  slightly differently; print that it did.
- Cache the scan per clip (`<clip>.face.json`, keyed on size and mtime), so the
  whole-clip scan is paid once per round, and every preview and the full
  render reuse it. The docstring's reason for not caching — 7 min against a
  3 h render — does not hold for a 5 min preview, which is what you run
  several times.

The cache keeps the preview honest; the first option is less code.

## Verification

A `--duration 150` render of `2026-oct/` spends seconds, not minutes, before
"face framing centred on", and frames the PiP where the full render does.

## Comments
