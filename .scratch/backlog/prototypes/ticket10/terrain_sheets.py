"""Throwaway driver: what terrain buys, measured and rendered."""
import math, sys, time
import numpy as np
sys.path.insert(0, "/tmp/ticket10"); sys.path.insert(0, "/home/kod/code/urhpk")
from terrain import *
from twolevel import level

locs = worked()
lat0, lon0 = geo.maidenhead_to_latlon(ME)


def pick_zoom(half_km, budget=240):
    for z in range(12, 5, -1):
        mpp = 156543.03392 * math.cos(math.radians(lat0)) / 2 ** z
        across = 2 * (half_km * 1000 / mpp) / 256 + 2
        if across ** 2 <= budget:
            return z
    return 6


def scene(call, alt_k=0.38, back_k=0.45):
    brg, dist = geo.bearing_between(ME, locs[call]), geo.distance_between(ME, locs[call])
    cam = Cam(brg, dist, alt=alt_k * dist, back=back_k * dist)
    half = (cam.back + dist * 1.9) * 1.05
    z = pick_zoom(half)
    zi = pick_zoom(half, budget=190)
    print(f"\n{call}  {dist:.0f} km {brg:.0f}deg  back {cam.back:.1f} alt {cam.alt:.1f} km  "
          f"tilt {math.degrees(cam.tilt):.1f}deg  half {half:.0f} km")
    hgt, hk, horg = dem(lat0, lon0, half_km=half, z=z)
    lv = [level(lat0, lon0, half_km=half, z=zi), level(lat0, lon0, half_km=40, z=11)]
    return cam, lv, hgt, hk, horg, call


def displacement(cam, hgt, hk, horg, exag=1.0):
    """How many screen pixels the relief moves the ground, at this composition."""
    e, n, h, rng, hit = march(cam, hgt, hk, horg, exag)
    t = cam.tilt
    fwd = e * math.sin(math.radians(cam.brg)) + n * math.cos(math.radians(cam.brg)) + cam.back
    right = e * math.cos(math.radians(cam.brg)) - n * math.sin(math.radians(cam.brg))

    def row(hh):
        zw = hh - cam.alt
        yc = fwd * math.cos(t) - zw * math.sin(t)
        zc = fwd * math.sin(t) + zw * math.cos(t)
        return H / 2 - F * zc / yc

    dy = np.abs(row(h) - row(np.zeros_like(h)))[hit]
    return float(np.median(dy)), float(np.percentile(dy, 99)), float(h[hit].max() * 1000)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"

    if which in ("all", "settled"):
        panels = []
        for call in ("HA7NS", "HA3KHB", "HA1VHF"):
            sc = scene(call)
            med, p99, hmax = displacement(*[sc[0], sc[2], sc[3], sc[4]])
            print(f"  relief displacement: median {med:.1f} px, 99th {p99:.1f} px "
                  f"(peak {hmax:.0f} m in frame)")
            t0 = time.perf_counter()
            panels.append(frame(sc[0], sc[1], sc[2], sc[3], sc[4], call, flat=True,
                                label=f"{call} {sc[0].dist:.0f} km — FLAT (shipped)"))
            panels.append(frame(sc[0], sc[1], sc[2], sc[3], sc[4], call, exag=1.0,
                                label=f"{call} — terrain 1x  (moves ground {med:.1f} px median)"))
            print(f"  2 frames in {time.perf_counter()-t0:.1f} s")
        sheet(panels, cols=2, path="/tmp/ticket10/terrain-settled.png")

    if which in ("all", "exag"):
        sc = scene("HA3KHB")
        panels = [frame(sc[0], sc[1], sc[2], sc[3], sc[4], "HA3KHB", flat=True, label="flat"),
                  frame(sc[0], sc[1], sc[2], sc[3], sc[4], "HA3KHB", exag=1, hillshade=0.35,
                        label="terrain 1x + hillshade"),
                  frame(sc[0], sc[1], sc[2], sc[3], sc[4], "HA3KHB", exag=4, hillshade=0.35,
                        label="terrain 4x + hillshade"),
                  frame(sc[0], sc[1], sc[2], sc[3], sc[4], "HA3KHB", exag=8, hillshade=0.35,
                        label="terrain 8x + hillshade")]
        sheet(panels, cols=2, path="/tmp/ticket10/terrain-exag.png")

    if which in ("all", "low"):
        panels = []
        for ak, tag in ((0.38, "alt 0.38 D (shipped)"), (0.15, "alt 0.15 D"), (0.06, "alt 0.06 D")):
            sc = scene("HA3KHB", alt_k=ak)
            med, p99, _ = displacement(sc[0], sc[2], sc[3], sc[4])
            print(f"  {tag}: tilt {math.degrees(sc[0].tilt):.1f}deg, "
                  f"relief {med:.1f} px median / {p99:.1f} px 99th")
            panels.append(frame(sc[0], sc[1], sc[2], sc[3], sc[4], "HA3KHB", flat=True,
                                label=f"{tag} — flat"))
            panels.append(frame(sc[0], sc[1], sc[2], sc[3], sc[4], "HA3KHB", exag=1,
                                hillshade=0.3,
                                label=f"{tag} — terrain ({med:.1f} px median)"))
        sheet(panels, cols=2, path="/tmp/ticket10/terrain-low.png")

    if which in ("all", "curve"):
        sc = scene("HA1VHF")
        panels = [frame(sc[0], sc[1], sc[2], sc[3], sc[4], "HA1VHF", exag=1, hillshade=0.3,
                        label="terrain, flat earth"),
                  frame(sc[0], sc[1], sc[2], sc[3], sc[4], "HA1VHF", exag=1, hillshade=0.3,
                        curve=True, label="terrain + earth curvature")]
        sheet(panels, cols=1, path="/tmp/ticket10/terrain-curve.png")
