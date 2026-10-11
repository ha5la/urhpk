"""Throwaway: the drone shot on real terrain instead of a flat plane.

Flat ground is a homography, so the shipped renderer inverts it per pixel:
every screen pixel knows its own ground point in closed form. A heightfield has
no such inverse -- a ray can clear one ridge and be stopped by the next -- so
this marches every ray outwards until it goes under the terrain. That also buys
the two things flat ground cannot have at any price: a horizon that is a
ridgeline, and pins that hide behind hills.

DEM is Mapzen/Tilezen terrarium tiles off AWS Open Data (SRTM and friends),
same XYZ scheme as the imagery, so the level machinery is unchanged.
"""
import math, os, sys, time, urllib.request
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/tmp/ticket10"); sys.path.insert(0, "/home/kod/code/urhpk")
from path_view import W, H, FOV, FONT, deg2tile, worked
from twolevel import level
from urhpk import geo

ME, MY_CALL = "JN97TF", "HA5LA"
F = (W / 2) / math.tan(math.radians(FOV) / 2)
R_EARTH = 6371.0
DEM_CACHE = "/tmp/ticket10/tiles-dem"
DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
UA = "urhpk-feasibility-mock/0.1 (amateur radio contest video; one-off)"


# ── DEM ──────────────────────────────────────────────────────────────────
def dem_tile(z, x, y):
    path = f"{DEM_CACHE}/{z}_{x}_{y}.png"
    if not os.path.exists(path):
        os.makedirs(DEM_CACHE, exist_ok=True)
        req = urllib.request.Request(DEM_URL.format(z=z, x=x, y=y), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
    return np.asarray(Image.open(path).convert("RGB"), np.float32)


def dem(lat, lon, half_km, z):
    """Elevation in km around (lat, lon), with km/px and the QTH's pixel."""
    mpp = 156543.03392 * math.cos(math.radians(lat)) / 2 ** z
    half_px = half_km * 1000 / mpp
    cx, cy = deg2tile(lat, lon, z)
    x0, x1 = int(cx - half_px / 256) - 1, int(cx + half_px / 256) + 1
    y0, y1 = int(cy - half_px / 256) - 1, int(cy + half_px / 256) + 1
    n = (x1 - x0 + 1) * (y1 - y0 + 1)
    assert n <= 260, f"{n} DEM tiles at z{z}"
    out = np.zeros(((y1 - y0 + 1) * 256, (x1 - x0 + 1) * 256), np.float32)
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            t = dem_tile(z, tx, ty)
            h = (t[..., 0] * 256 + t[..., 1] + t[..., 2] / 256) - 32768
            out[(ty - y0) * 256:(ty - y0 + 1) * 256, (tx - x0) * 256:(tx - x0 + 1) * 256] = h
    print(f"  DEM z{z}: {out.shape[1]}x{out.shape[0]} px @ {mpp:.0f} m/px, {n} tiles, "
          f"{out.min():.0f}..{out.max():.0f} m")
    # terrarium carries bathymetry; the sea is a surface here, not a hole
    return np.maximum(out, 0.0) / 1000.0, mpp / 1000.0, ((cx - x0) * 256, (cy - y0) * 256)


def sample(tex, u, v):
    """Bilinear gather at flat index arrays, clamped to the edge."""
    h, w = tex.shape[:2]
    u = np.clip(u, 0, w - 1.001)
    v = np.clip(v, 0, h - 1.001)
    u0, v0 = u.astype(np.int32), v.astype(np.int32)
    fu, fv = u - u0, v - v0
    if tex.ndim == 3:
        fu, fv = fu[..., None], fv[..., None]
    a, b = tex[v0, u0], tex[v0, u0 + 1]
    c, d = tex[v0 + 1, u0], tex[v0 + 1, u0 + 1]
    return a * (1 - fu) * (1 - fv) + b * fu * (1 - fv) + c * (1 - fu) * fv + d * fu * fv


# ── camera ───────────────────────────────────────────────────────────────
class Cam:
    def __init__(self, brg, dist, alt=None, back=None):
        self.brg, self.dist = brg, dist
        self.back = 0.45 * dist if back is None else back
        self.alt = 0.38 * dist if alt is None else alt
        self.tilt = (math.atan2(self.alt, self.back)
                     + math.atan2(self.alt, self.back + dist)) / 2

    def to_en(self, right, fwd):
        b = math.radians(self.brg)
        return (right * math.cos(b) + (fwd - self.back) * math.sin(b),
                -right * math.sin(b) + (fwd - self.back) * math.cos(b))

    def project(self, right, fwd, h):
        """Screen position of a point at height h km, and its horizontal range."""
        t = self.tilt
        zw = h - self.alt
        yc = fwd * math.cos(t) - zw * math.sin(t)
        zc = fwd * math.sin(t) + zw * math.cos(t)
        return W / 2 + F * right / yc, H / 2 - F * zc / yc, math.hypot(right, fwd)


# ── the march ────────────────────────────────────────────────────────────
def march(cam, hgt, hk, horg, exag=1.0, curve=False, steps=420):
    """First terrain hit per pixel: (east, north, height, range, hit mask)."""
    px = np.repeat((np.arange(W) - W / 2)[None, :], H, 0).ravel()
    py = np.repeat((np.arange(H) - H / 2)[:, None], W, 1).ravel()
    rx = px
    ry = F * math.cos(cam.tilt) - py * math.sin(cam.tilt)
    rz = -(F * math.sin(cam.tilt) + py * math.cos(cam.tilt))
    rh = np.hypot(rx, ry)
    ux, uy, slope = rx / rh, ry / rh, rz / rh

    far = min(cam.back + cam.dist * 1.9, (min(hgt.shape) / 2 - 2) * hk)
    near = 0.02 * (cam.back + cam.dist)
    q = (far / near) ** (1.0 / steps)

    live = np.arange(H * W)                      # pixels still above ground
    d_hit = np.full(H * W, np.nan, np.float32)
    prev_d = np.full(H * W, near, np.float32)
    prev_gap = None
    d = near
    for _ in range(steps):
        d *= q
        e, n = cam.to_en(ux[live] * d, uy[live] * d)
        g = sample(hgt, horg[0] + e / hk, horg[1] - n / hk) * exag
        if curve:
            g -= d * d / (2 * R_EARTH)
        gap = (cam.alt + slope[live] * d) - g   # ray height above terrain
        if prev_gap is not None:
            struck = gap <= 0
            if struck.any():
                s = struck.nonzero()[0]
                f = prev_gap[s] / np.maximum(prev_gap[s] - gap[s], 1e-9)
                d_hit[live[s]] = prev_d[live[s]] + f * (d - prev_d[live[s]])
                keep = ~struck
                prev_gap, live = gap[keep], live[keep]
                prev_d[live] = d
                if live.size == 0:
                    break
                continue
        prev_gap = gap
        prev_d[live] = d

    hit = np.isfinite(d_hit)
    dh = np.where(hit, d_hit, 0.0)
    e, n = cam.to_en(ux * dh, uy * dh)
    h = sample(hgt, horg[0] + e / hk, horg[1] - n / hk) * exag
    if curve:
        h -= dh * dh / (2 * R_EARTH)
    shp = (H, W)
    return (e.reshape(shp), n.reshape(shp), h.reshape(shp),
            dh.reshape(shp), hit.reshape(shp))


def shade(hgt, hk, horg, e, n, exag, az=315.0, el=45.0):
    """Hillshade from the DEM's own gradient at the hit points."""
    u, v = horg[0] + e / hk, horg[1] - n / hk
    step = 1.0
    dzdx = (sample(hgt, u + step, v) - sample(hgt, u - step, v)) * exag / (2 * step * hk)
    dzdy = (sample(hgt, u, v - step) - sample(hgt, u, v + step)) * exag / (2 * step * hk)
    nx, ny, nz = -dzdx, -dzdy, np.ones_like(dzdx)
    ln = np.sqrt(nx * nx + ny * ny + 1)
    a, z = math.radians(az), math.radians(el)
    lx, ly, lz = math.sin(a) * math.cos(z), math.cos(a) * math.cos(z), math.sin(z)
    return np.clip((nx * lx + ny * ly + nz * lz) / ln, 0, 1)


# ── frame ────────────────────────────────────────────────────────────────
def frame(cam, levels, hgt, hk, horg, dx_call, exag=1.0, curve=False,
          hillshade=0.0, label=None, flat=False):
    e, n, h, rng, hit = march(cam, hgt, hk, horg, 0.0 if flat else exag, curve)
    img = np.empty((H, W, 3), np.float32)
    img[:] = (np.linspace(0.30, 0.68, H)[:, None, None] * np.array([120, 150, 200])
              + np.array([30, 40, 55]))
    for tex, kmpp, org in sorted(levels, key=lambda L: -L[1]):
        u, v = org[0] + e / kmpp, org[1] - n / kmpp
        ok = hit & (u >= 0) & (v >= 0) & (u < tex.shape[1] - 1) & (v < tex.shape[0] - 1)
        img[ok] = sample(tex, u[ok], v[ok])
    if hillshade and not flat:
        sh = shade(hgt, hk, horg, e, n, exag)
        img = np.where(hit[..., None], img * (1 - hillshade + hillshade * 2 * sh[..., None]), img)
    haze = np.clip(np.where(hit, rng, 0) / (cam.dist * 3.2), 0, 0.82)[..., None]
    img = np.where(hit[..., None], img * (1 - haze) + np.array([150, 172, 200]) * haze, img)
    out = Image.fromarray(img.clip(0, 255).astype(np.uint8))

    d = ImageDraw.Draw(out)
    fnt = ImageFont.truetype(FONT, 12)
    for along, call, col in ((cam.back, MY_CALL, (90, 220, 255)),
                             (cam.back + cam.dist, dx_call, (255, 176, 0))):
        pe, pn = cam.to_en(0.0, along)
        ph = 0.0 if flat else float(sample(hgt, np.array([horg[0] + pe / hk]),
                                           np.array([horg[1] - pn / hk]))[0]) * exag
        if curve:
            ph -= along * along / (2 * R_EARTH)
        x, y, prng = cam.project(0.0, along, ph)
        # a pin behind a ridge is behind it: the depth buffer says so
        xi, yi = int(round(x)), int(round(y))
        hidden = (0 <= xi < W and 0 <= yi < H and hit[yi, xi] and rng[yi, xi] < prng * 0.995)
        a = 70 if hidden else 255
        d.line([(x, y - 16), (x, y)], fill=col + (a,), width=2)
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=col + (a,), outline=(20, 20, 20))
        tw = d.textlength(call, font=fnt)
        d.rectangle([x - tw / 2 - 4, y - 34, x + tw / 2 + 4, y - 17], fill=(0, 0, 0))
        d.text((x - tw / 2, y - 33), call, font=fnt, fill=col + (a,))
    if label:
        tw = d.textlength(label, font=fnt)
        d.rectangle([4, 2, 10 + tw, 20], fill=(0, 0, 0))
        d.text((7, 4), label, font=fnt, fill=(255, 255, 160))
    d.text((6, H - 16), "Imagery: Sentinel-2 cloudless (EOX, CC BY 4.0)  ·  "
                        "DEM: Mapzen terrarium (AWS)", font=ImageFont.truetype(FONT, 9),
           fill=(215, 220, 225))
    d.rectangle([0, 0, W - 1, H - 1], outline=(70, 70, 60))
    return out


def sheet(panels, cols=2, path="/tmp/ticket10/terrain.png"):
    rows = (len(panels) + cols - 1) // cols
    s = Image.new("RGB", (cols * W + (cols - 1) * 8, rows * H + (rows - 1) * 8), (0, 0, 0))
    for i, p in enumerate(panels):
        s.paste(p, ((i % cols) * (W + 8), (i // cols) * (H + 8)))
    s.save(path)
    print("wrote", path)
