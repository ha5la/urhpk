"""The same two pairs, at a camera that puts the horizon in shot.

Left is the production ground model -- a flat earth, which has no horizon at
all: the ground simply runs to the edge of the mosaic. Right is curvature,
3x supersampling and borders. Same camera in both, so the only differences
are the three features.

Two things this camera forces, both visible here and neither optional:
  * it is the same camera for every path, because pinning the QTH to 90% and
    the horizon to 10% uses up both degrees of freedom -- so a short path no
    longer frames itself, it just sits closer to the QTH;
  * the haze can no longer be scaled by the path length. At 690 km of visible
    ground a 47 km path would put the whole frame past the 0.82 cap. Here it
    is scaled by the horizon instead, identically in both panels.
"""
import math, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
from urhpk import qth_map as Q
from urhpk.cast_render import CAST_FONT_PATH
from curve_bench import ground_curved, R
from curve_look import project_curved
from preview_pairs import (draw_borders, overlay, LAT0, LON0, ME, MY_CALL,
                           W, H, SS, PAIRS, flat_ground, flat_project)
from reframe import solve

ALT = 45.0
TILT, BACK = solve(ALT)
HORIZON = math.sqrt(2 * ALT * R)
HAZE_SCALE = (HORIZON - BACK) * 0.62


def cam_for(brg, dist):
    return Q.Camera(brg, dist, BACK, ALT, TILT)


def base(pyr, cam, w, h, curved):
    Q._ground = ground_curved if curved else flat_ground
    img, covered = Q.draw_ground(pyr, cam, w, h)
    sky = np.linspace(0.30, 0.68, h)[:, None, None] * np.array(
        [120, 150, 200]) + np.array([30, 40, 55])
    e, n = Q._ground(cam, w, h)
    rng = np.nan_to_num(np.hypot(e, n))
    haze = np.clip(rng / HAZE_SCALE, 0, 0.85)[..., None]
    img = np.where(covered[..., None], img * (1 - haze) + Q.HAZE_COLOUR * haze, sky)
    Q._ground = flat_ground
    return img


def panel(pyr, call, brg, dist, reach, new, tag):
    cam = cam_for(brg, dist)
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


def row_of(dist):
    d = BACK + dist
    zw = -d * d / (2 * R) - ALT
    yc = d * math.cos(TILT) - zw * math.sin(TILT)
    zc = d * math.sin(TILT) + zw * math.cos(TILT)
    return H / 2 - Q._focal(W) * zc / yc


if __name__ == "__main__":
    print(f"camera: alt {ALT:.0f} km  back {BACK:.0f} km  tilt {math.degrees(TILT):.1f}°  "
          f"horizon {HORIZON - BACK:.0f} km from the QTH (row {row_of(HORIZON-BACK):.0f})")
    for call, _, dist in PAIRS:
        print(f"  {call:8s} {dist:5.0f} km → row {row_of(dist):5.0f} "
              f"({row_of(dist)/H*100:.0f}% of frame height)")

    # a third, wider level: the existing pair only reaches 443 km and the
    # horizon is at 690. Coarsest is painted first, so this needs no new code.
    reach = 1250.0
    pyr = [Q.mip_pyramid(*Q.mosaic(LAT0, LON0, half, z))
           for half, z in ((reach, 6), (443.0, 7), (Q.NEAR_HALF_KM, Q.NEAR_ZOOM))]
    print(f"  three levels, widest reaches {reach:.0f} km\n")

    rows = []
    for call, brg, dist in PAIRS:
        rows.append((panel(pyr, call, brg, dist, HORIZON, False,
                           "production ground model (flat — no horizon)"),
                     panel(pyr, call, brg, dist, HORIZON, True,
                           "+ curvature, 3x supersampling, borders")))
    sheet = Image.new("RGB", (W * 2 + 8, H * 2 + 8), (0, 0, 0))
    for i, (a, b) in enumerate(rows):
        sheet.paste(a, (0, i * (H + 8)))
        sheet.paste(b, (W + 8, i * (H + 8)))
    sheet.save("/tmp/ticket10/preview-horizon.png")
    for i, (a, b) in enumerate(rows):
        s = Image.new("RGB", (W * 2 + 8, H), (0, 0, 0))
        s.paste(a, (0, 0)); s.paste(b, (W + 8, 0))
        s.save(f"/tmp/ticket10/preview-hz-{PAIRS[i][0]}.png")
    print("wrote /tmp/ticket10/preview-horizon.png")
