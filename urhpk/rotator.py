"""The rotator: where the antenna is pointing, and where to send it.

rotctld is polled rather than pushed, because Hamlib's rotator API never
defined an async hook for any backend (see hamlib_supervisor.py). One task
holds the current bearing; the UI reads it, and every reading that actually
moves is written to telemetry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from urhpk.recorders import telemetry_rot_record, telemetry_write
from urhpk.wiring import ROTCTLD_HOST, ROTCTLD_PORT

POLL_S = 1
TIMEOUT_S = 2.0

_rot: dict = {"az": 0.0, "online": False}

# Fire-and-forget commands, held so the loop cannot collect one mid-flight.
_pending: set[asyncio.Task] = set()


async def read_az() -> float:
    """Ask rotctld where the antenna is pointing. Raises if it cannot say."""
    reader, writer = await asyncio.open_connection(ROTCTLD_HOST, ROTCTLD_PORT)
    try:
        writer.write(b"p\n")
        await writer.drain()
        line = await reader.readline()
        return float(line.decode(errors="replace").strip())
    finally:
        writer.close()


async def poll() -> None:
    # Last azimuth written to telemetry (None = offline/never seen). Plain
    # inequality, no deadband: the rotator reports whole degrees, so there is
    # no sub-degree jitter to suppress -- checked against the real August
    # round, where every one of the 756 azimuth changes was >= 1.0°.
    logged_az = None
    while True:
        try:
            az = await asyncio.wait_for(read_az(), TIMEOUT_S)
            _rot.update(az=az, online=True)
        except Exception:
            az = None
            _rot.update(az=0.0, online=False)
        if az != logged_az:
            telemetry_write(telemetry_rot_record(datetime.now(timezone.utc), az))
            logged_az = az
        await asyncio.sleep(POLL_S)


def current() -> tuple[float, bool]:
    """(azimuth_degrees, online).

    No lock: `_rot` is written by the poll task and read by the UI, both on the
    one event loop, so neither can observe the other half-done.
    """
    return _rot["az"], _rot["online"]


def point_at(az: int) -> None:
    """Turn the antenna, without waiting for it.

    Called straight from a key binding, which prompt_toolkit runs on the event
    loop -- so there is a loop to schedule on, and the operator's keystroke
    never waits for a rotator that is slow or absent.
    """

    async def _send():
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ROTCTLD_HOST, ROTCTLD_PORT), TIMEOUT_S
            )
            writer.write(f"P {az:.1f} 0\n".encode())
            await writer.drain()
            writer.close()
        except Exception:
            pass

    task = asyncio.get_running_loop().create_task(_send())
    _pending.add(task)
    task.add_done_callback(_pending.discard)
