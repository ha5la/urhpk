import math, sys, time
sys.path.insert(0, "/tmp/ticket10"); sys.path.insert(0, "/home/kod/code/urhpk")
from terrain import *
from twolevel import level

locs = worked()
lat0, lon0 = geo.maidenhead_to_latlon(ME)
call = "HA7NS"
brg, dist = geo.bearing_between(ME, locs[call]), geo.distance_between(ME, locs[call])
cam = Cam(brg, dist)
half = (cam.back + dist * 1.9) * 1.05
print(f"{call}: {dist:.0f} km {brg:.0f}deg  back {cam.back:.1f} alt {cam.alt:.1f} "
      f"tilt {math.degrees(cam.tilt):.1f}deg  DEM half {half:.0f} km")
hgt, hk, horg = dem(lat0, lon0, half_km=half, z=10)
lv = [level(lat0, lon0, half_km=half, z=9), level(lat0, lon0, half_km=40, z=11)]
for exag, hs, tag in ((1.0, 0.0, "terrain 1x"), (3.0, 0.35, "terrain 3x + hillshade")):
    t0 = time.perf_counter()
    im = frame(cam, lv, hgt, hk, horg, call, exag=exag, hillshade=hs, label=tag)
    print(f"{tag}: {(time.perf_counter()-t0)*1000:.0f} ms")
    im.save(f"/tmp/ticket10/smoke-{exag:g}.png")
t0 = time.perf_counter()
frame(cam, lv, hgt, hk, horg, call, flat=True, label="flat")
print(f"flat: {(time.perf_counter()-t0)*1000:.0f} ms")
