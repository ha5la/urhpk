"""Is terrain invisible because of geometry, or because of the haze?

At 70 km altitude the geometric horizon is 845 km from the QTH. No amount of
atmosphere lets you see 845 km of it -- the record for a real sight line is
about 440 km. So the picture's real edge is the haze, not the horizon, and
anything terrain does out there is behind it.
"""
import math, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
import skyline as S
from urhpk import qth_map as Q
from urhpk.cast_render import CAST_FONT_PATH
from terrain import dem
from reframe import solve

BRG = 30.0
ALT = 20.0                       # horizon 472 km from the QTH -- within sight
S.TILT, S.BACK = solve(ALT)
S.ALT = ALT
S.HORIZON = math.sqrt(2 * ALT * S.R)
print(f"alt {ALT:.0f} km  back {S.BACK:.0f} km  tilt {math.degrees(S.TILT):.1f}°  "
      f"horizon {S.HORIZON - S.BACK:.0f} km from the QTH")

levels = []
for half, z in ((1250.0, 6), (443.0, 7), (50.0, 11)):
    tex, kmpp, org = Q.mosaic(S.LAT0, S.LON0, half, z)
    levels.append((tex.astype("float32"), kmpp, org))
hgt, hk, horg = dem(S.LAT0, S.LON0, half_km=1250.0, z=7)
far = min(700.0, (min(hgt.shape) / 2 - 2) * hk)

panels = []
for haze, htag in ((330.0, "haze as measured (visibility ~330 km)"),
                   (900.0, "haze reduced (the Google Earth cheat)")):
    a = S.render(BRG, levels, ss=3, haze_km=haze, tag=f"no terrain · {htag}")
    b = S.render(BRG, levels, ss=3, terrain=(hgt, hk, horg, far), haze_km=haze,
                 tag=f"TERRAIN · {htag}")
    panels += [a, b]

sheet = Image.new("RGB", (S.W * 2 + 8, S.H * 2 + 8), (0, 0, 0))
for i, p in enumerate(panels):
    sheet.paste(p, ((i % 2) * (S.W + 8), (i // 2) * (S.H + 8)))
sheet.save("/tmp/ticket10/haze.png")
for i, p in enumerate(panels):
    p.save(f"/tmp/ticket10/haze-{i}.png")

band = (0, 18, S.W, 78)
zoom = Image.new("RGB", (S.W * 3, (band[3] - band[1]) * 3 * 2 + 44), (0, 0, 0))
d = ImageDraw.Draw(zoom)
f13 = ImageFont.truetype(CAST_FONT_PATH, 13)
for i, (p, tag) in enumerate(((panels[2], "no terrain, haze reduced — 3x"),
                              (panels[3], "TERRAIN, haze reduced — 3x  (Tatras at 250 km)"))):
    y = i * ((band[3] - band[1]) * 3 + 22)
    d.text((8, y + 3), tag, font=f13, fill=(255, 255, 160))
    zoom.paste(p.crop(band).resize((S.W * 3, (band[3] - band[1]) * 3), Image.LANCZOS),
               (0, y + 22))
zoom.save("/tmp/ticket10/haze-zoom.png")
print("wrote /tmp/ticket10/haze.png, haze-zoom.png")
