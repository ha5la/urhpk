"""Great-circle geometry over Maidenhead locators.

Two layers. The lat/lon functions are the maths; the locator functions are the
domain, and take a *pair* of stations because that is what a distance or a
bearing is a property of.
"""

from __future__ import annotations

import math
import re

EARTH_RADIUS_KM = 6371.0

_LOCATOR = re.compile(r"[A-R]{2}[0-9]{2}([A-X]{2})?")


def maidenhead_to_latlon(locator: str) -> tuple[float, float] | None:
    """Centre of a 4- or 6-character locator, or None if it isn't one."""
    loc = (locator or "").strip().upper()
    if not _LOCATOR.fullmatch(loc):
        return None
    lon = (ord(loc[0]) - 65) * 20 - 180 + int(loc[2]) * 2
    lat = (ord(loc[1]) - 65) * 10 - 90 + int(loc[3])
    if len(loc) >= 6:
        lon += (ord(loc[4]) - 65) * (2 / 24) + 1 / 24
        lat += (ord(loc[5]) - 65) * (1 / 24) + 1 / 48
    else:
        lon += 1.0
        lat += 0.5
    return (lat, lon)


def is_locator(text: str) -> bool:
    """Whether `text` is a well-formed 4- or 6-character Maidenhead locator."""
    return _LOCATOR.fullmatch((text or "").strip().upper()) is not None


def latlon_to_maidenhead(lat: float, lon: float) -> str:
    lon += 180
    lat += 90
    loc = chr(ord("A") + int(lon / 20))
    loc += chr(ord("A") + int(lat / 10))
    loc += str(int(lon % 20 / 2))
    loc += str(int(lat % 10))
    loc += chr(ord("A") + int(lon % 2 / (2 / 24)))
    loc += chr(ord("A") + int(lat % 1 / (1 / 24)))
    return loc


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    φ1, λ1, φ2, λ2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (
        math.sin((φ2 - φ1) / 2) ** 2
        + math.cos(φ1) * math.cos(φ2) * math.sin((λ2 - λ1) / 2) ** 2
    )
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Forward azimuth in degrees true.

    *Initial* is load-bearing: the bearing changes along a great circle, so
    this is the value at the first point and not the one at the second. On a
    500 km east-west path at 47°N the two differ by about 5°.
    """
    φ1, λ1, φ2, λ2 = map(math.radians, (lat1, lon1, lat2, lon2))
    x = math.sin(λ2 - λ1) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(λ2 - λ1)
    return math.degrees(math.atan2(x, y)) % 360


def distance_between(from_locator: str, to_locator: str) -> float | None:
    """Kilometres between two locators, or None if either won't parse."""
    a = maidenhead_to_latlon(from_locator)
    b = maidenhead_to_latlon(to_locator)
    if a is None or b is None:
        return None
    return haversine_km(*a, *b)


def bearing_between(from_locator: str, to_locator: str) -> float | None:
    """Initial bearing from one locator to another, or None if either won't parse."""
    a = maidenhead_to_latlon(from_locator)
    b = maidenhead_to_latlon(to_locator)
    if a is None or b is None:
        return None
    return initial_bearing(*a, *b)
