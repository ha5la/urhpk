"""Drive the real logger through a whole round in a pty.

Opt-in (`-m smoke`), because it costs ~20s and starts a real process. The
default run and pre-commit skip it.

It exists because the logger's UI is verified by running rounds, not by unit
tests, and `main()` is never called by anything else in the suite. That gap is
not theoretical: it hid an `UnboundLocalError` in `main()` through 584 green
tests, and it is the only check that covers the startup wizard, the
prompt_toolkit application actually starting, and the files a round leaves
behind.

No radio and no rotator are present, so this is the offline path — which is
also the path a real round falls back to when the radio is off.
"""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wiring import RIG_SERVER_PORT

pytestmark = pytest.mark.smoke

LOGGER = str(Path(__file__).resolve().parent.parent / "puskas_logger.py")

# Answered in the order the logger asks. The band/mode wizard only appears
# because no radio answered; with one connected the logger takes both from it.
PROMPTS = [
    (b"Your locator", b"\r"),
    (b"Contest name", b"\r"),
    (b"[Enter to start]", b"\r"),
    (b"Band [", b"2M\r"),
    (b"Mode [", b"CW\r"),
]


class Pty:
    """A pty the logger runs on, so prompt_toolkit renders for real."""

    def __init__(self, cwd: Path):
        self.master, slave = pty.openpty()
        os.set_blocking(self.master, False)
        self.proc = subprocess.Popen(
            ["uv", "run", LOGGER],
            cwd=cwd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={
                **os.environ,
                "TERM": "xterm-256color",
                "COLUMNS": "120",
                "LINES": "40",
            },
        )
        os.close(slave)
        self.buf = b""

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([self.master], [], [], 0.2)[0]:
                try:
                    self.buf += os.read(self.master, 65536)
                except OSError:
                    return

    def wait_for(self, marker: bytes, timeout: float = 40.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if marker in self.buf:
                return
            self.pump(0.3)
        raise AssertionError(
            f"never saw {marker!r}. Screen so far:\n{self.text[-2000:]}"
        )

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    @property
    def text(self) -> str:
        return self.buf.decode("utf-8", "replace")

    def close_terminal(self) -> None:
        """Make the terminal vanish, as `tmux kill-session` does."""
        if self.master is not None:
            os.close(self.master)
            self.master = None

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.close_terminal()


@pytest.fixture
def logger(tmp_path):
    p = Pty(tmp_path)
    try:
        yield p
    finally:
        p.close()


def test_a_whole_round_offline(logger, tmp_path):
    for marker, reply in PROMPTS:
        logger.wait_for(marker)
        logger.send(reply)

    logger.wait_for(b"PUSK")  # the toolbar: the application is up
    logger.send(b"HA7NS 599 001 JN97WM\r")
    logger.wait_for(b"2M:1q")  # the header scored it
    logger.send(b"\x04")  # Ctrl-D: save and exit
    logger.wait_for(b"Saving EDI")
    logger.pump(3.0)

    assert logger.proc.wait(timeout=10) == 0, logger.text[-2000:]

    # The right-hand prompt resolved the typed locator live, which exercises
    # the locator cache and geo together.
    assert "JN97WM" in logger.text and "km" in logger.text

    edi = list(tmp_path.glob("*-2M.edi"))
    assert len(edi) == 1, sorted(p.name for p in tmp_path.iterdir())
    assert "HA7NS" in edi[0].read_text()

    events = [
        json.loads(line)
        for line in (next(tmp_path.glob("*-input.jsonl"))).read_text().splitlines()
    ]
    kinds = {e["event"] for e in events}
    assert "text" in kinds, "keystrokes were not recorded"
    assert "qso" in kinds, "the logged QSO was not recorded"


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def test_the_rig_server_answers_while_the_logger_runs(logger):
    """The bridge asks the running logger for the radio's frequency.

    Unit tests cover the dialect; this covers the part they cannot -- that the
    server is actually being served, by the same event loop that is drawing the
    UI. With no radio attached the honest answer is the error reply.
    """
    if not _port_free(RIG_SERVER_PORT):
        pytest.skip(f"port {RIG_SERVER_PORT} already in use on this machine")

    for marker, reply in PROMPTS:
        logger.wait_for(marker)
        logger.send(reply)
    logger.wait_for(b"PUSK")

    with socket.create_connection(("127.0.0.1", RIG_SERVER_PORT), timeout=5) as s:
        s.sendall(b"f\n")
        assert s.makefile("rb").readline() == b"RPRT -1\n"


def test_sigterm_finishes_instead_of_hanging(logger, tmp_path):
    """SIGTERM is how a round ends -- the entrypoint kills the tmux session.

    A signal handler runs on the main thread wherever it is, and the teardown
    it runs used to take a lock the UI holds, so the process hung and could not
    even be killed by a second SIGTERM. Here the UI is idle rather than
    mid-critical-section, so this does not reproduce that race -- it pins the
    weaker property that the handler completes and the process exits.
    """
    for marker, reply in PROMPTS:
        logger.wait_for(marker)
        logger.send(reply)
    logger.wait_for(b"PUSK")

    logger.proc.send_signal(signal.SIGTERM)
    assert logger.proc.wait(timeout=10) == 128 + signal.SIGTERM


def test_sighup_with_the_terminal_already_gone_still_runs_teardown(logger):
    """How a round actually ends: `tmux kill-session` closes the pty *and*
    sends SIGHUP, so the teardown has to run on a UI whose terminal is already
    dead. 128+SIGHUP as the exit code is the proof it ran — the default
    disposition would report a signalled death instead.
    """
    for marker, reply in PROMPTS:
        logger.wait_for(marker)
        logger.send(reply)
    logger.wait_for(b"PUSK")

    logger.close_terminal()
    logger.proc.send_signal(signal.SIGHUP)
    assert logger.proc.wait(timeout=10) == 128 + signal.SIGHUP


if __name__ == "__main__":  # a quick manual run without pytest
    sys.exit(pytest.main([__file__, "-m", "smoke", "-q", "--no-cov"]))
