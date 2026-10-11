"""Does terrain read near the horizon, where it did not read underfoot?

Underfoot the ground is nearly face-on, so height buys almost no screen row.
Near the horizon the surface is edge-on to the line of sight and the same
height buys a great deal -- and past the horizon a mountain is the only thing
left to see at all. This measures both.
"""
import math, sys
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
from urhpk import qth_map as Q
from reframe import solve, W, H

R = 6371.0
ALT = 70.0
TILT, BACK = solve(ALT)
F = Q._focal(W)
HORIZON = math.sqrt(2 * ALT * R)


def row(d, h):
    zw = (h - d * d / (2 * R)) - ALT
    yc = d * math.cos(TILT) - zw * math.sin(TILT)
    zc = d * math.sin(TILT) + zw * math.cos(TILT)
    return H / 2 - F * zc / yc


print(f"camera alt {ALT:.0f} km, tilt {math.degrees(TILT):.1f}°, "
      f"horizon {HORIZON:.0f} km at row {row(HORIZON, 0):.1f}\n")
print(f"{'d km':>6} {'row (flat)':>11} {'+500 m':>8} {'+1000 m':>9} "
      f"{'+2000 m':>9} {'+2655 m':>9}   what is out there")
where = {100: "Mátra 1014 m", 300: "High Tatras 2655 m", 500: "E Carpathians ~2000 m",
         700: "", 900: "", 944: "← the horizon", 1000: "", 1057: "limit for a 2655 m peak"}
for d in (100, 300, 500, 700, 900, 944, 1000, 1057):
    r0 = row(d, 0.0)
    cells = "".join(f"{r0 - row(d, h):9.1f}" for h in (0.5, 1.0, 2.0, 2.655))
    vis = "" if d <= HORIZON else \
        ("(only peaks above %.0f m)" % ((d - HORIZON) ** 2 / (2 * R) * 1000))
    print(f"{d:6.0f} {r0:11.1f}{cells}   {where.get(d,'')} {vis}")

print(f"\nA peak of height h stays visible out to sqrt(2·alt·R)+sqrt(2·h·R):")
for h in (0.5, 1.0, 2.0, 2.655):
    print(f"  {h*1000:5.0f} m  →  {HORIZON + math.sqrt(2*h*R):.0f} km "
          f"({math.sqrt(2*h*R):.0f} km past the horizon)")
