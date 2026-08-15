"""Tests for the webcam PiP's face framing.

The detector itself is a library call behind an optional dependency and is
never imported here; what is worth pinning is the geometry it feeds -- which
detections count, where the median of them puts the crop, and what happens at
the edges of the frame.

The realistic numbers come from tests/fixtures/august-face-scan.json, a real
2h Alt+V round scanned every 5s (see FINDINGS.md).
"""

import json
from pathlib import Path

import pytest

from urhpk.webcam_face import face_centre, face_crop

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "august-face-scan.json").read_text()
)
RECESS = (245, 250)  # the HUD artwork's face slot


def det(frame, x, w=200.0, score=0.9):
    return (frame, x, 200.0, w, 280.0, score)


class TestFaceCentre:
    def test_median_of_the_centres(self):
        # centres at 100, 200, 900 -- the median ignores the outlier a mean
        # would drag the whole round's framing towards
        d = [det(0, 0.0, w=200.0), det(1, 100.0, w=200.0), det(2, 800.0, w=200.0)]
        assert face_centre(d) == 200.0

    def test_low_score_detections_are_dropped(self):
        d = [det(0, 100.0, score=0.9), det(1, 900.0, score=0.2)]
        assert face_centre(d) == 200.0

    def test_largest_face_wins_within_a_frame(self):
        # someone walks through the background: two faces in one frame
        d = [det(0, 100.0, w=200.0), det(0, 900.0, w=50.0)]
        assert face_centre(d) == 200.0

    def test_no_usable_detections_is_none(self):
        assert face_centre([]) is None
        assert face_centre([det(0, 100.0, score=0.1)]) is None

    def test_real_round_median(self):
        assert face_centre(FIXTURE["detections"]) == pytest.approx(782, abs=1)


class TestFaceCrop:
    def test_centred_when_no_face_was_found(self):
        assert face_crop(1280, 720, *RECESS) == (287, 0, 706, 720)

    def test_recess_aspect_is_kept(self):
        x, y, w, h = face_crop(1280, 720, *RECESS, face_cx=640.0)
        assert w / h == pytest.approx(245 / 250, abs=0.002)

    def test_crop_is_centred_on_the_face(self):
        x, y, w, h = face_crop(1280, 720, *RECESS, face_cx=782.0)
        assert x + w / 2 == pytest.approx(782, abs=1)
        assert (x, y, w, h) == (429, 0, 706, 720)

    def test_only_x_moves(self):
        # the head already fills ~64% of the frame height, so there is nothing
        # for a zoom to gain: height and width never depend on the face
        for cx in (0.0, 300.0, 782.0, 1279.0):
            _, y, w, h = face_crop(1280, 720, *RECESS, face_cx=cx)
            assert (y, w, h) == (0, 706, 720)

    def test_clamped_to_the_left_edge(self):
        assert face_crop(1280, 720, *RECESS, face_cx=10.0)[0] == 0

    def test_clamped_to_the_right_edge(self):
        assert face_crop(1280, 720, *RECESS, face_cx=1270.0)[0] == 1280 - 706

    def test_phone_sized_source(self):
        x, y, w, h = face_crop(1920, 1080, *RECESS, face_cx=960.0)
        assert (x, y, w, h) == (431, 0, 1058, 1080)


class TestAgainstTheRealRound:
    """The measurements the ticket was decided on, as a regression."""

    def offsets(self, crop):
        crop_x, _, crop_w, _ = crop
        k = RECESS[0] / crop_w  # source px -> recess px
        centre = crop_x + crop_w / 2
        return sorted(abs(d[1] + d[3] / 2 - centre) * k for d in FIXTURE["detections"])

    def p(self, v, q):
        return v[int(len(v) * q)]

    def test_framing_beats_the_centre_crop(self):
        cx = face_centre(FIXTURE["detections"])
        framed = self.offsets(face_crop(1280, 720, *RECESS, face_cx=cx))
        centred = self.offsets(face_crop(1280, 720, *RECESS))
        assert self.p(framed, 0.5) < 25  # measured 18.7 recess px
        assert self.p(framed, 0.95) < 65  # measured 55.5
        assert self.p(centred, 0.5) > 45  # what it is today: 49.1

    def test_face_never_leaves_the_crop(self):
        cx = face_centre(FIXTURE["detections"])
        x, _, w, _ = face_crop(1280, 720, *RECESS, face_cx=cx)
        assert all(x <= d[1] + d[3] / 2 <= x + w for d in FIXTURE["detections"])

    def test_a_well_framed_clip_is_left_alone(self):
        # the July round was shot on a phone the operator could watch, so its
        # framing was already right; the algorithm must not damage that
        centred = [det(i, 640.0 - 100.0) for i in range(100)]
        assert face_crop(1280, 720, *RECESS, face_cx=face_centre(centred)) == face_crop(
            1280, 720, *RECESS
        )
