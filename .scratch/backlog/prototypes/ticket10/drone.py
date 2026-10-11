"""Throwaway: a tilted camera above the QTH, turning to a heading.

Over flat terrain a tilted pinhole camera looking at the ground is exactly a
homography of an ortho image -- so this needs no 3D, only one aerial picture.
Ground texture here is synthetic (no imagery fetched); what is being tested is
the geometry, the cost, and where worked stations land.
"""
import glob, math, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk")
from urhpk import edi, geo

ME = "JN97TF"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
W, H = 672, 350
FOV_DEG = 60.0


def ground_texture(n=2048, span_km=600.0):
    """Stand-in for an ortho image: roads, fields and a river, in km coords."""
    rng = np.random.default_rng(7)
    t = (rng.normal(0.45, 0.06, (n, n, 3)) * np.array([0.55, 0.75, 0.45])).clip(0, 1)
    step = n // 48
    t[::step, :, :] *= 0.55          # roads
    t[:, ::step, :] *= 0.55
    for _ in range(140):             # fields
        y, x = rng.integers(0, n - 90, 2)
        h, w = rng.integers(30, 90, 2)
        t[y:y + h, x:x + w] *= rng.uniform(0.6, 1.4)
    yy = np.arange(n)
    xx = (n * 0.5 + 0.16 * n * np.sin(yy / n * 7)).astype(int)
    for dx in range(-7, 8):
        t[yy, (xx + dx).clip(0, n - 1)] = (0.20, 0.32, 0.50)
    return (t.clip(0, 1) * 255).astype(np.uint8), span_km


TEX, SPAN = ground_texture()


def view(alt_km, tilt_deg, heading_deg, stations):
    """Look from alt_km above the QTH, tilted down by tilt_deg, along heading."""
    f = (W / 2) / math.tan(math.radians(FOV_DEG) / 2)
    px, py = np.meshgrid(np.arange(W) - W / 2, np.arange(H) - H / 2)
    # camera ray, then pitch down by tilt
    t = math.radians(tilt_deg)
    dx, dy, dz = px, f * math.cos(t) - py * math.sin(t), -(f * math.sin(t) + py * math.cos(t))
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(dz < 0, -alt_km / dz, np.nan)  # hits the ground only looking down
    fwd, right = dy * s, dx * s
    hd = math.radians(heading_deg)
    e = right * math.cos(hd) + fwd * math.sin(hd)
    n = -right * math.sin(hd) + fwd * math.cos(hd)
    u = (e / SPAN + 0.5) * TEX.shape[1]
    v = (0.5 - n / SPAN) * TEX.shape[0]
    ok = np.isfinite(u) & np.isfinite(v) & (u >= 0) & (v >= 0) & (u < TEX.shape[1]) & (v < TEX.shape[0])
    img = np.zeros((H, W, 3), np.uint8)
    img[..., 0], img[..., 1], img[..., 2] = 18, 26, 44  # sky
    ui, vi = np.where(ok, u, 0).astype(int), np.where(ok, v, 0).astype(int)
    img[ok] = TEX[vi[ok], ui[ok]]
    haze = np.clip(np.where(ok, np.abs(s), 0) / (alt_km * 22 + 40), 0, 0.85)[..., None]
    img = (img * (1 - haze) + np.array([120, 140, 170]) * haze).astype(np.uint8)

    out = Image.fromarray(img)
    d = ImageDraw.Draw(out)
    fnt = ImageFont.truetype(FONT, 11)
    for call, (brg, km) in stations:                    # project each station
        a = math.radians(brg - heading_deg)
        fw, rt = km * math.cos(a), km * math.sin(a)
        dyc, dzc = fw * math.cos(t) + alt_km * math.sin(t), -fw * math.sin(t) + alt_km * math.cos(t)
        if dyc <= 0:
            continue
        sx, sy = W / 2 + f * rt / dyc, H / 2 + f * (-dzc) / dyc
        if not (0 <= sx < W and -20 <= sy < H):
            continue
        d.ellipse([sx - 3, sy - 3, sx + 3, sy + 3], fill=(255, 176, 0))
        d.text((sx + 5, sy - 6), call, font=fnt, fill=(255, 245, 220))
    d.text((6, 4), f"alt {alt_km:g} km  tilt {tilt_deg:g}°  hdg {heading_deg:03.0f}°",
           font=fnt, fill=(190, 210, 190))
    d.rectangle([0, 0, W - 1, H - 1], outline=(70, 70, 60))
    return out


def stations():
    seen = {}
    for fp in sorted(glob.glob("/home/kod/code/urhpk/2026-sep/*.edi")):
        for r in edi.read(fp).records:
            loc = getattr(r, "loc", None) or getattr(r, "wwl", None)
            if not loc:
                continue
            b, k = geo.bearing_between(ME, loc), geo.distance_between(ME, loc)
            if b is not None:
                seen[r.callsign] = (b, k)
    return sorted(seen.items(), key=lambda kv: kv[1][1])


sts = stations()
cases = [(0.3, 12, 307), (3, 20, 307), (25, 35, 307), (90, 55, 307), (25, 35, 30), (25, 35, 121)]
sheet = Image.new("RGB", (W * 2 + 24, H * 3 + 24), (0, 0, 0))
for i, (a, t, h) in enumerate(cases):
    sheet.paste(view(a, t, h, sts), ((i % 2) * (W + 24), (i // 2) * (H + 12)))
sheet.save("/tmp/ticket10/drone.png")

import time
t0 = time.perf_counter()
for i in range(20):
    view(25, 35, i, sts)
print(f"{(time.perf_counter()-t0)/20*1000:.0f} ms per heading; 360 headings = "
      f"{(time.perf_counter()-t0)/20*360:.1f} s for a whole round's worth")
