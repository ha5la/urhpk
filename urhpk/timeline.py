"""The round's timeline: segments, QSOs, and the mapping between them.

Two clocks meet here. The recorder's WAV files carry wall-clock time in their
filenames; the finished video has an audio timeline that is just the sum of
segment durations, which diverges from wall clock whenever a gap is cut. Every
other component asks this module to convert between the two.

The EDI log is read through edi.py and arrives here as Qso records, so nothing
downstream of this module has to know the format.
"""

from __future__ import annotations

import os
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from urhpk import edi
from urhpk.cw_decode import MAX_OVER_S, CharEvent
from urhpk.wav import parse_wav_title, read_wav_title


@dataclass
class Segment:
    path: str
    wall: datetime  # local wall-clock start (from filename)
    dur: float  # seconds (full recorded duration)
    audio_t: float  # start offset in the output video (seconds)
    events: list[CharEvent] = field(default_factory=list)
    eff_dur: float | None = None  # trimmed duration in output; None = use full dur
    freq_hz: int | None = (
        None  # from the WAV's own IC-9700 metadata (read_wav_metadata)
    )
    mode: str | None = None  # ditto
    ptt: bool | None = (
        None  # ditto -- ground truth at the segment's own start, no telemetry lag
    )


def _eff(s: Segment) -> float:
    return s.dur if s.eff_dur is None else s.eff_dur


@dataclass
class Qso:
    dt: datetime  # UTC (from EDI)
    callsign: str
    rst_s: str
    nr_s: str
    rst_r: str
    nr_r: str
    loc: str
    pts: int
    dup: bool
    band: str = ""  # from the EDI PBand header (2M/70CM/23CM); '' if unknown
    mode: str = ""  # SSB/CW/FM, from the EDI per-QSO mode code; '' if unknown


def scan_segments(recdir: str) -> list[Segment]:
    segs: list[Segment] = []
    audio_t = 0.0
    files = sorted(f for f in os.listdir(recdir) if f.lower().endswith(".wav"))
    for f in files:
        try:
            wall = datetime.strptime(f[:15], "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        p = os.path.join(recdir, f)
        w = wave.open(p)
        dur = w.getnframes() / w.getframerate()
        w.close()
        segs.append(Segment(p, wall, dur, audio_t))
        audio_t += dur
    return segs


def read_wav_metadata(segs: list[Segment]) -> None:
    """Populate freq_hz/mode/ptt on each segment straight from its own WAV
    file's embedded IC-9700 metadata. Leaves them None for a file with no
    recognized tag -- no fallback heuristic, since there's nothing to
    fall back to that's as trustworthy (see build_state_events)."""
    for s in segs:
        title = read_wav_title(s.path)
        parsed = parse_wav_title(title) if title else None
        if parsed:
            s.freq_hz, s.mode, s.ptt = parsed


GAP_KEEP_S = 3.0  # seconds kept from each silent gap when --skip-gaps is used


def remap_audio_t(segs: list[Segment], cw_span_segs: set[int] | None = None) -> None:
    """Shorten gap segments to GAP_KEEP_S and recompute audio_t for all segments.

    A gap segment is one with no trusted decoded events and a duration longer
    than MAX_OVER_S — i.e. a listening / calling-CQ stretch between QSOs.
    Call this *after* gate_events has been applied to s.events.

    `cw_span_segs` (a set of `id(seg)`, from the segments decode_cw_subranges
    recovered content from) marks segments that are long for this reason but
    still carry real recovered CW content -- these must not be trimmed to
    GAP_KEEP_S, or concat_audio's outpoint would cut the very audio just
    decoded out of the rendered output entirely, even though the ticker
    still expects to show its text.
    """
    cw_span_segs = cw_span_segs or set()
    t = 0.0
    for s in segs:
        s.audio_t = t
        if not s.events and s.dur > MAX_OVER_S and id(s) not in cw_span_segs:
            s.eff_dur = GAP_KEEP_S
        t += _eff(s)


def trim_to_duration(segs: list[Segment], max_dur: float) -> list[Segment]:
    """Keep only the segments needed to cover the first max_dur seconds of
    real round time (a --duration preview), shortening the last one to
    land exactly on the cutoff.

    Called *before* CW decoding, not after: decode_segment/gate_events are
    the expensive part of the pipeline, and a short preview has no use for
    segments past the cutoff, so this skips decoding them at all rather than
    decoding the full round and discarding most of the result.
    """
    out = [s for s in segs if s.audio_t < max_dur]
    if out:
        last = out[-1]
        cut = max(0.0, min(_eff(last), max_dur - last.audio_t))
        if cut < _eff(last):
            last.eff_dur = cut
    return out


# Reverse of puskas_logger's own EDI encodings (_BAND_FREQ / _MODE_CODE), so
# a rendered chapter/caption can name the band+mode the logger recorded.


def parse_edi(path: str) -> tuple[str, str, list[Qso]]:
    log = edi.read(path)
    qsos = [
        Qso(
            r.dt,
            r.callsign,
            r.rst_s,
            r.nr_s,
            r.rst_r,
            r.nr_r,
            r.loc,
            r.points,
            r.duplicate,
            band=log.band,
            mode=r.mode,
        )
        for r in log.records
    ]
    return log.my_callsign, log.my_locator, qsos


def merge_edi(paths: list[str]) -> tuple[str, str, list[Qso]]:
    """Merge one or more per-band EDI logs (e.g. 2M + 70CM from the same
    round) into a single chronological QSO list -- the recording is one
    continuous audio timeline regardless of how many bands were worked."""
    my_callsign, mywwl = "", ""
    qsos: list[Qso] = []
    for path in paths:
        mc, mw, qs = parse_edi(path)
        if not my_callsign:
            my_callsign, mywwl = mc, mw
        qsos.extend(qs)
    qsos.sort(key=lambda q: q.dt)
    return my_callsign, mywwl, qsos


def audio_time_for(wall: datetime, segs: list[Segment]) -> float:
    """Map a local wall-clock time to a position in the output video."""
    for s in segs:
        if wall < s.wall:
            return s.audio_t
        if wall < s.wall + timedelta(seconds=s.dur):
            offset = min((wall - s.wall).total_seconds(), _eff(s))
            return s.audio_t + offset
    return segs[-1].audio_t + _eff(segs[-1])


def stream_start(wall: datetime, segs: list[Segment]) -> float:
    """Where a side stream's own frame 0 belongs in the output timeline.

    Same as audio_time_for once the recording has started, but *negative*
    when the stream began before the first WAV segment -- audio_time_for
    clamps that case to segs[0].audio_t, which is exactly wrong here. A cast
    or scope recording clamped to 0 gets its frame 0 pinned to video t=0, so
    everything in it reads late by the whole cast-to-WAV gap (25 s in the
    dry-run that caught it, as the cast PiP's clock lagging the round).

    This is the normal case, not an edge case: run-recorded-round.sh
    starts asciinema before the radio recorder is switched on, so every cast
    made through the documented entrypoint begins ahead of the audio.
    render() turns the negative value into an -ss seek *into* the stream."""
    if wall < segs[0].wall:
        return segs[0].audio_t - (segs[0].wall - wall).total_seconds()
    return audio_time_for(wall, segs)


def derive_utc_offset(segs: list[Segment], qsos: list[Qso]) -> int:
    """Integer-hour offset such that qso_utc + offset ~= wav local time."""
    if not qsos:
        return 0
    wav_mid = segs[0].wall + timedelta(seconds=(segs[-1].audio_t + segs[-1].dur) / 2)
    qso_mid = qsos[0].dt + (qsos[-1].dt - qsos[0].dt) / 2
    return round((wav_mid - qso_mid).total_seconds() / 3600)
