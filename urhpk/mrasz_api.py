"""The MRASZ contest server's read-only JSON API, and the cache in front of it.

Two components read it — the harvester before a round and the standings script
after one — so the cache is shared: whichever runs first pays for the fetch.
"""

import json
import time
import urllib.request
from pathlib import Path

BASE_URL = "https://bb.mrasz.hu/nest"
LIST_URL = f"{BASE_URL}/events/list?site=bb.mrasz.hu"
CACHE_DIR = Path(".puskas_cache")
REQUEST_DELAY = 0.3
REQUEST_TIMEOUT = 15
# The event list gains a round a month and is the only way to learn one exists,
# so it is cached for convenience within a session, never across days.
EVENT_LIST_TTL = 3600
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PuskasHarvester/1.0)",
    "Accept": "application/json",
    "Referer": "https://bb.mrasz.hu/",
}


def cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    safe = key.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_")
    return CACHE_DIR / f"{safe}.json"


def cached_get(
    url: str, max_age: float | None = None, now=time.time
) -> dict | list | None:
    """Fetch `url`, serving a cached copy younger than `max_age` seconds.

    `max_age=None` never expires, which is right only where the server's answer
    cannot change any more — an evaluated round. For anything still moving, a
    cached file that predates the newest round is not a fast answer but a wrong
    one, and the caller says so by passing a bound.
    """
    path = cache_path(url.replace(BASE_URL, ""))
    if path.exists() and (max_age is None or now() - path.stat().st_mtime < max_age):
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        time.sleep(REQUEST_DELAY)
        return data
    except Exception as e:
        print(f"  [error] {url} → {e}")
        return None
