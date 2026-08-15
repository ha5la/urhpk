"""What the rig and rotator were doing, moment by moment.

Two sources describe the same radio and neither is wholly better. `ptt` is the
WAV file's embedded IC-9700 metadata alone (see timeline.read_wav_metadata),
permanently, because a PTT transition is what cuts the file. freq_hz and mode
are both sources merged as timestamped observations, latest-wins -- see
rig_runs, and ARCHITECTURE.md's provenance table for the whole rule. The
logger's `*-telemetry.jsonl` also supplies az and the meters, which the WAV
metadata does not carry at all.

The logger's `*-input.jsonl` is the third source: keystrokes and logged QSOs,
used to place a QSO on the timeline more precisely than the EDI log's
minute-resolution timestamp can.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from urhpk.timeline import Qso, Segment, _eff, audio_time_for


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
    # And again for the rig: an explicit `"freq_hz": null` is the logger saying
    # the radio went away, which a rotator line silent about the rig is not.
    rig_offline: bool = False


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
                rig_offline="freq_hz" in rec and rec["freq_hz"] is None,
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


def _khz(freq_hz: int | None) -> int | None:
    """The resolution "did it change?" is asked at -- see rig_runs."""
    return None if freq_hz is None else round(freq_hz / 1000)


def rig_runs(
    segs: list[Segment], telemetry: list[TelemetrySample], offset_h: int
) -> list[tuple[datetime, int | None, str | None]]:
    """(utc, freq_hz, mode) at every instant either of them changes.

    The one answer to "what was the radio tuned to, and in what mode, at time
    t". Both sources are read as plain timestamped observations of the same
    radio and merged latest-wins, neither seeded from nor corrected by the
    other, with no branch anywhere on which generation of round is being read
    -- ARCHITECTURE.md's provenance table has why that is the whole rule.

    A telemetry line silent about the rig carries the current pair forward; an
    explicit `rig_offline` one ends the carry-forward with a (None, None) run,
    which is the honest reading when the radio has gone away rather than
    merely stayed put.

    "Did it change?" is asked at kHz, the resolution the QRG readout displays
    and the band lookup needs. The two sources do not agree below that -- the
    WAV metadata has 10 Hz resolution and the old 1 Hz sampler re-parsed a
    kHz-rounded string -- and comparing them exactly fragments the runs at
    almost every segment boundary. A run keeps the value it began with, since
    anything within it displays identically anyway."""
    obs = [(t.t, t.freq_hz, t.mode, t.rig_offline) for t in telemetry]
    # After telemetry, so that a WAV and a telemetry observation stamped the
    # same second resolve to the WAV's: it is timed by the transition that cut
    # the file, where a 1 Hz sample is timed by when the sampler got round to it.
    obs += [
        (s.wall - timedelta(hours=offset_h), s.freq_hz, s.mode, False)
        for s in segs
        if s.freq_hz is not None or s.mode is not None
    ]

    runs: list[tuple[datetime, int | None, str | None]] = []
    freq_hz: int | None = None
    mode: str | None = None
    for t, obs_freq, obs_mode, offline in sorted(obs, key=lambda o: o[0]):
        if offline:
            freq_hz, mode = None, None
        elif obs_freq is not None or obs_mode is not None:
            freq_hz = obs_freq if obs_freq is not None else freq_hz
            mode = obs_mode if obs_mode is not None else mode
        else:
            continue
        if not runs or (_khz(freq_hz), mode) != (_khz(runs[-1][1]), runs[-1][2]):
            runs.append((t, freq_hz, mode))
    return runs


def build_state_events(
    segs: list[Segment], telemetry: list[TelemetrySample], offset_h: int
) -> list[tuple[float, float, SegState]]:
    """RX/TX + QRG/mode events, one per stretch those stay constant.

    freq_hz/mode are whatever rig_runs says the radio was doing over each
    stretch of the segment, which is why a long segment with no PTT activity
    at all (minutes of listening between overs) still splits where the
    operator QSY'd, with nothing in the audio to cut the WAV on.

    ptt is `s.ptt` alone, one value for the whole segment: unlike freq/mode it
    cannot legitimately change mid-segment, because a real transition is
    exactly what causes the recorder to cut a new file. A segment with no WAV
    metadata at all (rare -- e.g. a non-IC-9700 recording) is skipped rather
    than guessed at, since nothing then knows its ptt.

    Azimuth is deliberately *not* here, though it used to be: a run is
    whatever stretch freq/mode hold for, which can be minutes, and one
    number for all of it (the median of its samples) left the compass
    needle standing still through a real slew and then jumping at the run
    boundary. It is its own time series now -- see hud_az_marks."""
    runs = rig_runs(segs, telemetry, offset_h)
    run_times = [r[0] for r in runs]

    events: list[tuple[float, float, SegState]] = []
    for i_seg, s in enumerate(segs):
        if s.ptt is None and s.freq_hz is None and s.mode is None:
            continue

        utc_start = s.wall - timedelta(hours=offset_h)
        utc_end = utc_start + timedelta(seconds=s.dur)
        # Filenames are stamped to the whole second, so a segment's nominal
        # span routinely runs past the next one's start -- half of every round
        # on disk, by up to 1.6 s. The video timeline butts them together and
        # they never overlap on it, so the overrun is an artefact and must not
        # collect observations that belong to the segment after this one.
        if i_seg + 1 < len(segs):
            utc_end = min(utc_end, segs[i_seg + 1].wall - timedelta(hours=offset_h))
        # The run in force at the segment's start, then every one beginning
        # inside it. A segment carrying only ptt has contributed no
        # observation of its own, so it can precede every run there is.
        i = bisect.bisect_right(run_times, utc_start) - 1
        held = (runs[i][1], runs[i][2]) if i >= 0 else (None, None)
        spans: list[tuple[datetime, int | None, str | None]] = [(utc_start, *held)]
        spans += runs[i + 1 : bisect.bisect_left(run_times, utc_end)]

        seg_end = s.audio_t + _eff(s)
        for k, (utc, freq_hz, mode) in enumerate(spans):
            start = (
                s.audio_t
                if k == 0
                else audio_time_for(utc + timedelta(hours=offset_h), segs)
            )
            end = (
                audio_time_for(spans[k + 1][0] + timedelta(hours=offset_h), segs)
                if k + 1 < len(spans)
                else seg_end
            )
            if end <= start:
                continue
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
