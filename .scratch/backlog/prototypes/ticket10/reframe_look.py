"""Render the reframed camera: QTH at 90%, horizon at 10%, curvature on.

Three texture levels, not two: showing a real horizon means the wide level has
to reach it, and one that reaches 900 km is far too coarse for the middle
distance. Coarsest is painted first, so adding a level needs no code change.
"""
import math, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
from urhpk import qth_map as Q
from urhpk.cast_render import CAST_FONT_PATH
from urhpk.geo import maidenhead_to_latlon
from curve_bench import ground_curved, R
from curve_look import project_curved
from reframe import solve, row_of, near_edge, W, H, DIST

ME, MY_CALL, CALL, BRG = "JN97TF", "HA5LA", "HA1VHF", 271.0
lat, lon = maidenhead_to_latlon(ME)
flat_ground, flat_project, flat_cam = Q._ground, Q.project, Q.camera_for
ALTS = [20.0, 45.0, 70.0]

# one pyramid set, wide enough for the highest camera's horizon
wide_half = math.sqrt(2 * max(ALTS) * R) * 1.05
levels = [(wide_half, Q.zoom_for(lat, wide_half)),
          (DIST * 1.9, Q.zoom_for(lat, DIST * 1.9)),
          (Q.NEAR_HALF_KM, Q.NEAR_ZOOM)]
pyr = []
for half, z in levels:
    tex, kmpp, org = Q.mosaic(lat, lon, half, z)
    print(f"  level: half {half:5.0f} km  z{z}  {tex.shape[1]}x{tex.shape[0]} px "
          f"@ {kmpp*1000:.0f} m/px")
    pyr.append(Q.mip_pyramid(tex, kmpp, org))

panels = []
for alt in ALTS:
    tilt, back = solve(alt)
    cam = Q.Camera(BRG, DIST, back, alt, tilt)
    Q._ground, Q.project, Q.camera_for = ground_curved, project_curved, lambda b, d: cam
    tgt = Q.Target(0.0, 1.0, CALL, BRG, DIST)
    im = Q.draw_map_frame(pyr, Q.Shot(cam, tgt, None, 1.0), W, H, MY_CALL).convert("RGB")
    Q._ground, Q.project, Q.camera_for = flat_ground, flat_project, flat_cam

    r = row_of(alt, tilt, back, DIST)
    d = ImageDraw.Draw(im)
    f12 = ImageFont.truetype(CAST_FONT_PATH, 12)
    f10 = ImageFont.truetype(CAST_FONT_PATH, 10)
    for y, tag, col in ((0.10 * H, "horizon · 10%", (255, 120, 120)),
                        (0.90 * H, "HA5LA · 90%", (120, 220, 255))):
        d.line([(0, y), (W, y)], fill=col, width=1)
        d.text((W - 96, y - 12 if y > H / 2 else y + 2), tag, font=f10, fill=col)
    d.line([(0, r), (W, r)], fill=(255, 200, 60), width=1)
    d.text((6, r + 2), f"HA1VHF · {r/H*100:.0f}%", font=f10, fill=(255, 200, 60))
    tag = (f"alt {alt:.0f} km · back {back:.0f} km · tilt {math.degrees(tilt):.1f}° · "
           f"horizon {math.sqrt(2*alt*R):.0f} km")
    tw = d.textlength(tag, font=f12)
    d.rectangle([4, 2, 10 + tw, 20], fill=(0, 0, 0))
    d.text((7, 4), tag, font=f12, fill=(255, 255, 160))
    d.rectangle([0, 0, W - 1, H - 1], outline=(70, 70, 60))
    print(f"alt {alt:.0f}: HA1VHF at row {r:.0f} ({r/H*100:.1f}%), "
          f"{r - 0.1*H:.0f} px below the horizon; "
          f"near edge {near_edge(alt, tilt, back):.0f} km")
    panels.append(im)

sheet = Image.new("RGB", (W, len(panels) * (H + 8) - 8), (0, 0, 0))
for i, p in enumerate(panels):
    sheet.paste(p, (0, i * (H + 8)))
sheet.save("/tmp/ticket10/reframed.png")
for i, p in enumerate(panels):
    p.save(f"/tmp/ticket10/reframed-{ALTS[i]:.0f}.png")
print("wrote /tmp/ticket10/reframed.png")
