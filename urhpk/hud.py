"""The HUD's data layer: what the bar shows at any moment of the video.

Pure functions turning the round's own sources -- segments, the EDI log,
telemetry, the scope sweeps, the CW decode -- into a HudState for a given
video time. No art, no fonts and no ffmpeg, which is what makes it fully
unit-testable; hud_draw.py is the other half, and knows nothing about where
the numbers came from.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from urhpk.cw_decode import CharEvent
from urhpk.geo import distance_between, initial_bearing, maidenhead_to_latlon
from urhpk.icom_net import band_from_hz
from urhpk.rig_state import SegState, TelemetrySample, rig_runs
from urhpk.scope_render import SCOPE_AMP_MAX
from urhpk.timeline import Qso, Segment, _eff, audio_time_for


def ticker_chunks(
    segs: list[Segment],
    cw_spans: list[tuple[float, float, list[CharEvent]]] | None,
) -> list[tuple[float, float, list[CharEvent]]]:
    """Trusted CW content as one chronological list of (start, end, events).

    Two sources merge here: the CW-mode spans decode_round confirmed against
    telemetry -- possibly several per segment, since we may have followed
    more than one on-air exchange without ever transmitting ourselves -- and
    the whole-segment decode of any segment whose mode was never known at
    all. Both have already been judged; nothing is filtered here."""
    chunks: list[tuple[float, float, list[CharEvent]]] = []
    for s in segs:
        if s.events:
            chunks.append((s.audio_t, s.audio_t + _eff(s), s.events))
    chunks.extend(cw_spans or [])
    chunks.sort(key=lambda c: c[0])
    return chunks


def ticker_stream(
    chunks: list[tuple[float, float, list[CharEvent]]],
) -> list[tuple[float, str]]:
    """(absolute video time, character) for every decoded character.

    There is no flush marker and no separator inserted between overs. Both
    used to exist because the ticker held a static transcript that had to be
    cleared before it went stale; the display now scrolls on a clock (see
    HudTimeline.at), so a gap between overs *is* a gap on screen and text from
    an earlier burst has physically left the display long before a later one
    arrives. Time does both jobs."""
    stream: list[tuple[float, str]] = []
    for start, _, events in chunks:
        for e in events:
            stream.append((start + e.t, e.ch))
    return stream


HUD_TICKER_CHARS = 15  # cells in the ticker display, set by the artwork's own
# CW slot: 15 cells at a 5px dot pitch fill 445 of its 446 pixels at 1080p, and
# 16 would drop the pitch to 4. The display scrolls, so a short window loses
# nothing -- its value is "something is arriving right now", not a backlog.
HUD_TICKER_SPAN_S = 8.0  # seconds for a character to cross it
HUD_TICKER_BURST_S = 3.0  # gap beyond which the operator has stopped sending,
# not merely paused between characters: the longest single character is ~2 s at
# the slowest speed worked here, and word gaps arrive as their own ' '.
HUD_RATE_WINDOW_S = 600.0  # trailing window behind the QSOs/hour readout
HUD_SCORE_ANIM_S = 0.6  # score count-up + panel flash after each QSO
# The band/mode chips light and fade like filaments rather than switching: a
# lamp reaches full brightness far quicker than it stops glowing. Only the
# transitions animate. A steady glow instead would change every frame and cost
# the whole of hud_frame_key's reuse -- 8x on the August round, 26,557 frames
# drawn for 218,995 -- where that round's 118 transitions add at most ~1,000.
HUD_CHIP_RISE_S = 0.08
HUD_CHIP_DECAY_S = 0.35
HUD_S_CENTRE_BINS = 3  # scope bins taken as "the tuned frequency"
HUD_S_HOLD_S = 1.0  # no sweep for this long = no signal reading at all

# The ticker is a dot-matrix display: 5x7 glyphs, scrolled a whole dot column
# at a time, which is what a real dot-matrix panel does -- there are no
# sub-dot positions on one. The glyph table itself is hud_draw's business; the
# geometry is here because the scroll clock is computed from it.
HUD_MATRIX_COLS, HUD_MATRIX_ROWS = 5, 7
HUD_TICKER_CELL_COLS = HUD_MATRIX_COLS + 1
HUD_TICKER_COLS_PER_S = HUD_TICKER_CHARS * HUD_TICKER_CELL_COLS / HUD_TICKER_SPAN_S


@dataclass
class HudState:
    """Everything the HUD draws, at one instant of video time."""

    t: float = 0.0
    utc: datetime | None = None
    score: int = 0  # animated: counts up over HUD_SCORE_ANIM_S after a QSO
    score_flash: float = 0.0  # 1.0 right after a QSO, decaying to 0.0
    qsos: int = 0
    rate_per_h: float = 0.0
    best_km: int = 0
    freq_hz: int | None = None
    mode: str | None = None
    band: str | None = None
    ptt: bool | None = None
    rot_az: float | None = None  # where the rotator actually points
    target_az: float | None = None  # bearing to the station being worked
    s_level: float | None = None  # 0..1, from the scope's own centre bins
    # (column offset from the display's left edge, character) for whatever is
    # currently on the scrolling matrix -- not a string, because a character
    # sits at a dot-column position rather than in a slot.
    ticker: list[tuple[int, str]] = field(default_factory=list)
    # Per-chip lamp brightness, 0..1. A chip absent from it has never been lit.
    chip_glow: dict[str, float] = field(default_factory=dict)
    vd: float | None = None  # volts -- no recording carries these yet; the
    id_a: float | None = None  # panel renders "---" until the logger records them


def hud_qso_marks(
    qsos: list[Qso], windows: list[tuple[float, float]], my_loc: str
) -> list[tuple[float, int, int, int]]:
    """(video_t, cumulative score, cumulative QSO count, best DX km) at the
    moment each QSO completes.

    The mark lands on `windows[i][1]` -- the QSO's *end*, which wherever the
    input log gave an exact submit time is the real instant the operator hit
    Enter (see qso_windows). That is when a score genuinely changes, so it's
    also when the HUD's counter should tick over.

    Best DX is the distance the panel's ODX KM caption promises, measured from
    the locators, and not q.pts -- which is that distance rounded up. A dup is
    left out of it: it scores nothing, so it cannot be the round's best."""
    order = sorted(range(len(qsos)), key=lambda i: windows[i][1])
    marks: list[tuple[float, int, int, int]] = []
    score = best = 0
    for n, i in enumerate(order, start=1):
        q = qsos[i]
        score += q.pts
        km = None if q.dup else distance_between(my_loc, q.loc)
        if km is not None:
            best = max(best, int(km))
        marks.append((windows[i][1], score, n, best))
    return marks


def hud_target_spans(
    qsos: list[Qso], windows: list[tuple[float, float]], my_loc: str
) -> list[tuple[float, float, float]]:
    """(start, end, bearing) of the station being worked, for the HUD
    compass's second (ghost) needle -- so the rotator needle can be seen
    swinging onto the target. Silently skips a QSO whose locator or our own
    won't parse; there is simply no bearing to show then."""
    me = maidenhead_to_latlon(my_loc)
    if me is None:
        return []
    spans: list[tuple[float, float, float]] = []
    for q, (start, end) in zip(qsos, windows):
        them = maidenhead_to_latlon(q.loc)
        if them is not None:
            spans.append((start, end, initial_bearing(*me, *them)))
    spans.sort(key=lambda s: s[0])
    return spans


def hud_s_marks(
    records: list[tuple[float, int, int, bytes]],
    segs: list[Segment],
    offset_h: int,
    bins: int = HUD_S_CENTRE_BINS,
) -> list[tuple[float, float]]:
    """(video_t, 0..1 signal level) from the scope recording's own centre
    bins -- a genuine S-meter for the HUD that costs no new recording and
    works retroactively on every round captured since the logger's scope
    recorder went in.

    The centre bin really is the tuned frequency: the IC-9700's scope runs in
    Centre mode (see icom_net.parse_scope_frame), and at 475 bins across a
    1 MHz span one bin is ~2.1 kHz -- close enough to an SSB passband that
    this is a real reading rather than a rough proxy. `bins` are taken as a
    max, not a mean, so a signal sitting in one bin isn't diluted by the
    quiet ones either side of it.

    Not the same quantity as CI-V's own S-meter (`15 02`), which is
    post-filter and post-AGC; a live capture confirmed the radio only ever
    reports that when polled, so no existing recording has it."""
    marks: list[tuple[float, float]] = []
    half = max(0, bins // 2)
    for ts, _, _, pixels in records:
        if not pixels:
            continue
        centre = len(pixels) // 2
        window = pixels[max(0, centre - half) : centre + half + 1]
        wall = datetime.fromtimestamp(ts, tz=timezone.utc).replace(
            tzinfo=None
        ) + timedelta(hours=offset_h)
        marks.append(
            (audio_time_for(wall, segs), min(1.0, max(window) / SCOPE_AMP_MAX))
        )
    marks.sort(key=lambda m: m[0])
    return marks


# --- meter calibration ------------------------------------------------------
#
# The logger records raw 0-255 meter readings and conversion happens here, at
# render time, deliberately: this is the least trustworthy data in the whole
# pipeline, and keeping it raw on disk makes a corrected curve a one-line
# change rather than a recording that has to be thrown away.
#
# Vd, SWR and Po use Icom's own published calibration points, and Vd was
# checked against a multimeter on this radio -- raw 152 converts to 13.66 V
# against a measured 13.78 V, 0.9% out. Po's 100% point was confirmed too
# (raw 213 during a full-power transmission).
#
# Id is the exception and is NOT Icom's curve: theirs (0/97/146/241 ->
# 0/10/15/25 A) reads 17.6 A for a raw 171 that measures ~12.8 A of real PA
# drain. Measured directly against a multimeter in series with the supply, at
# raw 55/60/61/62/64 plus a 100%-power anchor at raw 171, PA drain (total
# current less the 1.18 A measured receive baseline) fits a straight line
# *through the origin* at 0.0741 A per raw unit, i.e. ~17.9 A full scale
# rather than Icom's 25 A. The low cluster alone gives 0.0726 and adding the
# 100% anchor gives 0.0741 -- two nearly independent estimates a factor of
# three apart in current, agreeing to 2%, which is what makes the line
# through zero believable rather than merely fitted; the low cluster spans
# only raw 55-64, far too short a lever arm to determine a slope by itself.
# Residuals within +-5.3%, worst at the lowest point, where a cheap meter on
# a 20 A range has its poorest resolution and where the assumption of a
# constant receive baseline is least safe (the meter's own burden had the
# radio down at ~10.2-10.7 V during these readings).
_VD_CURVE = [(0, 0.0), (13, 10.0), (241, 16.0)]
_ID_CURVE = [(0, 0.0), (241, 17.85)]
_SWR_CURVE = [(0, 1.0), (48, 1.5), (80, 2.0), (120, 3.0)]
_PO_CURVE = [(0, 0.0), (143, 50.0), (213, 100.0)]


def _meter_value(curve: list[tuple[int, float]], raw: int | None) -> float | None:
    """Piecewise-linear lookup, extrapolating from the last segment above the
    curve's top point (Icom's own points stop short of full scale)."""
    if raw is None:
        return None
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if raw <= x1 or (x1, y1) == curve[-1]:
            return y0 + (raw - x0) * (y1 - y0) / (x1 - x0)
    return curve[-1][1]


def vd_volts(raw: int | None) -> float | None:
    return _meter_value(_VD_CURVE, raw)


def id_amps(raw: int | None) -> float | None:
    return _meter_value(_ID_CURVE, raw)


def swr_ratio(raw: int | None) -> float | None:
    return _meter_value(_SWR_CURVE, raw)


def po_percent(raw: int | None) -> float | None:
    return _meter_value(_PO_CURVE, raw)


def hud_meter_marks(
    telemetry: list[TelemetrySample], segs: list[Segment], offset_h: int
) -> list[tuple[float, TelemetrySample]]:
    """(video_t, sample) for every telemetry line that carries meter readings.

    Meters are change-only in the recording, like everything else in that
    file, so a mark holds until the next one -- there is no staleness horizon
    the way the scope-derived S-meter has, because an unchanging supply
    voltage is a real reading rather than a gap in the data.

    That is exactly why a radio disconnect has to be marked explicitly rather
    than left as silence: "no reason for the voltage to have changed" and "the
    radio is gone" are indistinguishable from absence alone, and a real
    session dropped three times in nine minutes would otherwise show the
    pre-outage voltage throughout each outage."""
    marks = [
        (audio_time_for(t.t + timedelta(hours=offset_h), segs), t)
        for t in telemetry
        if t.vd is not None or t.id_raw is not None or t.meters_offline
    ]
    marks.sort(key=lambda m: m[0])
    return marks


HUD_AZ_INTERP_S = 2.0  # rotator samples closer than this are one movement


def hud_az_marks(
    telemetry: list[TelemetrySample], segs: list[Segment], offset_h: int
) -> list[tuple[float, float | None]]:
    """(video_t, azimuth) for every telemetry line that reports on the rotator,
    offline ones included -- an explicit `"az": null` is a real mark carrying
    None, so the needle stops there instead of pointing at the last known
    azimuth for the rest of the video. A line that only reports the rig says
    nothing about the rotator and is not a mark at all, even though both load
    as `az=None`.

    The compass reads this directly rather than taking `SegState.az` (a median
    over a freq/mode run) the way the old text badge did: a run can be minutes
    long, so a rotator swung from 250 to 31 degrees over half a minute inside
    one of them collapsed to a single median and the needle stood still, then
    jumped at the run boundary -- seen in the real August round."""
    marks = [
        (audio_time_for(t.t + timedelta(hours=offset_h), segs), t.az)
        for t in telemetry
        if t.az is not None or t.az_offline
    ]
    marks.sort(key=lambda m: m[0])
    return marks


def hud_chip_marks(
    telemetry: list[TelemetrySample], segs: list[Segment], offset_h: int
) -> list[tuple[float, str | None, str | None]]:
    """(video_t, band, mode) wherever the lit pair of chips changes.

    Read from rig_runs rather than from SegState for the same reason the
    compass reads hud_az_marks: a SegState run is whatever stretch freq/mode
    hold for *within one segment*, so it says nothing across the stretches
    where nothing was being recorded, which is exactly where the operator
    tunes."""
    marks: list[tuple[float, str | None, str | None]] = []
    last: tuple[str | None, str | None] | None = None
    for utc, freq_hz, mode in rig_runs(segs, telemetry, offset_h):
        pair = (band_from_hz(freq_hz) if freq_hz else None, mode)
        if pair != last:
            marks.append((audio_time_for(utc + timedelta(hours=offset_h), segs), *pair))
            last = pair
    return marks


def _chip_ramp(level: float, lit: bool, dt: float) -> float:
    """One lamp's brightness dt seconds after it was last at `level`."""
    if lit:
        return min(1.0, level + dt / HUD_CHIP_RISE_S)
    return max(0.0, level - dt / HUD_CHIP_DECAY_S)


def _chip_levels(
    marks: list[tuple[float, str | None, str | None]],
) -> list[dict[str, float]]:
    """Every lamp's brightness at the instant of each mark.

    Folded forward once here rather than ramped from zero at query time, so a
    chip switched off and straight back on again resumes from however far it
    had actually faded instead of jumping to dark."""
    levels: list[dict[str, float]] = []
    cur: dict[str, float] = {}
    for i, (t, band, mode) in enumerate(marks):
        if i:
            dt = t - marks[i - 1][0]
            prev = {marks[i - 1][1], marks[i - 1][2]}
            cur = {n: _chip_ramp(v, n in prev, dt) for n, v in cur.items()}
        cur = cur | {n: cur.get(n, 0.0) for n in (band, mode) if n}
        levels.append(cur)
    return levels


def _az_between(a: float, b: float, frac: float) -> float:
    """Bearing `frac` of the way from a to b, the short way round -- 250 to 31
    degrees is a 141 degree swing clockwise through north, not 219 the other
    way."""
    return (a + ((b - a + 180) % 360 - 180) * frac) % 360


def wall_time_at(
    t: float, segs: list[Segment], starts: list[float] | None = None
) -> datetime | None:
    """Local wall-clock time at video position t -- the inverse of
    audio_time_for. `starts` is an optional precomputed [s.audio_t ...] so a
    caller reading this once per rendered frame doesn't rebuild it every
    time."""
    if not segs:
        return None
    starts = starts if starts is not None else [s.audio_t for s in segs]
    s = segs[max(0, bisect.bisect_right(starts, t) - 1)]
    return s.wall + timedelta(seconds=max(0.0, min(t - s.audio_t, _eff(s))))


@dataclass
class HudTimeline:
    """Precomputed HUD sources, queried per rendered frame by `at()`.

    Every source is stored as a time-sorted list and looked up by bisect
    rather than scanned: a two-hour render asks this ~216,000 times, so a
    linear scan per frame over hundreds of segments or thousands of decoded
    characters would dominate the whole pass."""

    segs: list[Segment]
    offset_h: int = 0
    qso_marks: list[tuple[float, int, int, int]] = field(default_factory=list)
    target_spans: list[tuple[float, float, float]] = field(default_factory=list)
    state_events: list[tuple[float, float, SegState]] = field(default_factory=list)
    az_marks: list[tuple[float, float | None]] = field(default_factory=list)
    chip_marks: list[tuple[float, str | None, str | None]] = field(default_factory=list)
    s_marks: list[tuple[float, float]] = field(default_factory=list)
    meter_marks: list[tuple[float, TelemetrySample]] = field(default_factory=list)
    stream: list[tuple[float, str, bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._seg_starts = [s.audio_t for s in self.segs]
        self._qso_t = [m[0] for m in self.qso_marks]
        self._target_t = [s[0] for s in self.target_spans]
        self._state_t = [e[0] for e in self.state_events]
        self._az_t = [m[0] for m in self.az_marks]
        self._chip_t = [m[0] for m in self.chip_marks]
        self._chip_level = _chip_levels(self.chip_marks)
        self._s_t = [m[0] for m in self.s_marks]
        self._meter_t = [m[0] for m in self.meter_marks]
        self._ticker_t = [e[0] for e in self.stream]
        # Where each character sits on the strip, in dot columns. Within an
        # over that is exactly one cell after the one before it, whatever the
        # gap in real time: a T is one dit of air time and a 0 nineteen, so
        # placing characters by keying time (which is what this used to do)
        # spaced them raggedly, by fractions of a cell, for no reason a viewer
        # could see. The keying time drives the *scroll* instead -- see
        # _ticker_scroll -- which is where that timing genuinely belongs.
        #
        # Real elapsed time takes over once the gap exceeds HUD_TICKER_BURST_S,
        # by which point the operator has stopped sending rather than paused
        # between characters (word gaps are their own decoded ' ' characters,
        # so they need no room of their own here). That is what still drains
        # the display between overs, and it is why staleness stays
        # structurally impossible.
        self._ticker_cols: list[float] = []
        prev_col, prev_t = 0.0, None
        for t, _ in self.stream:
            if prev_t is None:
                prev_col = t * HUD_TICKER_COLS_PER_S
            elif t - prev_t <= HUD_TICKER_BURST_S:
                prev_col += HUD_TICKER_CELL_COLS
            else:
                prev_col += (t - prev_t) * HUD_TICKER_COLS_PER_S
            prev_t = t
            self._ticker_cols.append(prev_col)

    def _az_at(self, t: float) -> float | None:
        """The rotator's azimuth at t, swept between samples rather than
        stepped to them.

        The poller reports whole degrees about once a second, so a real slew
        arrives as a run of closely-spaced samples: interpolating across gaps
        no longer than HUD_AZ_INTERP_S turns those steps into one continuous
        turn, while a longer gap is not a slow movement at all -- it is the
        rotator sitting still (change-only telemetry writes nothing then), so
        the bearing holds and the next sample is where it moved to."""
        i = bisect.bisect_right(self._az_t, t)
        if not i:
            return None
        az = self.az_marks[i - 1][1]
        if az is None or i >= len(self.az_marks):
            return az
        nxt_t, nxt_az = self.az_marks[i]
        span = nxt_t - self._az_t[i - 1]
        if nxt_az is None or span > HUD_AZ_INTERP_S or span <= 0:
            return az
        return _az_between(az, nxt_az, (t - self._az_t[i - 1]) / span)

    def _chip_glow_at(self, t: float) -> dict[str, float]:
        i = bisect.bisect_right(self._chip_t, t) - 1
        if i < 0:
            return {}
        _, band, mode = self.chip_marks[i]
        lit = {band, mode}
        dt = t - self._chip_t[i]
        return {n: _chip_ramp(v, n in lit, dt) for n, v in self._chip_level[i].items()}

    def _ticker_scroll(self, t: float) -> float:
        """How far the ticker's strip has scrolled, in dot columns.

        Each character is pinned: when it was keyed, it had just arrived at the
        right-hand edge. Between two pins the strip moves at whatever rate
        carries it exactly one cell in the real time between them, so the
        display hurries along under fast keying and idles under slow -- which
        is how a fixed inter-character spacing can still show real timing.
        Outside the pins (before the first character, after the last, and
        across the real-time gaps between overs) it runs at the base rate, so
        the display always drains within HUD_TICKER_SPAN_S of the last
        character."""
        i = bisect.bisect_right(self._ticker_t, t)
        if not self._ticker_t:
            return t * HUD_TICKER_COLS_PER_S
        if i == 0 or i == len(self._ticker_t):
            j = max(0, i - 1)
            edge = self._ticker_cols[j] + HUD_TICKER_CELL_COLS
            return edge + (t - self._ticker_t[j]) * HUD_TICKER_COLS_PER_S
        span = self._ticker_t[i] - self._ticker_t[i - 1]
        if span <= 0:
            return self._ticker_cols[i] + HUD_TICKER_CELL_COLS
        frac = (t - self._ticker_t[i - 1]) / span
        moved = self._ticker_cols[i] - self._ticker_cols[i - 1]
        return self._ticker_cols[i - 1] + HUD_TICKER_CELL_COLS + frac * moved

    def at(self, t: float) -> HudState:
        st = HudState(t=t, utc=None)
        wall = wall_time_at(t, self.segs, self._seg_starts)
        if wall is not None:
            st.utc = wall - timedelta(hours=self.offset_h)

        i = bisect.bisect_right(self._qso_t, t)
        if i:
            mark_t, score, n, best = self.qso_marks[i - 1]
            prev = self.qso_marks[i - 2][1] if i >= 2 else 0
            # Count up to the new total rather than snapping to it, and flash
            # the panel over the same window -- DOOM's health readout is the
            # thing the eye goes to, so a QSO landing should be visible.
            phase = min(1.0, (t - mark_t) / HUD_SCORE_ANIM_S) if HUD_SCORE_ANIM_S else 1
            st.score = round(prev + (score - prev) * phase)
            st.score_flash = max(0.0, 1.0 - phase)
            st.qsos, st.best_km = n, best
        lo = bisect.bisect_right(self._qso_t, t - HUD_RATE_WINDOW_S)
        st.rate_per_h = (i - lo) * 3600.0 / HUD_RATE_WINDOW_S

        j = bisect.bisect_right(self._target_t, t)
        if j and t < self.target_spans[j - 1][1]:
            st.target_az = self.target_spans[j - 1][2]

        k = bisect.bisect_right(self._state_t, t)
        if k and t < self.state_events[k - 1][1]:
            seg_state = self.state_events[k - 1][2]
            st.ptt, st.mode = seg_state.ptt, seg_state.mode
            st.freq_hz = seg_state.freq_hz
            if st.freq_hz:
                st.band = band_from_hz(st.freq_hz)

        st.rot_az = self._az_at(t)
        st.chip_glow = self._chip_glow_at(t)

        m = bisect.bisect_right(self._s_t, t)
        if m and t - self.s_marks[m - 1][0] <= HUD_S_HOLD_S:
            st.s_level = self.s_marks[m - 1][1]

        q = bisect.bisect_right(self._meter_t, t)
        if q:
            sample = self.meter_marks[q - 1][1]
            st.vd = vd_volts(sample.vd)
            st.id_a = id_amps(sample.id_raw)

        # Everything still on the display: a character enters at the right
        # edge when the scroll reaches its own column and leaves on the left
        # HUD_TICKER_SPAN_S later, with no clearing rule needed -- staleness
        # is structurally impossible rather than guarded against.
        width = HUD_TICKER_CHARS * HUD_TICKER_CELL_COLS
        scroll = self._ticker_scroll(t)
        p = bisect.bisect_right(self._ticker_t, t)
        for i in range(p - 1, -1, -1):
            offset = round(self._ticker_cols[i] - scroll) + width
            if offset <= -HUD_MATRIX_COLS:
                break
            if offset < width:
                st.ticker.append((offset, self.stream[i][1]))
        st.ticker.reverse()
        return st


def build_hud_timeline(
    segs: list[Segment],
    qsos: list[Qso],
    windows: list[tuple[float, float]],
    my_loc: str,
    offset_h: int,
    state_events: list[tuple[float, float, SegState]] | None = None,
    scope_records: list[tuple[float, int, int, bytes]] | None = None,
    cw_spans: list[tuple[float, float, list[CharEvent]]] | None = None,
    telemetry: list[TelemetrySample] | None = None,
) -> HudTimeline:
    return HudTimeline(
        segs=segs,
        offset_h=offset_h,
        qso_marks=hud_qso_marks(qsos, windows, my_loc),
        target_spans=hud_target_spans(qsos, windows, my_loc),
        state_events=state_events or [],
        az_marks=hud_az_marks(telemetry or [], segs, offset_h),
        chip_marks=hud_chip_marks(telemetry or [], segs, offset_h),
        s_marks=hud_s_marks(scope_records or [], segs, offset_h),
        meter_marks=hud_meter_marks(telemetry or [], segs, offset_h),
        stream=ticker_stream(ticker_chunks(segs, cw_spans)),
    )
