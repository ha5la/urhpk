"""Throwaway: two texture levels instead of one, on Sentinel-2 cloudless.

A single mosaic has to cover the far end of the path, which sets its texel
size everywhere -- including the near ground at the bottom of the frame, which
is where the camera has the most resolution to spend. Two levels: a fine, small
one for the near ground and a coarse, wide one for everything beyond it.
"""
import math, os, sys, urllib.request
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/tmp/ticket10"); sys.path.insert(0, "/home/kod/code/urhpk")
from path_view import W, H, FOV, FONT, deg2tile, worked
from urhpk import geo

ME, MY_CALL = "JN97TF", "HA5LA"
CACHE = "/tmp/ticket10/tiles-s2"
URL = ("https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/"
       "default/GoogleMapsCompatible/{z}/{y}/{x}.jpg")
UA = "urhpk-feasibility-mock/0.1 (amateur radio contest video; one-off)"


def tile(z, x, y):
    path = f"{CACHE}/{z}_{x}_{y}.jpg"
    if not os.path.exists(path):
        os.makedirs(CACHE, exist_ok=True)
        req = urllib.request.Request(URL.format(z=z, x=x, y=y), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
    return Image.open(path).convert("RGB")


def level(lat, lon, half_km, z):
    """One texture level: pixels, km per pixel, and where the QTH sits in it."""
    mpp = 156543.03392 * math.cos(math.radians(lat)) / 2 ** z
    half_px = half_km * 1000 / mpp
    cx, cy = deg2tile(lat, lon, z)
    x0, x1 = int(cx - half_px / 256) - 1, int(cx + half_px / 256) + 1
    y0, y1 = int(cy - half_px / 256) - 1, int(cy + half_px / 256) + 1
    n = (x1 - x0 + 1) * (y1 - y0 + 1)
    assert n <= 200, f"{n} tiles at z{z}"
    out = Image.new("RGB", ((x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256))
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            out.paste(tile(z, tx, ty), ((tx - x0) * 256, (ty - y0) * 256))
    print(f"  z{z}: {out.width}x{out.height} px @ {mpp:.0f} m/px, {n} tiles")
    return np.asarray(out), mpp / 1000.0, ((cx - x0) * 256, (cy - y0) * 256)


def render(levels, brg, dist, dx_call):
    back, alt = 0.45 * dist, 0.38 * dist
    tilt = (math.atan2(alt, back) + math.atan2(alt, back + dist)) / 2
    f = (W / 2) / math.tan(math.radians(FOV) / 2)
    px, py = np.meshgrid(np.arange(W) - W / 2, np.arange(H) - H / 2)
    dy = f * math.cos(tilt) - py * math.sin(tilt)
    dz = -(f * math.sin(tilt) + py * math.cos(tilt))
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(dz < 0, -alt / dz, np.nan)
    fwd, right = dy * s, px * s
    rng = np.hypot(fwd, right)
    b = math.radians(brg)
    e = right * math.cos(b) + (fwd - back) * math.sin(b)
    n = -right * math.sin(b) + (fwd - back) * math.cos(b)

    img = np.empty((H, W, 3), float)
    img[:] = np.linspace(0.30, 0.68, H)[:, None, None] * np.array([120, 150, 200]) + np.array([30, 40, 55])
    covered = np.zeros((H, W), bool)
    # Coarsest first, so each finer level paints over it where it reaches.
    for tex, kmpp, org in sorted(levels, key=lambda L: -L[1]):
        u, v = org[0] + e / kmpp, org[1] - n / kmpp
        ok = (np.isfinite(u) & np.isfinite(v) & (u >= 0) & (v >= 0)
              & (u < tex.shape[1]) & (v < tex.shape[0]))
        ui, vi = np.where(ok, u, 0).astype(int), np.where(ok, v, 0).astype(int)
        img[ok] = tex[vi[ok], ui[ok]]
        covered |= ok
    haze = np.clip(np.where(covered, rng, 0) / (dist * 3.2), 0, 0.82)[..., None]
    img = np.where(covered[..., None], img * (1 - haze) + np.array([150, 172, 200]) * haze, img)
    out = Image.fromarray(img.clip(0, 255).astype(np.uint8))

    d = ImageDraw.Draw(out)
    fnt = ImageFont.truetype(FONT, 12)

    def project(along):
        dyc = along * math.cos(tilt) + alt * math.sin(tilt)
        dzc = along * math.sin(tilt) - alt * math.cos(tilt)
        return W / 2, H / 2 - f * dzc / dyc

    for along, call, col in ((back, MY_CALL, (90, 220, 255)),
                             (back + dist, dx_call, (255, 176, 0))):
        x, y = project(along)
        d.line([(x, y - 16), (x, y)], fill=col, width=2)
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=col, outline=(20, 20, 20))
        tw = d.textlength(call, font=fnt)
        d.rectangle([x - tw / 2 - 4, y - 34, x + tw / 2 + 4, y - 17], fill=(0, 0, 0))
        d.text((x - tw / 2, y - 33), call, font=fnt, fill=col)
    d.text((6, H - 16), "Imagery: Sentinel-2 cloudless by EOX (CC BY 4.0)",
           font=ImageFont.truetype(FONT, 9), fill=(215, 220, 225))
    d.rectangle([0, 0, W - 1, H - 1], outline=(70, 70, 60))
    return out


if __name__ == "__main__":
    locs = worked()
    lat0, lon0 = geo.maidenhead_to_latlon(ME)
    far = geo.distance_between(ME, locs["HA3KHB"])       # the swing's wider end
    print("coarse level (has to cover the 168 km path):")
    coarse = level(lat0, lon0, half_km=far * 1.9, z=8)
    print("fine level (near ground only):")
    fine = level(lat0, lon0, half_km=50, z=11)

    brg = geo.bearing_between(ME, locs["HG5O"])
    dist = geo.distance_between(ME, locs["HG5O"])
    a = render([coarse], brg, dist, "HG5O")
    b = render([coarse, fine], brg, dist, "HG5O")
    sheet = Image.new("RGB", (W, H * 2 + 8), (0, 0, 0))
    sheet.paste(a, (0, 0)); sheet.paste(b, (0, H + 8))
    d = ImageDraw.Draw(sheet)
    fnt = ImageFont.truetype(FONT, 12)
    d.text((6, 4), "one level, 415 m/px", font=fnt, fill=(255, 255, 160))
    d.text((6, H + 12), "two levels, 52 m/px near + 415 m/px far", font=fnt, fill=(255, 255, 160))
    sheet.save("/tmp/ticket10/twolevel.png")
    print("wrote /tmp/ticket10/twolevel.png")
