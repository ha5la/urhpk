"""Tests for the render itself: how the ffmpeg filter branches combine.

The branch-combining bugs this guards against are invisible to the component
tests -- each branch can be right while the graph that joins them is wrong."""

import json
import re
import wave
from datetime import datetime, timedelta

import numpy as np

import contest_video as cv
from urhpk import cast_render, hud_draw, video_format
from urhpk.cast_render import CastClip
from urhpk.timeline import SPLIT_EXCESS_S, Segment, _eff
from urhpk.webcam_face import FaceScan
from urhpk.webcam_sync import WebcamClip

WEBCAM = WebcamClip("w.mp4", 0.0)
CAST = CastClip("c.mp4", 0.0)
SR = 16000  # the recorder's sample rate


def _render_cmd(**kw):
    """render()'s ffmpeg command, without running it. Every render has a HUD,
    so the bar and its face recess are defaulted rather than repeated."""
    kw.setdefault("hud", "h.mp4")
    kw.setdefault("hud_face", (940, 20, 240, 130))
    captured = {}

    def fake_run(cmd, **_):
        captured["cmd"] = cmd
        return None

    real = cv.subprocess.run
    cv.subprocess.run = fake_run
    try:
        cv.render("a.wav", "o.mp4", 1920, 1080, **kw)
    finally:
        cv.subprocess.run = real
    return captured["cmd"]


class TestHudRender:
    def test_frame_key_ignores_time_but_tracks_everything_drawn(self):
        a = hud_draw.hud_demo_state()
        b = hud_draw.hud_demo_state()
        b.t = a.t + 5.0
        assert hud_draw.hud_frame_key(a) == hud_draw.hud_frame_key(
            b
        )  # t alone changes nothing
        b.score = a.score + 1
        assert hud_draw.hud_frame_key(a) != hud_draw.hud_frame_key(b)

    def test_frame_key_quantises_the_continuously_varying_values(self):
        # The meter is 18 discrete segments and a needle rounded to a degree
        # moves under a pixel; without this the scope-derived signal level
        # would force a fresh draw ~30 times a second for no visible gain.
        a = hud_draw.hud_demo_state()
        b = hud_draw.hud_demo_state()
        b.s_level = a.s_level + 0.001
        b.rot_az = a.rot_az + 0.2
        assert hud_draw.hud_frame_key(a) == hud_draw.hud_frame_key(b)
        b.s_level = a.s_level + 0.2
        assert hud_draw.hud_frame_key(a) != hud_draw.hud_frame_key(b)

    def test_bar_height_is_even_at_every_supported_resolution(self):
        # libx264 refuses an odd dimension and 720p rounds to 173. Found by
        # rendering a real 720p clip, not by any string-level assertion --
        # the 1080p reference height is already even.
        for _, H in video_format.RESOLUTIONS.values():
            assert hud_draw.hud_height(H) % 2 == 0

    def test_render_places_the_hud_bar_along_the_bottom(self):
        cmd = _render_cmd(hud="h.mp4")
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "[hudbar]overlay=x=0:y=main_h-h" in graph

    def _panels(self, **kw):
        """(scope rect, map rect) as the filtergraph actually places them."""
        cmd = _render_cmd(hud="h.mp4", casts=[CAST], **kw)
        graph = cmd[cmd.index("-filter_complex") + 1]

        def rect(scaled, placed):
            size = re.search(scaled, graph)
            pos = re.search(placed, graph)
            if size is None or pos is None:
                return None
            return (*(int(v) for v in pos.groups()), *(int(v) for v in size.groups()))

        return (
            rect(
                r"scale=(\d+):(\d+),fps=\d+,format=yuv420p",
                r"\[scopebg\]overlay=x=(\d+):y=(\d+)",
            ),
            rect(
                r"scale=(\d+):(\d+),fps=\d+,tpad[^;]*\[mappanel\]",
                r"\[mappanel\]overlay=x=(\d+):y=(\d+)",
            ),
        )

    def test_the_map_shares_the_right_column_with_the_scope(self):
        """Waterfall above, drone shot below it, in the column the scope used
        to have to itself -- and not one pixel of overlap."""
        scope, qmap = self._panels(scope="s.mp4", qth_map="m.mp4")
        sx, sy, sw, sh = scope
        mx, my, mw, mh = qmap
        assert (mx, mw) == (sx, sw)
        assert my >= sy + sh, "the map overlaps the waterfall"
        assert my + mh <= 1080 - hud_draw.hud_height(1080)

    def test_without_a_map_the_scope_keeps_the_whole_column(self):
        scope, qmap = self._panels(scope="s.mp4")
        assert qmap is None
        assert scope[3] == 1080 - hud_draw.hud_height(1080) - 2 * scope[1]

    def test_both_panels_are_even_high_for_libx264(self):
        # Each is encoded as its own clip before render() sees it, and x264
        # refuses an odd dimension -- the same trap hud_height fell into.
        scope, qmap = self._panels(scope="s.mp4", qth_map="m.mp4")
        assert scope[3] % 2 == 0 and qmap[3] % 2 == 0

    def test_the_map_panel_is_opaque(self):
        """A panel of its own, not an inset over the waterfall -- unlike the
        cast PiP beside it, which is deliberately translucent."""
        cmd = _render_cmd(hud="h.mp4", casts=[CAST], qth_map="m.mp4")
        graph = cmd[cmd.index("-filter_complex") + 1]
        branch = graph[graph.index("[mappanel]") : graph.index("[vmap]")]
        assert "colorchannelmixer" not in branch and "yuva" not in branch

    def test_the_webcam_goes_into_the_artworks_face_recess(self):
        # Its own crop and position come from the theme, so this pins that the
        # rect render() is handed is the rect it actually uses.
        face = hud_draw.hud_art(
            hud_draw.load_hud_theme(), 1920, hud_draw.hud_height(1080)
        ).slots["face"]
        cmd = _render_cmd(hud="h.mp4", webcams=[WEBCAM], hud_face=face)
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert f"scale={face[2]}:{face[3]}" in graph
        assert f"overlay=x={face[0]}:" in graph

    def test_a_scanned_clip_is_cropped_onto_its_own_face(self):
        clip = WebcamClip("w.mp4", 0.0, face=FaceScan(782.0, (1280, 720), 1440, 1423))
        cmd = _render_cmd(webcams=[clip], hud_face=(940, 20, 245, 250))
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "crop=706:720:429:0" in graph

    def test_an_unscanned_clip_keeps_the_centred_crop(self):
        # no detector installed: the crop stays the expression it always was,
        # which needs no frame size of its own
        cmd = _render_cmd(webcams=[WEBCAM], hud_face=(940, 20, 245, 250))
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "crop=min(iw\\,ih*245/250)" in graph

    def test_each_clip_gets_its_own_crop(self):
        clips = [
            WebcamClip("a.mp4", 0.0, face=FaceScan(782.0, (1280, 720), 10, 10)),
            WebcamClip("b.mp4", 90.0, face=FaceScan(300.0, (640, 480), 10, 10)),
        ]
        cmd = _render_cmd(webcams=clips, hud_face=(940, 20, 245, 250))
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "crop=706:720:429:0" in graph
        assert "crop=470:480:65:0" in graph


class TestSideStreamTrimming:
    def _cast(self, tmp_path, duration):
        f = tmp_path / "s.cast"
        lines = [json.dumps({"version": 2, "width": 20, "height": 4, "timestamp": 0})]
        t = 0.0
        while t <= duration:
            lines.append(json.dumps([t, "o", "x"]))
            t += 1.0
        f.write_text("\n".join(lines) + "\n")
        return str(f)

    def test_cast_render_stops_at_the_cut_instead_of_replaying_it_all(
        self, tmp_path, monkeypatch
    ):
        # A --duration preview shows the first minutes of a round, so
        # replaying the whole cast is wasted work -- and it is the slowest
        # stage, so it dominates exactly the case the flag exists to speed up.
        frames = []

        class FakeProc:
            stdin = type(
                "S",
                (),
                {"write": lambda _, b: frames.append(b), "close": lambda _: None},
            )()

            def wait(self):
                return 0

        monkeypatch.setattr(cv.subprocess, "Popen", lambda *a, **k: FakeProc())
        cast = self._cast(tmp_path, 60.0)
        cast_render.render_cast_video(cast, str(tmp_path / "o.mp4"), fps=1.0)
        full = len(frames)
        frames.clear()
        cast_render.render_cast_video(
            cast, str(tmp_path / "o.mp4"), fps=1.0, max_duration=10.0
        )
        assert len(frames) < full / 4
        assert len(frames) == 11  # 0..10s inclusive at 1 fps

    def test_no_limit_still_renders_the_whole_cast(self, tmp_path, monkeypatch):
        frames = []

        class FakeProc:
            stdin = type(
                "S",
                (),
                {"write": lambda _, b: frames.append(b), "close": lambda _: None},
            )()

            def wait(self):
                return 0

        monkeypatch.setattr(cv.subprocess, "Popen", lambda *a, **k: FakeProc())
        cast_render.render_cast_video(
            self._cast(tmp_path, 30.0), str(tmp_path / "o.mp4"), fps=1.0
        )
        assert len(frames) == 31


class TestRenderInputIndices:
    def _sources(self, **kw):
        """For each branch's own output label, the file that is really the
        ffmpeg input its filter chain reads from."""
        cmd = _render_cmd(**kw)
        files = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
        graph = cmd[cmd.index("-filter_complex") + 1]
        out = {}
        for label in ("scopebg", "hudbar", "castpip0", "pip0"):
            m = re.search(r"\[(\d+):v\]((?!\[\d+:v\]).)*?\[" + label + r"\]", graph)
            if m:
                out[label] = files[int(m.group(1))]
        return out

    def test_every_branch_reads_its_own_clip(self):
        # Regression for a real bug seen in a rendered frame: indices were
        # assigned in one order and inputs appended in another, so the HUD was
        # drawn at the cast PiP's position and size, the terminal ended up in
        # the webcam's face recess and the webcam was stretched full-width
        # along the bottom. Every branch's own filter string was well-formed,
        # so only the *combination* was wrong -- which is why this asserts
        # which file each chain reads, not merely that all four are used.
        assert self._sources(
            scope="s.mp4", scope_start=1.0, scope_end=9.0,
            casts=[CAST], webcams=[WEBCAM], hud="h.mp4",
        ) == {
            "scopebg": "s.mp4",
            "hudbar": "h.mp4",
            "castpip0": "c.mp4",
            "pip0": "w.mp4",
        }  # fmt: skip

    def test_indices_stay_correct_with_only_some_streams(self):
        assert self._sources(casts=[CAST], hud="h.mp4") == {
            "castpip0": "c.mp4",
            "hudbar": "h.mp4",
        }
        assert self._sources(webcams=[WEBCAM], hud="h.mp4") == {
            "pip0": "w.mp4",
            "hudbar": "h.mp4",
        }
        assert self._sources(hud="h.mp4") == {"hudbar": "h.mp4"}


class TestHudLayering:
    def _graph(self, **kw):
        cmd = _render_cmd(**kw)
        return cmd[cmd.index("-filter_complex") + 1]

    def test_the_bar_is_composited_after_the_cast_so_nothing_covers_it(self):
        # Regression from a rendered frame: the terminal PiP was drawn over
        # the bar, putting the logger's own toolbar across SCORE and QSOS.
        graph = self._graph(casts=[CAST], hud="h.mp4")
        assert graph.index("[castpip0]overlay") < graph.index("[hudbar]overlay")

    def test_the_webcam_is_composited_after_the_bar_to_sit_in_the_recess(self):
        graph = self._graph(webcams=[WEBCAM], hud="h.mp4")
        assert graph.index("[hudbar]overlay") < graph.index("[pip0]overlay")

    def test_several_captures_share_the_recess_in_time_order(self):
        # A round Alt+V'd more than once is several clips in one recess. Each
        # is overlaid on the composite so far, so the later clip must come
        # second in the graph *and* read its own file -- and each takes the
        # recess only from its own start, leaving the one before it (frozen on
        # its last frame by tpad) to fill the gap between them.
        cmd = _render_cmd(
            webcams=[WebcamClip("a.mp4", 22.497), WebcamClip("b.mp4", 106.52)],
            hud="h.mp4",
        )
        files = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
        graph = cmd[cmd.index("-filter_complex") + 1]
        reads = {
            label: files[
                int(re.search(rf"\[(\d+):v\][^;]*\[{label}\]", graph).group(1))
            ]
            for label in ("pip0", "pip1")
        }
        assert reads == {"pip0": "a.mp4", "pip1": "b.mp4"}
        assert (
            graph.index("[hudbar]overlay")
            < graph.index("[pip0]overlay")
            < graph.index("[pip1]overlay")
        )
        assert "enable='gte(t,22.497)'" in graph
        assert "enable='gte(t,106.520)'" in graph

    def test_several_logger_sessions_share_the_pip_slot_in_time_order(self):
        # A round the logger was restarted in is several casts in one slot,
        # the same shape as the webcam recess above: each is overlaid on the
        # composite so far, reads its own file and takes the slot from its own
        # start, with the one before it frozen by tpad to fill the gap -- and
        # handing the slot over when the next one starts, so the two are never
        # drawn on top of each other (they are transparent; see
        # test_an_earlier_cast_stops_being_drawn_where_the_next_one_starts).
        cmd = _render_cmd(
            casts=[CastClip("a.mp4", 12.5), CastClip("b.mp4", 4501.25)],
            hud="h.mp4",
        )
        files = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
        graph = cmd[cmd.index("-filter_complex") + 1]
        reads = {
            label: files[
                int(re.search(rf"\[(\d+):v\][^;]*\[{label}\]", graph).group(1))
            ]
            for label in ("castpip0", "castpip1")
        }
        assert reads == {"castpip0": "a.mp4", "castpip1": "b.mp4"}
        assert graph.index("[castpip0]overlay") < graph.index("[castpip1]overlay")
        assert graph.index("[castpip1]overlay") < graph.index("[hudbar]overlay")
        assert "enable='between(t,12.500,4501.250)'" in graph
        assert "enable='gte(t,4501.250)'" in graph

    def test_the_cast_is_sized_to_the_room_above_the_bar(self):
        # Height-constrained with a HUD: a width fraction picked before the
        # bar existed overran the space and had to be clipped by the bar.
        H = 1080
        expect = H - hud_draw.hud_height(H) - 2 * round(H * cv.CAST_PIP_MARGIN_FRAC)
        assert f"scale=-2:{expect}" in self._graph(casts=[CAST], hud="h.mp4")


class TestConcatAudio:
    """The assembled WAV is the timeline everything else is drawn against, so
    its length has to be exactly what the Segments say it is -- see
    FINDINGS.md on the split excess."""

    def _seg(self, tmp_path, name, dur, eff_dur=None):
        path = str(tmp_path / name)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(b"\0\0" * round(dur * SR))
        return Segment(path, datetime(2026, 8, 3, 16, 0), dur, 0.0, eff_dur=eff_dur)

    def test_honours_eff_dur_to_the_sample(self, tmp_path):
        # 5.57 ms is far shorter than one demuxed PCM packet, which is what
        # the concat demuxer's `outpoint` can actually cut on.
        segs = [
            self._seg(tmp_path, "a.wav", 3.0, eff_dur=3.0 - SPLIT_EXCESS_S),
            self._seg(tmp_path, "b.wav", 2.0, eff_dur=2.0 - SPLIT_EXCESS_S),
            self._seg(tmp_path, "c.wav", 1.5),
        ]
        out = str(tmp_path / "out.wav")
        cv.concat_audio(segs, out)
        with wave.open(out) as w:
            assert w.getnframes() == sum(round(_eff(s) * SR) for s in segs)


class TestDualWatchAudio:
    """With dual watch on the radio plays the sub band in the right ear and
    the main band in the left; the rest of the time both ears get the main
    band. The video plays what the operator heard."""

    T0 = datetime(2026, 10, 5, 16, 14, 0)

    def _wav(self, tmp_path, name, samples, start_s, eff_dur=None):
        path = str(tmp_path / name)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(np.asarray(samples, np.int16).tobytes())
        return Segment(
            path,
            self.T0 + timedelta(seconds=start_s),
            len(samples) / SR,
            0.0,
            eff_dur=eff_dur,
        )

    def _channels(self, path):
        with wave.open(path) as w:
            assert w.getnchannels() == 2
            pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
        return pcm[0::2], pcm[1::2]

    def test_the_right_ear_hears_the_sub_band_while_it_records(self, tmp_path):
        main = [self._wav(tmp_path, "a.wav", [1000] * 3 * SR, 0.0)]
        sub = [self._wav(tmp_path, "b.wav", [-2000] * SR, 1.0)]
        out = str(tmp_path / "out.wav")
        cv.concat_audio(main, out, sub_band=sub)
        left, right = self._channels(out)
        assert (left == 1000).all()
        assert (right[:SR] == 1000).all()
        assert (right[SR : 2 * SR] == -2000).all()
        assert (right[2 * SR :] == 1000).all()

    def test_the_sub_band_stays_on_wall_clock_across_a_main_band_split(self, tmp_path):
        """The split excess is cut from the main band's segments, not from the
        sub band's one file, so each main segment picks the sub band up at its
        own wall-clock start rather than where the previous one left off."""
        first = self._wav(
            tmp_path, "a1.wav", [1000] * 2 * SR, 0.0, eff_dur=2.0 - SPLIT_EXCESS_S
        )
        second = self._wav(tmp_path, "a2.wav", [1000] * 2 * SR, 2.0)
        ramp = np.arange(3 * SR) % 30000
        sub = [self._wav(tmp_path, "b.wav", ramp, 1.0)]
        out = str(tmp_path / "out.wav")
        cv.concat_audio([first, second], out, sub_band=sub)
        _, right = self._channels(out)
        at_split = round(_eff(first) * SR)
        assert right[at_split] == ramp[SR]  # 2.0 s wall = 1.0 s into the sub band
        assert right[at_split - 1] == ramp[round((2.0 - SPLIT_EXCESS_S - 1.0) * SR) - 1]

    def test_a_round_without_dual_watch_stays_mono(self, tmp_path):
        main = [self._wav(tmp_path, "a.wav", [1000] * SR, 0.0)]
        out = str(tmp_path / "out.wav")
        cv.concat_audio(main, out)
        with wave.open(out) as w:
            assert w.getnchannels() == 1
