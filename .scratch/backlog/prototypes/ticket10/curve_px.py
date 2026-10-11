"""Curvature's displacement in screen pixels -- the same metric terrain scored
0.4-2.0 px on. Take the true (curved) ground point under each pixel and ask
where a flat-earth camera would have drawn it."""
import math, sys
import numpy as np
sys.path.insert(0, "/home/kod/code/urhpk")
from urhpk import qth_map as Q
sys.path.insert(0, "/tmp/ticket10")
from curve_bench import ground_curved, R, W, H

for call, brg, dist in (("HA7NS", 30.0, 37.0), ("HA3KHB", 251.0, 168.0),
                        ("HA1VHF", 271.0, 233.0)):
    cam = Q.camera_for(brg, dist)
    f, t = Q._focal(W), cam.tilt
    e, n = ground_curved(cam, W, H)
    b = math.radians(cam.bearing)
    fwd = e * math.sin(b) + n * math.cos(b) + cam.back_km
    right = e * math.cos(b) - n * math.sin(b)
    d = np.hypot(fwd, right)
    drop = d * d / (2 * R)          # how far below the tangent plane it really is

    def row(z):                      # screen row of a point at height z
        zw = z - cam.alt_km
        return H / 2 - f * (fwd * math.sin(t) + zw * math.cos(t)) / \
                       (fwd * math.cos(t) - zw * math.sin(t))

    dy = np.abs(row(-drop) - row(np.zeros_like(drop)))
    ok = np.isfinite(dy)
    horizon = math.sqrt(2 * cam.alt_km * R)
    print(f"{call:7s} {dist:3.0f} km  alt {cam.alt_km:5.1f} km   "
          f"curvature moves the ground {np.median(dy[ok]):5.1f} px median, "
          f"{np.percentile(dy[ok], 99):6.1f} px 99th   "
          f"(drop at the DX {(cam.back_km+dist)**2/(2*R)*1000:4.0f} m, "
          f"true horizon {horizon:.0f} km)")
