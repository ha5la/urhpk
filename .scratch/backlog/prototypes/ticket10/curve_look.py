"""What the shipped map panel looks like with earth curvature in it.

Real Sentinel-2 tiles, the real qth_map renderer; the only change is that the
ray meets z = -d^2/2R instead of z = 0, and the pins sit on that surface.
"""
import math, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
from urhpk import qth_map as Q
from urhpk.cast_render import CAST_FONT_PATH
from curve_bench import ground_curved, R

ME, MY_CALL, W, H = "JN97TF", "HA5LA", 672, 350
flat_ground, flat_project = Q._ground, Q.project


def project_curved(cam, bearing, distance_km, w, h):
    """Same as the shipped project, but the point sits on the curved ground."""
    rel = math.radians(bearing - cam.bearing)
    along = distance_km * math.cos(rel) + cam.back_km
    across = distance_km * math.sin(rel)
    d = math.hypot(along, across)
    z = -d * d / (2 * R)                       # below the tangent plane
    depth = along * math.cos(cam.tilt) + (cam.alt_km - z) * math.sin(cam.tilt)
    if depth <= 0.5:
        return None
    up = along * math.sin(cam.tilt) - (cam.alt_km - z) * math.cos(cam.tilt)
    return w / 2 + Q._focal(w) * across / depth, h / 2 - Q._focal(w) * up / depth


def panel(pyr, brg, dist, call, curved, tag):
    Q._ground = ground_curved if curved else flat_ground
    Q.project = project_curved if curved else flat_project
    tgt = Q.Target(0.0, 1.0, call, brg, dist)
    shot = Q.Shot(Q.camera_for(brg, dist), tgt, None, 1.0)
    t0 = time.perf_counter()
    im = Q.draw_map_frame(pyr, shot, W, H, MY_CALL).convert("RGB")
    Q._ground, Q.project = flat_ground, flat_project
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(CAST_FONT_PATH, 12)
    tw = d.textlength(tag, font=f)
    d.rectangle([4, 2, 10 + tw, 20], fill=(0, 0, 0))
    d.text((7, 4), tag, font=f, fill=(255, 255, 160))
    d.rectangle([0, 0, W - 1, H - 1], outline=(70, 70, 60))
    print(f"  {tag}: {(time.perf_counter()-t0)*1000:.0f} ms")
    return im


if __name__ == "__main__":
    paths = [("HA3KHB", 251.0, 168.0), ("HA1VHF", 271.0, 233.0)]
    pyr = Q.build_pyramids(ME, [Q.Target(0, 1, c, b, d) for c, b, d in paths])
    rows = []
    for call, brg, dist in paths:
        print(f"{call} {dist:.0f} km, camera alt {Q.camera_for(brg, dist).alt_km:.1f} km")
        rows.append((panel(pyr, brg, dist, call, False,
                           f"{call} {dist:.0f} km — FLAT EARTH (shipped)"),
                     panel(pyr, brg, dist, call, True,
                           f"{call} {dist:.0f} km — WITH CURVATURE")))
    sheet = Image.new("RGB", (W * 2 + 8, H * 2 + 8), (0, 0, 0))
    for i, (a, b) in enumerate(rows):
        sheet.paste(a, (0, i * (H + 8)))
        sheet.paste(b, (W + 8, i * (H + 8)))
    sheet.save("/tmp/ticket10/curvature-look.png")
    for i, (a, b) in enumerate(rows):
        s = Image.new("RGB", (W * 2 + 8, H), (0, 0, 0))
        s.paste(a, (0, 0)); s.paste(b, (W + 8, 0))
        s.save(f"/tmp/ticket10/cl-row{i}.png")
    print("wrote /tmp/ticket10/curvature-look.png")
