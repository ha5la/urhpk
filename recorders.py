"""The round's side-channel recorders: everything written alongside the audio.

Four files, all of them consumed later by contest_video.py -- telemetry, the
input box, the radio's own scope sweeps, and the webcam. They are together
because they are the same kind of thing: a per-round file this process opens,
appends to from whichever thread has news, and closes when the round ends.

Each recorder is inert until its `*_open` is called, so a round that does not
want one simply never opens it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import icom_net
import webcam_log

WEBCAM_DEVICE = "/dev/video0"  # find with: v4l2-ctl --list-devices
WEBCAM_AUDIO_SOURCE = "default"  # find with: pactl list short sources


# ──────────────────────────────────────────────────────────────
# Scope recorder — the radio's own sweeps to a .scope file
# (contest_video.py --scope). Lives here because the logger owns the
# radio's single network session; the icom_net CLI harness can't run
# alongside it.
# ──────────────────────────────────────────────────────────────
_scope_rec: dict = {"path": None, "file": None}
_scope_rec_lock = threading.Lock()


def scope_open(path: Path) -> None:
    """Arm the scope recorder. The file itself is opened by the first sweep,
    so a round where the radio never connects leaves no empty .scope behind."""
    with _scope_rec_lock:
        _scope_rec["path"] = path


def on_scope(start_hz: int, end_hz: int, pixels: bytes) -> None:
    with _scope_rec_lock:
        if _scope_rec["path"] is None:
            return
        if _scope_rec["file"] is None:
            _scope_rec["file"] = open(_scope_rec["path"], "ab")
        icom_net.write_scope_record(
            _scope_rec["file"], time.time(), start_hz, end_hz, pixels
        )
        _scope_rec["file"].flush()


# ──────────────────────────────────────────────────────────────
# Telemetry recorder — one JSON line per actual change, to CWD.
#
# Records are partial: a rig event carries freq_hz/mode, a rotator event
# carries az. A field a line doesn't mention simply didn't change, and
# rig_state.build_state_events carries each one forward.
#
# This was a 1 Hz sampler of current_rig()/rotator.current() while the rig was
# behind rigctld and had to be polled anyway. freq/mode now arrive as
# icom_net CI-V Transceive pushes -- and IcomNetRig._apply_update already
# suppresses no-op updates, so the callback *is* the change stream. Sampling
# it on a timer put back onto a lag-free source the very latency icom_net
# exists to remove, blurred any retune shorter than the poll interval, and
# wrote overwhelmingly duplicate lines: on the real July round only 616 of
# 9313 lines carried anything new.
#
# az has no push source at all -- Hamlib's rotator API never defined the
# async hook, for any backend (see hamlib_supervisor.py's notes) -- so it
# stays polled, but only writes when the reading actually moves.
#
# No ptt field: the WAV recordings' own IC-9700 metadata already carries it
# straight from the rig with zero lag (see contest_video.py's
# read_wav_metadata).
# ──────────────────────────────────────────────────────────────

_telem: dict = {"fh": None}
_telem_lock = threading.Lock()
_telem_meters: dict = {"last": None}


def _utc_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def telemetry_open(path: Path) -> None:
    with _telem_lock:
        _telem["fh"] = open(path, "a")


def telemetry_write(rec: dict) -> None:
    """Two threads write here — the radio's CI-V receive thread and the
    rotator poller — so the lock guards a genuine race, not a theoretical one."""
    with _telem_lock:
        fh = _telem["fh"]
        if fh is None:
            return
        try:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
        except Exception:
            pass


def telemetry_rig_record(now: datetime, freq_hz: int | None, mode: str | None) -> dict:
    """freq_hz is the radio's own exact value, not the toolbar's kHz-rounded
    `qrg` string the 1 Hz sampler used to re-parse. That rounding is where
    contest_video's documented 160/250/300/310 Hz "WAV vs telemetry
    disagreement" came from — it was this logger quantizing, not two sources
    reading the rig differently."""
    return {"t": _utc_stamp(now), "freq_hz": freq_hz, "mode": mode}


def telemetry_meter_record(now: datetime, meters: dict) -> dict:
    """Raw meter readings, 0-255 exactly as the radio reports them.

    Deliberately not converted to volts/amps/SWR here. The calibration is the
    least trustworthy part of this data -- the IC-7300's published Id curve
    reads ~1.5x high against a measured 12 A on this radio -- so the raw
    numbers are recorded and contest_video.py converts at render time, which
    makes a better curve a one-line change instead of a ruined recording."""
    rec = {"t": _utc_stamp(now)}
    rec.update({name: meters[name] for name in sorted(meters)})
    return rec


def on_radio_meters(meters: dict) -> None:
    """One line per *change*, matching the rest of the telemetry stream.

    Unlike freq/mode this genuinely has to be sampled -- the radio only
    reports meters when asked (confirmed on the wire) -- so there is no
    lag-free source being needlessly re-sampled here, which was the objection
    to the old 1 Hz rig sampler. While receiving, Po/SWR/Id sit at a flat zero
    and Vd barely moves, so change-only keeps an idle hour nearly silent."""
    if meters == _telem_meters["last"]:
        return
    _telem_meters["last"] = dict(meters)
    telemetry_write(telemetry_meter_record(datetime.now(timezone.utc), meters))


def forget_meters() -> None:
    """Drop the last meter reading, so the first one after a radio reconnect
    is written even if it happens to match the last one before the drop."""
    _telem_meters["last"] = None


def telemetry_rot_record(now: datetime, az: float | None) -> dict:
    return {"t": _utc_stamp(now), "az": round(az, 1) if az is not None else None}


# ──────────────────────────────────────────────────────────────
# Input-box recorder — event-triggered (one line per keystroke), not
# polled: a 1 Hz sample like telemetry would blur or entirely miss fast
# typing, and the buffer only changes on a keypress in the first place, so
# there's nothing to poll. Feeds contest_video.py's typewriter overlay.
#
# Two event kinds share the file: "text" (one per keystroke, the full
# current buffer contents) and "qso" (one per QSO actually appended to the
# log, microsecond-precise). The EDI format only stores QSO time to the
# minute, which is what makes qso_windows.py's QSO timing an
# audio-structure-snapping guess rather than exact — this file's "qso"
# events give it an exact submit timestamp to use instead, when available.
# Note this deliberately does *not* try to distinguish a submit from an
# abort (Enter vs Ctrl+U/Escape) at the "text" level — both just clear the
# buffer the same way, and even Enter's own clear only happens at the start
# of the *next* prompt, not at keypress time. Trying to infer "QSO logged"
# from that stream is unreliable; the explicit "qso" event below, written
# from the one place in the code that actually knows a QSO was appended, is
# the unambiguous alternative.
# ──────────────────────────────────────────────────────────────

_input_log_fh = None


def input_log_open(path: Path) -> None:
    global _input_log_fh
    _input_log_fh = open(path, "a")


def log_input_event(rec: dict) -> None:
    if _input_log_fh is None:
        return
    try:
        _input_log_fh.write(json.dumps(rec) + "\n")
        _input_log_fh.flush()
    except Exception:
        pass


def on_buffer_changed(buf) -> None:
    log_input_event(
        {
            "t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "event": "text",
            "text": buf.text,
        }
    )


# ──────────────────────────────────────────────────────────────
# Webcam capture (Alt+V toggles start/stop) -- unlike the phone recording
# contest_video.py had to sync via audio cross-correlation (two independent,
# unrelated clocks), this runs on the same machine as the logger: start/stop
# is logged through the exact same log_input_event/datetime.now(timezone.utc)
# used for QSOs and keystrokes, so the recording's real start time is known
# precisely with no separate device clock to reconcile at all.
# ──────────────────────────────────────────────────────────────

_webcam_proc = None
_webcam_log_fh = None
_webcam_out_path = None
_webcam_log_path = None


def _webcam_precise_name(out_path: str, start: datetime) -> str:
    """Insert a precise, filesystem-safe UTC timestamp before the extension,
    e.g. `foo-webcam.mp4` -> `foo-webcam-20260722T121101.868307Z.mp4`. A
    rename is an instant, zero-extra-disk-space metadata operation on the
    same filesystem -- unlike tagging the file's own container metadata,
    which needs a full second copy of the video data (see CLAUDE.md)."""
    stem, ext = os.path.splitext(out_path)
    return f"{stem}-{start.strftime('%Y%m%dT%H%M%S.%fZ')}{ext}"


def _webcam_capture_cmd(device: str, audio_source: str, out_path: str) -> list[str]:
    """The ffmpeg command to capture the local webcam + mic to `out_path`.
    -preset ultrafast keeps this cheap enough to run alongside the logger
    for a multi-hour session without competing for CPU with the radio/rotator
    threads or the UI itself.

    -use_wallclock_as_timestamps 1 on the v4l2 input stamps every captured
    frame with the real gettimeofday wallclock, so ffmpeg logs an exact
    frame-0 UTC start in the *-webcam.log. webcam_toggle's stop branch reads
    it back (webcam_log.frame_zero_utc) to rename the finished file with that
    exact timestamp -- webcam_sync.webcam_start_from_log parsing the
    same log is only a fallback now, for recordings made before this existed
    or where the rename didn't happen. Without the flag, v4l2 timestamps are
    CLOCK_MONOTONIC (uptime), useless as an absolute time -- and the
    logger's own webcam_start event is stamped *before* this subprocess even
    spawns, so it leads real frame 0 by the ffmpeg + camera warmup latency
    (~1s, variable) -- which is exactly why that log line, not the event, is
    the source of truth."""
    return [
        "ffmpeg",
        "-y",
        "-f",
        "v4l2",
        "-use_wallclock_as_timestamps",
        "1",
        "-i",
        device,
        "-f",
        "pulse",
        "-i",
        audio_source,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        out_path,
    ]


def webcam_toggle(path_prefix: str) -> str | None:
    """Start or stop webcam capture; returns a status message for the
    toolbar/notice area, or None if nothing changed (e.g. ffmpeg missing)."""
    global _webcam_proc, _webcam_log_fh, _webcam_out_path, _webcam_log_path
    now = datetime.now(timezone.utc)
    if _webcam_proc is None:
        out_path = f"{path_prefix}-webcam.mp4"
        log_path = f"{path_prefix}-webcam.log"
        try:
            _webcam_log_fh = open(log_path, "a")
            _webcam_proc = subprocess.Popen(
                _webcam_capture_cmd(WEBCAM_DEVICE, WEBCAM_AUDIO_SOURCE, out_path),
                stdin=subprocess.DEVNULL,
                stdout=_webcam_log_fh,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            _webcam_proc = None
            return f"webcam start failed: {e}"
        _webcam_out_path = out_path
        _webcam_log_path = log_path
        log_input_event(
            {"t": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "event": "webcam_start"}
        )
        return f"recording {out_path}"
    else:
        _webcam_proc.send_signal(signal.SIGINT)
        try:
            _webcam_proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            _webcam_proc.terminate()
        _webcam_proc = None
        if _webcam_log_fh:
            _webcam_log_fh.close()
            _webcam_log_fh = None
        if _webcam_out_path and _webcam_log_path:
            start = webcam_log.frame_zero_utc(_webcam_log_path)
            if start is not None:
                renamed = _webcam_precise_name(_webcam_out_path, start)
                try:
                    Path(_webcam_out_path).rename(renamed)
                except OSError:
                    pass  # leave it at its original name -- not fatal
        _webcam_out_path = None
        _webcam_log_path = None
        log_input_event(
            {"t": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "event": "webcam_stop"}
        )
        return "recording stopped"


def webcam_recording() -> bool:
    """Whether a webcam capture is running right now — what the toolbar's REC
    indicator shows."""
    return _webcam_proc is not None


def webcam_stop_if_running() -> None:
    """Called on exit so a still-running capture is stopped cleanly (SIGINT,
    not killed) rather than left orphaned or the mp4 left unfinalized."""
    if _webcam_proc is not None:
        webcam_toggle("")  # path_prefix unused on the stop branch
