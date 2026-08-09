"""What the rig and rotator were doing, moment by moment.

Two sources, in descending order of trust. A segment's ptt/freq_hz/mode at its
own start come from the WAV file's embedded IC-9700 metadata (see
timeline.read_wav_metadata) -- ground truth straight from the rig, with none of
a 1 Hz poll's lag. The logger's `*-telemetry.jsonl` fills in drift *within* a
long segment, and supplies az, which the WAV metadata does not carry at all.

The logger's `*-input.jsonl` is the third source: keystrokes and logged QSOs,
used to place a QSO on the timeline more precisely than the EDI log's
minute-resolution timestamp can.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from timeline import Qso, Segment, _eff, audio_time_for


@dataclass
class TelemetrySample:
    t: datetime
    freq_hz: int | None
    mode: str | None
    az: float | None
    # Raw 0-255 meter readings, converted only here at render time -- see the
    # meter curves below for why the logger records them uncalibrated.
    vd: int | None = None
    id_raw: int | None = None
    swr: int | None = None
    po: int | None = None
    # As with az_offline: an absent "vd" key and an explicit `"vd": null` both
    # land as None but mean opposite things -- a line that says nothing about
    # the meters, versus one reporting that the radio went away.
    meters_offline: bool = False
    # An absent "az" key and an explicit `"az": null` both land as az=None but
    # mean opposite things -- silence about the rotator (a rig event) versus a
    # report that it went offline. Only the latter ends az's carry-forward.
    az_offline: bool = False


@dataclass
class SegState:
    ptt: bool | None = None
    freq_hz: int | None = None
    mode: str | None = None


def _parse_telemetry_time(s: str) -> datetime:
    """Both stamp precisions the format has carried: whole seconds (the
    original 1 Hz sampler) and microseconds (the current change-driven
    writer, matching the input log). Raises ValueError on anything else,
    which load_telemetry treats as a bad line."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized telemetry timestamp: {s!r}")


def load_telemetry(path: str) -> list[TelemetrySample]:
    """Parse a puskas_logger `*-telemetry.jsonl` file.

    Records are partial: the rig's own push events carry freq_hz/mode with
    no az, the rotator poller's carry az alone. A missing key is simply
    "this event says nothing about that field" -- build_state_events carries
    each field forward across the events that don't mention it."""
    samples: list[TelemetrySample] = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = _parse_telemetry_time(rec["t"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        samples.append(
            TelemetrySample(
                ts,
                rec.get("freq_hz"),
                rec.get("mode"),
                rec.get("az"),
                az_offline="az" in rec and rec["az"] is None,
                meters_offline="vd" in rec and rec["vd"] is None,
                vd=rec.get("vd"),
                id_raw=rec.get("id"),
                swr=rec.get("swr"),
                po=rec.get("po"),
            )
        )
    return samples


@dataclass
class InputLogEvent:
    t: datetime
    kind: str  # 'text' (keystroke) or 'qso' (an actual submit)
    text: str = ""  # kind == 'text': the full input-box contents
    callsign: str = ""  # kind == 'qso'
    dup: bool = False  # kind == 'qso'


def load_input_log(path: str) -> list[InputLogEvent]:
    """Parse a puskas_logger `*-input.jsonl` log. Two event kinds share the
    file (see recorders.py's own comment on why): 'text' is one line per
    keystroke feeding the typewriter overlay, microsecond-precise but with
    no reliable way to tell a submit from an abort. 'qso' is one line per
    QSO actually appended to the log, written from the one place that
    unambiguously knows -- see match_qso_times, which uses it to give QSO
    panels an exact submit time instead of the EDI's minute-precision guess."""
    out: list[InputLogEvent] = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = datetime.strptime(rec["t"], "%Y-%m-%dT%H:%M:%S.%fZ")
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        kind = rec.get("event", "text")
        out.append(
            InputLogEvent(
                ts,
                kind,
                rec.get("text", ""),
                rec.get("call", ""),
                rec.get("dup", False),
            )
        )
    return out


FREQ_MATCH_TOLERANCE_HZ = 500  # see build_state_events' docstring


def build_state_events(
    segs: list[Segment], telemetry: list[TelemetrySample], offset_h: int
) -> list[tuple[float, float, SegState]]:
    """RX/TX + QRG/mode events, one per stretch those stay constant.

    ptt/freq_hz/mode at a segment's own start come straight from
    `Segment.ptt`/`.freq_hz`/`.mode` (read_wav_metadata) -- the WAV file's
    own embedded IC-9700 recorder metadata, ground truth from the rig at
    the exact instant it started recording, with none of a 1 Hz telemetry
    poll's lag. A segment with no such metadata (rare -- e.g. a non-IC-9700
    recording) is skipped entirely rather than guessed at.

    ptt never needs telemetry at all: unlike freq/mode it cannot
    legitimately change mid-segment -- a real transition is exactly what
    causes the recorder to cut a new WAV file -- so it's one value,
    `s.ptt`, for the whole segment. (An earlier version tried to derive
    ptt from telemetry, including a "last sample wins" fix for telemetry's
    own polling lag -- all now unnecessary and removed, since the WAV
    metadata has no lag to correct for in the first place.)

    freq_hz/mode still benefit from telemetry, though: a long segment with
    no PTT activity at all (minutes of listening/tuning between overs) can
    still see the operator QSY with nothing to split the WAV on, so the
    WAV's own metadata (fixed at file-creation time) only captures the
    *starting* frequency/mode. Telemetry sub-divides the segment wherever a
    later sample shows them actually changing -- seeded from the WAV's
    starting value, not from telemetry, so a segment with no telemetry
    change at all just keeps the WAV-sourced value for its whole span.

    Azimuth is deliberately *not* here, though it used to be: a run is
    whatever stretch freq/mode hold for, which can be minutes, and one
    number for all of it (the median of its samples) left the compass
    needle standing still through a real slew and then jumping at the run
    boundary. It is its own time series now -- see hud_az_marks.

    Comparing the two frequency sources exactly (Hz for Hz) is unsound:
    the WAV metadata and rigctld-via-telemetry don't agree to the exact
    Hz even when nothing changed. Checked against this real round's own
    data: a systematic disagreement of 160/250/300/310 Hz (depending on
    band) shows up on *every* segment's very first telemetry sample, which
    would otherwise look like a spurious retune right at the start of
    almost every segment. Genuine retunes in the same data are >=1000 Hz
    (mostly round kHz steps, as a human tuning by hand would produce) --
    a clean gap, zero occurrences between 310 Hz and 1000 Hz -- so
    FREQ_MATCH_TOLERANCE_HZ=500 safely separates "same frequency, two
    slightly disagreeing sources" from "the operator actually retuned"."""
    events: list[tuple[float, float, SegState]] = []
    for s in segs:
        if s.ptt is None and s.freq_hz is None and s.mode is None:
            continue

        utc_start = s.wall - timedelta(hours=offset_h)
        utc_end = utc_start + timedelta(seconds=s.dur)
        inside = sorted(
            (t for t in telemetry if utc_start <= t.t < utc_end), key=lambda t: t.t
        )

        # Runs of consecutive (freq_hz, mode), seeded from the WAV's own
        # metadata, not from telemetry -- only sub-divided when a later
        # telemetry sample shows a genuine change within the segment
        # (frequency beyond FREQ_MATCH_TOLERANCE_HZ, mode by exact string
        # match -- mode has no equivalent rounding-disagreement problem).
        runs: list[tuple[tuple, list[TelemetrySample]]] = [((s.freq_hz, s.mode), [])]
        cur_freq, cur_mode = s.freq_hz, s.mode
        for t in inside:
            new_freq = t.freq_hz if t.freq_hz is not None else cur_freq
            new_mode = t.mode if t.mode is not None else cur_mode
            freq_changed = (
                new_freq is not None
                and cur_freq is not None
                and abs(new_freq - cur_freq) > FREQ_MATCH_TOLERANCE_HZ
            )
            mode_changed = new_mode is not None and new_mode != cur_mode
            if freq_changed or mode_changed:
                cur_freq, cur_mode = new_freq, new_mode
                runs.append(((cur_freq, cur_mode), [t]))
            else:
                runs[-1][1].append(t)

        seg_end = s.audio_t + _eff(s)
        for i, (key, samples) in enumerate(runs):
            start = (
                s.audio_t
                if i == 0
                else audio_time_for(samples[0].t + timedelta(hours=offset_h), segs)
            )
            end = (
                audio_time_for(runs[i + 1][1][0].t + timedelta(hours=offset_h), segs)
                if i + 1 < len(runs)
                else seg_end
            )
            if end <= start:
                continue
            freq_hz, mode = key
            events.append((start, end, SegState(ptt=s.ptt, freq_hz=freq_hz, mode=mode)))
    return events


def match_qso_times(
    qsos: list[Qso], input_log: list[InputLogEvent]
) -> list[datetime | None]:
    """Precise submit timestamp for each qsos[i], from the input log's 'qso'
    events -- an exact replacement for the EDI's minute-precision q.dt when
    available, None otherwise (older recordings, or a --duration cut that
    excludes the matching event).

    Matched by call, in chronological order *within that call* -- deliberately
    not by time, even though the two normally agree exactly (puskas_logger
    derives `q.dt` and the event's own microsecond stamp from one captured
    `now`, so the former is the latter's minute-truncation). Time matching
    breaks the moment they don't: a hand-written or edited log whose timestamp
    crosses a minute boundary silently matches nothing. Call+order has no such
    trap -- a --duration cut only ever removes a *suffix* in time, so the
    surviving occurrences of any call are still a prefix of the full sequence,
    and "next unused" stays correct."""
    by_callsign: dict[str, list[datetime]] = {}
    for e in input_log:
        if e.kind == "qso":
            by_callsign.setdefault(e.callsign, []).append(e.t)
    used: dict[str, int] = {}
    out: list[datetime | None] = []
    for q in qsos:
        i = used.get(q.callsign, 0)
        cands = by_callsign.get(q.callsign, [])
        if i < len(cands):
            out.append(cands[i])
            used[q.callsign] = i + 1
        else:
            out.append(None)
    return out
