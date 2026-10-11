"""Terrain's screen gain, by camera altitude, at the real distances from the QTH.

Distances below are from JN97TF, not from the camera -- the camera sits `back`
short of it, which is what the first pass got wrong.
"""
import math, sys
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
from urhpk import qth_map as Q
from reframe import solve, W, H

R = 6371.0
F = Q._focal(W)
# along the 030° beam out of JN97TF
FEATURES = [("Mátra", 80, 1.014), ("Low Tatras", 200, 2.043),
            ("High Tatras", 250, 2.655), ("E Carpathians", 500, 2.061)]


def row(alt, tilt, back, from_qth, h):
    d = back + from_qth
    zw = (h - d * d / (2 * R)) - alt
    yc = d * math.cos(tilt) - zw * math.sin(tilt)
    zc = d * math.sin(tilt) + zw * math.cos(tilt)
    return H / 2 - F * zc / yc


print(f"{'alt':>5} {'horizon':>8} " + "".join(f"{n:>16}" for n, _, _ in FEATURES))
print(f"{'km':>5} {'from QTH':>8} " + "".join(f"{'px    row':>16}" for _ in FEATURES))
for alt in (10, 20, 30, 45, 70, 110):
    tilt, back = solve(alt)
    hz = math.sqrt(2 * alt * R) - back
    cells = ""
    for _, dist, h in FEATURES:
        if dist > hz + math.sqrt(2 * h * R):
            cells += f"{'beyond':>16}"
            continue
        gain = row(alt, tilt, back, dist, 0.0) - row(alt, tilt, back, dist, h)
        r = row(alt, tilt, back, dist, h)
        off = " (below horizon)" if dist > hz else ""
        cells += f"{gain:9.1f}{r:7.0f}" if not off else f"{gain:9.1f}{r:7.0f}"
    print(f"{alt:5.0f} {hz:8.0f} {cells}")

print("\nrow 35 = the horizon line, row 315 = the QTH, 350 = bottom of frame")
print("'beyond' = the peak is over the horizon and out of sight entirely")
for alt in (20, 70):
    tilt, back = solve(alt)
    hz = math.sqrt(2 * alt * R) - back
    print(f"\nalt {alt:.0f} km: horizon {hz:.0f} km from the QTH; a peak stays visible to:")
    for n, dist, h in FEATURES:
        reach = hz + math.sqrt(2 * h * R)
        print(f"   {n:14s} at {dist:4.0f} km, {h*1000:4.0f} m → visible out to "
              f"{reach:4.0f} km  {'YES' if dist <= reach else 'no'}")
