#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["folium", "matplotlib", "numpy"]
# ///
"""
Puskás URH Kupa – Station map and polar diagram
================================================
Loads puskas-seen-stations.json (built by puskas_harvester.py) and optionally
own log EDI files (from my-logs/) to mark missed stations.

Output: puskas_map.html, puskas_polar.png

Usage:  uv run puskas_visualizer.py [CALLSIGN LOCATOR]
"""

import math
import re
import sys
from collections import defaultdict
from pathlib import Path

import folium
import matplotlib.pyplot as plt
import numpy as np

MY_LOGS_DIR = Path("my-logs")
SEEN_STATIONS = Path("puskas-seen-stations.json")

RE_LOC = re.compile(r'^[A-R]{2}[0-9]{2}([A-X]{2})?$', re.IGNORECASE)

BAND_COLORS = {
    "2M":   "#2563eb",
    "70CM": "#16a34a",
    "23CM": "#dc2626",
}
BEARING_RESOLUTION = 10


# ──────────────────────────────────────────────────────────────
# Geo
# ──────────────────────────────────────────────────────────────

def maidenhead_to_latlon(loc: str) -> tuple[float, float] | tuple[None, None]:
    try:
        loc = loc.upper()
        lon = (ord(loc[0]) - 65) * 20 - 180
        lat = (ord(loc[1]) - 65) * 10 - 90
        lon += int(loc[2]) * 2
        lat += int(loc[3])
        if len(loc) >= 6:
            lon += (ord(loc[4]) - 65) * (5 / 60)
            lat += (ord(loc[5]) - 65) * (2.5 / 60)
            lon += 2.5 / 60
            lat += 1.25 / 60
        else:
            lon += 1.0
            lat += 0.5
        return lat, lon
    except Exception:
        return None, None


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    φ1, λ1, φ2, λ2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((φ2-φ1)/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin((λ2-λ1)/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def initial_bearing(lat1, lon1, lat2, lon2) -> float:
    φ1, λ1, φ2, λ2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dλ = λ2 - λ1
    x = math.sin(dλ) * math.cos(φ2)
    y = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(dλ)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def color_for_band(band: str) -> str:
    for k, v in BAND_COLORS.items():
        if k in band.upper():
            return v
    return "#6b7280"


# ──────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────

def load_seen_stations() -> dict[str, dict]:
    import json
    if not SEEN_STATIONS.exists():
        print(f"[!] {SEEN_STATIONS} not found — run puskas_harvester.py first")
        sys.exit(1)
    data = json.loads(SEEN_STATIONS.read_text(encoding="utf-8"))
    print(f"  {len(data)} stations from {SEEN_STATIONS.name}")
    return data


def load_my_info() -> tuple[str, str, set[tuple[str, str]]]:
    """Returns (my_callsign, my_locator, worked_set).
    worked_set: {(callsign, band)} from valid (non-dup) QSOs in my-logs/."""
    my_call = my_loc = ""
    worked: set[tuple[str, str]] = set()

    if not MY_LOGS_DIR.exists():
        return my_call, my_loc, worked

    for path in sorted(MY_LOGS_DIR.glob("*.[Ee][Dd][Ii]")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            band = ""
            in_qso = False
            seen_in_file: set[str] = set()
            for line in text.splitlines():
                if line.startswith("PCall=") and not my_call:
                    my_call = line[6:].strip().upper()
                elif line.startswith("PWWLo=") and not my_loc:
                    my_loc = line[6:].strip().upper()
                elif line.startswith("PBand="):
                    raw = line[6:].strip()
                    band = {"145 MHz": "2M", "435 MHz": "70CM",
                            "1296 MHz": "23CM"}.get(raw, "")
                elif line.startswith("[QSORecords"):
                    in_qso = True
                elif in_qso and ";" in line:
                    f = line.split(";")
                    if len(f) >= 10:
                        call = f[2].strip().upper()
                        if call and band and call not in seen_in_file:
                            worked.add((call, band))
                            seen_in_file.add(call)
        except Exception:
            pass

    return my_call, my_loc, worked


# ──────────────────────────────────────────────────────────────
# Analysis
# ──────────────────────────────────────────────────────────────

def analyze(stations: dict[str, dict], my_lat: float, my_lon: float,
            my_call: str) -> list[dict]:
    result = []
    for call, info in stations.items():
        if call.upper() == my_call.upper():
            continue
        wwl = info.get("wwl", "")
        lat, lon = maidenhead_to_latlon(wwl)
        if lat is None:
            continue
        brng = initial_bearing(my_lat, my_lon, lat, lon)
        dist = haversine_km(my_lat, my_lon, lat, lon)
        bands = info.get("bands", []) or ["?"]
        for band in bands:
            result.append({
                "callsign": call,
                "wwl": wwl,
                "band": band,
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "bearing": round(brng, 1),
                "distance_km": round(dist, 1),
            })
    return result


# ──────────────────────────────────────────────────────────────
# Map
# ──────────────────────────────────────────────────────────────

def make_map(analyzed: list[dict], missed: set[tuple[str, str]],
             my_lat: float, my_lon: float, my_call: str, my_loc: str):
    m = folium.Map(location=[my_lat, my_lon], zoom_start=7, tiles="CartoDB positron")
    folium.Marker(
        [my_lat, my_lon],
        popup=f"<b>{my_call}</b><br>{my_loc}",
        icon=folium.Icon(color="black", icon="star", prefix="fa"),
    ).add_to(m)

    by_band = defaultdict(list)
    for s in analyzed:
        by_band[s["band"]].append(s)

    for band, stations in sorted(by_band.items()):
        fg  = folium.FeatureGroup(name=f"Band: {band}")
        col = color_for_band(band)
        for s in stations:
            is_missed = (s["callsign"], s["band"]) in missed
            folium.PolyLine(
                [[my_lat, my_lon], [s["lat"], s["lon"]]],
                color="#ef4444" if is_missed else col,
                weight=1.5 if is_missed else 0.8,
                opacity=0.6 if is_missed else 0.3,
                dash_array="6 4" if is_missed else None,
            ).add_to(fg)
            folium.CircleMarker(
                location=[s["lat"], s["lon"]],
                radius=8 if is_missed else 6,
                color="#ef4444" if is_missed else col,
                fill=True,
                fill_color="#ef4444" if is_missed else col,
                fill_opacity=0.9 if is_missed else 0.7,
                popup=(
                    f"<b>{s['callsign']}</b>"
                    f"{'  ⚠ missed!' if is_missed else ''}<br>"
                    f"Locator: {s['wwl']}<br>"
                    f"Band: {s['band']}<br>"
                    f"Bearing: {s['bearing']}°<br>"
                    f"Distance: {s['distance_km']} km"
                ),
                tooltip=f"{'⚠ ' if is_missed else ''}{s['callsign']} ({s['band']})",
            ).add_to(fg)
        fg.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    out = Path("puskas_map.html")
    m.save(str(out))
    print(f"[OK] Map → {out}")


# ──────────────────────────────────────────────────────────────
# Polar
# ──────────────────────────────────────────────────────────────

def make_polar(analyzed: list[dict], my_call: str, my_loc: str):
    data = [s for s in analyzed if s["band"] != "?"]
    if not data:
        return

    fig, ax = plt.subplots(subplot_kw={"projection": "polar"}, figsize=(9, 9))
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_title(
        f"Puskás URH Kupa – stations by direction and distance\n{my_call} @ {my_loc}",
        pad=20, fontsize=13,
    )

    for band in sorted(set(s["band"] for s in data)):
        bs     = [s for s in data if s["band"] == band]
        angles = [math.radians(s["bearing"]) for s in bs]
        dists  = [s["distance_km"] for s in bs]
        labels = [s["callsign"] for s in bs]
        color  = color_for_band(band)
        ax.scatter(angles, dists, label=band, s=55, alpha=0.8, color=color, zorder=3)
        for a, d, lbl in zip(angles, dists, labels):
            ax.annotate(lbl, (a, d), fontsize=6, ha="center", va="bottom",
                        color=color, xytext=(0, 4), textcoords="offset points")

    ax.set_xticks(np.radians([0, 45, 90, 135, 180, 225, 270, 315]))
    ax.set_xticklabels(["N", "NE", "E", "SE", "S", "SW", "W", "NW"])
    ax.legend(loc="lower right", bbox_to_anchor=(1.35, -0.05), title="Bands")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    out = Path("puskas_polar.png")
    plt.savefig(str(out), dpi=150, bbox_inches="tight")
    print(f"[OK] Polar → {out}")
    plt.close()


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def main():
    print("Puskás URH Kupa – Visualizer")
    print("─" * 40)

    stations = load_seen_stations()

    my_call, my_loc, worked = load_my_info()

    # CLI overrides
    if len(sys.argv) >= 3:
        my_call = sys.argv[1].upper()
        my_loc  = sys.argv[2].upper()
    elif len(sys.argv) == 2:
        print("Usage: puskas_visualizer.py [CALLSIGN LOCATOR]")
        sys.exit(1)

    if not my_call:
        my_call = input("Your callsign: ").strip().upper()
    if not my_loc:
        my_loc = input("Your locator: ").strip().upper()

    my_lat, my_lon = maidenhead_to_latlon(my_loc)
    if my_lat is None:
        print(f"[!] Invalid locator: {my_loc!r}")
        sys.exit(1)

    print(f"  {my_call} @ {my_loc}  ({my_lat:.4f}N, {my_lon:.4f}E)")
    if worked:
        print(f"  {len(worked)} worked (callsign, band) pairs from my-logs/")

    print("Analyzing...")
    analyzed = analyze(stations, my_lat, my_lon, my_call)
    real = sum(1 for s in analyzed if s["band"] != "?")
    print(f"  {real} station-band records")

    # missed = in seen_stations but not in my worked set
    missed: set[tuple[str, str]] = set()
    if worked:
        for s in analyzed:
            if s["band"] != "?" and (s["callsign"], s["band"]) not in worked:
                missed.add((s["callsign"], s["band"]))
        print(f"  {len(missed)} missed station-band pairs")

    print("Generating outputs...")
    make_map(analyzed, missed, my_lat, my_lon, my_call, my_loc)
    make_polar(analyzed, my_call, my_loc)


if __name__ == "__main__":
    main()
