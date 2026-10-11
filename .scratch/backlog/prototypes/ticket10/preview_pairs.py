"""What the three features would do to the panel as it ships today.

Left column is the production renderer untouched. Right column is the same
camera with curvature, 3x supersampling and country borders. The camera is
NOT reframed -- that is a separate decision -- so the horizon is still 1062 km
away and out of shot; what changes here is the ground's placement, the edge
quality and the map furniture.
"""
import math, struct, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
from urhpk import qth_map as Q
from urhpk.cast_render import CAST_FONT_PATH
from urhpk.geo import maidenhead_to_latlon
from curve_bench import ground_curved, R
from curve_look import project_curved

ME, MY_CALL, W, H, SS = "JN97TF", "HA5LA", 672, 350, 3
LAT0, LON0 = maidenhead_to_latlon(ME)
SHP = ("/home/kod/.local/share/cartopy/shapefiles/natural_earth/"
       "cultural/ne_50m_admin_0_countries.shp")
PAIRS = [("HA1VHF", 271.0, 233.0), ("HG5O", 307.0, 47.0)]
flat_ground, flat_project = Q._ground, Q.project


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
    span = 156543.03392 * math.cos(math.radians(LAT0)) / 1000.0 * 256

    def xy(lo, la):
        return ((np.asarray(lo) + 180.0) / 360.0,
                (1 - np.arcsinh(np.tan(np.radians(np.asarray(la)))) / math.pi) / 2)

    x0, y0 = xy(LON0, LAT0)
    x, y = xy(lon, lat)
    return (x - x0) * span, (y0 - y) * span


def draw_borders(draw, cam, w, h, reach_km, width):
    f = Q._focal(w)
    b = math.radians(cam.bearing)
    horizon = math.sqrt(2 * cam.alt_km * R)
    span = min(16.0, reach_km / 78.0)
    for ring in rings((LON0 - span * 1.6, LAT0 - span, LON0 + span * 1.6, LAT0 + span)):
        e, n = lonlat_to_en(ring[:, 0], ring[:, 1])
        along = e * math.sin(b) + n * math.cos(b) + cam.back_km
        across = e * math.cos(b) - n * math.sin(b)
        d = np.hypot(along, across)
        zw = -d * d / (2 * R) - cam.alt_km
        yc = along * math.cos(cam.tilt) - zw * math.sin(cam.tilt)
        zc = along * math.sin(cam.tilt) + zw * math.cos(cam.tilt)
        with np.errstate(divide="ignore", invalid="ignore"):
            x, y = w / 2 + f * across / yc, h / 2 - f * zc / yc
        # a border is only drawn where there is ground under it
        vis = (yc > 0.5) & (d <= min(horizon, reach_km)) & np.isfinite(x) & np.isfinite(y)
        run = []
        for i in range(len(x)):
            if vis[i] and -w < x[i] < 2 * w and -h < y[i] < 2 * h:
                run.append((x[i], y[i]))
            else:
                if len(run) > 1:
                    draw.line(run, fill=(255, 245, 210, 150), width=width)
                run = []
        if len(run) > 1:
            draw.line(run, fill=(255, 245, 210, 150), width=width)


# ── frame ────────────────────────────────────────────────────────────────
def base(pyr, cam, w, h, curved):
    Q._ground = ground_curved if curved else flat_ground
    img, covered = Q.draw_ground(pyr, cam, w, h)
    sky = np.linspace(0.30, 0.68, h)[:, None, None] * np.array(
        [120, 150, 200]) + np.array([30, 40, 55])
    e, n = Q._ground(cam, w, h)
    rng = np.nan_to_num(np.hypot(e, n))
    haze = np.clip(rng / (cam.distance_km * 3.2), 0, 0.82)[..., None]
    img = np.where(covered[..., None], img * (1 - haze) + Q.HAZE_COLOUR * haze, sky)
    Q._ground = flat_ground
    return img


def overlay(im, cam, call, dist, curved):
    """The marker/label pass, at panel resolution -- a copy of draw_map_frame's."""
    Q.project = project_curved if curved else flat_project
    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    font = ImageFont.truetype(CAST_FONT_PATH, 12)
    small = ImageFont.truetype(CAST_FONT_PATH, 9)
    me = Q.project(cam, cam.bearing, 0.0, W, H)
    point = Q.project(cam, cam.bearing, dist, W, H)

    def marker(p, label, colour):
        x, y = p
        d.line([(x, y - 16), (x, y)], fill=colour + (255,), width=2)
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=colour + (255,),
                  outline=(20, 20, 20, 255))
        tw = d.textlength(label, font=font)
        lx = min(max(x, tw / 2 + 5), W - tw / 2 - 5)
        d.rectangle([lx - tw / 2 - 4, y - 34, lx + tw / 2 + 4, y - 17], fill=(0, 0, 0, 255))
        d.text((lx - tw / 2, y - 33), label, font=font, fill=colour + (255,))

    if point and point[1] >= 40:
        if me:
            d.line([me, point], fill=Q.DX_COLOUR + (140,), width=1)
        marker(point, call, Q.DX_COLOUR)
    if me:
        marker(me, MY_CALL, Q.QTH_COLOUR)
    d.text((6, 4), f"{MY_CALL} → {call}   {dist:.0f} km", font=font, fill=(235, 240, 235, 255))
    d.text((6, H - 15), Q.TILE_ATTRIBUTION, font=small, fill=(215, 220, 225, 220))
    Q.project = flat_project
    return Image.alpha_composite(im.convert("RGBA"), over).convert("RGB")


def panel(pyr, call, brg, dist, reach, new, tag):
    cam = Q.camera_for(brg, dist)
    t0 = time.perf_counter()
    ss = SS if new else 1
    img = base(pyr, cam, W * ss, H * ss, curved=new)
    big = Image.fromarray(img.clip(0, 255).astype(np.uint8)).convert("RGBA")
    if new:
        ov = Image.new("RGBA", big.size, (0, 0, 0, 0))
        draw_borders(ImageDraw.Draw(ov), cam, W * ss, H * ss, reach, max(1, ss))
        big = Image.alpha_composite(big, ov)
    im = big.resize((W, H), Image.BOX) if ss > 1 else big
    im = overlay(im, cam, call, dist, curved=new)
    d = ImageDraw.Draw(im)
    f12 = ImageFont.truetype(CAST_FONT_PATH, 12)
    tw = d.textlength(tag, font=f12)
    d.rectangle([4, H - 40, 10 + tw, H - 22], fill=(0, 0, 0))
    d.text((7, H - 38), tag, font=f12, fill=(255, 255, 160))
    d.rectangle([0, 0, W - 1, H - 1], outline=(70, 70, 60))
    print(f"  {tag} {call}: {time.perf_counter()-t0:.2f} s")
    return im


if __name__ == "__main__":
    targets = [Q.Target(0, 1, c, b, d) for c, b, d in PAIRS]
    pyr = Q.build_pyramids(ME, targets)
    reach = max(d for _, _, d in PAIRS) * 1.9
    print(f"mosaic reaches {reach:.0f} km")
    rows = []
    for call, brg, dist in PAIRS:
        rows.append((panel(pyr, call, brg, dist, reach, False, "as it ships today"),
                     panel(pyr, call, brg, dist, reach, True,
                           "+ curvature, 3x supersampling, borders")))
    sheet = Image.new("RGB", (W * 2 + 8, H * 2 + 8), (0, 0, 0))
    for i, (a, b) in enumerate(rows):
        sheet.paste(a, (0, i * (H + 8)))
        sheet.paste(b, (W + 8, i * (H + 8)))
    sheet.save("/tmp/ticket10/preview-pairs.png")
    for i, (a, b) in enumerate(rows):
        s = Image.new("RGB", (W * 2 + 8, H), (0, 0, 0))
        s.paste(a, (0, 0)); s.paste(b, (W + 8, 0))
        s.save(f"/tmp/ticket10/preview-{PAIRS[i][0]}.png")
    print("wrote /tmp/ticket10/preview-pairs.png")
