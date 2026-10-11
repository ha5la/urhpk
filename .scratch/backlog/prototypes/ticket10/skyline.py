"""The horizon shot, with the three things it was missing.

Supersampling, because the horizon is a hard per-pixel boolean and jaggy.
Terrain, because past the horizon a mountain is the only thing left to see.
Borders, because at 845 km of visible ground the picture needs a map in it.
"""
import math, struct, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
from urhpk import qth_map as Q
from urhpk.cast_render import CAST_FONT_PATH
from urhpk.geo import maidenhead_to_latlon
from terrain import dem, sample
from reframe import solve

R = 6371.0
ME, MY_CALL = "JN97TF", "HA5LA"
W, H, FOV = 672, 350, 60.0
ALT = 70.0
SHP = ("/home/kod/.local/share/cartopy/shapefiles/natural_earth/"
       "cultural/ne_50m_admin_0_countries.shp")
LAT0, LON0 = maidenhead_to_latlon(ME)
TILT, BACK = solve(ALT)
HORIZON = math.sqrt(2 * ALT * R)


# ── ground ───────────────────────────────────────────────────────────────
def rays(w, h):
    f = (w / 2) / math.tan(math.radians(FOV) / 2)
    px = np.repeat((np.arange(w) - w / 2)[None, :], h, 0).ravel()
    py = np.repeat((np.arange(h) - h / 2)[:, None], w, 1).ravel()
    ry = f * math.cos(TILT) - py * math.sin(TILT)
    rz = -(f * math.sin(TILT) + py * math.cos(TILT))
    rh = np.hypot(px, ry)
    return px / rh, ry / rh, rz / rh


def to_en(right, fwd, brg):
    b = math.radians(brg)
    return (right * math.cos(b) + (fwd - BACK) * math.sin(b),
            -right * math.sin(b) + (fwd - BACK) * math.cos(b))


def ground_smooth(w, h, brg):
    """Curved earth, no terrain: the ray meets z = -d^2/2R in closed form."""
    ux, uy, slope = rays(w, h)
    disc = slope * slope - 2 * ALT / R
    hit = (slope < 0) & (disc >= 0)
    d = np.where(hit, R * (-slope - np.sqrt(np.abs(disc))), 0.0)
    e, n = to_en(ux * d, uy * d, brg)
    return e.reshape(h, w), n.reshape(h, w), d.reshape(h, w), hit.reshape(h, w)


def ground_terrain(w, h, brg, hgt, hk, horg, far, steps=460):
    """Same camera, but the ray is marched against the heightfield."""
    ux, uy, slope = rays(w, h)
    near = 0.02 * (BACK + 200)
    q = (far / near) ** (1.0 / steps)
    live = np.arange(w * h)
    d_hit = np.full(w * h, np.nan, np.float32)
    prev_d = np.full(w * h, near, np.float32)
    prev_gap, d = None, near
    for _ in range(steps):
        d *= q
        e, n = to_en(ux[live] * d, uy[live] * d, brg)
        g = sample(hgt, horg[0] + e / hk, horg[1] - n / hk) - d * d / (2 * R)
        gap = (ALT + slope[live] * d) - g
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
    e, n = to_en(ux * dh, uy * dh, brg)
    return e.reshape(h, w), n.reshape(h, w), dh.reshape(h, w), hit.reshape(h, w)


def shade_ground(e, n, d, hit, levels, w, h, haze_km=330.0):
    img = np.empty((h, w, 3), np.float32)
    img[:] = (np.linspace(0.14, 0.62, h)[:, None, None] * np.array([110, 145, 205])
              + np.array([26, 38, 62]))
    for tex, kmpp, org in sorted(levels, key=lambda L: -L[1]):
        u, v = org[0] + e / kmpp, org[1] - n / kmpp
        ok = hit & (u >= 0) & (v >= 0) & (u < tex.shape[1] - 1) & (v < tex.shape[0] - 1)
        img[ok] = sample(tex, u[ok], v[ok])
    haze = np.where(hit, 1 - np.exp(-d / haze_km), 0.0).clip(0, 0.90)[..., None]
    return np.where(hit[..., None], img * (1 - haze) + np.array([168, 186, 210]) * haze, img)


# ── borders ──────────────────────────────────────────────────────────────
def rings(bbox):
    west, south, east, north = bbox
    data = open(SHP, "rb").read()
    out, off = [], 100
    while off < len(data):
        _, clen = struct.unpack_from(">ii", data, off)
        rec, off = off + 8, off + 8 + clen * 2
        if struct.unpack_from("<i", data, rec)[0] != 5:
            continue
        xmin, ymin, xmax, ymax = struct.unpack_from("<4d", data, rec + 4)
        if xmax < west or xmin > east or ymax < south or ymin > north:
            continue
        nparts, npoints = struct.unpack_from("<ii", data, rec + 36)
        parts = struct.unpack_from(f"<{nparts}i", data, rec + 44)
        base = rec + 44 + nparts * 4
        for i, start in enumerate(parts):
            end = parts[i + 1] if i + 1 < nparts else npoints
            pts = np.frombuffer(data, "<f8", (end - start) * 2, base + start * 16)
            out.append(pts.reshape(-1, 2))
    return out


def lonlat_to_en(lon, lat):
    """Into the same Mercator-relative km the mosaics are sampled in."""
    k = 156543.03392 * math.cos(math.radians(LAT0)) / 1000.0   # km per 'pixel' at z0/256
    def xy(lo, la):
        x = (np.asarray(lo) + 180.0) / 360.0
        y = (1 - np.arcsinh(np.tan(np.radians(np.asarray(la)))) / math.pi) / 2
        return x, y
    x0, y0 = xy(LON0, LAT0)
    x, y = xy(lon, lat)
    span = k * 256                                              # km across the world
    return (x - x0) * span, (y0 - y) * span


def project_en(e, n, brg, w, h):
    f = (w / 2) / math.tan(math.radians(FOV) / 2)
    b = math.radians(brg)
    along = e * math.sin(b) + n * math.cos(b) + BACK
    across = e * math.cos(b) - n * math.sin(b)
    d = np.hypot(along, across)
    zw = -d * d / (2 * R) - ALT
    yc = along * math.cos(TILT) - zw * math.sin(TILT)
    zc = along * math.sin(TILT) + zw * math.cos(TILT)
    with np.errstate(divide="ignore", invalid="ignore"):
        x = w / 2 + f * across / yc
        y = h / 2 - f * zc / yc
    return x, y, d, yc > 0.5


def draw_borders(draw, brg, w, h, scale):
    span = 14.0                                  # degrees around the QTH, generous
    for ring in rings((LON0 - span * 1.6, LAT0 - span, LON0 + span * 1.6, LAT0 + span)):
        e, n = lonlat_to_en(ring[:, 0], ring[:, 1])
        x, y, d, front = project_en(e, n, brg, w, h)
        vis = front & (d <= HORIZON) & np.isfinite(x) & np.isfinite(y)
        run = []
        for i in range(len(x)):
            if vis[i] and -w < x[i] < 2 * w and -h < y[i] < 2 * h:
                run.append((x[i], y[i]))
            else:
                if len(run) > 1:
                    draw.line(run, fill=(255, 245, 210, 150), width=scale)
                run = []
        if len(run) > 1:
            draw.line(run, fill=(255, 245, 210, 150), width=scale)


# ── frame ────────────────────────────────────────────────────────────────
def render(brg, levels, ss=1, terrain=None, borders=False, tag="", ground=None,
           haze_km=330.0, alt=None, tilt=None, back=None):
    w, h = W * ss, H * ss
    t0 = time.perf_counter()
    if ground is not None:
        e, n, d, hit = ground
    elif terrain is None:
        e, n, d, hit = ground_smooth(w, h, brg)
    else:
        hgt, hk, horg, far = terrain
        e, n, d, hit = ground_terrain(w, h, brg, hgt, hk, horg, far)
    render.last_ground = (e, n, d, hit)
    img = shade_ground(e, n, d, hit, levels, w, h, haze_km)
    big = Image.fromarray(img.clip(0, 255).astype(np.uint8)).convert("RGBA")
    if borders:
        ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw_borders(ImageDraw.Draw(ov), brg, w, h, max(1, ss))
        big = Image.alpha_composite(big, ov)
    out = big.resize((W, H), Image.BOX).convert("RGB")
    d_ = ImageDraw.Draw(out)
    f12 = ImageFont.truetype(CAST_FONT_PATH, 12)
    if tag:
        tw = d_.textlength(tag, font=f12)
        d_.rectangle([4, 2, 10 + tw, 20], fill=(0, 0, 0))
        d_.text((7, 4), tag, font=f12, fill=(255, 255, 160))
    d_.rectangle([0, 0, W - 1, H - 1], outline=(70, 70, 60))
    print(f"  {tag or 'frame'}: {time.perf_counter()-t0:.1f} s")
    return out
