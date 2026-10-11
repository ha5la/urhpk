"""Throwaway: a rotating country-outline basemap under the pins, at real size."""
import glob, math, struct, sys
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk")
from urhpk import edi, geo

SHP = "/home/kod/.local/share/cartopy/shapefiles/natural_earth/cultural/ne_50m_admin_0_countries.shp"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
ME = "JN97TF"
LAT0, LON0 = 47.4375, 19.2083  # JN97TF centre


def read_polylines(path, bbox):
    """Every polygon ring in the shapefile that touches bbox, as lon/lat lists."""
    W, S, E, N = bbox
    data = open(path, "rb").read()
    out, off = [], 100
    while off < len(data):
        _, clen = struct.unpack_from(">ii", data, off)
        rec, off = off + 8, off + 8 + clen * 2
        if struct.unpack_from("<i", data, rec)[0] != 5:  # polygon only
            continue
        xmin, ymin, xmax, ymax = struct.unpack_from("<4d", data, rec + 4)
        if xmax < W or xmin > E or ymax < S or ymin > N:
            continue
        nparts, npoints = struct.unpack_from("<ii", data, rec + 36)
        parts = struct.unpack_from(f"<{nparts}i", data, rec + 44)
        pbase = rec + 44 + nparts * 4
        for i, start in enumerate(parts):
            end = parts[i + 1] if i + 1 < nparts else npoints
            ring = struct.unpack_from(f"<{2 * (end - start)}d", data, pbase + start * 16)
            out.append(list(zip(ring[0::2], ring[1::2])))
    return out


def stations():
    seen = {}
    for f in sorted(glob.glob("/home/kod/code/urhpk/2026-sep/*.edi")):
        for r in edi.read(f).records:
            loc = getattr(r, "loc", None) or getattr(r, "wwl", None)
            if not loc:
                continue
            b, d = geo.bearing_between(ME, loc), geo.distance_between(ME, loc)
            if b is not None:
                seen[r.callsign] = (b, d)
    return sorted(seen.items(), key=lambda kv: kv[1][1])


def panel(w, h, az, sts, span_km=520, compress=True):
    img = Image.new("RGB", (w, h), (10, 14, 18))
    d = ImageDraw.Draw(img)
    cx, cy, R = w / 2, h / 2, min(w, h) / 2 - 14
    kmdeg_lat, kmdeg_lon = 111.32, 111.32 * math.cos(math.radians(LAT0))
    dmax = span_km / 2

    def project(lon, lat):
        """North-up km from the QTH, then rotated so the azimuth points up."""
        e, n = (lon - LON0) * kmdeg_lon, (lat - LAT0) * kmdeg_lat
        rng = math.hypot(e, n)
        if rng > dmax * 1.6:
            return None
        a = math.atan2(e, n) - math.radians(az)
        f = min(rng / dmax, 1.6)
        r = R * (math.sqrt(f) if compress else f)
        return cx + r * math.sin(a), cy - r * math.cos(a)

    pad = 4.0
    for ring in read_polylines(SHP, (LON0 - pad * 1.5, LAT0 - pad, LON0 + pad * 1.5, LAT0 + pad)):
        pts = [p for p in (project(lon, lat) for lon, lat in ring) if p]
        if len(pts) > 1:
            d.line(pts, fill=(46, 78, 96), width=1)

    for km in (50, 100, 200, 400):
        r = R * math.sqrt(km / dmax) if compress else R * km / dmax
        if r > R:
            continue
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(34, 58, 40))
    d.polygon([(cx, cy), (cx - R * 0.12, cy - R), (cx + R * 0.12, cy - R)], fill=(70, 36, 0))
    f = ImageFont.truetype(FONT, 11)
    for ang, ch in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        a = math.radians(ang - az)
        d.text((cx + (R + 3) * math.sin(a) - 4, cy - (R + 3) * math.cos(a) - 7), ch,
               font=f, fill=(120, 160, 120))
    for call, (b, km) in sts:
        a = math.radians(b - az)
        r = R * (math.sqrt(km / dmax) if compress else km / dmax)
        x, y = cx + r * math.sin(a), cy - r * math.cos(a)
        d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 176, 0))
        d.text((x + 5, y - 6), call, font=f, fill=(225, 225, 205))
    d.rectangle([0, 0, w - 1, h - 1], outline=(70, 70, 60))
    return img


sts = stations()
sheet = Image.new("RGB", (672 + 360 + 30, 708), (0, 0, 0))
sheet.paste(panel(672, 350, 307, sts), (0, 0))
sheet.paste(panel(672, 350, 30, sts), (0, 358))
sheet.paste(panel(350, 350, 307, sts), (702, 0))
sheet.paste(panel(350, 350, 307, sts, compress=False), (702, 358))
sheet.save("/tmp/ticket10/basemap.png")
print("ok")
