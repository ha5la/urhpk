"""Would shading rescue terrain?

Hillshade is the cheap kind: a Lambertian term off the DEM's own gradient, no
ray casting -- it is already written. Cast shadows are the expensive kind. If
hillshade does not read, cast shadows cannot, so this tests the cheap one
first and at three sun elevations, low sun last because that is where relief
shading is strongest.
"""
import math, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
import skyline as S
from urhpk import qth_map as Q
from urhpk.cast_render import CAST_FONT_PATH
from terrain import dem, shade
from reframe import solve

BRG, ALT, SS, HAZE = 30.0, 20.0, 3, 900.0
S.TILT, S.BACK = solve(ALT)
S.ALT, S.HORIZON = ALT, math.sqrt(2 * ALT * S.R)

levels = [(t.astype("float32"), k, o) for t, k, o in
          (Q.mosaic(S.LAT0, S.LON0, half, z) for half, z in
           ((1250.0, 6), (443.0, 7), (50.0, 11)))]
hgt, hk, horg = dem(S.LAT0, S.LON0, half_km=1250.0, z=7)
far = min(700.0, (min(hgt.shape) / 2 - 2) * hk)

w, h = S.W * SS, S.H * SS
t0 = time.perf_counter()
e, n, d, hit = S.ground_terrain(w, h, BRG, hgt, hk, horg, far)
print(f"march: {time.perf_counter()-t0:.0f} s  (reused for every panel below)")
base = S.shade_ground(e, n, d, hit, levels, w, h, HAZE)

panels = []
for el, strength, tag in ((None, 0.0, "terrain, no shading"),
                          (45.0, 0.35, "+ hillshade, sun 45°"),
                          (20.0, 0.50, "+ hillshade, sun 20°"),
                          (8.0, 0.65, "+ hillshade, sun 8° (long shadows)")):
    img = base.copy()
    if el is not None:
        t1 = time.perf_counter()
        sh = shade(hgt, hk, horg, e, n, 1.0, az=135.0, el=el)
        img = np.where(hit[..., None],
                       img * (1 - strength + strength * 2 * sh[..., None]), img)
        print(f"  {tag}: hillshade {time.perf_counter()-t1:.1f} s")
    out = Image.fromarray(img.clip(0, 255).astype(np.uint8)).resize((S.W, S.H), Image.BOX)
    dr = ImageDraw.Draw(out)
    f12 = ImageFont.truetype(CAST_FONT_PATH, 12)
    tw = dr.textlength(tag, font=f12)
    dr.rectangle([4, 2, 10 + tw, 20], fill=(0, 0, 0))
    dr.text((7, 4), tag, font=f12, fill=(255, 255, 160))
    dr.rectangle([0, 0, S.W - 1, S.H - 1], outline=(70, 70, 60))
    panels.append(out)

sheet = Image.new("RGB", (S.W * 2 + 8, S.H * 2 + 8), (0, 0, 0))
for i, p in enumerate(panels):
    sheet.paste(p, ((i % 2) * (S.W + 8), (i // 2) * (S.H + 8)))
sheet.save("/tmp/ticket10/shadows.png")
for i, p in enumerate(panels):
    p.save(f"/tmp/ticket10/shadows-{i}.png")

# the mid-distance band, where hillshade has the most ground to work on
band = (0, 60, S.W, 150)
z = Image.new("RGB", (S.W * 2, (band[3] - band[1]) * 2 * 2 + 44), (0, 0, 0))
dz = ImageDraw.Draw(z)
f13 = ImageFont.truetype(CAST_FONT_PATH, 13)
for i, (p, tag) in enumerate(((panels[0], "no shading — 2x"),
                              (panels[3], "hillshade, sun 8° — 2x"))):
    y = i * ((band[3] - band[1]) * 2 + 22)
    dz.text((8, y + 3), tag, font=f13, fill=(255, 255, 160))
    z.paste(p.crop(band).resize((S.W * 2, (band[3] - band[1]) * 2), Image.LANCZOS), (0, y + 22))
z.save("/tmp/ticket10/shadows-zoom.png")

diff = np.abs(np.asarray(panels[3], float) - np.asarray(panels[0], float)).mean(2)
print(f"\nhillshade changes the frame by {diff.mean():.1f} grey levels mean, "
      f"{np.percentile(diff, 99):.1f} at the 99th percentile")
print("wrote /tmp/ticket10/shadows.png, shadows-zoom.png")
