"""What earth curvature costs in the shipped renderer.

Flat ground: the ray meets z=0, one division. Curved ground: the ray meets
z = -d^2/2R, which is still closed form -- a quadratic in the horizontal
distance, whose discriminant going negative *is* the horizon. So curvature
does not need the ray march terrain needed; it replaces one divide with a
quadratic, in the same per-pixel inverse map.
"""
import math, sys, time
import numpy as np
sys.path.insert(0, "/home/kod/code/urhpk")
from urhpk import qth_map as Q

R = 6371.0
W, H, TAPS = 672, 350, Q.MAX_TAPS


def ground_curved(cam, w, h, row_offset=0.0):
    px = (np.arange(w) - w / 2)[None, :]
    py = (np.arange(h) - h / 2)[:, None] + row_offset
    f = Q._focal(w)
    dy = f * math.cos(cam.tilt) - py * math.sin(cam.tilt)
    dz = -(f * math.sin(cam.tilt) + py * math.cos(cam.tilt))
    rh = np.hypot(px, dy)
    slope = dz / rh
    disc = slope * slope - 2 * cam.alt_km / R
    with np.errstate(invalid="ignore"):
        d = np.where(disc >= 0, R * (-slope - np.sqrt(np.abs(disc))), np.nan)
    s = np.where((dz < 0) & (disc >= 0), d / rh, np.nan)
    forward, right = dy * s, px * s
    b = math.radians(cam.bearing)
    return (right * math.cos(b) + (forward - cam.back_km) * math.sin(b),
            -right * math.sin(b) + (forward - cam.back_km) * math.cos(b))


def fake_pyramids():
    """The two real level sizes, random pixels -- the gather cost is identical
    and this needs no tile server."""
    rng = np.random.default_rng(3)
    out = []
    for (hh, ww), kmpp in (((2816, 2560), 0.415), ((2304, 2304), 0.052)):
        tex = rng.integers(0, 255, (hh, ww, 3), dtype=np.uint8)
        out.append(Q.mip_pyramid(tex, kmpp, (ww / 2, hh / 2)))
    return out


def bench(fn, n=7):
    fn()                                    # warm
    t0 = time.perf_counter()
    for i in range(n):
        fn(i)
    return (time.perf_counter() - t0) / n * 1000


if __name__ == "__main__":
    pyr = fake_pyramids()
    cam = Q.camera_for(251.0, 168.0)
    flat = Q._ground

    print(f"panel {W}x{H}, taps={TAPS}, camera alt {cam.alt_km:.1f} km "
          f"tilt {math.degrees(cam.tilt):.1f}deg\n")

    g_flat = bench(lambda i=0: flat(cam, W, H, row_offset=i * 0.01))
    g_curv = bench(lambda i=0: ground_curved(cam, W, H, row_offset=i * 0.01))
    print(f"_ground alone      flat {g_flat:7.2f} ms   curved {g_curv:7.2f} ms   "
          f"+{g_curv - g_flat:.2f} ms ({(g_curv/g_flat - 1)*100:+.0f}%)")

    d_flat = bench(lambda i=0: Q.draw_ground(pyr, Q.camera_for(251.0 + i * 0.1, 168.0), W, H))
    Q._ground = ground_curved
    d_curv = bench(lambda i=0: Q.draw_ground(pyr, Q.camera_for(251.0 + i * 0.1, 168.0), W, H))
    Q._ground = flat
    print(f"draw_ground        flat {d_flat:7.1f} ms   curved {d_curv:7.1f} ms   "
          f"+{d_curv - d_flat:.1f} ms ({(d_curv/d_flat - 1)*100:+.0f}%)")

    # how far the curved ground actually moves the picture
    ef, nf = flat(cam, W, H)
    ec, nc = ground_curved(cam, W, H)
    both = np.isfinite(ef) & np.isfinite(ec)
    shift = np.hypot(ec - ef, nc - nf)[both]
    lost = int((np.isfinite(ef) & ~np.isfinite(ec)).sum())
    print(f"\nground shift       median {np.median(shift):6.2f} km   "
          f"99th {np.percentile(shift, 99):6.2f} km   max {shift.max():6.2f} km")
    print(f"pixels the flat model puts on ground that is over the horizon: "
          f"{lost} ({lost / (W * H) * 100:.1f}% of the panel)")
