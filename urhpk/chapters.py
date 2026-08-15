"""YouTube chapters and SRT captions -- seeking without scrubbing.

Both are built from the same QSO windows, and both are cheap to produce, which
makes them the fast way to check a round's timing: they are written before the
render starts, so a wrong offset shows up in seconds rather than after an hour
of ffmpeg.
"""

from __future__ import annotations

from urhpk.timeline import Qso

MIN_CHAPTER_GAP_S = 10  # YouTube ignores chapters closer together than this
CAPTION_DUR_S = 8.0  # how long each SRT cue is shown


def _yt_time(t: float) -> str:
    """Format seconds as a YouTube description timestamp (M:SS or H:MM:SS)."""
    t = int(round(max(0.0, t)))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _qso_label(i: int, q: Qso) -> str:
    """The one-line label shared by chapter markers and SRT cues, so the two
    never drift: 'QSO 001 HA5MIG  2M SSB', band/mode omitted when unknown,
    with a ' (dup)' suffix for duplicates. Deliberately just call + band/mode
    -- locator, distance, serials and reports were dropped from the caption as
    redundant noise (they're already on the logger's own on-screen PiP)."""
    bm = " ".join(x for x in (q.band, q.mode) if x)
    bm = f"  {bm}" if bm else ""
    tag = " (dup)" if q.dup else ""
    return f"QSO {i + 1:03d} {q.callsign}{bm}{tag}"


def build_chapters(qsos: list[Qso], windows: list[tuple[float, float]]) -> str:
    """YouTube description chapter markers, one per QSO (plus the mandatory 0:00).

    YouTube requires the first chapter at 0:00, at least 3 chapters, and each
    at least MIN_CHAPTER_GAP_S apart -- closer QSOs are dropped from the list
    (they still get an SRT cue, just no separate chapter marker).
    """
    lines = ["0:00 Start"]
    last_t = 0
    for i, (q, (start, _end)) in enumerate(zip(qsos, windows)):
        t = int(round(start))
        if t - last_t < MIN_CHAPTER_GAP_S:
            continue
        lines.append(f"{_yt_time(t)} {_qso_label(i, q)}")
        last_t = t
    return "\n".join(lines) + "\n"


def _srt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(qsos: list[Qso], windows: list[tuple[float, float]]) -> str:
    """One caption cue per QSO -- gives a clickable transcript in the YouTube
    sidebar, independent of the chapter markers (and of whether CC is on)."""
    blocks = []
    for i, (q, (start, end)) in enumerate(zip(qsos, windows)):
        end = min(end, start + CAPTION_DUR_S)
        text = _qso_label(i, q)
        blocks.append(f"{i + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    return "\n".join(blocks)
