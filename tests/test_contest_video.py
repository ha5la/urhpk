"""Tests for the render itself: how the ffmpeg filter branches combine.

The branch-combining bugs this guards against are invisible to the component
tests -- each branch can be right while the graph that joins them is wrong."""

import json
import re

import contest_video as cv
from urhpk import cast_render, hud_draw, video_format
from urhpk.webcam_sync import WebcamClip

WEBCAM = WebcamClip("w.mp4", 0.0)


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
        for label in ("scopebg", "hudbar", "castpip", "pip0"):
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
            cast="c.mp4", webcams=[WEBCAM], hud="h.mp4",
        ) == {
            "scopebg": "s.mp4",
            "hudbar": "h.mp4",
            "castpip": "c.mp4",
            "pip0": "w.mp4",
        }  # fmt: skip

    def test_indices_stay_correct_with_only_some_streams(self):
        assert self._sources(cast="c.mp4", hud="h.mp4") == {
            "castpip": "c.mp4",
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
        graph = self._graph(cast="c.mp4", hud="h.mp4")
        assert graph.index("[castpip]overlay") < graph.index("[hudbar]overlay")

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

    def test_the_cast_is_sized_to_the_room_above_the_bar(self):
        # Height-constrained with a HUD: a width fraction picked before the
        # bar existed overran the space and had to be clipped by the bar.
        H = 1080
        expect = H - hud_draw.hud_height(H) - 2 * round(H * cv.CAST_PIP_MARGIN_FRAC)
        assert f"scale=-2:{expect}" in self._graph(cast="c.mp4", hud="h.mp4")
