"""Tests for the shared contracts, the round-directory guard, and discovery.

The guard runs at startup, before the radio and the recorders come up. A false
positive costs a round, which is worse than the pile of files it
prevents — so the negative cases matter more here than the positive one.

Discovery is the same shape of risk from the other end: picking the wrong file
is not found out until a render hours later, so what it refuses to do matters
more than what it finds.
"""

from pathlib import Path

import pytest

from urhpk import wiring


class TestContractsAgree:
    """Both ends of each contract read the same value from one place."""

    def test_databases_are_under_puskas_dir(self):
        assert wiring.SEEN_STATIONS.parent == wiring.PUSKAS_DIR
        assert wiring.ON4KST_SEEN.parent == wiring.PUSKAS_DIR

    def test_rig_server_and_rotctld_are_different_ports(self):
        assert wiring.RIG_SERVER_PORT != wiring.ROTCTLD_PORT

    def test_project_root_holds_pyproject(self):
        assert (wiring.PROJECT_ROOT / "pyproject.toml").is_file()


@pytest.fixture
def round_dir(tmp_path):
    (tmp_path / "recording").mkdir()
    (tmp_path / "260811-HA5LA-2M.edi").touch()
    return tmp_path


class TestRoundInputDiscovery:
    def test_finds_the_recording_directory_and_the_log(self, round_dir):
        found = wiring.discover_round_inputs(round_dir)
        assert found.recdir == str(round_dir / "recording")
        assert found.edi == [str(round_dir / "260811-HA5LA-2M.edi")]

    def test_finds_the_optional_side_channels(self, round_dir):
        for name in (
            "260811-HA5LA-telemetry.jsonl",
            "260811-HA5LA-input.jsonl",
            "2026-08-11T19:16:06+00:00.cast",
            "260811-HA5LA.scope",
        ):
            (round_dir / name).touch()
        found = wiring.discover_round_inputs(round_dir)
        assert found.telemetry == str(round_dir / "260811-HA5LA-telemetry.jsonl")
        assert found.input_log == str(round_dir / "260811-HA5LA-input.jsonl")
        assert found.cast == str(round_dir / "2026-08-11T19:16:06+00:00.cast")
        assert found.scope == str(round_dir / "260811-HA5LA.scope")

    def test_leaves_absent_side_channels_unset(self, round_dir):
        found = wiring.discover_round_inputs(round_dir)
        assert (found.telemetry, found.input_log, found.cast, found.scope) == (
            None,
            None,
            None,
            None,
        )

    def test_takes_every_webcam_clip_in_timestamp_order(self, round_dir):
        for stamp in ("20260811T191746.519807Z", "20260811T191622.497368Z"):
            (round_dir / f"260811-HA5LA-webcam-{stamp}.mp4").touch()
        found = wiring.discover_round_inputs(round_dir)
        assert [Path(p).name for p in found.webcams] == [
            "260811-HA5LA-webcam-20260811T191622.497368Z.mp4",
            "260811-HA5LA-webcam-20260811T191746.519807Z.mp4",
        ]

    def test_never_mistakes_a_rendered_video_for_a_webcam_clip(self, round_dir):
        for name in ("out.mp4", "contest_video.hud.mp4", "260811-HA5LA-twocam.mp4"):
            (round_dir / name).touch()
        assert wiring.discover_round_inputs(round_dir).webcams == []

    def test_refuses_to_choose_between_two_casts_and_names_both(self, round_dir):
        (round_dir / "first.cast").touch()
        (round_dir / "second.cast").touch()
        with pytest.raises(ValueError) as exc:
            wiring.discover_round_inputs(round_dir)
        assert "first.cast" in str(exc.value)
        assert "second.cast" in str(exc.value)

    def test_refuses_a_directory_with_no_recording_in_it(self, tmp_path):
        (tmp_path / "260811-HA5LA-2M.edi").touch()
        with pytest.raises(ValueError, match="recording"):
            wiring.discover_round_inputs(tmp_path)

    def test_refuses_a_directory_with_no_edi_log(self, tmp_path):
        (tmp_path / "recording").mkdir()
        with pytest.raises(ValueError, match="edi"):
            wiring.discover_round_inputs(tmp_path)


class TestRoundDirectoryGuard:
    def test_project_root_is_refused(self, tmp_path):
        assert wiring.round_directory_error(tmp_path, tmp_path) is not None

    def test_message_says_what_to_do(self, tmp_path):
        message = wiring.round_directory_error(tmp_path, tmp_path)
        assert str(tmp_path) in message
        assert "mkdir" in message

    def test_subdirectory_is_allowed(self, tmp_path):
        round_dir = tmp_path / "26szeptember"
        round_dir.mkdir()
        assert wiring.round_directory_error(round_dir, tmp_path) is None

    def test_nested_subdirectory_is_allowed(self, tmp_path):
        nested = tmp_path / "26szeptember" / "recording"
        nested.mkdir(parents=True)
        assert wiring.round_directory_error(nested, tmp_path) is None

    def test_directory_outside_the_project_is_allowed(self, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        root = tmp_path / "project"
        root.mkdir()
        assert wiring.round_directory_error(outside, root) is None

    def test_parent_of_the_project_is_allowed(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        assert wiring.round_directory_error(tmp_path, root) is None

    def test_symlinked_root_is_still_the_root(self, tmp_path):
        """Both sides resolve, so reaching the root by another name is caught."""
        root = tmp_path / "project"
        root.mkdir()
        link = tmp_path / "link-to-project"
        link.symlink_to(root, target_is_directory=True)
        assert wiring.round_directory_error(link, root) is not None

    def test_symlinked_round_directory_is_allowed(self, tmp_path):
        root = tmp_path / "project"
        (root / "26szeptember").mkdir(parents=True)
        link = tmp_path / "link-to-round"
        link.symlink_to(root / "26szeptember", target_is_directory=True)
        assert wiring.round_directory_error(link, root) is None

    def test_require_exits_in_the_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wiring, "PROJECT_ROOT", tmp_path)
        with pytest.raises(SystemExit) as exc:
            wiring.require_round_directory(tmp_path)
        assert exc.value.code == 2

    def test_require_is_silent_in_a_round_directory(self, tmp_path, monkeypatch):
        round_dir = tmp_path / "26szeptember"
        round_dir.mkdir()
        monkeypatch.setattr(wiring, "PROJECT_ROOT", tmp_path)
        wiring.require_round_directory(round_dir)  # must not raise
