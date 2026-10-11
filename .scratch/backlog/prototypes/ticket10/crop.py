"""Throwaway: the same distant ground, filtered three ways."""
import sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/tmp/ticket10"); sys.path.insert(0, "/home/kod/code/urhpk")
from path_view import FONT, worked
from twolevel import level
from aniso import mips, render
from urhpk import geo

ME = "JN97TF"
BOX = (250, 60, 430, 150)   # a strip of distant ground, x0,y0,x1,y1
ZOOM = 4

locs = worked()
lat0, lon0 = geo.maidenhead_to_latlon(ME)
lv = [mips(*level(lat0, lon0, half_km=geo.distance_between(ME, locs["HA3KHB"]) * 1.9, z=8)),
      mips(*level(lat0, lon0, half_km=50, z=11))]
brg = geo.bearing_between(ME, locs["HG5O"])
dist = geo.distance_between(ME, locs["HG5O"])

tiles = []
for label, mode, taps in (("bilinear (what flickers)", "bilinear", 1),
                          ("anisotropic, 4 taps", "aniso", 4),
                          ("anisotropic, 8 taps", "aniso", 8)):
    img, _ = render(lv, brg, dist, mode, taps)
    crop = Image.fromarray(img.clip(0, 255).astype(np.uint8)).crop(BOX)
    crop = crop.resize((crop.width * ZOOM, crop.height * ZOOM), Image.NEAREST)
    d = ImageDraw.Draw(crop)
    f = ImageFont.truetype(FONT, 14)
    d.rectangle([0, 0, d.textlength(label, font=f) + 8, 20], fill=(0, 0, 0))
    d.text((4, 3), label, font=f, fill=(255, 255, 160))
    tiles.append(crop)

w, h = tiles[0].size
sheet = Image.new("RGB", (w, h * len(tiles) + 8 * (len(tiles) - 1)), (0, 0, 0))
for i, t in enumerate(tiles):
    sheet.paste(t, (0, i * (h + 8)))
sheet.save("/tmp/ticket10/crop.png")
print("wrote /tmp/ticket10/crop.png", sheet.size)
