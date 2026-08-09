"""The rotator: where the antenna is pointing, and where to send it.

rotctld is polled rather than pushed, because Hamlib's rotator API never
defined an async hook for any backend (see hamlib_supervisor.py). One daemon
thread holds the current bearing; the UI reads it, and every reading that
actually moves is written to telemetry.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone

from recorders import telemetry_rot_record, telemetry_write
from wiring import ROTCTLD_HOST, ROTCTLD_PORT

POLL_S = 1

_rot: dict = {"az": 0.0, "online": False}
_rot_lock = threading.Lock()


def read_az() -> float | None:
    with socket.create_connection((ROTCTLD_HOST, ROTCTLD_PORT), timeout=2.0) as s:
        s.sendall(b"p\n")
        buf = b""
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.0:
            s.settimeout(2.0 - (time.monotonic() - t0))
            chunk = s.recv(64)
            if not chunk:
                break
            buf += chunk
            if len(buf.splitlines()) >= 1:
                break
        return float(buf.decode(errors="replace").splitlines()[0])


def poll_thread():
    # Last azimuth written to telemetry (None = offline/never seen). Plain
    # inequality, no deadband: the rotator reports whole degrees, so there is
    # no sub-degree jitter to suppress -- checked against the real August
    # round, where every one of the 756 azimuth changes was >= 1.0°.
    logged_az = None
    while True:
        try:
            az = read_az()
            with _rot_lock:
                _rot.update(az=az, online=True)
        except Exception:
            az = None
            with _rot_lock:
                _rot.update(az=0.0, online=False)
        if az != logged_az:
            telemetry_write(telemetry_rot_record(datetime.now(timezone.utc), az))
            logged_az = az
        time.sleep(POLL_S)


def current() -> tuple[float, bool]:
    """(azimuth_degrees, online)."""
    with _rot_lock:
        return _rot["az"], _rot["online"]


def point_at(az: int) -> None:
    def _do():
        try:
            with socket.create_connection(
                (ROTCTLD_HOST, ROTCTLD_PORT), timeout=2.0
            ) as s:
                s.sendall(f"P {az:.1f} 0\n".encode())
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()
