"""Solve the camera from two framing constraints, on a curved earth.

Given the altitude, there is exactly one (tilt, back) that puts the QTH at
90% of the frame height and the horizon at 10%. Altitude stays free -- and it
is what decides where the DX lands, because the horizon runs away as
sqrt(2*alt*R) while the station stays at 233 km.
"""
import math, sys
sys.path.insert(0, "/home/kod/code/urhpk")
from urhpk import qth_map as Q

R = 6371.0
W, H = 672, 350
DIST = 233.0
QTH_ROW, HORIZON_ROW = 0.90 * H, 0.10 * H


def solve(alt):
    """(tilt, back) putting the QTH and the horizon on their rows."""
    f = Q._focal(W)
    # horizon: the grazing ray has world slope -sqrt(2 alt/R), so its screen
    # row is H/2 - f*tan(tilt + atan(slope)) -- one equation, one unknown.
    phi = math.atan(-math.sqrt(2 * alt / R))
    tilt = math.atan((H / 2 - HORIZON_ROW) / f) - phi
    # QTH: solve back, whose own curvature drop depends on back. Two passes.
    target = (H / 2 - QTH_ROW) / f
    back = alt
    for _ in range(60):
        k = alt + back * back / (2 * R)
        back = k * (math.cos(tilt) + target * math.sin(tilt)) / \
                   (math.sin(tilt) - target * math.cos(tilt))
    return tilt, back


def row_of(alt, tilt, back, dist):
    d = back + dist
    zw = -d * d / (2 * R) - alt
    yc = d * math.cos(tilt) - zw * math.sin(tilt)
    zc = d * math.sin(tilt) + zw * math.cos(tilt)
    return H / 2 - Q._focal(W) * zc / yc


def near_edge(alt, tilt, back):
    """Ground distance at the bottom row -- how close the foreground gets."""
    f = Q._focal(W)
    py = H / 2
    dy = f * math.cos(tilt) - py * math.sin(tilt)
    dz = -(f * math.sin(tilt) + py * math.cos(tilt))
    slope = dz / abs(dy)
    disc = slope * slope - 2 * alt / R
    return R * (-slope - math.sqrt(disc)) if disc > 0 else float("nan")


print(f"panel {W}x{H}; QTH pinned to row {QTH_ROW:.0f} (90%), "
      f"horizon to row {HORIZON_ROW:.0f} (10%)\n")
print(f"{'alt km':>7} {'back km':>8} {'tilt':>6} {'horizon km':>11} "
      f"{'HA1VHF row':>11} {'% of H':>7} {'below horiz':>12} "
      f"{'near edge km':>13} {'m/px bottom':>12}")
for alt in (10, 15, 20, 30, 45, 70, 110, 170):
    tilt, back = solve(alt)
    r = row_of(alt, tilt, back, DIST)
    ne = near_edge(alt, tilt, back)
    mpp = ne * 1000 / Q._focal(W)      # ground metres per screen pixel, across
    print(f"{alt:7.0f} {back:8.1f} {math.degrees(tilt):5.1f}° "
          f"{math.sqrt(2*alt*R):11.0f} {r:11.1f} {r/H*100:6.1f}% "
          f"{r-HORIZON_ROW:11.1f} {ne:13.1f} {mpp:12.0f}")


def minimal_change():
    """Keep the shipped BACK_FRAC/ALT_FRAC; move only the tilt so the horizon
    lands on 10%. Where do the QTH and the DX end up then?"""
    alt, back = Q.ALT_FRAC * DIST, Q.BACK_FRAC * DIST
    f = Q._focal(W)
    phi = math.atan(-math.sqrt(2 * alt / R))
    tilt = math.atan((H / 2 - HORIZON_ROW) / f) - phi
    qth = row_of(alt, tilt, back, 0.0)
    dx = row_of(alt, tilt, back, DIST)
    print(f"\nshipped fractions, tilt moved to put the horizon on 10%:")
    print(f"  alt {alt:.1f} km, back {back:.1f} km, tilt {math.degrees(tilt):.1f}° "
          f"(shipped tilt {math.degrees(Q.camera_for(BRG_, DIST).tilt):.1f}°)")
    print(f"  horizon {math.sqrt(2*alt*R):.0f} km at row {HORIZON_ROW:.0f} (10%)")
    print(f"  HA5LA  row {qth:5.1f} ({qth/H*100:4.1f}%)")
    print(f"  HA1VHF row {dx:5.1f} ({dx/H*100:4.1f}%)   near edge "
          f"{near_edge(alt, tilt, back):.0f} km")


BRG_ = 271.0
if __name__ == "__main__":
    minimal_change()
