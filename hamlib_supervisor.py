#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Hamlib daemon supervisor
=========================
Starts/stops rigctld and rotctld based on USB device presence, so a
replugged/re-enumerated radio or rotator controller is picked up
automatically instead of leaving a stale daemon holding a dead file
descriptor (see CLAUDE.md for the background).

No polling: uses inotify (via ctypes, no external dependency) on the
parent directory of each configured device path, so a plug/unplug is
noticed the instant udev creates or removes the symlink.

Each device path should be a *stable* symlink — either the distro's
own `/dev/serial/by-id/...` entries (check with `ls /dev/serial/by-id/`
before filling in RIG_DEVICE/ROT_DEVICE below) or a custom udev
SYMLINK+= rule, NOT a raw /dev/ttyUSBn path, since the kernel-assigned
number is exactly what changes across a replug.

Usage:
    uv run hamlib_supervisor.py
    (run permanently, e.g. from tmux or a systemd --user unit)
"""
from __future__ import annotations

import ctypes
import os
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ============================================================
# Configuration
# ============================================================
RIG_DEVICE  = Path(
    "/dev/serial/by-id/"
    "usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_IC-9700_13013358_A-if00-port0"
)
RIG_MODEL   = "3081"          # Icom IC-9700 (RIG_MODEL_IC9700)
RIG_BAUD    = "115200"
RIGCTLD_PORT = 4532

ROT_DEVICE  = Path("/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
ROT_MODEL   = "603"           # Yaesu GS-232B-compatible (custom Arduino)
ROT_BAUD    = "9600"
ROTCTLD_PORT = 4533

STOP_TIMEOUT_S = 5.0

# ============================================================
# inotify (ctypes, pure stdlib — no watchdog/inotify_simple dependency)
# ============================================================
IN_CREATE     = 0x00000100
IN_DELETE     = 0x00000200
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO   = 0x00000080
WATCH_MASK    = IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_init1.restype = ctypes.c_int
_libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.inotify_add_watch.restype = ctypes.c_int

_EVENT_HEADER = struct.Struct("iIII")  # wd, mask, cookie, len


class INotify:
    def __init__(self) -> None:
        self.fd = _libc.inotify_init1(0)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")

    def add_watch(self, path: Path) -> int:
        wd = _libc.inotify_add_watch(self.fd, str(path).encode(), WATCH_MASK)
        if wd < 0:
            raise OSError(ctypes.get_errno(), f"inotify_add_watch({path}) failed")
        return wd

    def read_events(self) -> list[tuple[int, int, str]]:
        buf = os.read(self.fd, 4096)
        events = []
        i = 0
        while i < len(buf):
            wd, mask, _cookie, name_len = _EVENT_HEADER.unpack_from(buf, i)
            i += _EVENT_HEADER.size
            name = buf[i:i + name_len].split(b"\0", 1)[0].decode()
            i += name_len
            events.append((wd, mask, name))
        return events


# ============================================================
# Daemon lifecycle
# ============================================================

def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class Daemon:
    name: str
    device: Path
    cmd: list[str]
    proc: subprocess.Popen | None = field(default=None, repr=False)

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        _log(f"{self.name}: device present, starting ({' '.join(self.cmd)})")
        self.proc = subprocess.Popen(self.cmd)

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = None
            return
        _log(f"{self.name}: stopping")
        self.proc.terminate()
        try:
            self.proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc = None


def build_daemons() -> list[Daemon]:
    return [
        Daemon(
            name="rigctld",
            device=RIG_DEVICE,
            cmd=[
                "rigctld", "-m", RIG_MODEL, "-r", str(RIG_DEVICE),
                "-s", RIG_BAUD, "-t", str(RIGCTLD_PORT),
            ],
        ),
        Daemon(
            name="rotctld",
            device=ROT_DEVICE,
            cmd=[
                "rotctld", "-m", ROT_MODEL, "-r", str(ROT_DEVICE),
                "-s", ROT_BAUD, "-t", str(ROTCTLD_PORT),
            ],
        ),
    ]


def reconcile_initial_state(daemons: list[Daemon]) -> None:
    """Start any daemon whose device already exists.

    inotify only reports *future* events, so a device already present
    at startup needs an explicit check — otherwise it would sit unwatched
    until the next unplug/replug cycle.
    """
    for d in daemons:
        if d.device.exists():
            d.start()
        else:
            _log(f"{d.name}: {d.device} not present, waiting")


def route_event(daemons: list[Daemon], mask: int, name: str) -> None:
    for d in daemons:
        if d.device.name != name:
            continue
        if mask & (IN_CREATE | IN_MOVED_TO):
            d.start()
        elif mask & (IN_DELETE | IN_MOVED_FROM):
            _log(f"{d.name}: device gone")
            d.stop()


def main() -> None:
    daemons = build_daemons()

    inotify = INotify()
    wd_to_daemons: dict[int, list[Daemon]] = {}
    dir_to_wd: dict[Path, int] = {}
    for d in daemons:
        parent = d.device.parent
        wd = dir_to_wd.get(parent)
        if wd is None:
            wd = inotify.add_watch(parent)
            dir_to_wd[parent] = wd
        wd_to_daemons.setdefault(wd, []).append(d)

    def shutdown(signum, frame) -> None:
        for d in daemons:
            d.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    reconcile_initial_state(daemons)

    while True:
        for wd, mask, name in inotify.read_events():
            route_event(wd_to_daemons.get(wd, []), mask, name)


if __name__ == "__main__":
    main()
