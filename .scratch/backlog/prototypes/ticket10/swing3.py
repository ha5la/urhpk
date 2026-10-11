"""Throwaway: the swing with anisotropic ground filtering."""
import math, os, subprocess, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/tmp/ticket10"); sys.path.insert(0, "/home/kod/code/urhpk")
from path_view import W, H, FOV, FONT, worked
from twolevel import level
from aniso import mips, render as ground_render
from urhpk import geo

ME, MY_CALL, FPS = "JN97TF", "HA5LA", 30
F = (W / 2) / math.tan(math.radians(FOV) / 2)


def smoothstep(t): return t * t * (3 - 2 * t)
def shortest(a, b): return a + (b - a + 180) % 360 - 180


def frame(levels, brg, dist, pins, caption, mode="aniso", taps=4):
    back, alt = 0.45 * dist, 0.38 * dist
    tilt = (math.atan2(alt, back) + math.atan2(alt, back + dist)) / 2
    img, covered = ground_render(levels, brg, dist, mode, taps)
    px, py = np.meshgrid(np.arange(W) - W / 2, np.arange(H) - H / 2)
    dy = F * math.cos(tilt) - py * math.sin(tilt)
    dz = -(F * math.sin(tilt) + py * math.cos(tilt))
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(dz < 0, -alt / dz, np.nan)
    rng = np.hypot(dy * s, px * s)
    sky = np.linspace(0.30, 0.68, H)[:, None, None] * np.array([120, 150, 200]) + np.array([30, 40, 55])
    img = np.where(covered[..., None], img, sky)
    haze = np.clip(np.where(covered, rng, 0) / (dist * 3.2), 0, 0.82)[..., None]
    img = np.where(covered[..., None], img * (1 - haze) + np.array([150, 172, 200]) * haze, img)
    base = Image.fromarray(img.clip(0, 255).astype(np.uint8)).convert("RGBA")

    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    fnt = ImageFont.truetype(FONT, 12)

    def place(pb, pd):
        hb = math.radians(brg)
        ex, ny = pd * math.sin(math.radians(pb)), pd * math.cos(math.radians(pb))
        ex, ny = ex + back * math.sin(hb), ny + back * math.cos(hb)
        along = ex * math.sin(hb) + ny * math.cos(hb)
        across = ex * math.cos(hb) - ny * math.sin(hb)
        dyc = along * math.cos(tilt) + alt * math.sin(tilt)
        if dyc <= 0.5:
            return None
        dzc = along * math.sin(tilt) - alt * math.cos(tilt)
        return W / 2 + F * across / dyc, H / 2 - F * dzc / dyc

    def marker(p, call, col, a):
        x, y = p
        A = int(255 * a)
        d.line([(x, y - 16), (x, y)], fill=col + (A,), width=2)
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=col + (A,), outline=(20, 20, 20, A))
        tw = d.textlength(call, font=fnt)
        lx = min(max(x, tw / 2 + 5), W - tw / 2 - 5)
        d.rectangle([lx - tw / 2 - 4, y - 34, lx + tw / 2 + 4, y - 17], fill=(0, 0, 0, A))
        d.text((lx - tw / 2, y - 33), call, font=fnt, fill=col + (A,))

    me = place(0.0, 0.0)
    for call, pb, pd, col, a in pins:
        p = place(pb, pd)
        if p is None or a <= 0.01 or p[1] < 40:
            continue
        if me:
            d.line([me, p], fill=(255, 176, 0, int(140 * a)), width=1)
        marker(p, call, col, a)
    if me:
        marker(me, MY_CALL, (90, 220, 255), 1.0)
    for text, a in caption:
        if a > 0.01:
            d.text((6, 4), text, font=fnt, fill=(235, 240, 235, int(255 * a)))
    d.text((6, H - 16), "Imagery: Sentinel-2 cloudless by EOX (CC BY 4.0)",
           font=ImageFont.truetype(FONT, 9), fill=(215, 220, 225, 225))
    return Image.alpha_composite(base, over).convert("RGB")


if __name__ == "__main__":
    locs = worked()
    a_call, b_call = "HG5O", "HA3KHB"
    A = (geo.bearing_between(ME, locs[a_call]), geo.distance_between(ME, locs[a_call]))
    B = (geo.bearing_between(ME, locs[b_call]), geo.distance_between(ME, locs[b_call]))
    lat0, lon0 = geo.maidenhead_to_latlon(ME)
    levels = [mips(*level(lat0, lon0, half_km=B[1] * 1.9, z=8)),
              mips(*level(lat0, lon0, half_km=50, z=11))]

    n_hold, n_swing = round(1.0 * FPS), round(1.4 * FPS)
    ts = [0.0] * n_hold + [smoothstep((i + 1) / n_swing) for i in range(n_swing)] + [1.0] * n_hold
    os.makedirs("/tmp/ticket10/swing3", exist_ok=True)
    t0 = time.perf_counter()
    for k, t in enumerate(ts):
        brg = A[0] + (shortest(A[0], B[0]) - A[0]) * t
        dist = math.exp(math.log(A[1]) * (1 - t) + math.log(B[1]) * t)
        fo, fi = min(1.0, max(0.0, 1 - t * 2)), min(1.0, max(0.0, t * 2 - 1))
        frame(levels, brg, dist,
              [(a_call, A[0], A[1], (255, 176, 0), fo),
               (b_call, B[0], B[1], (255, 176, 0), fi)],
              [(f"{MY_CALL} → {a_call}   {A[1]:.0f} km", fo),
               (f"{MY_CALL} → {b_call}   {B[1]:.0f} km", fi)]
              ).save(f"/tmp/ticket10/swing3/{k:04d}.png")
    print(f"{len(ts)} frames, {(time.perf_counter()-t0)/len(ts)*1000:.0f} ms/frame")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS),
                    "-i", "/tmp/ticket10/swing3/%04d.png", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-crf", "18", "/tmp/ticket10/swing3.mp4"], check=True)
    print("wrote /tmp/ticket10/swing3.mp4")
