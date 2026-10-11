"""Throwaway: the one place terrain is actually visible -- the skyline."""
import sys
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/tmp/ticket10"); sys.path.insert(0, "/home/kod/code/urhpk")
from terrain import *
from twolevel import level

lat0, lon0 = geo.maidenhead_to_latlon(ME)
hgt, hk, horg = dem(lat0, lon0, half_km=130, z=10)
lv = [level(lat0, lon0, half_km=130, z=9), level(lat0, lon0, half_km=40, z=11)]
cam = Cam(30, 80, alt=1.5, back=10)
a = frame(cam, lv, hgt, hk, horg, "DX", curve=True, flat=True)
b = frame(cam, lv, hgt, hk, horg, "DX", curve=True, exag=1.0, hillshade=0.30)
band = (0, 100, W, 190)
BH = (band[3] - band[1]) * 2
out = Image.new("RGB", (W * 2, BH * 2 + 44), (0, 0, 0))
fnt = ImageFont.truetype(FONT, 13)
d = ImageDraw.Draw(out)
for i, (im, tag) in enumerate(((a, "FLAT — the horizon is a ruled line"),
                               (b, "TERRAIN — the Mátra breaks the skyline"))):
    out.paste(im.crop(band).resize((W * 2, (band[3] - band[1]) * 2), Image.LANCZOS),
              (0, i * (BH + 22) + 22))
    d.text((8, i * (BH + 22) + 3), tag, font=fnt, fill=(255, 255, 160))
out.save("/tmp/ticket10/terrain-skyline.png")
print("wrote /tmp/ticket10/terrain-skyline.png")
