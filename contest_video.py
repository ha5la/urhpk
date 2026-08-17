#!/usr/bin/env -S uv run --extra render
"""Produce an annotated CW contest video from a recording + EDI log.

Given a directory of timestamped WAV segments (split on RX/TX switches, as
recorded during the contest) and the EDI log for the same round, this builds a
YouTube-ready MP4 with:

  * a scrolling audio spectrogram (SDR-style waterfall) as background, or the
    radio's own spectrum scope where a recording of it covers (--scope)
  * a HUD bar along the bottom carrying the score, the RX/TX lamp, the QRG and
    band/mode readouts, the compass and a live CW decode ticker
  * optionally, a large picture-in-picture of the logger/irssi terminal
    (--cast, an asciinema recording) and a small webcam PiP in the HUD's own
    face recess

Every overlay is a video: the HUD, the cast PiP and the scope background are
each rendered to their own clip and composited with the webcam in a single
ffmpeg pass -- no frame-by-frame rendering of the main video.

Usage:
    uv run contest_video.py RECORDING_DIR EDI_FILE [-o OUT.mp4]

The WAV filenames must start with a `YYYYMMDD_HHMMSS` local-time stamp (the
format the recorder writes). Segments are concatenated in filename order; the
audio timeline is the sum of segment durations, and wall-clock time (from the
filenames) is used only to line QSOs up against the audio. The EDI QSO times
are UTC; the UTC->local offset is derived automatically from the data, so DST
is handled without configuration.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from urhpk import wiring
from urhpk.cast_render import (
    cast_start_fraction,
    parse_cast_header,
    render_cast_video,
)
from urhpk.chapters import build_chapters, build_srt
from urhpk.cw_decode import (
    MAX_OVER_S,
    decode_round,
)
from urhpk.hud import (
    build_hud_timeline,
)
from urhpk.hud_draw import (
    HUD_THEME_DIR,
    draw_hud_frame,
    hud_art,
    hud_demo_state,
    hud_height,
    hud_theme_overlay,
    load_hud_theme,
    render_hud_video,
)
from urhpk.icom_net import read_scope_records
from urhpk.qso_windows import qso_windows
from urhpk.rig_state import (
    apply_clock_offset,
    build_state_events,
    load_input_log,
    load_telemetry,
    match_qso_times,
)
from urhpk.scope_render import render_scope_video
from urhpk.timeline import (
    GAP_KEEP_S,
    SPLIT_EXCESS_S,
    Qso,
    Segment,
    _eff,
    audio_time_for,
    compensate_split_excess,
    derive_utc_offset,
    merge_edi,
    read_wav_metadata,
    remap_audio_t,
    scan_segments,
    stream_start,
    trim_to_duration,
)
from urhpk.video_format import RENDER_FPS, RESOLUTIONS
from urhpk.webcam_face import face_crop, scan_faces
from urhpk.webcam_sync import (
    WebcamClip,
    parse_webcam_precise_filename,
    parse_webcam_wall,
    refine_webcam_start,
    sync_webcam_start,
    webcam_start_wall,
)

# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------


def concat_audio(segs: list[Segment], out_wav: str) -> None:
    listfile = out_wav + ".txt"
    with open(listfile, "w") as fh:
        for s in segs:
            fh.write(f"file '{os.path.abspath(s.path)}'\n")
            if s.eff_dur is not None:
                fh.write(f"outpoint {s.eff_dur:.6f}\n")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            listfile,
            "-c",
            "copy",
            out_wav,
        ],
        check=True,
    )
    os.remove(listfile)


def _ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return float(out.strip())


CAST_PIP_X_FRAC = 0.0104  # the terminal PiP is the dominant visual element, not
CAST_PIP_MARGIN_FRAC = 0.015  # a small inset -- the logger UI is most of what
# there is to watch. Its size is height-constrained (see render): with the HUD
# along the bottom, the room above it is the limit.
CAST_PIP_ALPHA = 0.85  # slightly transparent so the waterfall shows
# faintly through the terminal PiP; 1.0 = opaque
STREAM_TRIM_MARGIN_S = 5.0  # slack when trimming a side stream to the cut,
# so tpad's last-frame cloning never shows at the end of a preview


def _stream_input_args(start: float, path: str) -> list[str]:
    """ffmpeg input args placing a side stream's frame 0 at `start`.

    A negative start (the stream began before the audio -- see stream_start)
    is an -ss seek *into* the stream, not a negative -itsoffset: ffmpeg has
    no meaningful "shift these timestamps earlier than the output starts",
    and the frames before t=0 are simply ones the output never shows."""
    if start < 0:
        return ["-ss", f"{-start:.3f}", "-i", path]
    return ["-itsoffset", f"{start:.3f}", "-i", path]


def render(
    wav: str,
    out: str,
    W: int,
    H: int,
    webcams: list[WebcamClip] | None = None,
    cast: str | None = None,
    cast_start: float = 0.0,
    cast_rate: float = 0.0,
    scope: str | None = None,
    scope_start: float = 0.0,
    scope_end: float = 0.0,
    hud: str = "",
    hud_face: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-stats", "-loglevel", "warning", "-i", wav]

    # Inputs are added in this order when present: scope, cast, webcam --
    # indices computed up front so each branch below references its own
    # input by number regardless of which others are present, rather than
    # each branch guessing its own index from what came before it.
    # An input's index is taken at the moment it is appended rather than from
    # a separate list that has to be kept in the same order as the branches
    # below. Those two silently drifted apart the moment the HUD branch was
    # inserted ahead of the cast branch, and every stream then read another
    # stream's clip: the HUD was drawn at the cast PiP's position and size,
    # the terminal was squeezed into the webcam's face recess, and the webcam
    # was stretched full-width along the bottom where the HUD belongs. The
    # filter-graph string assertions all still passed, because each branch
    # was individually well-formed.
    def add_input(args: list[str]) -> int:
        idx = sum(1 for a in cmd if a == "-i")
        cmd.extend(args)
        return idx

    hud_h = hud_height(H)

    # Full-screen scrolling waterfall, dimmed to ~half luma so it reads as an
    # ambient background and the text stays crisp on top. overlap=0.8 makes it
    # scroll fast enough to fill the frame within the first few seconds.
    fchain = (
        f"[0:a]showspectrum=s={W}x{H}:mode=combined:slide=scroll:overlap=0.8:"
        f"color=intensity:scale=cbrt:fscale=log:saturation=1.6,"
        f"lutyuv=y=val*0.42,format=yuv420p,fps={RENDER_FPS}[specbg]"
    )
    bg = "specbg"
    if scope:
        # scope is our own render_scope_video output -- like the cast branch
        # (and unlike webcam), its own timestamps are real/absolute
        # (icom_net.py's write_scope_record uses real time.time() values),
        # so a plain -itsoffset positions it exactly, no drift-rate
        # correction needed. Drawn *under* the subtitles pass (unlike
        # cast/webcam, which sit on top of it as PiPs) so it acts as a real
        # replacement background rather than an inset -- the audio-derived
        # showspectrum layer stays underneath as a fallback for any stretch
        # the scope recording doesn't cover (didn't start recording yet,
        # stopped early, or a `--duration` cut lands outside its range).
        # tpad still guards against the shared filtergraph ending early the
        # same way it does for cast/webcam; enable='between(...)' (not
        # eof_action=pass) handles both the before-start and after-end gaps
        # with the one proven mechanism already used for those PiPs' own
        # start gate, rather than mixing two different techniques for the
        # same class of problem.
        scope_idx = add_input(_stream_input_args(scope_start, scope))
        fchain += (
            f";[{scope_idx}:v]scale={W}:{H},fps={RENDER_FPS},format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration=99999[scopebg]"
            f";[{bg}][scopebg]overlay=x=0:y=0:"
            f"enable='between(t,{max(scope_start, 0.0):.3f},{scope_end:.3f})'[bg2]"
        )
        bg = "bg2"
    cur = bg
    if cast:
        # cast is our own render_cast_video output -- a synthetic, constant-
        # framerate file we just encoded, so no drift *of its own* -- but
        # its internal timestamps came from asciinema's real-time capture
        # on the same laptop as the webcam, so the same laptop-clock-vs-
        # audio-clock drift measured via the webcam (see main(), cast_rate)
        # applies here too: setpts stretches its timeline the same way the
        # webcam branch's does, before the fps=RENDER_FPS resample. itsoffset
        # positions its own t=0 (the moment the logger started) at
        # cast_start in the output timeline. tpad clones its last frame so a
        # cast shorter than the round can't truncate the shared filtergraph,
        # same reasoning as the webcam branch below.
        # Height-constrained, not width-constrained: with a bar along the bottom
        # the terminal's limit is the room above it, and a width fraction
        # picked before the HUD existed simply overran it -- the logger's own
        # toolbar was drawn across the SCORE and QSOS panels.
        cast_x = round(W * CAST_PIP_X_FRAC)
        cast_y = round(H * CAST_PIP_MARGIN_FRAC)
        cast_scale = f"-2:{H - hud_h - 2 * cast_y}"
        cast_idx = add_input(_stream_input_args(cast_start, cast))
        # format=yuva420p + colorchannelmixer=aa lowers the PiP's alpha so the
        # overlay blends it over the waterfall (a little transparency, not a
        # wash) -- overlay honours the top input's own alpha channel.
        fchain += (
            f";[{cast_idx}:v]setpts=PTS/{1 - cast_rate:.8f},scale={cast_scale},fps={RENDER_FPS},"
            f"format=yuva420p,colorchannelmixer=aa={CAST_PIP_ALPHA},"
            f"tpad=stop_mode=clone:stop_duration=99999[castpip]"
            f";[{cur}][castpip]overlay=x={cast_x}:y={cast_y}:"
            f"enable='gte(t,{max(cast_start, 0.0):.3f})'[v1]"
        )
        cur = "v1"
    if hud:
        # No -itsoffset: unlike every other side stream here, the HUD clip is
        # generated *from* the output timeline rather than captured against an
        # independent clock, so its t=0 already is the output's t=0. Composited after
        # the cast (the bar is a status bar -- nothing overlaps it) and before
        # the webcam, which lands on top of it inside the face recess.
        hud_idx = add_input(["-i", hud])
        fchain += (
            f";[{hud_idx}:v]scale={W}:{hud_h},fps={RENDER_FPS},"
            f"tpad=stop_mode=clone:stop_duration=99999[hudbar]"
            f";[{cur}][hudbar]overlay=x=0:y=main_h-h[vhud]"
        )
        cur = "vhud"
    if webcams:
        # itsoffset delays the whole cam stream's presentation timestamps so
        # its own frame 0 lands at its start in the output timeline --
        # exactly right, since that's the real moment the phone started
        # recording. tpad clones the cam's last frame indefinitely so a clip
        # a little shorter than the round (as here) can never end the
        # shared filtergraph early and truncate the main waterfall/audio.
        # The cam is *not* mirrored: the logger's own Alt+V capture records
        # the laptop webcam already the right way round (an earlier phone
        # front-camera capture recorded raw/un-mirrored and needed an hflip;
        # the same-machine capture that replaced it does not).
        #
        # fps=RENDER_FPS on this branch matters even though the source
        # already claims 30fps: a real phone recording verified against
        # this (ffprobe: r_frame_rate 30/1, but avg_frame_rate ~29.997,
        # derived from its actual per-frame timestamps) is genuinely
        # variable-rate under a constant-looking label -- not one big
        # pause but 3,444 scattered micro frame-drops across the ~2h
        # recording (checked directly via each packet's own pts_time;
        # typical of thermal/buffer pressure on a long phone capture),
        # summing to exactly 0.753s of extra real time the frame count
        # alone doesn't account for. Left unfiltered, this is a real
        # reported symptom (in sync at the start of the video, over a
        # second off by the end): the PiP was silently running very
        # slightly fast relative to the audio-driven main timeline the
        # whole way through, since something upstream of this filter
        # apparently laid its frames out by count rather than by their
        # own true timestamps. The fps filter resamples using the
        # decoder's true per-frame PTS as its reference, duplicating
        # frames onto a clean 30fps grid that absorbs every one of those
        # scattered drops and actually matches real elapsed time --
        # eliminating the drift instead of just reducing it.
        #
        # setpts=PTS/(1-webcam_rate), applied first (before fps resamples
        # onto a clean grid, so that resampling itself uses the corrected
        # timeline): the phone and the radio recorder are two independent
        # devices whose clocks don't tick at exactly the same *rate* --
        # see refine_webcam_start, which fits this rate from real audio
        # cross-correlation. A rate mismatch is a linear drift, which
        # -itsoffset (a constant shift) cannot correct on its own; scaling
        # every presentation timestamp by 1/(1-rate) stretches or
        # compresses the PiP's own timeline just enough to compensate,
        # while -itsoffset still handles the constant (intercept) part.
        # webcam_rate defaults to 0.0 (identity scaling) when no rate was
        # determined (e.g. --webcam-offset was used instead, or
        # cross-correlation found no confident match).
        # The webcam belongs in the artwork's own face recess, exactly where
        # DOOM's portrait sits. Cropped to the recess's aspect (a centre crop
        # of a webcam pointed at the operator *is* a face portrait) rather
        # than letterboxed, which would leave bars inside the frame.
        #
        # A round has one clip per Alt+V start/stop pair, and they share the
        # single recess: each is overlaid on top of the one before it, in the
        # chronological order sync_webcams returns them in, and takes the
        # recess from its own start onwards. Between two clips the earlier
        # one's tpad-cloned last frame is what stays on screen.
        fx, fy, fw, fh = hud_face
        pip_x, pip_y = fx, H - hud_h + fy
        centred = f"crop=min(iw\\,ih*{fw}/{fh}):min(ih\\,iw*{fh}/{fw})"
        for i, clip in enumerate(webcams):
            last = i == len(webcams) - 1
            idx = add_input(["-itsoffset", f"{clip.start:.3f}", "-i", clip.path])
            # Each clip is cropped onto its own face: the operator cannot see
            # the Alt+V capture while it records, and a centred crop left them
            # off-centre for a third of a real round. Without a scan (no
            # detector installed) the crop is the size-agnostic expression it
            # always was.
            crop = centred
            if clip.face:
                cx, cy, cw, ch = face_crop(
                    *clip.face.source, fw, fh, face_cx=clip.face.cx
                )
                crop = f"crop={cw}:{ch}:{cx}:{cy}"
            fchain += (
                f";[{idx}:v]setpts=PTS/{1 - clip.rate:.8f},fps={RENDER_FPS},"
                f"{crop},scale={fw}:{fh},"
                f"tpad=stop_mode=clone:stop_duration=99999[pip{i}]"
                f";[{cur}][pip{i}]overlay=x={pip_x}:y={pip_y}:"
                f"enable='gte(t,{clip.start:.3f})'[{'v' if last else f'v{i}'}]"
            )
            cur = "v" if last else f"v{i}"
    if cur != "v":
        fchain += f";[{cur}]null[v]"
    cmd += [
        "-filter_complex",
        fchain,
        "-map",
        "[v]",
        "-map",
        "0:a",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "21",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-shortest",
        out,
    ]
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------


def _webcam_start(
    path: str,
    segs: list[Segment],
    qsos_all: list[Qso],
    offset_h: int,
    input_log: str | None,
) -> tuple[float, bool, str]:
    """Where one clip's frame 0 lands in the output, whether that placement is
    exact, and what it was derived from.

    Prefer the exact timestamp baked into the filename itself
    (parse_webcam_precise_filename -- self-contained, no sidecar file needed),
    then the logger's webcam_start event (~1s early, stamped before ffmpeg
    spawned). Both are same-machine, so placement is exact either way, no
    cross-correlation needed."""
    cam_wall = parse_webcam_precise_filename(path)
    src = "exact timestamp in filename"
    if cam_wall is None and input_log:
        cam_wall = webcam_start_wall(input_log)
        src = "logged webcam_start event"
    if cam_wall is not None:
        return audio_time_for(cam_wall + timedelta(hours=offset_h), segs), True, src
    cam_wall = parse_webcam_wall(path)
    cam_dur = _ffprobe_duration(path)
    start = sync_webcam_start(cam_wall, cam_dur, qsos_all, segs, offset_h)
    return start, False, "coarse, whole-hour only"


def sync_webcams(
    paths: list[str],
    segs: list[Segment],
    qsos_all: list[Qso],
    offset_h: int,
    input_log: str | None = None,
    manual_offset: float | None = None,
) -> tuple[list[WebcamClip], tuple[float, float] | None]:
    """Place every capture of the round on the output timeline, in
    chronological order, and return the clock-drift correction the cast PiP
    shares with them -- (intercept, rate), or None if none could be fitted.

    The drift is this laptop's clock against the radio's, so the round has one
    rate, fitted from the *longest* clip -- the widest lever arm and the most
    anchors make it the best-conditioned of the fits. A clip too short or too
    quiet to fit its own drifts just the same, so it borrows that rate. It does
    not borrow the intercept, which is only meaningful at the clip it was
    measured at, and which a µs-precise filename start does not need.

    The webcam_start-event fallback reads the log's *first* such event, so it
    can only stand in for a single clip; with several, each one's own filename
    carries its start (every Alt+V capture is renamed with it)."""

    def tag(i: int) -> str:
        return f"webcam {i + 1}/{len(paths)}" if len(paths) > 1 else "webcam"

    placed = [
        _webcam_start(
            p, segs, qsos_all, offset_h, input_log if len(paths) == 1 else None
        )
        for p in paths
    ]
    for i, (start, exact, src) in enumerate(placed):
        detail = (
            f"exact -- {src}, same-machine clock, no cross-correlation needed"
            if exact
            else f"{src} -- see refine_webcam_start below"
        )
        print(f"  {tag(i)}: synced to start at {start:.0f}s in the output ({detail})")

    if manual_offset is not None:
        print(
            f"  webcam: manual offset {manual_offset:+.2f}s applied to every clip "
            f"(no drift-rate correction -- pass no --webcam-offset to use "
            f"automatic cross-correlation instead)"
        )
        clips = [
            WebcamClip(p, start + manual_offset)
            for p, (start, _, _) in zip(paths, placed)
        ]
        return sorted(clips, key=lambda c: c.start), None

    # Even an exact filename/log-derived start only fixes the constant offset
    # -- the webcam capture (this machine's system clock, via gettimeofday) and
    # the radio recording (the WAV sample clock, an independent crystal in the
    # IC-9700) still aren't ticking at exactly the same *rate*. Confirmed on a
    # real ~2h same-machine Alt+V recording: cross-correlation anchors showed a
    # consistent, low-noise linear drift (~-1.2s intercept, residual std ~0.1s
    # after outlier rejection) growing to ~+5s by the end -- not measurement
    # noise, and large enough to be audible/visible. So this runs regardless of
    # an exact start; that start is still a much better seed for the
    # correlation search than the coarse whole-hour one.
    #
    # That ~+5s was measured before the radio's clock was put on NTP, while it
    # was still free-running, so some of the rate was time-of-day drift that NTP
    # now removes -- but not the sample-clock part, which no NTP can reach. The
    # rate printed below re-measures what is actually left on the next long round.
    fits: dict[int, tuple[float, float]] = {}
    for i, (path, (start, exact, _)) in enumerate(zip(paths, placed)):
        refined, rate, n = refine_webcam_start(path, segs, start)
        if n:
            fits[i] = (refined, rate)
            print(
                f"  {tag(i)}: audio cross-correlation refined start by "
                f"{refined - start:+.2f}s and found a "
                f"{rate * 3600:+.3f}s/hour clock-drift rate using {n} anchor(s) "
                f"-> starts at {refined:.2f}s"
            )
        else:
            print(
                f"  {tag(i)}: audio cross-correlation found no confident match "
                "(no audio track, or no TX segments long enough) -- using "
                f"{'exact' if exact else 'coarse whole-hour'} sync only; "
                "pass --webcam-offset to fine-tune manually"
            )

    drift = None
    if fits:
        best = max(fits, key=lambda i: _ffprobe_duration(paths[i]))
        drift = (fits[best][0] - placed[best][0], fits[best][1])
        if len(fits) > 1:
            print(
                f"  webcam: clock drift taken from {tag(best)}, the longest clip "
                f"({drift[1] * 3600:+.3f}s/hour)"
            )
    borrowed = drift[1] if drift else 0.0
    clips = [
        WebcamClip(path, *fits.get(i, (placed[i][0], borrowed)))
        for i, path in enumerate(paths)
    ]
    return sorted(clips, key=lambda c: c.start), drift


def main() -> None:
    # An unattended render is redirected to a log, where block-buffered stdout
    # lands whole kilobytes behind the progress bars and ffmpeg's own -stats,
    # both of which go to stderr unbuffered: the stage each bar belongs to was
    # announced after the stage had finished.
    sys.stdout.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # recdir/edi are optional because --hud-demo runs with no recording at hand,
    # and because giving neither means "find them here" (checked right after
    # parsing); every other mode still requires both.
    ap.add_argument(
        "recdir",
        nargs="?",
        help="directory of timestamped WAV segments -- omit it, and every "
        "other input too, to take them from the round directory you are in",
    )
    ap.add_argument(
        "edi",
        nargs="*",
        help="EDI log(s) for the same round -- pass more than one "
        "to merge multiple bands worked in one recording",
    )
    ap.add_argument("-o", "--out", default="contest_video.mp4")
    ap.add_argument("--pitch", type=float, default=600.0, help="CW tone Hz")
    ap.add_argument("--res", choices=RESOLUTIONS, default="1080p")
    ap.add_argument(
        "--skip-gaps",
        action="store_true",
        help=f"trim silent gaps between QSOs to {GAP_KEEP_S:.0f}s each",
    )
    ap.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="keep the intermediate .wav and side-stream clips for inspection",
    )
    ap.add_argument(
        "--telemetry",
        help="puskas_logger *-telemetry.jsonl -- optional: the RX/TX + QRG/mode "
        "badge already comes from the WAV files' own IC-9700 metadata; this "
        "only adds bearing (ROT) and refines QRG/mode within long segments "
        "where the operator QSY'd with nothing to split the WAV on",
    )
    ap.add_argument(
        "--duration",
        type=float,
        help="trim to the first DURATION seconds of real round time "
        "(chronological preview; also skips CW-decoding past the "
        "cutoff, so a short preview is much faster to build)",
    )
    ap.add_argument(
        "--no-video",
        action="store_true",
        help="stop after writing the chapters and .srt -- they are ready in "
        "about a second, and checking them beats waiting out a whole render "
        "to find the timeline wrong",
    )
    ap.add_argument(
        "--cast",
        help="asciinema cast (v2) recording of the logger/irssi terminal, "
        "shown as a large picture-in-picture -- synced from "
        "the cast header's own Unix-epoch timestamp, exact real-world "
        "UTC with no whole-hour rounding needed",
    )
    ap.add_argument(
        "--scope",
        help="icom_net.py *-scope recording (uv run icom_net.py <ip> --scope "
        "FILE) -- replaces the audio-derived showspectrum background with "
        "the radio's own real spectrum-scope sweeps wherever the recording "
        "covers, falling back to the audio waterfall elsewhere. Synced from "
        "each sweep's own Unix-epoch timestamp, exact like --cast",
    )
    ap.add_argument(
        "--webcam",
        action="append",
        metavar="FILE",
        help="picture-in-picture selfie/webcam clip, synced automatically "
        "from its own filename timestamp (e.g. VID_20260706_180003.mp4), "
        "then refined via audio cross-correlation against the operator's "
        "own TX audio (see --webcam-offset to override). Repeat the flag for "
        "a round recorded in several Alt+V captures: each is placed by its "
        "own timestamp and they share the one face recess, in time order",
    )
    ap.add_argument(
        "--webcam-offset",
        type=float,
        help="manual fine-tune correction (seconds, may be negative) added to "
        "the coarse whole-hour webcam sync -- bypasses the automatic audio "
        "cross-correlation entirely; use this if it finds no confident "
        "match (e.g. the webcam clip has no audio track), or to override it",
    )
    ap.add_argument(
        "--input-log",
        help="puskas_logger *-input.jsonl for exact QSO-panel/chapter/caption "
        "timing (its 'qso' events) instead of the EDI's minute-precision "
        "clock -- optional, older recordings won't have one",
    )
    ap.add_argument(
        "--hud-demo",
        metavar="OUT.png",
        help="write a single HUD bar filled with dummy values and exit -- no "
        "recording needed, for checking the layout against the artwork",
    )
    ap.add_argument(
        "--hud-preview",
        metavar="OUT.png",
        help="write a single HUD bar built from this recording's real data at "
        "--hud-preview-t and exit without rendering video (pair with "
        "--duration to keep the CW decode short)",
    )
    ap.add_argument(
        "--hud-preview-t",
        type=float,
        default=0.0,
        help="video-time position (seconds) sampled by --hud-preview",
    )
    ap.add_argument(
        "--hud-theme",
        metavar="DIR",
        default=HUD_THEME_DIR,
        help="HUD theme directory (artwork.png + theme.json), default the "
        "hud-theme/ next to this script",
    )
    ap.add_argument(
        "--hud-theme-check",
        nargs="?",
        const="",
        metavar="OUT.png",
        help="draw every rect in the theme's theme.json back onto its artwork "
        "and exit -- the way to check a hand-edited theme. With no path it "
        "writes <theme dir>/theme-check.png and opens it in the default image "
        "viewer; give a path to only write the file",
    )
    args = ap.parse_args()

    if args.hud_theme_check is not None:
        overlay = hud_theme_overlay(load_hud_theme(args.hud_theme))
        # Always write the file -- it is the scriptable, diffable artifact --
        # and additionally open a viewer when no path was asked for, since the
        # editing loop this exists for is GIMP on one side and a look on the
        # other, not a file to go hunting for.
        out = args.hud_theme_check or os.path.join(args.hud_theme, "theme-check.png")
        overlay.save(out)
        print(f"wrote {out} from {args.hud_theme}/theme.json")
        if not args.hud_theme_check:
            overlay.show()
        return

    if args.hud_demo:
        art = hud_art(load_hud_theme(args.hud_theme))
        draw_hud_frame(hud_demo_state(), art).save(args.hud_demo)
        print(f"wrote {args.hud_demo} (dummy values)")
        return

    # Neither positional given means "find them here". All or nothing: there is
    # no half-discovered state to reason about, and a scripted invocation that
    # names its recording keeps naming every other input too. A flag given
    # explicitly still wins over what was found.
    if not args.recdir and not args.edi:
        try:
            found = wiring.discover_round_inputs(Path())
        except ValueError as exc:
            ap.error(str(exc))
        args.recdir = found.recdir
        args.edi = found.edi
        args.telemetry = args.telemetry or found.telemetry
        args.input_log = args.input_log or found.input_log
        args.cast = args.cast or found.cast
        args.scope = args.scope or found.scope
        args.webcam = args.webcam or found.webcams
        print("found in this round directory:")
        for label, value in (
            ("recording", args.recdir),
            ("edi", ", ".join(args.edi)),
            ("telemetry", args.telemetry),
            ("input-log", args.input_log),
            ("cast", args.cast),
            ("scope", args.scope),
            ("webcam", ", ".join(args.webcam or [])),
        ):
            print(f"  {label:<10} {value or '--'}")

    if not args.recdir or not args.edi:
        ap.error("recdir and at least one EDI file are required")

    # Only the render is guarded: --hud-demo and --hud-theme-check write one
    # PNG and exit, and iterating the HUD's layout from the project root is
    # exactly how they are meant to be used.
    wiring.require_round_directory()

    W, H = RESOLUTIONS[args.res]
    segs = scan_segments(args.recdir)
    if not segs:
        sys.exit(f"no timestamped WAVs found in {args.recdir}")
    compensate_split_excess(segs)
    print(f"{len(segs)} segments, {segs[-1].audio_t + _eff(segs[-1]):.0f}s audio")
    print(
        f"  split excess: {len(segs) - 1} boundaries x {SPLIT_EXCESS_S * 1000:.2f} ms "
        f"= {(len(segs) - 1) * SPLIT_EXCESS_S:.2f}s removed"
    )

    my_callsign, mywwl, qsos_all = merge_edi(args.edi)
    offset_h = derive_utc_offset(segs, qsos_all)
    print(f"{my_callsign} {mywwl}: {len(qsos_all)} QSOs, UTC+{offset_h} local")

    # Before anything reads a segment's wall time -- the cast and scope sync
    # just below, and every source aligned after them -- and after offset_h,
    # which is derived to the hour and cannot care about a sub-second shift.
    telemetry = load_telemetry(args.telemetry) if args.telemetry else []
    moved = apply_clock_offset(segs, telemetry, offset_h)
    if moved:
        shifts = sorted(t.clock_offset_s for t in telemetry if t.clock_offset_s)
        print(
            f"  clock: radio led the laptop by {shifts[0]:+.2f}..{shifts[-1]:+.2f}s "
            f"over {len(shifts)} measurements -- {moved} segments moved onto the "
            f"laptop's clock"
        )
    elif args.telemetry:
        print("  clock: no radio/laptop offset measured this round -- uncorrected")

    # Read once, up front: the cast sync below pins its own start against these
    # keystrokes, and qso_windows' exact QSO timing reads the same file later.
    input_log = load_input_log(args.input_log) if args.input_log else []

    cast_start = None
    cast_rate = 0.0
    if args.cast:
        cast_wall, cast_cols, cast_rows = parse_cast_header(args.cast)
        fraction = cast_start_fraction(args.cast, cast_wall, input_log)
        if fraction is not None:
            cast_wall += timedelta(seconds=fraction)
        cast_start = stream_start(cast_wall + timedelta(hours=offset_h), segs)
        print(
            f"  cast: {cast_cols}x{cast_rows} terminal, synced to start at "
            f"{cast_start:.2f}s in the output (Unix-epoch timestamp; "
            f"see below for a clock-drift correction shared with --webcam, if given)"
        )
        if fraction is not None:
            print(
                f"    +{fraction:.2f}s of it from the input log's own keystrokes -- "
                f"the header's timestamp is truncated to the whole second"
            )
        else:
            print(
                "    no input log to pin the header's dropped sub-second on -- "
                "the PiP can be up to 1s early"
            )

    scope_records: list[tuple[float, int, int, bytes]] = []
    scope_start = None
    scope_end = None
    if args.scope:
        scope_records = read_scope_records(args.scope)
        if len(scope_records) < 2:
            print(f"  scope: {args.scope} has fewer than 2 sweeps -- ignoring")
            scope_records = []
        else:
            first_wall = datetime.fromtimestamp(
                scope_records[0][0], tz=timezone.utc
            ).replace(tzinfo=None) + timedelta(hours=offset_h)
            last_wall = datetime.fromtimestamp(
                scope_records[-1][0], tz=timezone.utc
            ).replace(tzinfo=None) + timedelta(hours=offset_h)
            scope_start = stream_start(first_wall, segs)
            scope_end = audio_time_for(last_wall, segs)
            print(
                f"  scope: {len(scope_records)} sweeps, synced to "
                f"{scope_start:.0f}-{scope_end:.0f}s in the output "
                f"(exact -- Unix-epoch timestamps, same as --cast)"
            )

    # read_wav_metadata runs before --duration trims segs (unlike the CW
    # decode loop further down, which *should* skip past the cutoff) so the
    # webcam fine-tune below can search for TX anchors across the *full*
    # round, same reasoning as sync_webcam_start using qsos_all above --
    # a short preview otherwise has too few candidates to find a confident
    # match.
    read_wav_metadata(segs)
    known_wav = sum(1 for s in segs if s.ptt is not None)
    print(f"  WAV metadata: {known_wav}/{len(segs)} segments have IC-9700 rig tags")

    webcams: list[WebcamClip] = []
    if args.webcam:
        webcams, drift = sync_webcams(
            args.webcam,
            segs,
            qsos_all,
            offset_h,
            input_log=args.input_log,
            manual_offset=args.webcam_offset,
        )
        # The webcam capture and the cast recording (asciinema, also on this
        # machine) are timestamped by the *same* laptop system clock -- so the
        # intercept/rate correction measured against the webcam's own audio
        # (the only stream with anything to cross-correlate against the radio's
        # WAV audio) applies to the cast PiP too. Confirmed needed from a real
        # report: the operator saw the logger's own on-screen mode change
        # happen visibly before the audio caught up with it, late in the same
        # round this webcam drift was found in -- consistent with one shared
        # laptop-clock drift, not two unrelated bugs.
        if drift and args.cast and cast_start is not None:
            intercept, rate = drift
            cast_start += intercept
            cast_rate = rate
            print(
                f"  cast: applying the same clock-drift correction "
                f"({intercept:+.2f}s, {rate * 3600:+.3f}s/hour) -> "
                f"starts at {cast_start:.2f}s"
            )

    if args.duration:
        segs = trim_to_duration(segs, args.duration)
        print(
            f"  duration: preview cut to first {args.duration:.0f}s "
            f"({len(segs)} segments)"
        )

    state_events = build_state_events(segs, telemetry, offset_h)
    known = sum(1 for _, _, st in state_events if st.ptt is not None)
    suffix = (
        f" ({args.telemetry} refines freq/mode between the WAVs' own tags)"
        if args.telemetry
        else ""
    )
    print(f"  RX/TX: {known} state changes{suffix}")

    cw_raw = decode_round(segs, state_events, args.pitch)
    decoded = sum(len(s.events) for s in segs) + sum(len(ev) for _, _, _, ev in cw_raw)
    trusted_overs = sum(1 for s in segs if s.events) + len(cw_raw)
    print(f"  {decoded} characters from {trusted_overs} trusted overs")
    recovered = sum(1 for s, _, _, _ in cw_raw if s.dur > MAX_OVER_S)
    if recovered:
        print(
            f"  including {recovered} CW exchange(s) recovered from "
            f"otherwise-too-long listening segments"
        )

    if args.skip_gaps:
        cw_span_segs = {id(s) for s, _, _, _ in cw_raw}
        remap_audio_t(segs, cw_span_segs)
        total = segs[-1].audio_t + _eff(segs[-1])
        print(
            f"  skip-gaps: {total:.0f}s video (was {segs[-1].audio_t + segs[-1].dur:.0f}s)"
        )

    total = segs[-1].audio_t + _eff(segs[-1])
    qsos = [
        q
        for q in qsos_all
        if audio_time_for(q.dt + timedelta(hours=offset_h), segs) < total
    ]
    if len(qsos) < len(qsos_all):
        print(f"  {len(qsos)}/{len(qsos_all)} QSOs fall within the {total:.0f}s cut")

    dropped = [c for c in webcams if c.start >= total]
    if dropped:
        print(f"  {len(dropped)} webcam clip(s) start after the cut ends -- dropped")
        webcams = [c for c in webcams if c.start < total]

    # After the cut has dropped what it drops, so a preview never scans a clip
    # it will not show, and before anything is rendered, so the operator
    # watching the first minutes sees where the PiP will be framed.
    for i, clip in enumerate(webcams):
        tag = f"webcam {i + 1}/{len(webcams)}" if len(webcams) > 1 else "webcam"
        scan = scan_faces(clip.path)
        if scan is None:
            print(f"  {tag}: face framing unavailable (no opencv) -- centred crop")
        elif scan.cx is None:
            print(f"  {tag}: no face found in {scan.samples} samples -- centred crop")
        else:
            print(
                f"  {tag}: face framing centred on x={scan.cx:.0f} of "
                f"{scan.source[0]} ({scan.hits}/{scan.samples} samples)"
            )
        webcams[i] = clip._replace(face=scan)

    if cast_start is not None and cast_start >= total:
        print("  cast starts after the cut ends -- dropping the PiP overlay")
        cast_start = None

    if scope_start is not None and scope_start >= total:
        print("  scope starts after the cut ends -- dropping the background")
        scope_records, scope_start, scope_end = [], None, None
    elif scope_end is not None:
        scope_end = min(scope_end, total)

    # Resolved to absolute video-timeline time only now, using each
    # segment's final audio_t (post-remap, if --skip-gaps was used).
    cw_spans = [
        (seg.audio_t + t0, seg.audio_t + t1, events) for seg, t0, t1, events in cw_raw
    ]

    # Feeds qso_windows()'s exact chapter/caption timing -- the typewriter
    # overlay this also used to drive is gone, since the cast PIP already
    # shows exactly what was typed, live.
    qso_times = None
    if input_log:
        qso_times = match_qso_times(qsos, input_log)
        matched = sum(1 for t in qso_times if t is not None)
        print(
            f"  {matched}/{len(qsos)} QSOs got an exact submit time from the input log"
        )

    stem = os.path.splitext(args.out)[0]
    windows = qso_windows(qsos, segs, offset_h, total, qso_times)

    if args.hud_preview:
        timeline = build_hud_timeline(
            segs,
            qsos,
            windows,
            mywwl,
            offset_h,
            state_events=state_events,
            scope_records=scope_records,
            cw_spans=cw_spans,
            telemetry=telemetry,
        )
        state = timeline.at(args.hud_preview_t)
        art = hud_art(load_hud_theme(args.hud_theme))
        draw_hud_frame(state, art).save(args.hud_preview)
        print(f"wrote {args.hud_preview} at t={args.hud_preview_t:.1f}s: {state}")
        return

    with open(stem + ".chapters.txt", "w") as fh:
        fh.write(build_chapters(qsos, windows))
    with open(stem + ".srt", "w") as fh:
        fh.write(build_srt(qsos, windows))
    print(f"wrote {stem}.chapters.txt and {stem}.srt")

    if args.no_video:
        return

    wav = os.path.splitext(args.out)[0] + ".concat.wav"
    print("concatenating audio ...")
    concat_audio(segs, wav)

    cast_video = None
    if args.cast and cast_start is not None:
        cast_video = stem + ".cast.mp4"
        # How much of the cast's own timeline the cut can ever display.
        # render() positions it with -itsoffset cast_start and stretches it by
        # cast_rate, so clip time tau shows at cast_start + tau/(1-cast_rate);
        # invert that at tau = total. The margin keeps tpad's frame-cloning
        # from being visible at the very end of a preview.
        cast_span = (total - cast_start) * (1 - cast_rate) + STREAM_TRIM_MARGIN_S
        render_cast_video(args.cast, cast_video, max_duration=cast_span)

    hud_video = stem + ".hud.mp4"
    hud_art_ = hud_art(load_hud_theme(args.hud_theme), W, hud_height(H))
    drawn = render_hud_video(
        build_hud_timeline(
            segs,
            qsos,
            windows,
            mywwl,
            offset_h,
            state_events=state_events,
            scope_records=scope_records,
            cw_spans=cw_spans,
            telemetry=telemetry,
        ),
        hud_video,
        hud_art_,
        total,
    )
    frames = max(1, int(total * RENDER_FPS))
    print(f"  {drawn} frames drawn for {frames} ({frames / max(1, drawn):.0f}x reuse)")

    scope_video = None
    if scope_records and scope_start is not None:
        scope_video = stem + ".scope.mp4"
        # The overlay is gated to scope_end, so anything past it is invisible.
        scope_span = min(total, scope_end or total) - scope_start
        render_scope_video(
            args.scope,
            scope_video,
            W,
            H,
            max_duration=scope_span + STREAM_TRIM_MARGIN_S,
        )

    print("rendering (this takes a while) ...")
    render(
        wav,
        args.out,
        W,
        H,
        webcams=webcams,
        cast=cast_video,
        cast_start=cast_start or 0.0,
        cast_rate=cast_rate,
        scope=scope_video,
        scope_start=scope_start or 0.0,
        scope_end=scope_end or 0.0,
        hud=hud_video,
        hud_face=hud_art_.slots["face"],
    )

    if not args.keep_intermediates:
        os.remove(wav)
        if cast_video:
            os.remove(cast_video)
        if scope_video:
            os.remove(scope_video)
        os.remove(hud_video)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
