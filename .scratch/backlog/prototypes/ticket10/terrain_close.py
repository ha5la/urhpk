"""Throwaway: the composition where terrain is the picture, not a rounding error.

The settled shot ties altitude to the path length, so relief is always ~1% of
the frame. This gives that up: the camera sits low and near real hills, and the
station is wherever it happens to fall.
"""
import math, sys, time
sys.path.insert(0, "/tmp/ticket10"); sys.path.insert(0, "/home/kod/code/urhpk")
from terrain import *
from twolevel import level

lat0, lon0 = geo.maidenhead_to_latlon(ME)
print(f"QTH {ME} = {lat0:.3f} {lon0:.3f}")
hgt, hk, horg = dem(lat0, lon0, half_km=130, z=10)
lv = [level(lat0, lon0, half_km=130, z=9), level(lat0, lon0, half_km=40, z=11)]

# 340deg from JN97TF runs up the Danube Bend into the Borzsony (939 m);
# 30deg runs over the Godollo hills to the Matra (1014 m).
cases = [(340, 45, 1.2, 8, "brg 340° · alt 1.2 km · 8 km back — Danube Bend"),
         (340, 45, 0.6, 4, "brg 340° · alt 0.6 km · 4 km back"),
         (30, 80, 1.5, 10, "brg 030° · alt 1.5 km · 10 km back — Mátra"),
         (30, 80, 3.0, 20, "brg 030° · alt 3 km · 20 km back")]
panels = []
for brg, dist, alt, back, tag in cases:
    cam = Cam(brg, dist, alt=alt, back=back)
    t0 = time.perf_counter()
    panels.append(frame(cam, lv, hgt, hk, horg, "DX", exag=1.0, curve=True,
                        hillshade=0.30, label=tag))
    print(f"{tag}: tilt {math.degrees(cam.tilt):.1f}deg, {time.perf_counter()-t0:.1f} s")
sheet(panels, cols=2, path="/tmp/ticket10/terrain-close.png")
from PIL import Image
im = Image.open("/tmp/ticket10/terrain-close.png")
for i in range(2):
    im.crop((0, i * 358, 1352, i * 358 + 350)).save(f"/tmp/ticket10/tc-row{i}.png")
