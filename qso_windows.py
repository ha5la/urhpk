"""Where each QSO sits in the finished video.

The EDI log times a QSO to the minute, which is far too coarse to seek to.
A real exchange is a burst of short overs separated by listening gaps, so the
burst that contains the logged minute is found, its first transmission is
taken as the start, and nearby QSOs are snapped to a shared cluster start so
two QSOs worked in one burst do not get two different beginnings.

The windows produced here are what the chapters, the captions and the HUD's
QSO marks are all placed against.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from cw_decode import MAX_OVER_S
from timeline import Qso, Segment, audio_time_for


def _bursts(segs: list[Segment]) -> list[list[Segment]]:
    """Group into maximal runs of consecutive real-over segments (dur <=
    MAX_OVER_S), separated by genuine listening gaps."""
    groups: list[list[Segment]] = []
    cur: list[Segment] = []
    for s in segs:
        if s.dur <= MAX_OVER_S:
            cur.append(s)
        else:
            if cur:
                groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def _tx_start(burst: list[Segment]) -> float:
    """Where a QSO actually starts within a burst: the operator's own first
    TX, not necessarily the burst's first segment.

    Without PTT telemetry there's no ground truth for which segments are
    RX vs TX, but two things reliably hold: RX and TX strictly alternate
    (the recorder splits on every switch), and a TX segment -- a brief call
    or report -- is consistently shorter than the RX segment either side of
    it (listening for a reply). So whichever alternating phase (even or odd
    position in the burst) has the shorter median duration is TX, and its
    first occurrence is where this exchange really begins.

    This breaks down while calling CQ: a long stretch of repeated brief TX
    calls with only short listening gaps between them has no single
    "real" start to find this way, and an earlier fruitless call can look
    identical to the one that finally got answered. There's no fix for that
    here -- falls back to the burst's own first segment when the two phases
    aren't distinguishable (fewer than one of each, or equal medians)."""
    if len(burst) < 2:
        return burst[0].audio_t
    even = [s.dur for s in burst[0::2]]
    odd = [s.dur for s in burst[1::2]]
    if not even or not odd:
        return burst[0].audio_t
    even_med, odd_med = statistics.median(even), statistics.median(odd)
    if even_med == odd_med:
        return burst[0].audio_t
    tx_is_even = even_med < odd_med
    for i, s in enumerate(burst):
        if (i % 2 == 0) == tx_is_even:
            return s.audio_t
    return burst[0].audio_t  # unreachable: one phase is always non-empty


def cluster_starts(segs: list[Segment]) -> list[float]:
    """audio_t of the real start of every fresh burst of on-air activity --
    see `_bursts` for how a burst is delimited and `_tx_start` for how its
    real (TX-initiated) start is found within it.

    Deliberately keyed on duration alone, not on whether CW was actually
    decoded (`s.events`): a WAV segment boundary is a precise real-world
    RX/TX transition regardless of what's being transmitted. A voice-mode
    QSO never carries decodable CW, so requiring events made this blind to
    every voice over -- on a mostly-voice recording almost no QSO got the
    audio-precise snap at all. This is pure audio structure, independent of
    both CW content and the EDI log's minute-only timestamp precision."""
    return [_tx_start(b) for b in _bursts(segs)]


def _snap_to_cluster(t: float, clusters: list[float]) -> float:
    """The real activity-burst that produced the EDI-derived approximate
    time `t`. A QSO's own over necessarily starts at or before its (possibly
    minute-truncated) logged completion time, so this is the *latest*
    cluster start <= t -- not simply the nearest one, which can jump ahead
    to the *next* contact's burst if the current QSO took a while (calling,
    retries) to complete before being logged.

    If no cluster is <= t -- e.g. a QSO logged before any CW was ever
    decoded, common on a mostly-voice recording, or simply the first QSO --
    there is nothing to snap to, so `t` itself is used as-is. Falling back to
    the *first* cluster in the whole recording here was a real bug: it could
    pull an early QSO's panel minutes into the future."""
    candidates = [c for c in clusters if c <= t]
    return max(candidates) if candidates else t


def qso_windows(
    qsos: list[Qso],
    segs: list[Segment],
    offset_h: int,
    total: float,
    qso_times: list[datetime | None] | None = None,
) -> list[tuple[float, float]]:
    """Return the (start, end) video-time window shown for each QSO's panel.

    Only the *start* needs a heuristic at all: there's no way to know from
    the EDI or the input log exactly when a real over began, so it's
    snapped onto the actual WAV segment/burst boundary (see cluster_starts)
    nearest the QSO's own approximate time. The *end* doesn't need
    guessing wherever qso_times (from match_qso_times) has an exact
    submit time for that QSO -- that moment (the operator hitting Enter)
    is exact ground truth for when the QSO was done, so the panel simply
    clears there instead of lingering until the next QSO's own panel
    starts (the old behaviour, still used as a fallback when qso_times
    isn't available for a given QSO -- no better information exists then).

    qso_times also still feeds the *start* side, same as before: as the
    anchor into _snap_to_cluster in place of the EDI's minute-precision
    q.dt, which removes the minute-level slop that could otherwise point
    the snap at the wrong neighbouring burst.

    Two (or more) QSOs worked with no real listening gap between them --
    e.g. the same station on SSB then CW then FM in one continuous
    exchange -- are one burst as far as cluster_starts is concerned, since
    there's no audio structure to tell their overs apart at all. A QSO
    that snaps to the *same* cluster as the previous QSO instead starts
    exactly where the previous QSO's own window ended (its real, known
    finish) -- not audio-structure-precise either, but real, and
    critically leaves no overlap and no gap between the two."""
    clusters = cluster_starts(segs)
    starts: list[float] = []
    finishes: list[float | None] = []
    prev_cluster: float | None = None
    for i, q in enumerate(qsos):
        precise = qso_times[i] if qso_times else None
        anchor = precise if precise is not None else q.dt
        anchor_t = audio_time_for(anchor + timedelta(hours=offset_h), segs)
        snapped = _snap_to_cluster(anchor_t, clusters)
        if (
            precise is not None
            and snapped == prev_cluster
            and finishes[i - 1] is not None
        ):
            starts.append(finishes[i - 1])
        else:
            starts.append(snapped)
        finishes.append(anchor_t if precise is not None else None)
        prev_cluster = snapped
    for i in range(1, len(starts)):
        starts[i] = max(starts[i], starts[i - 1])  # keep panel order sane
    windows: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        fallback_end = starts[i + 1] if i + 1 < len(starts) else total
        end = finishes[i] if finishes[i] is not None else fallback_end
        windows.append((max(0.0, start), max(start + 1.0, end)))
    return windows
