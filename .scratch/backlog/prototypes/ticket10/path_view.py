"""Throwaway: the drone shot of one path -- my QTH in the foreground, the
worked station down the beam -- on real satellite imagery.

Camera sits behind the QTH along the bearing and frames both ends of the path,
so the shot composes itself from the target's distance alone.
"""
import glob, math, os, sys, urllib.request
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk")
from urhpk import edi, geo

ME = "JN97TF"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
W, H, FOV = 672, 350, 60.0
ZOOM = 10
CACHE = "/tmp/ticket10/tiles"
URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
       "World_Imagery/MapServer/tile/{z}/{y}/{x}")
UA = "urhpk-feasibility-mock/0.1 (amateur radio contest video; one-off, ~50 tiles)"


# ── tiles ────────────────────────────────────────────────────────────────
def deg2tile(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def tile(z, x, y):
    path = f"{CACHE}/{z}_{x}_{y}.jpg"
    if not os.path.exists(path):
        os.makedirs(CACHE, exist_ok=True)
        req = urllib.request.Request(URL.format(z=z, x=x, y=y), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
    return Image.open(path).convert("RGB")


def pick_zoom(lat, half_km, budget_px=2048):
    """The coarsest zoom that still fills a fixed pixel budget, so a long path
    costs the same handful of tiles as a short one."""
    mpp = 2 * half_km * 1000 / budget_px
    z = math.floor(math.log2(156543.03392 * math.cos(math.radians(lat)) / mpp))
    return max(6, min(12, z))


def mosaic(lat, lon, half_km, z=None):
    """Imagery around (lat, lon), plus the km-per-pixel and origin to sample it."""
    z = pick_zoom(lat, half_km) if z is None else z
    mpp = 156543.03392 * math.cos(math.radians(lat)) / 2 ** z
    half_px = half_km * 1000 / mpp
    cx, cy = deg2tile(lat, lon, z)
    x0, x1 = int(cx - half_px / 256) - 1, int(cx + half_px / 256) + 1
    y0, y1 = int(cy - half_px / 256) - 1, int(cy + half_px / 256) + 1
    n = (x1 - x0 + 1) * (y1 - y0 + 1)
    assert n <= 200, f"{n} tiles at z{z} -- too many to ask a public server for"
    out = Image.new("RGB", ((x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256))
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            out.paste(tile(z, tx, ty), ((tx - x0) * 256, (ty - y0) * 256))
    # pixel position of the QTH inside the mosaic
    return np.asarray(out), mpp / 1000.0, ((cx - x0) * 256, (cy - y0) * 256)


# ── camera ───────────────────────────────────────────────────────────────
def render(tex, kmpp, origin_px, brg, dist_km, me_call, dx_call, k=1.0):
    """Frame the whole path: camera behind the QTH, looking down the bearing.

    k=1 pulls the camera back in proportion to the path, so every path composes
    identically and only the label says how far it was. k<1 pulls back less
    than proportionally, so a long path reads as long -- at the price of the
    far end climbing towards the horizon."""
    ref = 47.0
    scale = dist_km if k == 1.0 else ref * (dist_km / ref) ** k
    back, alt = 0.45 * scale, 0.38 * scale
    dep_near = math.atan2(alt, back)                 # depression to the QTH
    dep_far = math.atan2(alt, back + dist_km)        # depression to the target
    tilt = (dep_near + dep_far) / 2
    f = (W / 2) / math.tan(math.radians(FOV) / 2)

    px, py = np.meshgrid(np.arange(W) - W / 2, np.arange(H) - H / 2)
    dy = f * math.cos(tilt) - py * math.sin(tilt)
    dz = -(f * math.sin(tilt) + py * math.cos(tilt))
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(dz < 0, -alt / dz, np.nan)
    fwd, right, rng = dy * s, px * s, None
    rng = np.hypot(fwd, right)
    b = math.radians(brg)
    # camera sits `back` short of the QTH along the bearing
    e = (right * math.cos(b) + (fwd - back) * math.sin(b))
    n = (-right * math.sin(b) + (fwd - back) * math.cos(b))
    u = origin_px[0] + e / kmpp
    v = origin_px[1] - n / kmpp
    ok = (np.isfinite(u) & np.isfinite(v) & (u >= 0) & (v >= 0)
          & (u < tex.shape[1]) & (v < tex.shape[0]))
    img = np.empty((H, W, 3), float)
    sky = np.linspace(0.30, 0.68, H)[:, None, None] * np.array([120, 150, 200])
    img[:] = sky + np.array([30, 40, 55])
    ui, vi = np.where(ok, u, 0).astype(int), np.where(ok, v, 0).astype(int)
    img[ok] = tex[vi[ok], ui[ok]]
    haze = np.clip(np.where(ok, rng, 0) / (dist_km * 3.2), 0, 0.82)[..., None]
    img = np.where(ok[..., None], img * (1 - haze) + np.array([150, 172, 200]) * haze, img)
    out = Image.fromarray(img.clip(0, 255).astype(np.uint8))

    d = ImageDraw.Draw(out)
    fnt = ImageFont.truetype(FONT, 12)

    def project(along, across=0.0):
        # camera at altitude alt, pitched down by tilt; the ground point is
        # alt below it, so `up` in camera frame is along*sin - alt*cos.
        dyc = along * math.cos(tilt) + alt * math.sin(tilt)
        dzc = along * math.sin(tilt) - alt * math.cos(tilt)
        return W / 2 + f * across / dyc, H / 2 - f * dzc / dyc

    x_me, y_me = project(back)
    x_dx, y_dx = project(back + dist_km)
    d.line([(x_me, y_me), (x_dx, y_dx)], fill=(255, 176, 0, 90), width=1)
    for (x, y), call, col in (((x_me, y_me), me_call, (90, 220, 255)),
                              ((x_dx, y_dx), dx_call, (255, 176, 0))):
        d.line([(x, y - 16), (x, y)], fill=col, width=2)
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=col, outline=(20, 20, 20))
        t = call
        tw = d.textlength(t, font=fnt)
        d.rectangle([x - tw / 2 - 4, y - 34, x + tw / 2 + 4, y - 17], fill=(0, 0, 0))
        d.text((x - tw / 2, y - 33), t, font=fnt, fill=col)
    d.text((6, 4), f"{me_call} → {dx_call}   {dist_km:.0f} km   {brg:.0f}°",
           font=fnt, fill=(235, 240, 235))
    d.text((6, H - 16), "Imagery: Esri World Imagery", font=ImageFont.truetype(FONT, 9),
           fill=(210, 215, 220))
    d.rectangle([0, 0, W - 1, H - 1], outline=(70, 70, 60))
    return out


def worked():
    seen = {}
    for fp in sorted(glob.glob("/home/kod/code/urhpk/2026-sep/*.edi")):
        for r in edi.read(fp).records:
            loc = getattr(r, "loc", None) or getattr(r, "wwl", None)
            if loc:
                seen[r.callsign] = loc
    return seen


if __name__ == "__main__":
    locs = worked()
    lat0, lon0 = geo.maidenhead_to_latlon(ME)
    targets = sys.argv[1:] or ["HG5O"]
    panels = []
    for call in targets:
        loc = locs[call]
        brg = geo.bearing_between(ME, loc)
        dist = geo.distance_between(ME, loc)
        half = max(60.0, dist * 1.9)
        tex, kmpp, org = mosaic(lat0, lon0, half_km=half)
        panels.append(render(tex, kmpp, org, brg, dist, "HA5LA", call,
                             k=float(os.environ.get("K", "1.0"))))
        print(f"{call} {loc}  {dist:.0f} km  {brg:.0f}deg  "
              f"mosaic {tex.shape[1]}x{tex.shape[0]} px @ {kmpp*1000:.0f} m/px")
    sheet = Image.new("RGB", (W, len(panels) * (H + 8) - 8), (0, 0, 0))
    for i, pnl in enumerate(panels):
        sheet.paste(pnl, (0, i * (H + 8)))
    sheet.save(os.environ.get("OUT", "/tmp/ticket10/path.png"))
