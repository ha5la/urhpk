#!/usr/bin/env -S uv run
"""
Puskás URH Kupa – Contest QSO Logger
=====================================
Usage:  uv run puskas_logger.py
Input:  CALL RST NR LOC
          HA7NS 59 015 JN97WM    → locator required
          HA7NS 599 014 JN97WM   → CW with locator
Commands: !undo  !help
Ctrl-D at empty prompt → save EDI files and exit
"""

import asyncio
import netrc
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_completions
from prompt_toolkit.formatted_text import HTML, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import DynamicStyle, Style

import edi
import icom_net
import loc_cache
import rig_server
import rotator
from geo import is_locator
from logbook import (
    BANDS,
    MODES,
    QSO,
    LogBook,
    _is_dup_in_log,
    band_summary,
    load_from_edi,
    save_all,
    tname_for,
)
from recorders import (
    forget_meters,
    input_log_open,
    log_input_event,
    on_buffer_changed,
    on_radio_meters,
    on_scope,
    scope_open,
    telemetry_clock_record,
    telemetry_meter_record,
    telemetry_open,
    telemetry_rig_record,
    telemetry_write,
    webcam_finalize_name,
    webcam_reap,
    webcam_status,
    webcam_stop_if_running,
    webcam_toggle,
)
from wiring import (
    RIG_SERVER_PORT,
    require_round_directory,
)

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────
# Credentials in ~/.netrc under this machine name. Overridable so a test run
# on this LAN cannot take a session slot from the radio a round is using.
RADIO_HOST = os.environ.get("PUSKAS_RADIO_HOST", "icom9700")
RADIO_CONNECT_TIMEOUT_S = 5.0
RADIO_STALE_S = 5.0  # no CI-V-socket traffic for this long = session dead
RADIO_RECONNECT_S = 15.0  # quiet time the radio needs before accepting a new session

# Every QSO's points and the EDI's PWWLo come from this, so the startup screen prints
# it: a round from another QTH has to be noticed before the first QSO.
MY_LOCATOR = "JN97TF"
START_BAND, START_MODE = "2M", "SSB"


_BEARING_ARROWS = "↑↗→↘↓↙←↖"


def _bearing_arrow(degrees: int) -> str:
    return _BEARING_ARROWS[int((degrees + 22.5) / 45) % 8]


def _format_combos(by_band: dict[str, list[str]]) -> str:
    """Compact 'BAND:MODE,MODE' listing for the rprompt, e.g.
    '2M:SSB,CW 70CM:CW' -- see LogBook.worked_combos."""
    return " ".join(f"{b}:{','.join(ms)}" for b, ms in by_band.items())


# Radio — direct Ethernet CI-V (icom_net), replaces rigctld entirely.
# freq/mode arrive as CI-V Transceive pushes the instant they change on
# the radio, not up to a poll interval late.
# ──────────────────────────────────────────────────────────────
_rig: dict = {"band": "", "mode": "", "qrg": "", "online": False}
# What the round logs on while the radio is not answering: the starting
# constants until it has been seen, then whatever it was last on.
_rig_manual: dict = {"band": START_BAND, "mode": START_MODE}
# "rig" is the live session, None while offline; "probed" is _await_radio's gate.
_radio: dict = {"rig": None, "task": None, "probed": asyncio.Event()}

# Fire-and-forget tasks, held so the loop cannot collect one mid-flight.
_pending: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


def _on_radio_update(freq_hz, mode, band) -> None:
    # connect() primes freq and mode with separate queries -- until both have
    # arrived, stay "offline" rather than show a half-known state.
    if freq_hz is None or mode is None:
        return
    # freq_hz/raw_mode: what the rigctld-dialect server replies with --
    # raw dial mode ("USB"), not the contest-normalized one ("SSB").
    _rig.update(
        band=band,
        mode=edi.mode_from_radio(mode),
        qrg=f"{freq_hz / 1e6:.3f}",
        online=True,
        freq_hz=freq_hz,
        raw_mode=mode,
    )
    _radio["probed"].set()
    # _apply_update has already filtered out no-op updates, so every call here
    # is a real change.
    telemetry_write(
        telemetry_rig_record(
            datetime.now(timezone.utc), freq_hz, edi.mode_from_radio(mode)
        )
    )


async def _radio_task() -> None:
    """Keep one live icom_net session up; reconnect after drops.

    RADIO_RECONNECT_S between attempts is not just politeness: the radio
    refuses new sessions for a while after an uncleanly-dropped one, so
    hammering it would only push recovery further away.

    Cancelling this task is how a round says goodbye to the radio: the `finally`
    below closes whatever session it had reached, including one still inside
    connect().
    """
    while True:
        rig = None
        try:
            user, password = icom_net._load_netrc_credentials(RADIO_HOST)
            rig = icom_net.IcomNetRig(RADIO_HOST, user, password)
            rig.on_update(_on_radio_update)
            await rig.connect(timeout=RADIO_CONNECT_TIMEOUT_S)
            # Both re-armed on every (re)connect: the meter poller is a task
            # belonging to the session it was started on, and re-enabling scope
            # costs one frame (the radio actually remembers the setting across
            # sessions -- see icom_net's notes -- but relying on that would make
            # recording depend on whatever the previous session left behind).
            rig.on_scope(on_scope)
            rig.enable_scope()
            rig.on_meters(on_radio_meters)
            rig.enable_meters()
            _radio["rig"] = rig
            _spawn(_recorder_check_run())
            _spawn(_ntp_check_run())
            while rig.last_rx_age() < RADIO_STALE_S:
                await asyncio.sleep(1.0)
        except Exception:
            pass
        finally:
            _radio["rig"] = None
            _radio["probed"].set()
            was_online = _rig["online"]
            # The band the operator is on outlives the session that reported
            # it: a drop goes on logging there, not back at the starting band.
            if was_online:
                _rig_manual.update(band=_rig["band"], mode=_rig["mode"])
            _rig.update(band="", mode="", qrg="", online=False, freq_hz=0, raw_mode="")
            # Only on a real online→offline transition: this loop also runs
            # while the radio has simply never been reachable, and a null line
            # every RADIO_RECONNECT_S would say nothing new.
            if was_online:
                now = datetime.now(timezone.utc)
                telemetry_write(telemetry_rig_record(now, None, None))
                # The meters go with it. Without an explicit null a consumer
                # keeps carrying the last reading forward -- and since it has no
                # reason to expect a supply voltage to change, it would show the
                # voltage from before the outage for the whole outage. Resetting
                # the change-detector too guarantees the first reading after a
                # reconnect is written even if it happens to match the last one
                # before the drop.
                forget_meters()
                telemetry_write(
                    telemetry_meter_record(
                        now, dict.fromkeys(icom_net.CIV_METERS.values())
                    )
                )
            if rig is not None:
                try:
                    await rig.close()
                except Exception:
                    pass
        await asyncio.sleep(RADIO_RECONNECT_S)


def _radio_start() -> None:
    # A fresh Event rather than clear(): it binds to the loop that first awaits it.
    _radio["probed"] = asyncio.Event()
    _radio["task"] = _spawn(_radio_task())
    # Outlives any one session, unlike the meter poller: it has its own idle
    # state for "radio offline" and picks the next session up by itself.
    _spawn(_clock_monitor_run())


def _radio_rig() -> icom_net.IcomNetRig | None:
    """The live session, or None — for commands (CW keying, clock set)."""
    return _radio["rig"]


async def _radio_stop() -> None:
    """Say goodbye to the radio on exit, so a restart never races the radio's
    abandoned-session cooldown (see icom_net.close). Called from every exit
    path, and idempotent."""
    task, _radio["task"] = _radio["task"], None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _install_signal_handlers() -> None:
    """A round ends by killing the tmux session that runs the logger
    (see run-recorded-round.sh), so SIGTERM/SIGHUP is an ordinary
    exit path here -- and the one that used to leave the radio streaming to a
    dead socket, refusing new sessions. EDI/telemetry/input logs are all
    flushed as they are written, so exiting straight from the handler loses
    nothing.

    asyncio's own signal handling rather than signal.signal(): the teardown
    runs as an ordinary loop callback, on a loop that is between callbacks,
    instead of between two bytecodes of whatever the main thread was in the
    middle of. That is what makes it safe to touch the same state the UI
    touches -- the shape that used to be a self-deadlock (FINDINGS.md)."""

    async def _terminate(signum: int) -> None:
        webcam_stop_if_running()
        await _radio_stop()
        os._exit(128 + signum)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, lambda s=sig: _spawn(_terminate(s)))


def rig_snapshot() -> tuple[bool, int, str]:
    """(online, freq_hz, raw_mode) — what rig_server answers a query with."""
    return _rig["online"], _rig.get("freq_hz", 0), _rig.get("raw_mode", "")


def current_rig() -> tuple[str, str, str, bool]:
    """(band, mode, qrg, online) — falls back to manual override if offline.

    No lock: `_rig` is written by the radio task and read by the UI and the rig
    server, all on the one event loop, so none can observe another half-done.
    """
    if _rig["online"]:
        return _rig["band"], _rig["mode"], _rig["qrg"], True
    return _rig_manual["band"], _rig_manual["mode"], "", False


# ──────────────────────────────────────────────────────────────
# Radio clock monitor
#
# The radio syncs itself over NTP from this laptop (RECORDING.md sets it up),
# which leaves one job here: notice if it ever stops. Every timestamp in the
# pipeline is joined on the two clocks agreeing, and the way that assumption
# used to break was silent -- so the offset is measured continuously, shown in
# the toolbar, and recorded to telemetry rather than checked once at startup.
#
# Measuring it needs the poll: the radio reports HH:MM and no seconds, so the
# only observable instant is the minute rollover, and the only way to catch it
# is to be asking when it happens.
#
# But only the *first* rollover has to be hunted for. Once the offset is known
# the next one is predictable, so the monitor sleeps until just before it and
# asks in a short burst instead of polling the whole time: ~40 queries every
# CLOCK_SAMPLE_S rather than one a second, and a 20 Hz burst pins the rollover
# to ±25 ms where a 1 Hz stream could only bracket it to ±0.5 s. Cheaper and
# sharper at once.
#
# A burst that sees no rollover means the prediction was wrong -- the radio's
# clock stepped, which is the interesting case -- so the offset goes back to
# unknown and the next pass re-acquires from scratch.
# ──────────────────────────────────────────────────────────────
CLOCK_ACQUIRE_POLL_S = 1.0  # hunting the first rollover, blind to its timing
CLOCK_ACQUIRE_S = 70.0  # one full minute plus slack to catch one
CLOCK_BURST_POLL_S = 0.05
CLOCK_WINDOW_S = 1.0  # half-width of the burst around the predicted rollover
CLOCK_SAMPLE_S = 300.0  # at least this between measurements, then the next rollover
CLOCK_WARN_S = 2.0  # beyond this the toolbar goes yellow

_clock_offset: dict = {"offset_s": None}


def _clock_chip() -> tuple[str, str]:
    """(style, text) for the toolbar's clock chip -- one function so the
    redraw signature cannot drift from what is actually drawn."""
    offset = _clock_offset["offset_s"]
    if offset is None:
        return "", "  CLK —  │  "
    style = "bg:ansiyellow fg:black" if abs(offset) >= CLOCK_WARN_S else ""
    return style, f"  CLK {offset:+.1f}s  │  "


def _clock_window_wait(offset_s: float, now: datetime) -> float:
    """Seconds to sleep so a burst starts CLOCK_WINDOW_S before the radio's
    next minute rollover.

    A radio running `offset_s` ahead turns its minute over that much *before*
    the laptop's own `:00`, so the rollover lands at second `-offset_s` of the
    laptop's minute. Always waits for a rollover still in the future, even when
    called from inside one's window."""
    target = (-offset_s) % 60
    second = now.second + now.microsecond / 1e6
    wait = (target - second) % 60 - CLOCK_WINDOW_S
    return wait if wait >= 0 else wait + 60


async def _measure_clock_offset(rig, now_fn, interval: float, window_s: float):
    """Poll the radio's clock until its minute increments, and return the
    offset that rollover implies together with the instant it was measured.
    None if it doesn't happen within `window_s` -- with a burst that means the
    rollover was not where it was predicted."""
    started = None
    last_hhmm = None
    last_reply_at = None
    while True:
        try:
            hhmm = await rig.read_clock()
        except Exception:
            hhmm = None
        now = now_fn()
        if started is None:
            started = now
        if hhmm is not None:
            if last_hhmm is not None and hhmm != last_hhmm:
                return icom_net.clock_offset_s(last_reply_at, now), now
            last_hhmm, last_reply_at = hhmm, now
        if (now - started).total_seconds() >= window_s:
            return None
        await asyncio.sleep(interval)


async def _clock_monitor_run(now_fn=None) -> None:
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    while True:
        rig = _radio_rig()
        if rig is None:
            # Nothing to report rather than a stale number: the next session
            # re-measures from its own first rollover.
            _clock_offset["offset_s"] = None
            await asyncio.sleep(CLOCK_ACQUIRE_POLL_S)
            continue

        known = _clock_offset["offset_s"]
        if known is None:
            measured = await _measure_clock_offset(
                rig, now_fn, CLOCK_ACQUIRE_POLL_S, CLOCK_ACQUIRE_S
            )
        else:
            await asyncio.sleep(_clock_window_wait(known, now_fn()))
            measured = await _measure_clock_offset(
                rig, now_fn, CLOCK_BURST_POLL_S, 2 * CLOCK_WINDOW_S
            )

        if measured is None:
            _clock_offset["offset_s"] = None
            continue
        offset, at = measured
        _clock_offset["offset_s"] = offset
        # Stamped at the rollover it measured, not at whenever this loop got
        # round to writing it.
        telemetry_write(telemetry_clock_record(at, offset))
        await asyncio.sleep(CLOCK_SAMPLE_S)


# ──────────────────────────────────────────────────────────────
# The radio's own Voice Recorder
#
# The audio is the one part of a round that cannot be reconstructed
# afterwards, and it is recorded by the radio to its own SD card, started by
# hand on the front panel. Nothing on the CI-V bus reports whether that is
# running (see icom_net's Voice Recorder section), so the toolbar carries an
# acknowledgement the operator clears with Alt+S and nothing else can.
#
# What *is* readable is how the recorder is configured, and two of those
# settings decide whether the segments are usable at all. Neither failure is
# visible during the round -- both surface days later, at render time, on the
# one recording that cannot be made again.
#
# Which of them is wrong goes in the notice slot rather than in a block of its
# own: the logger's pane is half of a 191-column terminal (even-horizontal,
# see run-recorded-round.sh), and both phrases spelled out
# permanently push the clock off the right-hand end. What stays is the short
# flag; Alt+S spells it out, which is the key pressed while the radio's menu
# is being visited anyway.
#
# The connect-time check therefore sets the flag but posts no notice: a notice
# is wide enough to crowd the clock, and every other one here is the answer to
# a keypress. Nothing in this toolbar appears unbidden.
# ──────────────────────────────────────────────────────────────
_recorder_check: dict = {"warnings": (), "notice": "", "until": 0.0}
RECORDER_NOTICE_S = 15.0


def recorder_warnings(
    file_split: int | None, rx_rec_condition: int | None
) -> list[str]:
    """What is wrong with the Voice Recorder's settings. An unread setting
    (None) is not a wrong one and says nothing."""
    warnings = []
    if file_split == 0:
        # qso_windows.py's whole model is that a segment boundary is an RX/TX
        # transition; File Split OFF gives it one 2 GB file with none in it.
        warnings.append("REC: File Split OFF")
    if rx_rec_condition == 1:
        # The radio's own default, and it drops every quiet stretch between
        # QSOs -- then splits a segment on each squelch transition too, so
        # RX and TX no longer strictly alternate.
        warnings.append("REC: RX = Squelch Auto")
    return warnings


async def _recorder_check_run() -> None:
    rig = _radio_rig()
    if rig is None:
        _recorder_check["warnings"] = ()
        return
    try:
        file_split = await rig.read_param(icom_net.CIV_PARAM_FILE_SPLIT)
        rx_rec_condition = await rig.read_param(icom_net.CIV_PARAM_RX_REC_CONDITION)
    except Exception:
        return  # a session that dropped mid-check says nothing new
    _recorder_check["warnings"] = tuple(recorder_warnings(file_split, rx_rec_condition))


# ──────────────────────────────────────────────────────────────
# The radio's NTP settings
#
# The clock monitor above measures whether the two clocks agree *now*. This
# checks whether they will go on agreeing, which is not the same question: a
# radio whose server address has been reset still reads right for hours
# afterwards, drifting too slowly to see, so the CLK chip stays quiet while
# the sync is already gone. The address is wrong the moment it is read.
#
# Same shape as the recorder check, and the same reason for it: a factory
# reset silently restores a setting whose failure only shows up later.
# ──────────────────────────────────────────────────────────────
_ntp_check: dict = {"warnings": ()}


def ntp_warnings(function: int | None, server: str | None, expected: str) -> list[str]:
    """What is wrong with the radio's NTP settings. An unread setting (None)
    is not a wrong one and says nothing."""
    warnings = []
    if function == 0:
        warnings.append("NTP: Function OFF")
    if server is not None and server != expected:
        # The factory value, time.nist.gov, is the one this reliably catches:
        # the radio has no route off its own link, so it never answers.
        warnings.append(f"NTP: server is {server or '(empty)'}, not {expected}")
    return warnings


async def _ntp_check_run() -> None:
    rig = _radio_rig()
    expected = None if rig is None else rig.local_addr
    if rig is None or expected is None:
        # Nothing to say rather than a stale complaint; _settings_check_run
        # is what reports an offline radio as such.
        _ntp_check["warnings"] = ()
        return
    try:
        function = await rig.read_param(icom_net.CIV_PARAM_NTP_FUNCTION)
        server = await rig.read_ntp_server()
    except Exception:
        return  # a session that dropped mid-check says nothing new
    _ntp_check["warnings"] = tuple(ntp_warnings(function, server, expected))


async def _settings_check_run() -> None:
    """Re-read everything about the radio a round depends on, and answer in one
    notice. One, because the notice slot is one: the toolbar has room for a
    short phrase and the round clock, not for two subsystems arguing."""
    if _radio_rig() is None:
        notice = "radio settings: radio offline"
    else:
        await _recorder_check_run()
        await _ntp_check_run()
        problems = list(_recorder_check["warnings"]) + list(_ntp_check["warnings"])
        notice = " · ".join(problems) or "radio settings OK"
    _recorder_check.update(notice=notice, until=time.monotonic() + RECORDER_NOTICE_S)


# ──────────────────────────────────────────────────────────────
# Input parser
# ──────────────────────────────────────────────────────────────
RE_CALLSIGN = re.compile(r"^(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,}(/[A-Z0-9P/]+)?$")


def parse_input(line: str) -> dict | str:
    """Parse 'CALL RST NR LOC'. Returns dict or error string."""
    tokens = line.upper().split()
    if not tokens:
        return ""
    if len(tokens) < 3:
        return "Usage: CALL RST NR LOC   e.g.  HA7NS 59 015 JN97WM"
    callsign = tokens[0]
    if not RE_CALLSIGN.match(callsign):
        return f"Invalid callsign: {callsign!r}"
    rst_r = tokens[1]
    try:
        nr_r = int(tokens[2])
        if not (0 < nr_r < 10000):
            raise ValueError
    except ValueError:
        return f"Expected serial number as third token, got {tokens[2]!r}"
    loc = ""
    for tok in tokens[3:]:
        if is_locator(tok):
            loc = tok[:6]
            break
    if not loc:
        return "Usage: CALL RST NR LOC   e.g.  HA7NS 59 015 JN97WM"
    return dict(callsign=callsign, rst_r=rst_r, nr_r=nr_r, loc=loc)


# ──────────────────────────────────────────────────────────────
# Received-NR prediction
# ──────────────────────────────────────────────────────────────
_NR_PREDICT_MAX_AGE = 5 * 60  # seconds


def _predict_nr(
    lb: LogBook, callsign: str, band: str, mode: str, now: datetime | None = None
) -> int | None:
    """Return last_nr_r + 1 if there is a recent cross-mode QSO for call on band.

    The other station's serial counter is per-band; a recent QSO on the same band
    in a different mode gives us a close estimate of their current serial.
    `now` is injectable for testing; defaults to the real wall clock.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    for q in reversed(lb.qsos):
        if q.callsign == callsign and q.band == band and q.mode != mode:
            if (now - q.dt).total_seconds() <= _NR_PREDICT_MAX_AGE:
                return q.nr_r + 1
            return None  # found but too old
    return None


# ──────────────────────────────────────────────────────────────
# Callsign autocomplete
# ──────────────────────────────────────────────────────────────
class CallsignCompleter(Completer):
    def __init__(self, loc_cache: dict[str, list[str]]):
        self._callsigns = sorted(loc_cache.keys())
        self._locs = loc_cache  # call → [most_recent, ...]

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        tokens = text.split()
        if not tokens:
            return
        trailing = text[-1] == " "

        # Callsign: first token being typed
        if len(tokens) == 1 and not trailing:
            prefix = tokens[0]
            for callsign in self._callsigns:
                if callsign.startswith(prefix.upper()):
                    yield Completion(callsign, start_position=-len(prefix))

        # Locator: after "CALL RST NR " (3 complete tokens + cursor past space)
        elif (len(tokens) == 3 and trailing) or len(tokens) == 4:
            callsign = tokens[0].upper()
            prefix = tokens[3] if len(tokens) == 4 else ""
            for loc in self._locs.get(callsign, []):
                if loc.startswith(prefix.upper()):
                    yield Completion(loc, start_position=-len(prefix))


# ──────────────────────────────────────────────────────────────
# Display helpers
# ──────────────────────────────────────────────────────────────
W = 80
_REDRAW = object()  # sentinel: exit prompt to force a full screen refresh

# CW macros bound to F1–F7.  Placeholders: <MYCALL> <HISCALL> <NUMBER> <LOCATOR>
CW_MACROS = [
    "CQ <MYCALL> <MYCALL> TEST",  # F1
    "<MYCALL>",  # F2
    "5NN <NUMBER> <LOCATOR>",  # F3
    "TU",  # F4
    "<HISCALL>",  # F5
    "DE <MYCALL>",  # F6
    "?",  # F7
    "282 282 SSB",  # F8
]


def _expand_cw(template: str, lb: LogBook, their_callsign: str, band: str) -> str:
    nr = lb.next_nr(band)
    nr_cw = f"{nr:03d}".replace("0", "T").replace("9", "N")
    return (
        template.replace("<MYCALL>", lb.my_callsign)
        .replace("<HISCALL>", their_callsign or "?")
        .replace("<NUMBER>", nr_cw)
        .replace("<LOCATOR>", lb.my_loc)
    )


def _cw_send(message: str) -> None:
    # A single UDP send -- no thread needed, unlike the old rigctld TCP
    # round-trip. Silently no-ops when the radio is offline.
    rig = _radio_rig()
    if rig is not None:
        try:
            rig.send_cw(message)
        except Exception:
            pass


def _cw_stop() -> None:
    rig = _radio_rig()
    if rig is not None:
        try:
            rig.stop_cw()
        except Exception:
            pass


_CW_LEGEND = "  F1:CQ  F2:MY  F3:EXCH  F4:TU  F5:HIS  F6:DE  F7:?  F8:QSY  ESC:STOP"


def _print_header(lb: LogBook):
    bar = "━" * W
    print(f"\n\033[1m{bar}\033[0m")
    print(f" PUSKÁS LOGGER  │  {band_summary(lb)}")
    print(f"\033[2m{_CW_LEGEND}\033[0m")
    print(f"\033[1m{bar}\033[0m")


def _print_recent(lb: LogBook, n: int = 8, focus: int | None = None):
    qsos = lb.qsos
    if focus is not None:
        before = n // 2
        start = max(0, min(focus - before, len(qsos) - n))
        window = qsos[start : start + n]
    else:
        start = max(0, len(qsos) - n)
        window = qsos[-n:]
    for abs_idx, q in enumerate(window, start=start):
        dup = _is_dup_in_log(qsos, q)
        bear = lb.bearing(q.loc)
        dist = f"  {lb.dist(q.loc):4d} km  {bear:3d}° {_bearing_arrow(bear)}"
        marker = "  \033[31mDUP\033[0m" if dup else ""
        row = (
            f"{q.dt.strftime('%H:%M')}  {q.callsign:<10}  {q.band:<5} {q.mode:<4}"
            f"  ↑{q.rst_s:<3} {q.nr_s:03d} ↓{q.rst_r:<3} {q.nr_r:03d}  {q.loc:<6}{dist}{marker}"
        )
        if abs_idx == focus:
            print(f"\033[1m> {row}\033[0m")
        else:
            print(f"  {row}")
    print("─" * W)


# ──────────────────────────────────────────────────────────────
# Command handler
# ──────────────────────────────────────────────────────────────
def _handle_command(line: str, lb: LogBook, tname: str):
    parts = line.split()
    cmd = parts[0].lower()

    if cmd == "!undo":
        q = lb.undo()
        if q:
            print(f"  Undone: {q.dt.strftime('%H:%M')} {q.callsign} {q.band} {q.mode}")
            save_all(lb, tname)
        else:
            print("  Nothing to undo.")

    elif cmd == "!help":
        print("  CALL RST NR LOC          — log a QSO (locator required)")
        print("  !undo                    — remove last QSO")
        print("  Alt+B                    — cycle band (rig offline)")
        print("  Alt+M                    — cycle mode (rig offline)")
        print("  Alt+R                    — point rotator at selected bearing")
        print(
            "  Alt+S                    — confirm SD recording; re-check radio settings"
        )
        print("  Alt+V                    — start/stop webcam recording")
        print("  !help                    — this help")
        print("  Ctrl-D                   — save and exit")

    else:
        print(f"  Unknown command: {cmd}  (try !help)")

    input("  [Enter to continue]")


async def _await_radio() -> None:
    """Hold the round back until the radio's first connect attempt resolves.

    The login handshake takes several round trips, so the band is not knowable
    at the instant the round starts -- drawing then would show the starting
    band for a radio that turns out to be on another one. Bounded by the
    connect timeout, since a failed attempt resolves this too.
    """
    if _radio["probed"].is_set():
        return
    print(f"\n  Connecting to the radio ({RADIO_HOST})…")
    await _radio["probed"].wait()


_CET = ZoneInfo("Europe/Budapest")


def _is_round_time(now: datetime | None = None) -> bool:
    """True during Puskás URH Kupa: first Monday of month, 18:00–20:00 CET/CEST."""
    if now is None:
        now = datetime.now(timezone.utc)
    local = now.astimezone(_CET)
    return local.weekday() == 0 and local.day <= 7 and 18 <= local.hour < 20


# ──────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────
async def run(lb: LogBook, tname: str):
    # 0 = last QSO selected for edit, 1 = second-to-last, None = no edit in progress
    _state: dict = {
        "edit_idx": None,
        "restore_text": "",
        "warn_until": 0.0,
        "prev_band": None,
        "prev_mode": None,
        "webcam_notice": ("", 0.0),
        # Starts false every round: the operator's confirmation is about this
        # round's recording, and a remembered one would be a lie on the next.
        "sd_ack": False,
    }
    _webcam_path_prefix = (
        f"{datetime.now(timezone.utc).strftime('%y%m%d')}-{lb.my_callsign}"
    )

    def _toolbar() -> FormattedText:
        band, mode, qrg, online = current_rig()
        now = datetime.now(timezone.utc)
        t = now.strftime("%H:%M:%S")

        # Trigger a full REDRAW when band or mode changes so the TX line stays accurate.
        # Suppressed during edit mode: a rig change must not clear the operator's input.
        # _toolbar() runs on the event-loop thread, making get_app().exit() safe here.
        if _state["prev_band"] is not None:
            if band != _state["prev_band"] or mode != _state["prev_mode"]:
                _state["prev_band"] = band
                _state["prev_mode"] = mode
                if _state["edit_idx"] is None:
                    try:
                        get_app().exit(result=_REDRAW)
                    except Exception:
                        pass
        else:
            _state["prev_band"] = band
            _state["prev_mode"] = mode

        parts: list[tuple[str, str]] = []

        # During edit, warn when the rig is on a different band/mode than the QSO.
        if _state["edit_idx"] is not None:
            real_idx = len(lb.qsos) - 1 - _state["edit_idx"]
            if 0 <= real_idx < len(lb.qsos):
                q = lb.qsos[real_idx]
                if band != q.band or mode != q.mode:
                    parts.append(
                        ("bg:ansiyellow fg:black", f"  RIG→{band} {mode}  │  ")
                    )

        if time.monotonic() < _state["warn_until"]:
            parts.append(
                ("bg:ansiyellow fg:black", "  rig online — Alt+B/M ignored  │  ")
            )
        elif online:
            parts.append(("", f"  {qrg} MHz  │  "))
        else:
            parts.append(("", "  offline  │  "))

        rot_az, rot_online = rotator.current()
        rot_str = f"{rot_az:.0f}°" if rot_online else "---"
        parts.append(("", f"  ROT: {rot_str}  │  "))

        if _state["sd_ack"]:
            parts.append(("", "  SD ●  │  "))
        else:
            parts.append(("bg:ansired fg:white", "  SD ✗  │  "))

        if _recorder_check["warnings"]:
            parts.append(("bg:ansiyellow fg:black", "  ■ REC SET  │  "))

        if _ntp_check["warnings"]:
            parts.append(("bg:ansiyellow fg:black", "  ■ NTP  │  "))

        parts.append(_clock_chip())

        webcam = webcam_status()
        if webcam == "recording":
            parts.append(("bg:ansired fg:white", "  ● REC  │  "))
        elif webcam == "died":
            parts.append(("bg:ansired fg:white", "  WEBCAM DIED  │  "))
        else:
            parts.append(("bg:ansiyellow fg:black", "  NO WEBCAM  │  "))

        if time.monotonic() < _recorder_check["until"]:
            style = "ansiyellow" if _recorder_check["warnings"] else "ansigreen"
            parts.append(
                (f"bg:{style} fg:black", f"  {_recorder_check['notice']}  │  ")
            )

        webcam_msg, webcam_until = _state["webcam_notice"]
        if time.monotonic() < webcam_until:
            parts.append(("bg:ansigreen fg:black", f"  {webcam_msg}  │  "))

        time_style = (
            "bg:ansigreen fg:black" if _is_round_time(now) else "bg:ansired fg:white"
        )
        parts.append((time_style, f" {t}Z "))
        return FormattedText(parts)

    def _toolbar_signature() -> tuple:
        """Pure (no side effects) summary of everything _toolbar() renders --
        used by _toolbar_watcher to decide whether a redraw is actually
        needed. Deliberately does not call _toolbar() itself: that would fire
        its band/mode REDRAW on a tick nobody is prompting for."""
        band, mode, qrg, online = current_rig()
        now = datetime.now(timezone.utc)
        rot_az, rot_online = rotator.current()

        mismatch = False
        if _state["edit_idx"] is not None:
            real_idx = len(lb.qsos) - 1 - _state["edit_idx"]
            if 0 <= real_idx < len(lb.qsos):
                q = lb.qsos[real_idx]
                mismatch = band != q.band or mode != q.mode

        webcam_msg, webcam_until = _state["webcam_notice"]
        webcam_active = time.monotonic() < webcam_until

        rec_notice_active = time.monotonic() < _recorder_check["until"]

        return (
            band,
            mode,
            qrg,
            online,
            mismatch,
            time.monotonic() < _state["warn_until"],
            round(rot_az, 1) if rot_online else None,
            rot_online,
            _state["sd_ack"],
            _recorder_check["warnings"],
            _ntp_check["warnings"],
            rec_notice_active,
            _recorder_check["notice"] if rec_notice_active else None,
            webcam_status(),
            _clock_chip(),
            webcam_active,
            webcam_msg if webcam_active else None,
            now.strftime("%H:%M:%S"),
            _is_round_time(now),
        )

    async def _toolbar_watcher(app) -> None:
        """Replaces prompt_toolkit's own refresh_interval polling: that
        called _toolbar() (and therefore redrew the screen) unconditionally
        every 100ms, 10x/s, even though almost every one of those ticks
        produces byte-for-byte identical output (the clock only changes
        once a second; rig/rotator/webcam state changes far less often than
        that). Each redraw still emits terminal escape codes (at minimum a
        cursor-repositioning sequence) even when no visible cell changes --
        under asciinema this means 10 recorded output events per second for
        the whole round, most of them redundant. Polling at the same 10Hz
        cadence (so a real second-boundary is still caught within ~100ms,
        preserving why 10Hz was chosen over 1Hz in the first place) but only
        calling invalidate() when the signature actually differs cuts this
        down to roughly one redraw per second in the common case.

        `app` is passed in rather than found with get_app(): this task is
        created before prompt_async() runs, so the application is not the
        current one in this context yet."""
        last = None
        while True:
            webcam_finalize_name()
            notice = webcam_reap()
            if notice:
                _state["webcam_notice"] = (notice, time.monotonic() + 5.0)
            sig = _toolbar_signature()
            if sig != last:
                last = sig
                app.invalidate()
            await asyncio.sleep(0.1)

    def _qso_to_input(q: QSO) -> str:
        parts = [q.callsign, q.rst_r, f"{q.nr_r:03d}"]
        if q.loc:
            parts.append(q.loc)
        return " ".join(parts)

    def _cache_loc(callsign: str, loc: str) -> None:
        loc_cache.remember(lb.loc_cache, callsign, loc)

    def _enter_edit(idx: int) -> None:
        """Set edit_idx and queue a REDRAW with the QSO's data in the buffer."""
        real_idx = len(lb.qsos) - 1 - idx
        if real_idx < 0 or real_idx >= len(lb.qsos):
            return
        _state["edit_idx"] = idx
        _state["restore_text"] = _qso_to_input(lb.qsos[real_idx])
        get_app().exit(result=_REDRAW)

    def _rprompt() -> HTML | str:
        if _state["edit_idx"] is not None:
            idx = _state["edit_idx"]
            real_idx = len(lb.qsos) - 1 - idx
            if 0 <= real_idx < len(lb.qsos):
                nr_s = lb.qsos[real_idx].nr_s
                return HTML(f"<ansiblue><b>  EDIT #{nr_s:03d}  </b></ansiblue>")
        try:
            text = get_app().current_buffer.text
        except Exception:
            return ""
        tokens = text.upper().split()
        if not tokens:
            return ""
        first = tokens[0]
        if is_locator(first) and len(tokens) == 1:
            dist = lb.dist(first)
            bear = lb.bearing(first)
            if dist:
                return HTML(
                    f"<ansigreen>  {dist} km  {bear}° {_bearing_arrow(bear)}  </ansigreen>"
                )
            return ""
        if not RE_CALLSIGN.match(first):
            return ""
        callsign = first
        band, mode, *_ = current_rig()
        locs = lb.loc_cache.get(callsign, [])
        geo = ""
        if locs:
            dist = lb.dist(locs[0])
            bear = lb.bearing(locs[0])
            if dist:
                geo = f"  {locs[0]}  {dist} km  {bear}° {_bearing_arrow(bear)}"
        worked_str = _format_combos(lb.worked_combos(callsign))
        # ansibrightred, not ansired: when the current band/mode is itself a
        # dup the whole input line background turns ansired (_get_input_style),
        # and that reaches the rprompt too -- plain red text would be red-on-red
        # and unreadable there. The brighter red stays legible on both the
        # default dark background and the ansired dup background.
        tail = f"  <ansibrightred>{worked_str}</ansibrightred>" if worked_str else ""
        if lb.is_dup(callsign, band, mode):
            return HTML(
                f"<ansired><b>  DUP  </b></ansired><ansigreen>{geo}  </ansigreen>{tail}"
            )
        if geo or tail:
            return HTML(f"<ansigreen>{geo}  </ansigreen>{tail}")
        return ""

    def _get_input_style() -> Style:
        if _state["edit_idx"] is not None:
            return Style.from_dict({})
        try:
            text = get_app().current_buffer.text.upper().split()
            if text and RE_CALLSIGN.match(text[0]):
                band, mode, *_ = current_rig()
                if lb.is_dup(text[0], band, mode):
                    return Style.from_dict({"": "bg:ansired fg:white"})
        except Exception:
            pass
        return Style.from_dict({})

    kb = KeyBindings()

    @kb.add(" ")
    def _on_space(event):
        buf = event.app.current_buffer
        if buf.cursor_position != len(buf.text):
            buf.insert_text(" ")
            return
        buf.insert_text(" ")
        tokens = buf.text.strip().split()
        if len(tokens) == 1:
            callsign = tokens[0].upper()
            if not RE_CALLSIGN.match(callsign):
                return
            band, mode, *_ = current_rig()
            rst = "599" if mode == "CW" else "59"
            predicted = _predict_nr(lb, callsign, band, mode)
            if predicted is not None:
                buf.insert_text(f"{rst} {predicted:03d}")
            else:
                buf.insert_text(rst + " ")
        elif len(tokens) == 3:
            locs = lb.loc_cache.get(tokens[0].upper(), [])
            if len(locs) == 1:
                buf.insert_text(locs[0])  # only one known — insert directly
            elif locs:
                buf.start_completion(select_first=True)  # multiple — show choice

    @kb.add("backspace")
    def _on_backspace(event):
        buf = event.app.current_buffer
        if buf.text:
            buf.delete_before_cursor()

    @kb.add("up")
    def _on_up(event):
        buf = event.app.current_buffer
        if buf.complete_state:
            buf.complete_previous()
            return
        if _state["edit_idx"] is None and buf.text:
            buf.history_backward()
            return
        n = len(lb.qsos)
        if n == 0:
            return
        new_idx = (
            0 if _state["edit_idx"] is None else min(_state["edit_idx"] + 1, n - 1)
        )
        _enter_edit(new_idx)

    @kb.add("down")
    def _on_down(event):
        buf = event.app.current_buffer
        if buf.complete_state:
            buf.complete_next()
            return
        if _state["edit_idx"] is None:
            if buf.text:
                buf.history_forward()
            return
        if _state["edit_idx"] > 0:
            _enter_edit(_state["edit_idx"] - 1)
        else:
            _state["edit_idx"] = None
            _state["restore_text"] = ""
            buf.set_document(Document(""))
            get_app().exit(result=_REDRAW)

    @kb.add("escape")
    def _on_escape(event):
        buf = event.app.current_buffer
        _cw_stop()
        if buf.complete_state:
            buf.cancel_completion()
            return
        if _state["edit_idx"] is not None:
            _state["edit_idx"] = None
            _state["restore_text"] = ""
            buf.set_document(Document(""))
            get_app().exit(result=_REDRAW)

    for _fn_idx, _macro in enumerate(CW_MACROS, 1):

        @kb.add(f"f{_fn_idx}")
        def _fn_key(event, _tmpl=_macro):
            buf = event.app.current_buffer
            tokens = buf.text.strip().split()
            their_callsign = tokens[0].upper() if tokens else ""
            band, *_ = current_rig()
            _cw_send(_expand_cw(_tmpl, lb, their_callsign, band))

    @kb.add("escape", "b")
    def _on_alt_b(event):
        if _rig["online"]:
            _state["warn_until"] = time.monotonic() + 2.0
        else:
            cur = _rig_manual.get("band", "")
            _rig_manual["band"] = (
                BANDS[(BANDS.index(cur) + 1) % len(BANDS)] if cur in BANDS else BANDS[0]
            )
        event.app.invalidate()

    @kb.add("escape", "m")
    def _on_alt_m(event):
        if _rig["online"]:
            _state["warn_until"] = time.monotonic() + 2.0
        else:
            cur = _rig_manual.get("mode", "")
            _rig_manual["mode"] = (
                MODES[(MODES.index(cur) + 1) % len(MODES)] if cur in MODES else MODES[0]
            )
        event.app.invalidate()

    @kb.add("escape", "r")
    def _on_alt_r(event):
        _, rot_online = rotator.current()
        if not rot_online:
            return
        loc = None
        if _state["edit_idx"] is not None:
            real_idx = len(lb.qsos) - 1 - _state["edit_idx"]
            if 0 <= real_idx < len(lb.qsos):
                loc = lb.qsos[real_idx].loc
        else:
            try:
                tokens = event.app.current_buffer.text.upper().split()
                if tokens:
                    first = tokens[0]
                    if is_locator(first):
                        loc = first
                    elif RE_CALLSIGN.match(first):
                        locs = lb.loc_cache.get(first, [])
                        if locs:
                            loc = locs[0]
            except Exception:
                pass
        if loc:
            rotator.point_at(lb.bearing(loc))

    @kb.add("escape", "s")
    def _on_alt_s(event):
        """Confirm the radio's SD-card recording is running — the one fact
        about this round the software has no way of finding out for itself.
        Also re-reads the radio's settings, since this is the key pressed
        straight after touching the radio: both the recorder's and the NTP
        client's, so one key answers "is the radio set up for a round"."""
        _state["sd_ack"] = not _state["sd_ack"]
        _spawn(_settings_check_run())
        event.app.invalidate()

    @kb.add("escape", "v")
    def _on_alt_v(_event):
        msg = webcam_toggle(_webcam_path_prefix)
        if msg:
            _state["webcam_notice"] = (msg, time.monotonic() + 5.0)

    @kb.add("enter", filter=has_completions)
    def _on_enter_completion(event):
        buf = event.app.current_buffer
        state = buf.complete_state
        if state and state.current_completion:
            buf.apply_completion(state.current_completion)
        else:
            buf.cancel_completion()

    session = PromptSession(
        completer=CallsignCompleter(lb.loc_cache),
        key_bindings=kb,
        complete_while_typing=False,
        enable_history_search=False,
    )
    session.default_buffer.on_text_changed += on_buffer_changed
    watcher = asyncio.create_task(_toolbar_watcher(session.app))
    rot_poll = asyncio.create_task(rotator.poll())
    await rig_server.start(RIG_SERVER_PORT, rig_snapshot)

    await _await_radio()

    while True:
        band, mode, qrg, online = current_rig()
        os.write(1, b"\033[2J\033[H")
        _print_header(lb)
        focus = (
            len(lb.qsos) - 1 - _state["edit_idx"]
            if _state["edit_idx"] is not None
            else None
        )
        try:
            rows = os.get_terminal_size().lines
        except OSError:
            rows = 24
        _print_recent(lb, n=max(3, rows - 9), focus=focus)

        band, mode, _, _ = current_rig()
        nr = lb.next_nr(band)
        rst = "599" if mode == "CW" else "59"
        print(f"\033[1;92m  TX ► {lb.my_callsign}  {rst}  {nr:03d}  {lb.my_loc}\033[0m")

        default = _state.pop("restore_text", "") or ""
        try:

            def _prompt_msg() -> str:
                if _state["edit_idx"] is not None:
                    real_idx = len(lb.qsos) - 1 - _state["edit_idx"]
                    if 0 <= real_idx < len(lb.qsos):
                        q = lb.qsos[real_idx]
                        return f"{q.band} {q.mode}  RX ► "
                b, m, *_ = current_rig()
                return f"{b} {m}  RX ► "

            result = await session.prompt_async(
                _prompt_msg,
                bottom_toolbar=_toolbar,
                rprompt=_rprompt,
                style=DynamicStyle(_get_input_style),
                default=default,
                pre_run=lambda: setattr(get_app(), "ttimeoutlen", 0.05),
            )
        except KeyboardInterrupt:
            _state["edit_idx"] = None
            continue
        except EOFError:
            break
        if result is _REDRAW:
            continue
        line = result.strip()

        if not line:
            _state["edit_idx"] = None
            continue

        if line.startswith("!"):
            _state["edit_idx"] = None
            _handle_command(line, lb, tname)
            continue

        parsed = parse_input(line)
        if isinstance(parsed, str):
            _state["edit_idx"] = None
            if parsed:
                print(f"\033[31m  {parsed}\033[0m")
                input("  [Enter to continue]")
            continue

        edit_idx = _state["edit_idx"]
        _state["edit_idx"] = None

        if edit_idx is not None:
            # Replace an existing QSO; preserve dt, band, mode, nr_s, rst_s
            real_idx = len(lb.qsos) - 1 - edit_idx
            if 0 <= real_idx < len(lb.qsos):
                old = lb.qsos[real_idx]
                loc = parsed["loc"]
                lb.qsos[real_idx] = QSO(
                    dt=old.dt,
                    band=old.band,
                    mode=old.mode,
                    callsign=parsed["callsign"],
                    rst_s=old.rst_s,
                    nr_s=old.nr_s,
                    rst_r=parsed["rst_r"],
                    nr_r=parsed["nr_r"],
                    loc=loc,
                    points=lb.points(loc),
                )
                lb.worked = {(q.callsign, q.band, q.mode) for q in lb.qsos}
                _cache_loc(parsed["callsign"], loc)
                save_all(lb, tname)
            continue

        # New QSO — re-read rig at the moment Enter is pressed
        band, mode, qrg, online = current_rig()

        callsign = parsed["callsign"]
        nr_r = parsed["nr_r"]
        loc = parsed["loc"]
        rst_def = "599" if mode == "CW" else "59"
        rst_s = rst_def
        rst_r = parsed["rst_r"]
        nr_s = lb.next_nr(band)
        points = lb.points(loc)

        now = datetime.now(timezone.utc)
        qso = QSO(
            dt=now.replace(second=0, microsecond=0),
            band=band,
            mode=mode,
            callsign=callsign,
            rst_s=rst_s,
            nr_s=nr_s,
            rst_r=rst_r,
            nr_r=nr_r,
            loc=loc,
            points=points,
        )

        dup = lb.add(qso)
        if dup:
            print(
                f"\033[31m  *** DUP *** {callsign} already in log for {band} {mode}\033[0m"
            )
            input("  [Enter to continue]")

        log_input_event(
            {
                "t": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "event": "qso",
                "call": callsign,
                "band": band,
                "mode": qso.mode,
                "nr_s": nr_s,
                "dup": dup,
            }
        )

        _cache_loc(callsign, loc)
        save_all(lb, tname)

    watcher.cancel()
    rot_poll.cancel()
    print("\nSaving EDI files...")
    paths = save_all(lb, tname)
    if paths:
        for p in paths:
            print(f"  {p}")
    else:
        print("  (no QSOs logged)")


async def _round(lb: LogBook, tname: str) -> None:
    """The loop-side lifecycle: bring the radio up, run the round, and tear
    both down however it ends -- normal Ctrl-D exit, an early return from the
    offline wizard, a crash, or Ctrl-C. Signals go through
    _install_signal_handlers instead."""
    _radio_start()
    _install_signal_handlers()
    try:
        await run(lb, tname)
    finally:
        webcam_stop_if_running()
        await _radio_stop()


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────
def _load_callsign() -> str:
    try:
        auth = netrc.netrc().authenticators("www.on4kst.info")
        if auth:
            return auth[0].upper()
    except Exception:
        pass
    return "HA5LA"


def _edi_qso_count(path: Path) -> int:
    try:
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("[QSORecords;"):
                return int(line.split(";")[1].rstrip("]"))
    except Exception:
        pass
    return 0


def main():
    require_round_directory()
    print("Puskás URH Kupa Logger")
    print("─" * 40)

    lb: LogBook | None = None
    tname: str = ""

    edi_files = sorted(Path(".").glob("*.[Ee][Dd][Ii]"))
    if edi_files:
        summary = ", ".join(f"{p.name} ({_edi_qso_count(p)} QSOs)" for p in edi_files)
        print(f"Found existing logs: {summary}")
        ans = input("Resume? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            print("Building locator cache...")
            cache = loc_cache.load()
            result = load_from_edi(edi_files, cache)
            if result:
                lb, tname = result
                for q in lb.qsos:
                    loc_cache.remember(lb.loc_cache, q.callsign, q.loc)
                print(f"Callsign: {lb.my_callsign}")
                print(f"Locator:  {lb.my_loc}")
                print(f"Contest:  {tname}")
                print(f"Loaded {len(lb.qsos)} QSOs")

    if lb is None:
        my_callsign = _load_callsign()
        tname = tname_for(datetime.now(timezone.utc))
        print(f"Callsign: {my_callsign}")
        print(f"Locator:  {MY_LOCATOR}")
        print(f"Contest:  {tname}")
        print("Building locator cache...")
        cache = loc_cache.load()
        lb = LogBook(my_callsign, MY_LOCATOR, cache)

    # Opened before the radio/rotator threads start: both write to it the
    # moment they have something, and the radio's first push arrives within
    # a second of connecting.
    _telem_path = Path(
        f"{datetime.now(timezone.utc).strftime('%y%m%d')}-{lb.my_callsign}-telemetry.jsonl"
    )
    telemetry_open(_telem_path)

    _scope_path = Path(
        f"{datetime.now(timezone.utc).strftime('%y%m%d')}-{lb.my_callsign}.scope"
    )
    scope_open(_scope_path)
    print(f"Scope:     {_scope_path} (written once the radio connects)")
    print(f"Telemetry: {_telem_path}")
    _input_log_path = Path(
        f"{datetime.now(timezone.utc).strftime('%y%m%d')}-{lb.my_callsign}-input.jsonl"
    )
    input_log_open(_input_log_path)
    print(f"Input log: {_input_log_path}")
    print("Webcam:    Alt+V to start/stop recording")
    print("Audio:     switch on the radio's Voice Recorder, then Alt+S to confirm")
    print("Clock:     the radio takes its time from this laptop — toolbar CLK / ■ NTP")
    print(f"Band/mode: from the radio, {START_BAND} {START_MODE} without one (Alt+B/M)")

    print()
    print("Input: CALL RST NR [LOC]   e.g.  HA7NS 59 015   or  HA7NS 59 015 JN97WM")
    print(
        "Tab-complete callsigns  │  Space after callsign fills RST  │  Space after NR fills locator"
    )
    print("!help for commands  │  Ctrl-D to save and exit")
    print()
    input("[Enter to start]")

    try:
        asyncio.run(_round(lb, tname))
    except Exception as e:
        print(f"\n[ERROR] {e}")
        save_all(lb, tname)
        raise


if __name__ == "__main__":
    main()
    # Interpreter shutdown joins prompt_toolkit's input thread, which can be
    # left blocked forever on a terminal that vanished (tmux kill-session).
    # The resulting process ignores even SIGTERM -- a main thread that has
    # already returned no longer runs Python signal handlers -- and sits on the
    # rig server port. Teardown is done and every file flushes as it writes, so
    # there is nothing left to lose by not unwinding.
    sys.stdout.flush()
    os._exit(0)
