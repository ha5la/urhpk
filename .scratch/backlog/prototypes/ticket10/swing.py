"""Throwaway: the map swinging from one worked station to the next.

Not driven by the rotator azimuth -- the HUD's compass already says that. This
follows the station being worked: it holds the finished path, smoothsteps
across to the new one, and holds that.
"""
import math, os, subprocess, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/tmp/ticket10")
sys.path.insert(0, "/home/kod/code/urhpk")
from path_view import W, H, FOV, FONT, mosaic, worked
from urhpk import geo

ME, MY_CALL = "JN97TF", "HA5LA"
FPS = 30


def smoothstep(t):
    return t * t * (3 - 2 * t)


def shortest(a, b):
    """b reached from a the short way round, in degrees."""
    return a + (b - a + 180) % 360 - 180


def camera(brg, dist):
    back, alt = 0.45 * dist, 0.38 * dist
    tilt = (math.atan2(alt, back) + math.atan2(alt, back + dist)) / 2
    return brg, back, alt, tilt


def frame(tex, kmpp, org, cam, pins, caption):
    brg, back, alt, tilt = cam
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
    u, v = org[0] + e / kmpp, org[1] - n / kmpp
    ok = (np.isfinite(u) & np.isfinite(v) & (u >= 0) & (v >= 0)
          & (u < tex.shape[1]) & (v < tex.shape[0]))
    img = np.empty((H, W, 3), float)
    img[:] = np.linspace(0.30, 0.68, H)[:, None, None] * np.array([120, 150, 200]) + np.array([30, 40, 55])
    ui, vi = np.where(ok, u, 0).astype(int), np.where(ok, v, 0).astype(int)
    img[ok] = tex[vi[ok], ui[ok]]
    haze = np.clip(np.where(ok, rng, 0) / (max(dist_ref, 40) * 3.2), 0, 0.82)[..., None]
    img = np.where(ok[..., None], img * (1 - haze) + np.array([150, 172, 200]) * haze, img)
    base = Image.fromarray(img.clip(0, 255).astype(np.uint8)).convert("RGBA")

    def project(along, across):
        dyc = along * math.cos(tilt) + alt * math.sin(tilt)
        dzc = along * math.sin(tilt) - alt * math.cos(tilt)
        if dyc <= 0.5:
            return None
        return W / 2 + f * across / dyc, H / 2 - f * dzc / dyc

    def place(pb, pd):
        """Screen position of a station at bearing pb, distance pd."""
        hb = math.radians(brg)
        ex, ny = pd * math.sin(math.radians(pb)), pd * math.cos(math.radians(pb))
        ex, ny = ex + back * math.sin(hb), ny + back * math.cos(hb)
        return project(ex * math.sin(hb) + ny * math.cos(hb),
                       ex * math.cos(hb) - ny * math.sin(hb))

    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    fnt = ImageFont.truetype(FONT, 12)
    me = place(0.0, 0.0)

    def marker(p, call, col, a):
        """Pin plus label, with the label kept inside the frame -- mid-swing a
        station can sit above the top edge and its name must not be sliced."""
        x, y = p
        A = int(255 * a)
        d.line([(x, y - 16), (x, y)], fill=col + (A,), width=2)
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=col + (A,), outline=(20, 20, 20, A))
        tw = d.textlength(call, font=fnt)
        lx = min(max(x, tw / 2 + 5), W - tw / 2 - 5)
        ly = min(max(y - 34, 20), H - 20)
        d.rectangle([lx - tw / 2 - 4, ly, lx + tw / 2 + 4, ly + 17], fill=(0, 0, 0, A))
        d.text((lx - tw / 2, ly + 1), call, font=fnt, fill=col + (A,))

    for call, pb, pd, col, a in pins:
        p = place(pb, pd)
        # A station still above the top edge has nowhere to put its name that
        # is not the caption, so it waits until it is in frame.
        if p is None or a <= 0.01 or p[1] < 22:
            continue
        if me:
            d.line([me, p], fill=(255, 176, 0, int(140 * a)), width=1)
        marker(p, call, col, a)
    if me:
        marker(me, MY_CALL, (90, 220, 255), 1.0)
    for text, a in caption:
        if a > 0.01:
            d.text((6, 4), text, font=fnt, fill=(235, 240, 235, int(255 * a)))
    d.text((6, H - 16), "Imagery: Esri World Imagery",
           font=ImageFont.truetype(FONT, 9), fill=(210, 215, 220, 220))
    return Image.alpha_composite(base, over).convert("RGB")


if __name__ == "__main__":
    locs = worked()
    a_call, b_call = "HG5O", "HA3KHB"
    A = (geo.bearing_between(ME, locs[a_call]), geo.distance_between(ME, locs[a_call]))
    B = (geo.bearing_between(ME, locs[b_call]), geo.distance_between(ME, locs[b_call]))
    dist_ref = max(A[1], B[1])
    lat0, lon0 = geo.maidenhead_to_latlon(ME)
    tex, kmpp, org = mosaic(lat0, lon0, half_km=dist_ref * 1.9)
    print(f"mosaic {tex.shape[1]}x{tex.shape[0]} @ {kmpp*1000:.0f} m/px")

    hold, swing = 1.0, 1.4
    frames = []
    n_hold, n_swing = round(hold * FPS), round(swing * FPS)
    for i in range(n_hold):
        frames.append((0.0,))
    for i in range(n_swing):
        frames.append((smoothstep((i + 1) / n_swing),))
    for i in range(n_hold):
        frames.append((1.0,))

    os.makedirs("/tmp/ticket10/swing", exist_ok=True)
    for k, (t,) in enumerate(frames):
        brg = A[0] + (shortest(A[0], B[0]) - A[0]) * t
        dist = math.exp(math.log(A[1]) * (1 - t) + math.log(B[1]) * t)
        pins = [(a_call, A[0], A[1], (255, 176, 0), min(1.0, max(0.0, 1 - t * 2.0))),
                (b_call, B[0], B[1], (255, 176, 0), min(1.0, max(0.0, t * 2.0 - 1.0)))]
        cap = [(f"{MY_CALL} → {a_call}   {A[1]:.0f} km", min(1.0, max(0.0, 1 - t * 2.0))),
               (f"{MY_CALL} → {b_call}   {B[1]:.0f} km", min(1.0, max(0.0, t * 2.0 - 1.0)))]
        frame(tex, kmpp, org, camera(brg, dist), pins, cap).save(f"/tmp/ticket10/swing/{k:04d}.png")
    print(f"{len(frames)} frames")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS),
                    "-i", "/tmp/ticket10/swing/%04d.png", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-crf", "18", "/tmp/ticket10/swing.mp4"], check=True)
    print("wrote /tmp/ticket10/swing.mp4")
