"""The rigctld dialect on port 4532, so other local tools can ask the radio.

The radio holds only ONE network session — a second connect silently kills the
first, verified live — so the logger owns it and serves everyone else from its
own push-fresh cache. Replies come from memory, which is why on4kst_irc_bridge
can query at the moment it composes a sked instead of keeping a poll cache.

Where the state comes from is not this module's business: `start` is handed a
`snapshot()` returning (online, freq_hz, raw_mode), and answers from whatever
that says at the instant a query arrives.
"""

from __future__ import annotations

import asyncio
from typing import Callable

Snapshot = Callable[[], tuple[bool, int, str]]


async def start(port: int, snapshot: Snapshot) -> asyncio.Server | None:
    """Serve the dialect on 127.0.0.1:port, or None if the port is taken.

    A taken port means something else is already answering there — a real
    rigctld, or a second logger — so the right move is to serve nothing rather
    than to fight over it.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                cmd = line.strip()
                online, freq_hz, raw_mode = snapshot()
                if cmd == b"f" and online:
                    writer.write(f"{freq_hz}\n".encode())
                elif cmd == b"m" and online:
                    writer.write(f"{raw_mode}\n0\n".encode())
                elif cmd == b"q":
                    return
                else:
                    writer.write(b"RPRT -1\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    try:
        return await asyncio.start_server(handle, "127.0.0.1", port)
    except OSError:
        return None
