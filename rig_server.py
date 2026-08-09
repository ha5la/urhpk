"""The rigctld dialect on port 4532, so other local tools can ask the radio.

The radio holds only ONE network session — a second connect silently kills the
first, verified live — so the logger owns it and serves everyone else from its
own push-fresh cache. Replies come from memory, which is why on4kst_irc_bridge
can query at the moment it composes a sked instead of keeping a poll cache.

Where the state comes from is not this module's business: `serve` is handed a
`snapshot()` returning (online, freq_hz, raw_mode), and answers from whatever
that says at the instant a query arrives.
"""

from __future__ import annotations

import socket
import threading
from typing import Callable

Snapshot = Callable[[], tuple[bool, int, str]]


def bind(port: int) -> socket.socket | None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", port))
    except OSError:
        srv.close()
        return None  # port taken (e.g. a real rigctld) — serve nothing
    srv.listen(4)
    return srv


def _serve_client(conn: socket.socket, snapshot: Snapshot) -> None:
    try:
        with conn, conn.makefile("rb") as r:
            for line in r:
                cmd = line.strip()
                online, freq_hz, raw_mode = snapshot()
                if cmd == b"f" and online:
                    conn.sendall(f"{freq_hz}\n".encode())
                elif cmd == b"m" and online:
                    conn.sendall(f"{raw_mode}\n0\n".encode())
                elif cmd == b"q":
                    break
                else:
                    conn.sendall(b"RPRT -1\n")
    except Exception:
        pass


def serve(srv: socket.socket, snapshot: Snapshot) -> None:
    """Accept loop: one thread per client, each answering from `snapshot`."""
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        threading.Thread(
            target=_serve_client, args=(conn, snapshot), daemon=True
        ).start()
