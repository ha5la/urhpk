"""Throwaway: does a rotating plan-position map read at the real panel size?"""
import glob, math, sys
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk")
from urhpk import edi, geo

ME = "JN97TF"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

def stations():
    seen = {}
    for f in sorted(glob.glob("/home/kod/code/urhpk/2026-sep/*.edi")):
        for r in edi.read(f).records:
            loc = getattr(r, "loc", None) or getattr(r, "wwl", None)
            if not loc: continue
            b = geo.bearing_between(ME, loc); d = geo.distance_between(ME, loc)
            if b is not None: seen[r.callsign] = (b, d)
    return sorted(seen.items(), key=lambda kv: kv[1][1])

def draw_panel(w, h, az, sts, compress, label=True):
    img = Image.new("RGB", (w, h), (14, 16, 14))
    d = ImageDraw.Draw(img)
    cx, cy = w / 2, h / 2
    R = min(w, h) / 2 - 14
    dmax = max(d_ for _, (b_, d_) in sts) * 1.1
    def radius(km):
        f = km / dmax
        return R * (math.sqrt(f) if compress else f)
    f_small = ImageFont.truetype(FONT, 11)
    f_ring = ImageFont.truetype(FONT, 10)
    # range rings
    for km in (50, 100, 200, 400):
        if km > dmax: continue
        r = radius(km)
        d.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(40, 70, 40))
        d.text((cx + 3, cy - r - 11), f"{km}", font=f_ring, fill=(55, 95, 55))
    # cardinal letters, rotated with the map (azimuth up)
    for ang, ch in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        a = math.radians(ang - az)
        d.text((cx + (R + 4) * math.sin(a) - 4, cy - (R + 4) * math.cos(a) - 7),
               ch, font=f_small, fill=(120, 160, 120))
    # beam: azimuth is up by construction
    d.polygon([(cx, cy), (cx - R*0.13, cy - R), (cx + R*0.13, cy - R)], fill=(60, 30, 0))
    # stations
    for call, (b, km) in sts:
        a = math.radians(b - az)
        r = radius(km)
        x, y = cx + r * math.sin(a), cy - r * math.cos(a)
        d.ellipse([x-3, y-3, x+3, y+3], fill=(255, 176, 0))
        if label:
            d.text((x + 5, y - 6), call, font=f_small, fill=(210, 210, 190))
    d.rectangle([0, 0, w-1, h-1], outline=(70, 70, 60))
    return img

sts = stations()
az = 307
sheet = Image.new("RGB", (672*2 + 30, 350*2 + 30), (0, 0, 0))
sheet.paste(draw_panel(672, 350, az, sts, compress=False), (0, 0))
sheet.paste(draw_panel(672, 350, az, sts, compress=True), (672+30, 0))
sheet.paste(draw_panel(350, 350, az, sts, compress=True), (0, 350+30))
sheet.paste(draw_panel(672, 350, az, sts, compress=True, label=False), (672+30, 350+30))
sheet.save("/tmp/ticket10/sheet.png")
print("stations:", [(c, round(b), round(k)) for c, (b, k) in sts])
