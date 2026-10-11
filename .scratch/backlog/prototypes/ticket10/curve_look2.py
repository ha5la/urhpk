"""Curvature made legible: the far band magnified, and the horizon it creates."""
import math, sys
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
from urhpk import qth_map as Q
from urhpk.cast_render import CAST_FONT_PATH
from curve_look import panel, ME, MY_CALL, W, H
from curve_bench import R

CALL, BRG, DIST = "HA1VHF", 271.0, 233.0
pyr = Q.build_pyramids(ME, [Q.Target(0, 1, CALL, BRG, DIST)])
fnt = ImageFont.truetype(CAST_FONT_PATH, 13)


def labelled_pair(a, b, tag_a, tag_b, scale=1):
    w, h = a.size
    out = Image.new("RGB", (w * 2 * scale + 8, h * scale + 22), (0, 0, 0))
    for i, (im, tag) in enumerate(((a, tag_a), (b, tag_b))):
        out.paste(im.resize((w * scale, h * scale), Image.LANCZOS), (i * (w * scale + 8), 22))
        ImageDraw.Draw(out).text((i * (w * scale + 8) + 6, 4), tag, font=fnt,
                                 fill=(255, 255, 160))
    return out


# 1. the shipped camera, far band magnified 2x -- where the 36 px lives
flat = panel(pyr, BRG, DIST, CALL, False, "")
curv = panel(pyr, BRG, DIST, CALL, True, "")
band = (0, 20, W, 130)
zoom = labelled_pair(flat.crop(band), curv.crop(band),
                     "FLAT EARTH — far third, 2x", "WITH CURVATURE — far third, 2x", scale=2)

# 2. drop the camera until the horizon falls inside the mosaic: alt 10 km puts
#    it at 357 km, and the imagery reaches 443 km.
low = Q.Camera(BRG, DIST, Q.BACK_FRAC * DIST, 10.0,
               (math.atan2(10.0, Q.BACK_FRAC * DIST)
                + math.atan2(10.0, Q.BACK_FRAC * DIST + DIST)) / 2)
print(f"low camera: alt {low.alt_km} km, tilt {math.degrees(low.tilt):.2f}deg, "
      f"true horizon {math.sqrt(2*low.alt_km*R):.0f} km, mosaic reaches {DIST*1.9:.0f} km")
orig = Q.camera_for
Q.camera_for = lambda b, d: low
lf = panel(pyr, BRG, DIST, CALL, False, "")
lc = panel(pyr, BRG, DIST, CALL, True, "")
Q.camera_for = orig
horizon = labelled_pair(lf, lc, "FLAT EARTH — ground runs off the top edge",
                        "WITH CURVATURE — a real horizon at 357 km")

sheet = Image.new("RGB", (max(zoom.width, horizon.width), zoom.height + horizon.height + 8),
                  (0, 0, 0))
sheet.paste(zoom, (0, 0))
sheet.paste(horizon, (0, zoom.height + 8))
sheet.save("/tmp/ticket10/curvature-look2.png")
zoom.save("/tmp/ticket10/curvature-zoom.png")
horizon.save("/tmp/ticket10/curvature-horizon.png")
print("wrote /tmp/ticket10/curvature-look2.png")
