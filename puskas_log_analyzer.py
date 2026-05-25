#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests", "folium", "maidenhead", "matplotlib", "pandas"]
# ///
"""
Puskás URH Kupa – Log elemző és iránytérkép generátor
=====================================================
Lekéri a beküldött logokat a bb.mrasz.hu API-ról,
összegyűjti az állomásokat sávonként, majd interaktív
térképet, polárdiagramot, CSV táblázatot és "missed
opportunities" listát készít.

Használat:
    uv run puskas_log_analyzer.py
"""

import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import folium
import maidenhead as mh
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

# ============================================================
# Konfiguráció
# ============================================================
EVENT_ID           = "69f763f0e0f63251aa32f0f1"
MY_CALLSIGN        = "HA5LA"
MY_LOCATOR         = "JN97TF"
BASE_URL           = "https://bb.mrasz.hu/nest"
BEARING_RESOLUTION = 10    # szektorfelbontás fokokban
REQUEST_DELAY      = 0.3   # másodperc kérések között
CACHE_DIR          = Path(".puskas_cache")
# ============================================================


# ============================================================
# Cache
# ============================================================

def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    safe = key.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_")
    return CACHE_DIR / f"{safe}.json"


def cached_get(session: requests.Session, url: str) -> dict | list | None:
    """GET kérés, eredményt JSON-ban cache-eli. Cache találatnál nem megy a hálózatra."""
    path = _cache_path(url.replace(BASE_URL, ""))
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        time.sleep(REQUEST_DELAY)
        return data
    except Exception as e:
        print(f"    [hiba] {url} → {e}")
        return None


# ============================================================
# Geo
# ============================================================

def locator_to_latlon(locator: str) -> tuple[float, float] | tuple[None, None]:
    try:
        lat, lon = mh.to_location(locator[:6] if len(locator) >= 6 else locator)
        return lat, lon
    except Exception:
        return None, None


def bearing(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def distance_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def sector_label(brng: float, res: int = BEARING_RESOLUTION) -> str:
    lo = int(brng / res) * res
    return f"{lo:03d}–{lo+res:03d}°"


# ============================================================
# API
# ============================================================

def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; PuskasLogAnalyzer/3.0)",
        "Accept": "application/json",
        "Referer": "https://bb.mrasz.hu/",
    })
    return s


def fetch_claimed(session: requests.Session) -> list[dict]:
    """1. lépés: minden beküldő (callsign, WWL) párja."""
    url = f"{BASE_URL}/claimed?eventId={EVENT_ID}"
    print(f"  GET {url}")
    data = cached_get(session, url)
    if not data:
        return []

    stations = {}
    for category in data:
        for log in category.get("logs", []):
            callsign = log.get("_id", {}).get("callsign", "").upper().strip()
            wwl      = log.get("_id", {}).get("WWL", "").upper().strip()
            if callsign and wwl and callsign not in stations:
                stations[callsign] = wwl

    result = [{"callsign": c, "wwl": w} for c, w in stations.items()]
    print(f"  → {len(result)} egyedi beküldő")
    return result


def fetch_round_codes(session: requests.Session, callsign: str) -> list[str]:
    """2. lépés: melyik sávkódokon adott be logot az állomás."""
    url = f"{BASE_URL}/log?eventId={EVENT_ID}&skip=0&limit=25&callsign={callsign}"
    data = cached_get(session, url)
    if not data:
        return []
    codes = []
    for log in data.get("logs", []):
        for rnd in log.get("rounds", []):
            code = rnd.get("code", "")
            if code and code not in codes:
                codes.append(code)
    return codes


def fetch_qsos(session: requests.Session, callsign: str, round_code: str) -> list[dict]:
    """3. lépés: QSO-partnerek egy adott sávon."""
    url = (f"{BASE_URL}/qso?eventId={EVENT_ID}"
           f"&callsign={callsign}&roundCode={round_code}&isClaimed=true")
    data = cached_get(session, url)
    if not data:
        return []
    qsos = []
    for q in data.get("qsos", []):
        dx_call = q.get("callsign", "").upper().strip()
        dx_wwl  = q.get("rWWL", "").upper().strip()
        band    = q.get("band", "").strip()
        if dx_call and dx_wwl:
            qsos.append({"callsign": dx_call, "wwl": dx_wwl, "band": band})
    return qsos


# ============================================================
# Adatgyűjtés + deduplikáció
# ============================================================

def collect_all_stations(session: requests.Session) -> tuple[list[dict], set[str]]:
    """
    Végigmegy minden beküldőn, lekéri a QSO-partnereiket sávonként.
    Visszaad:
      - stations: [{callsign, wwl, band}] – csak valódi sávos rekordok,
                  a '?' fallback sorok el vannak távolítva ha van jobb adat
      - my_worked: a saját logban szereplő hívójelek halmaza (callsign, band) párokként
    """
    print("\n[1] Beküldött állomások lekérése...")
    submitted = fetch_claimed(session)

    # (callsign, band) → wwl – valódi sávos adatok
    known: dict[tuple[str, str], str] = {}
    # callsign → wwl – fallback, ha semmi QSO-adat nincs
    fallback_wwl: dict[str, str] = {s["callsign"]: s["wwl"] for s in submitted}

    my_worked: set[tuple[str, str]] = set()  # (callsign, band) amit én is dolgoztam

    print(f"\n[2] QSO-partnerek lekérése ({len(submitted)} állomás)...")
    for i, s in enumerate(submitted, 1):
        callsign = s["callsign"]
        print(f"  [{i:2d}/{len(submitted)}] {callsign} @ {s['wwl']}", end="", flush=True)

        round_codes = fetch_round_codes(session, callsign)
        if not round_codes:
            print(" – nincs log")
            continue

        total = 0
        for code in round_codes:
            qsos = fetch_qsos(session, callsign, code)
            for q in qsos:
                key = (q["callsign"], q["band"])
                # Pontosabb (hosszabb) lokátort preferáljuk
                if key not in known or len(q["wwl"]) > len(known[key]):
                    known[key] = q["wwl"]
                # Ha ez a mi logunk, jegyezzük meg mit dolgoztunk
                if callsign == MY_CALLSIGN:
                    my_worked.add(key)
            total += len(qsos)

        print(f" – {len(round_codes)} sáv, {total} QSO")

    # Összerakás: valódi sávos rekordok
    stations = [
        {"callsign": call, "wwl": wwl, "band": band}
        for (call, band), wwl in known.items()
    ]

    # Fallback: beküldők akik egyetlen más logban sem szerepeltek partnerként
    calls_with_band = {call for call, _ in known}
    for call, wwl in fallback_wwl.items():
        if call not in calls_with_band:
            stations.append({"callsign": call, "wwl": wwl, "band": "?"})

    real = sum(1 for s in stations if s["band"] != "?")
    fb   = sum(1 for s in stations if s["band"] == "?")
    print(f"\n  → {real} valódi sávos rekord, {fb} fallback '?' rekord")
    return stations, my_worked


# ============================================================
# Elemzés
# ============================================================

def analyze(stations: list[dict], my_lat: float, my_lon: float) -> list[dict]:
    result = []
    for s in stations:
        if s["callsign"].upper() == MY_CALLSIGN.upper():
            continue
        lat, lon = locator_to_latlon(s["wwl"])
        if lat is None:
            continue
        brng = bearing(my_lat, my_lon, lat, lon)
        dist = distance_km(my_lat, my_lon, lat, lon)
        result.append({
            **s,
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "bearing": round(brng, 1),
            "distance_km": round(dist, 1),
            "sector": sector_label(brng),
        })
    return result


def missed_opportunities(
    analyzed: list[dict],
    my_worked: set[tuple[str, str]],
    top_n: int = 20,
) -> tuple[pd.DataFrame, set[tuple[str, str]]]:
    """
    Azok az állomások, akik aktívak voltak a fordulón, de velem nem forgalmaztak.
    Visszaad:
      - top_df: top_n sor távolság szerint csökkenően (CSV-hez, konzolos listához)
      - all_missed_keys: az összes kihagyott (callsign, band) pár (térképhez)
    Csak valódi sávos rekordokat vesz figyelembe.
    """
    rows = []
    for s in analyzed:
        if s["band"] == "?":
            continue
        key = (s["callsign"], s["band"])
        if key not in my_worked:
            rows.append(s)

    if not rows:
        return pd.DataFrame(), set()

    df = pd.DataFrame(rows).drop_duplicates(subset=["callsign", "band"])
    df = df.sort_values("distance_km", ascending=False)
    all_missed_keys = set(zip(df["callsign"], df["band"]))
    top_df = df.head(top_n)[["callsign", "wwl", "band", "bearing", "distance_km", "sector"]]
    return top_df, all_missed_keys


def bearing_table(analyzed: list[dict]) -> dict:
    table = defaultdict(lambda: defaultdict(set))
    for s in analyzed:
        if s["band"] != "?":
            table[s["band"]][s["sector"]].add(s["callsign"])
    return table


# ============================================================
# Kimenet
# ============================================================

def print_bearing_table(table: dict):
    print("\n" + "="*72)
    print("  IRÁNYTÁBLÁZAT  –  sáv → szektoronként hány állomás")
    print("="*72)
    for band in sorted(table.keys()):
        print(f"\n  Sáv: {band}")
        print(f"  {'Szektor':<15} {'db':>4}  Hívójelek")
        print(f"  {'-'*65}")
        for sec in sorted(table[band].keys()):
            calls = sorted(table[band][sec])
            print(f"  {sec:<15} {len(calls):>4}  {', '.join(calls)}")


def print_missed(df: pd.DataFrame):
    if df.empty:
        print("\n  (Nincs kihagyott lehetőség – mindenkit sikerült dolgozni!)")
        return
    print("\n" + "="*72)
    print(f"  KIHAGYOTT LEHETŐSÉGEK (top {len(df)}, távolság szerint csökkenő)")
    print("="*72)
    print(f"  {'Hívójel':<12} {'WWL':<8} {'Sáv':<6} {'Irány':>6} {'Táv (km)':>9}  Szektor")
    print(f"  {'-'*65}")
    for _, r in df.iterrows():
        print(f"  {r['callsign']:<12} {r['wwl']:<8} {r['band']:<6} "
              f"{r['bearing']:>6.1f}° {r['distance_km']:>8.1f}  {r['sector']}")


def export_csv(analyzed: list[dict], filename: str = "puskas_stations.csv"):
    df = pd.DataFrame(analyzed)
    df = df[df["band"] != "?"].sort_values(["band", "bearing"])
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"[OK] CSV mentve: {filename}")


BAND_COLORS = {
    "2M":   "#2563eb",
    "70CM": "#16a34a", "432": "#16a34a",
    "23CM": "#dc2626", "1296": "#dc2626",
    "13CM": "#9333ea",
    "6CM":  "#ea580c",
    "3CM":  "#0891b2",
}

def color_for_band(band: str) -> str:
    for k, v in BAND_COLORS.items():
        if k in band.upper():
            return v
    return "#6b7280"


def make_map(
    analyzed: list[dict],
    all_missed_keys: set[tuple[str, str]],
    my_lat: float,
    my_lon: float,
    filename: str = "puskas_map.html",
):
    m = folium.Map(location=[my_lat, my_lon], zoom_start=7, tiles="CartoDB positron")

    folium.Marker(
        [my_lat, my_lon],
        popup=f"<b>{MY_CALLSIGN}</b><br>{MY_LOCATOR}",
        icon=folium.Icon(color="black", icon="star", prefix="fa"),
    ).add_to(m)

    missed_keys = all_missed_keys

    by_band = defaultdict(list)
    for s in analyzed:
        if s["band"] != "?":
            by_band[s["band"]].append(s)

    for band, stations in sorted(by_band.items()):
        fg  = folium.FeatureGroup(name=f"Sáv: {band}")
        col = color_for_band(band)
        for s in stations:
            is_missed = (s["callsign"], s["band"]) in missed_keys
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
                    f"{'  ⚠️ kihagyott!' if is_missed else ''}<br>"
                    f"Lokátor: {s['wwl']}<br>"
                    f"Sáv: {s['band']}<br>"
                    f"Irány: {s['bearing']}°<br>"
                    f"Távolság: {s['distance_km']} km"
                ),
                tooltip=f"{'⚠ ' if is_missed else ''}{s['callsign']} ({s['band']})",
            ).add_to(fg)
        fg.add_to(m)

    # Legenda
    legend_html = """
    <div style="position:fixed;bottom:30px;left:30px;z-index:1000;background:white;
                padding:12px 16px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.2);
                font-family:monospace;font-size:13px;line-height:1.8">
      <b>Jelölések</b><br>
      <span style="color:#ef4444">●  ╌╌</span>  Kihagyott lehetőség<br>
      <span style="color:#2563eb">●  ───</span>  2M – dolgozva<br>
      <span style="color:#16a34a">●  ───</span>  70CM – dolgozva
    </div>"""
    m.get_root().html.add_child(folium.Element(legend_html))
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(filename)
    print(f"[OK] Térkép mentve: {filename}")


def make_polar_plot(analyzed: list[dict], filename: str = "puskas_polar.png"):
    data = [s for s in analyzed if s["band"] != "?"]
    if not data:
        return

    fig, ax = plt.subplots(subplot_kw={"projection": "polar"}, figsize=(9, 9))
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_title(
        f"Puskás URH Kupa – állomások iránya és távolsága\n{MY_CALLSIGN} @ {MY_LOCATOR}",
        pad=20, fontsize=13,
    )

    bands = sorted(set(s["band"] for s in data))
    for band in bands:
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
    ax.set_xticklabels(["É", "ÉK", "K", "DK", "D", "DNy", "Ny", "ÉNy"])
    ax.legend(loc="lower right", bbox_to_anchor=(1.35, -0.05), title="Sávok")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(filename, dpi=150, bbox_inches="tight")
    print(f"[OK] Polárdiagram mentve: {filename}")
    plt.close()


def export_missed_csv(df: pd.DataFrame, filename: str = "puskas_missed.csv"):
    if df.empty:
        return
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"[OK] Kihagyott lehetőségek CSV: {filename}")


# ============================================================
# Fő program
# ============================================================

def main():
    print("=" * 60)
    print(f"  Puskás URH Kupa – Log elemző v3")
    print(f"  Forduló: {EVENT_ID}")
    print(f"  Saját állomás: {MY_CALLSIGN} @ {MY_LOCATOR}")
    if CACHE_DIR.exists():
        cached = list(CACHE_DIR.glob("*.json"))
        print(f"  Cache: {len(cached)} fájl ({CACHE_DIR})")
    print("=" * 60)

    my_lat, my_lon = locator_to_latlon(MY_LOCATOR)
    if my_lat is None:
        print("[hiba] A lokátor nem értelmezhető.")
        sys.exit(1)
    print(f"Saját koordináta: {my_lat:.4f}°N, {my_lon:.4f}°E")

    session = get_session()
    stations, my_worked = collect_all_stations(session)

    if not stations:
        print("[!] Nem sikerült adatot betölteni.")
        sys.exit(1)

    print("\n[3] Irányok és távolságok számítása...")
    analyzed = analyze(stations, my_lat, my_lon)
    real = sum(1 for s in analyzed if s["band"] != "?")
    print(f"  → {real} valódi sávos rekord elemezve")

    missed_df, all_missed_keys = missed_opportunities(analyzed, my_worked, top_n=20)

    print_bearing_table(bearing_table(analyzed))
    print_missed(missed_df)

    print("\n[4] Fájlok generálása...")
    export_csv(analyzed)
    export_missed_csv(missed_df)
    make_map(analyzed, all_missed_keys, my_lat, my_lon)
    make_polar_plot(analyzed)

    print("\n✓ Kész! Generált fájlok:")
    print("  puskas_stations.csv  – összes állomás sávonként")
    print("  puskas_missed.csv    – kihagyott lehetőségek távolság szerint")
    print("  puskas_map.html      – interaktív térkép (piros = kihagyott)")
    print("  puskas_polar.png     – polárdiagram")
    print(f"\n  Cache: {CACHE_DIR}/ – töröld ha friss adatot akarsz")


if __name__ == "__main__":
    main()
