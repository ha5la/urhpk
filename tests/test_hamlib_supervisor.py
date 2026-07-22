import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hamlib_supervisor import (  # noqa: E402
    IN_CREATE,
    IN_DELETE,
    Daemon,
    INotify,
    reconcile_initial_state,
    route_event,
)

SLEEP_CMD = [sys.executable, "-c", "import time; time.sleep(5)"]


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ──────────────────────────────────────────────────────────────
# INotify
# ──────────────────────────────────────────────────────────────


def test_inotify_detects_file_create_and_delete(tmp_path):
    inotify = INotify()
    inotify.add_watch(tmp_path)

    target = tmp_path / "usb-radio-if00-port0"
    target.write_text("x")
    events = inotify.read_events()
    assert any(name == target.name and mask & IN_CREATE for _wd, mask, name in events)

    target.unlink()
    events = inotify.read_events()
    assert any(name == target.name and mask & IN_DELETE for _wd, mask, name in events)


# ──────────────────────────────────────────────────────────────
# Daemon lifecycle
# ──────────────────────────────────────────────────────────────


def test_daemon_start_launches_process(tmp_path):
    d = Daemon(name="test", device=tmp_path / "dev", cmd=SLEEP_CMD)
    d.start()
    try:
        assert d.proc is not None
        assert d.proc.poll() is None
    finally:
        d.stop()


def test_daemon_start_is_idempotent_while_running(tmp_path):
    d = Daemon(name="test", device=tmp_path / "dev", cmd=SLEEP_CMD)
    d.start()
    first_pid = d.proc.pid
    d.start()
    try:
        assert d.proc.pid == first_pid
    finally:
        d.stop()


def test_daemon_stop_terminates_the_process(tmp_path):
    d = Daemon(name="test", device=tmp_path / "dev", cmd=SLEEP_CMD)
    d.start()
    proc = d.proc
    d.stop()
    assert d.proc is None
    assert _wait_until(lambda: proc.poll() is not None)


def test_daemon_stop_when_never_started_is_a_noop(tmp_path):
    d = Daemon(name="test", device=tmp_path / "dev", cmd=SLEEP_CMD)
    d.stop()
    assert d.proc is None


# ──────────────────────────────────────────────────────────────
# reconcile_initial_state / route_event
# ──────────────────────────────────────────────────────────────


def test_reconcile_starts_only_daemons_whose_device_already_exists(tmp_path):
    present = tmp_path / "present"
    present.write_text("x")
    absent = tmp_path / "absent"

    d_present = Daemon(name="present", device=present, cmd=SLEEP_CMD)
    d_absent = Daemon(name="absent", device=absent, cmd=SLEEP_CMD)
    try:
        reconcile_initial_state([d_present, d_absent])
        assert d_present.proc is not None and d_present.proc.poll() is None
        assert d_absent.proc is None
    finally:
        d_present.stop()
        d_absent.stop()


def test_route_event_starts_matching_daemon_on_create(tmp_path):
    device = tmp_path / "usb-radio-if00-port0"
    d = Daemon(name="rig", device=device, cmd=SLEEP_CMD)
    try:
        route_event([d], IN_CREATE, device.name)
        assert d.proc is not None and d.proc.poll() is None
    finally:
        d.stop()


def test_route_event_stops_matching_daemon_on_delete(tmp_path):
    device = tmp_path / "usb-radio-if00-port0"
    d = Daemon(name="rig", device=device, cmd=SLEEP_CMD)
    d.start()
    route_event([d], IN_DELETE, device.name)
    assert d.proc is None


def test_route_event_ignores_events_for_other_device_names(tmp_path):
    device = tmp_path / "usb-radio-if00-port0"
    d = Daemon(name="rig", device=device, cmd=SLEEP_CMD)
    route_event([d], IN_CREATE, "some-other-device")
    assert d.proc is None
