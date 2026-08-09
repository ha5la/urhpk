"""Tests for the shared contracts, and for the round-directory guard.

The guard runs at startup, before the radio and the recorders come up. A false
positive costs a contest round, which is worse than the pile of files it
prevents — so the negative cases matter more here than the positive one.
"""

import pytest

import wiring


class TestContractsAgree:
    """Both ends of each contract read the same value from one place."""

    def test_databases_are_under_puskas_dir(self):
        assert wiring.SEEN_STATIONS.parent == wiring.PUSKAS_DIR
        assert wiring.ON4KST_SEEN.parent == wiring.PUSKAS_DIR

    def test_rig_server_and_rotctld_are_different_ports(self):
        assert wiring.RIG_SERVER_PORT != wiring.ROTCTLD_PORT

    def test_project_root_holds_pyproject(self):
        assert (wiring.PROJECT_ROOT / "pyproject.toml").is_file()


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
