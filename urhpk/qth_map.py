"""The QTH map: a drone shot over the station, turning to face who is worked.

A tilted camera looking at flat ground is exactly a homography of an ortho
image, and the QTH is on the Pest plain, so this needs no terrain model and no
3D engine -- only one satellite mosaic and a per-pixel inverse map. What makes
it read as Google Earth is the imagery, not the machinery.

Three things about the shape of the problem make the whole thing cheap:

* **The camera is three numbers derived from a bearing and a distance.** It
  sits behind the QTH along the bearing and frames both ends of the path, so a
  33 km contact and a 233 km one compose identically with no tuning.
* **The footprint of a screen pixel on the ground depends only on the screen
  row**, and its long axis is screen-vertical -- the camera has no roll. So the
  mip level and the anisotropic tap count are per-row constants, the taps are
  sub-pixel offsets in y, and the rows sharing a level are a contiguous band.
* **The camera only moves between QSOs.** Every frame of a hold is the same
  frame, so a round is a few thousand distinct renders rather than a quarter of
  a million.

It follows the **bearing** of the station being worked, never the rotator
**azimuth**: the HUD's compass already shows where the antenna points.
"""

from __future__ import annotations

import math
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from urhpk.cast_render import CAST_FONT_PATH
from urhpk.geo import distance_between, initial_bearing, maidenhead_to_latlon
from urhpk.progress import stage_bar
from urhpk.timeline import Qso
from urhpk.video_format import RENDER_FPS
from urhpk.wiring import PUSKAS_DIR

FOV_DEG = 60.0
SWING_S = 1.4  # how long the camera takes to turn onto the next station

# The framing rule: pull back and climb in proportion to the path, which puts
# the QTH in the lower third and the station in the upper third for any length.
# Tried and rejected: scaling by D**0.55, which makes a long path read as long
# and pushes the station off the top edge past about 150 km.
BACK_FRAC, ALT_FRAC = 0.45, 0.38

MAX_TAPS = 4  # the footprint anisotropy measures 3x median and 6x max at this
# tilt, so four taps is indistinguishable from sixteen; see docs/FINDINGS.md

TILE_PX = 256
MOSAIC_BUDGET_PX = 2048  # how big a mosaic may be, which is what picks the zoom
NEAR_HALF_KM = 50.0  # the fine level's reach, for the ground under the camera
NEAR_ZOOM = 11  # 52 m/px here -- still 5x coarser than Sentinel-2's own 10 m

TILE_DIR = PUSKAS_DIR / "tiles"
# Sentinel-2 cloudless, CC BY 4.0. Chosen over Esri's World Imagery for the
# licence (this ends up in a published video) and because a cloud-free annual
# composite holds its colour across the boundary between the two levels.
TILE_URL = (
    "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/"
    "default/GoogleMapsCompatible/{z}/{y}/{x}.jpg"
)
TILE_ATTRIBUTION = "Imagery: Sentinel-2 cloudless by EOX (CC BY 4.0)"
USER_AGENT = "urhpk/1.0 (amateur radio contest video; a few hundred tiles per QTH)"


# ──────────────────────────────────────────────────────────────
# The camera
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Camera:
    bearing: float  # degrees true, the direction the camera looks along
    distance_km: float  # the path being framed; the haze scale comes off it
    back_km: float  # how far behind the QTH it sits
    alt_km: float
    tilt: float  # radians, depression of the optical axis


def camera_for(bearing: float, distance_km: float) -> Camera:
    """The shot of one path. Tilt is the mean of the depression angles to the
    two ends, which is what centres the path rather than the ground."""
    back, alt = BACK_FRAC * distance_km, ALT_FRAC * distance_km
    tilt = (math.atan2(alt, back) + math.atan2(alt, back + distance_km)) / 2
    return Camera(bearing, distance_km, back, alt, tilt)


def _focal(w: int) -> float:
    return (w / 2) / math.tan(math.radians(FOV_DEG) / 2)


def project(
    cam: Camera, bearing: float, distance_km: float, w: int, h: int
) -> tuple[float, float] | None:
    """Where a station at (bearing, distance) from the QTH lands on screen, or
    None if it is behind the camera."""
    rel = math.radians(bearing - cam.bearing)
    along = distance_km * math.cos(rel) + cam.back_km
    across = distance_km * math.sin(rel)
    depth = along * math.cos(cam.tilt) + cam.alt_km * math.sin(cam.tilt)
    if depth <= 0.5:
        return None
    up = along * math.sin(cam.tilt) - cam.alt_km * math.cos(cam.tilt)
    f = _focal(w)
    return w / 2 + f * across / depth, h / 2 - f * up / depth


# ──────────────────────────────────────────────────────────────
# What the camera is looking at, over the round
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Target:
    start: float
    end: float
    callsign: str
    bearing: float
    distance_km: float


@dataclass(frozen=True)
class Shot:
    camera: Camera
    current: Target
    previous: Target | None
    blend: float  # 0 fully on `previous`, 1 fully on `current`

    def key(self) -> tuple:
        """What makes two frames identical -- a hold repeats one frame for its
        whole length, which is the entire render budget."""
        return (
            round(self.camera.bearing, 2),
            round(self.camera.distance_km, 3),
            self.current.callsign,
            self.previous.callsign if self.previous else None,
            round(self.blend, 3),
        )


def map_targets(
    qsos: list[Qso], windows: list[tuple[float, float]], my_loc: str
) -> list[Target]:
    """The stations worked, in the order the video reaches them.

    A QSO whose locator will not parse is left out rather than guessed at:
    there is no path to fly without one."""
    me = maidenhead_to_latlon(my_loc)
    if me is None:
        return []
    targets = []
    for q, (start, end) in zip(qsos, windows):
        them = maidenhead_to_latlon(q.loc)
        km = distance_between(my_loc, q.loc)
        if them is None or km is None:
            continue
        targets.append(Target(start, end, q.callsign, initial_bearing(*me, *them), km))
    targets.sort(key=lambda t: t.start)
    return targets


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def _short_way(a: float, b: float) -> float:
    """b as reached from a the short way round, so 350 to 010 goes east."""
    return a + (b - a + 180) % 360 - 180


def camera_at(targets: list[Target], t: float, swing_s: float = SWING_S) -> Shot | None:
    """The shot at video time t: holding on a station, or turning onto one.

    Distance is interpolated geometrically rather than linearly -- it drives
    how far the camera pulls back, and a 47 km framing opening out to 168 km
    otherwise rushes at one end."""
    if not targets:
        return None
    index = 0
    for i, target in enumerate(targets):
        if t >= target.start - swing_s:
            index = i
    current = targets[index]
    previous = targets[index - 1] if index else None
    if previous is None or t >= current.start:
        return Shot(
            camera_for(current.bearing, current.distance_km), current, previous, 1.0
        )
    blend = _smoothstep(max(0.0, (t - (current.start - swing_s)) / swing_s))
    bearing = (
        previous.bearing
        + (_short_way(previous.bearing, current.bearing) - previous.bearing) * blend
    )
    distance = math.exp(
        math.log(previous.distance_km) * (1 - blend)
        + math.log(current.distance_km) * blend
    )
    return Shot(camera_for(bearing, distance), current, previous, blend)


# ──────────────────────────────────────────────────────────────
# The ground: tiles, levels and the anisotropic sampler
# ──────────────────────────────────────────────────────────────


def tile_span_km(lat: float, z: int) -> float:
    """How much ground one tile covers at this latitude and zoom."""
    return 156543.03392 * math.cos(math.radians(lat)) * TILE_PX / 2**z / 1000.0


def zoom_for(lat: float, half_km: float, budget_px: int = MOSAIC_BUDGET_PX) -> int:
    """The coarsest zoom that still fills the pixel budget, so a 233 km path
    costs the same handful of tiles as a 33 km one. Without this the long
    paths ask a public tile server for well over a thousand tiles each."""
    mpp = 2 * half_km * 1000 / budget_px
    z = math.floor(math.log2(156543.03392 * math.cos(math.radians(lat)) / mpp))
    return max(0, min(18, z))


def _tile_xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2**z
    return (
        (lon + 180.0) / 360.0 * n,
        (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n,
    )


def fetch_tile(z: int, x: int, y: int, cache: Path = TILE_DIR) -> np.ndarray:
    """One tile, from the cache or from the server once and then the cache.

    Cached under ~ and not in the round directory: the imagery belongs to the
    QTH, not to the round, and every round from here wants the same tiles."""
    path = cache / f"{z}_{x}_{y}.jpg"
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            tmp = path.with_suffix(".part")
            tmp.write_bytes(response.read())
            os.replace(tmp, path)
    return np.asarray(Image.open(path).convert("RGB"))


def mosaic(
    lat: float, lon: float, half_km: float, z: int, fetch=fetch_tile
) -> tuple[np.ndarray, float, tuple[float, float]]:
    """Imagery covering half_km around (lat, lon): the pixels, the km per
    pixel, and where the QTH sits in them."""
    kmpp = tile_span_km(lat, z) / TILE_PX
    half_px = half_km / kmpp
    cx, cy = _tile_xy(lat, lon, z)
    x0, x1 = int(cx - half_px / TILE_PX) - 1, int(cx + half_px / TILE_PX) + 1
    y0, y1 = int(cy - half_px / TILE_PX) - 1, int(cy + half_px / TILE_PX) + 1
    out = np.empty(((y1 - y0 + 1) * TILE_PX, (x1 - x0 + 1) * TILE_PX, 3), np.uint8)
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            out[
                (ty - y0) * TILE_PX : (ty - y0 + 1) * TILE_PX,
                (tx - x0) * TILE_PX : (tx - x0 + 1) * TILE_PX,
            ] = fetch(z, tx, ty)
    return out, kmpp, ((cx - x0) * TILE_PX, (cy - y0) * TILE_PX)


def mip_pyramid(
    tex: np.ndarray, kmpp: float, origin: tuple[float, float], levels: int = 7
) -> list[tuple[np.ndarray, float, tuple[float, float]]]:
    """Box-halved levels; each one's scale and origin halve with it."""
    out, t, k, o = [], tex.astype(np.float32), kmpp, origin
    for _ in range(levels):
        out.append((t, k, o))
        h, w = t.shape[0] // 2 * 2, t.shape[1] // 2 * 2
        if h < 4 or w < 4:
            break
        t = t[:h, :w].reshape(h // 2, 2, w // 2, 2, 3).mean((1, 3))
        k, o = k * 2, (o[0] / 2, o[1] / 2)
    return out


def _ground(cam: Camera, w: int, h: int, row_offset=0.0):
    """East/north km from the QTH for every pixel, at a sub-pixel row offset.

    row_offset is a column so it can differ per row, which is how the
    anisotropic taps are spread."""
    px = (np.arange(w) - w / 2)[None, :]
    py = (np.arange(h) - h / 2)[:, None] + row_offset
    f = _focal(w)
    dy = f * math.cos(cam.tilt) - py * math.sin(cam.tilt)
    dz = -(f * math.sin(cam.tilt) + py * math.cos(cam.tilt))
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(dz < 0, -cam.alt_km / dz, np.nan)
    forward, right = dy * s, px * s
    b = math.radians(cam.bearing)
    return (
        right * math.cos(b) + (forward - cam.back_km) * math.sin(b),
        -right * math.sin(b) + (forward - cam.back_km) * math.cos(b),
    )


def row_taps(
    cam: Camera, kmpp: float, levels: int, w: int, h: int, maxtaps: int = MAX_TAPS
):
    """Per-row mip level and anisotropic tap count.

    A pixel spans `across` texels sideways and `along` texels into the screen.
    The taps run along the long axis; the level is set by the short one --
    except that `maxtaps` of them may not cover the long axis, in which case
    the level is raised to whatever they *can* cover. Choosing the level by the
    long axis instead blurs the ground to mush, and by the short axis alone it
    still aliases: mipmapping without the taps barely helps."""
    e0, n0 = _ground(cam, w, h)
    e1, n1 = _ground(cam, w, h, row_offset=1.0)
    across = np.hypot(np.gradient(e0, axis=1), np.gradient(n0, axis=1)) / kmpp
    along = np.hypot(e1 - e0, n1 - n0) / kmpp
    across = np.nan_to_num(np.nanmedian(across, axis=1), nan=1.0)
    along = np.nan_to_num(np.nanmedian(along, axis=1), nan=1.0)
    taps = np.clip(np.ceil(along / np.maximum(across, 1e-6)), 1, maxtaps).astype(int)
    span = np.maximum(across, along / taps)
    lod = np.clip(np.floor(np.log2(np.maximum(span, 1.0))).astype(int), 0, levels - 1)
    return lod, taps


def _bilinear(tex: np.ndarray, u: np.ndarray, v: np.ndarray):
    h, w = tex.shape[:2]
    ok = (
        np.isfinite(u)
        & np.isfinite(v)
        & (u >= 0)
        & (v >= 0)
        & (u < w - 1)
        & (v < h - 1)
    )
    us, vs = np.where(ok, u, 0.0), np.where(ok, v, 0.0)
    u0, v0 = us.astype(np.int32), vs.astype(np.int32)
    fu, fv = (us - u0)[..., None], (vs - v0)[..., None]
    a, b = tex[v0, u0], tex[v0, u0 + 1]
    c, d = tex[v0 + 1, u0], tex[v0 + 1, u0 + 1]
    return a * (1 - fu) * (1 - fv) + b * fu * (1 - fv) + c * (
        1 - fu
    ) * fv + d * fu * fv, ok


def draw_ground(pyramids, cam: Camera, w: int, h: int, taps: int = MAX_TAPS):
    """The satellite ground under `cam`, plus a mask of what it covered."""
    img = np.zeros((h, w, 3), np.float32)
    covered = np.zeros((h, w), bool)
    for pyramid in sorted(pyramids, key=lambda p: -p[0][1]):  # coarsest first
        lod, _ = row_taps(cam, pyramid[0][1], len(pyramid), w, h, taps)
        acc = np.zeros((h, w, 3), np.float32)
        hits = np.zeros((h, w), np.float32)
        # The footprint grows with range and range with row, so the rows
        # sharing a level are contiguous: slices, not masks, and no copies.
        edges = [0, *(np.flatnonzero(np.diff(lod)) + 1).tolist(), h]
        for k in range(taps):
            # Across *one screen pixel* in y: that is the whole footprint.
            # Spreading by the anisotropy ratio instead smears it to mush.
            offset = 0.0 if taps == 1 else (k + 0.5) / taps - 0.5
            e, n = _ground(cam, w, h, row_offset=offset)
            for lo, hi in zip(edges, edges[1:]):
                tex, kmpp, origin = pyramid[lod[lo]]
                vals, ok = _bilinear(
                    tex, origin[0] + e[lo:hi] / kmpp, origin[1] - n[lo:hi] / kmpp
                )
                np.add(acc[lo:hi], np.where(ok[..., None], vals, 0), out=acc[lo:hi])
                np.add(hits[lo:hi], ok, out=hits[lo:hi], casting="unsafe")
        got = hits > 0
        img[got] = acc[got] / hits[got][..., None]
        covered |= got
    return img, covered


# ──────────────────────────────────────────────────────────────
# The frame
# ──────────────────────────────────────────────────────────────

QTH_COLOUR = (90, 220, 255)
DX_COLOUR = (255, 176, 0)
HAZE_COLOUR = np.array([150, 172, 200])


def _fading(shot: Shot) -> list[tuple[Target, float]]:
    """Each station and how opaque it is right now.

    Both sit at their true positions through a swing, the one just worked
    fading out over its first half and the next fading in over its second:
    mid-turn the camera faces a bearing nobody was worked on, and nothing may
    be drawn where no station is."""
    if shot.previous is None:
        return [(shot.current, 1.0)]

    def clamp(a: float) -> float:
        return min(1.0, max(0.0, a))

    return [
        (shot.previous, clamp(1 - shot.blend * 2)),
        (shot.current, clamp(shot.blend * 2 - 1)),
    ]


def draw_map_frame(pyramids, shot: Shot, w: int, h: int, my_call: str) -> Image.Image:
    cam = shot.camera
    img, covered = draw_ground(pyramids, cam, w, h)
    sky = np.linspace(0.30, 0.68, h)[:, None, None] * np.array(
        [120, 150, 200]
    ) + np.array([30, 40, 55])
    e, n = _ground(cam, w, h)
    rng = np.nan_to_num(np.hypot(e, n))
    haze = np.clip(rng / (cam.distance_km * 3.2), 0, 0.82)[..., None]
    img = np.where(covered[..., None], img * (1 - haze) + HAZE_COLOUR * haze, sky)
    base = Image.fromarray(img.clip(0, 255).astype(np.uint8)).convert("RGBA")

    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    font = ImageFont.truetype(CAST_FONT_PATH, 12)
    small = ImageFont.truetype(CAST_FONT_PATH, 9)
    me = project(cam, cam.bearing, 0.0, w, h)

    def marker(point, label, colour, alpha):
        x, y = point
        a = int(255 * alpha)
        d.line([(x, y - 16), (x, y)], fill=colour + (a,), width=2)
        d.ellipse(
            [x - 4, y - 4, x + 4, y + 4], fill=colour + (a,), outline=(20, 20, 20, a)
        )
        tw = d.textlength(label, font=font)
        lx = min(max(x, tw / 2 + 5), w - tw / 2 - 5)
        d.rectangle(
            [lx - tw / 2 - 4, y - 34, lx + tw / 2 + 4, y - 17], fill=(0, 0, 0, a)
        )
        d.text((lx - tw / 2, y - 33), label, font=font, fill=colour + (a,))

    for target, alpha in _fading(shot):
        point = project(cam, target.bearing, target.distance_km, w, h)
        # A station still above the top edge has nowhere to put its name that
        # is not the caption, so it waits until it is in frame.
        if point is None or alpha <= 0.01 or point[1] < 40:
            continue
        if me:
            d.line([me, point], fill=DX_COLOUR + (int(140 * alpha),), width=1)
        marker(point, target.callsign, DX_COLOUR, alpha)
    if me:
        marker(me, my_call, QTH_COLOUR, 1.0)
    for target, alpha in _fading(shot):
        if alpha > 0.01:
            d.text(
                (6, 4),
                f"{my_call} → {target.callsign}   {target.distance_km:.0f} km",
                font=font,
                fill=(235, 240, 235, int(255 * alpha)),
            )
    d.text((6, h - 15), TILE_ATTRIBUTION, font=small, fill=(215, 220, 225, 220))
    return Image.alpha_composite(base, over).convert("RGB")


def build_pyramids(my_loc: str, targets: list[Target], fetch=fetch_tile):
    """The two texture levels this round needs: one wide enough for its
    longest path, one fine enough for the ground under the camera.

    One level cannot do both -- it has to reach the far end, and that sets its
    resolution where the camera has the most of its own to spend."""
    me = maidenhead_to_latlon(my_loc)
    if me is None or not targets:
        return []
    lat, lon = me
    far = max(t.distance_km for t in targets) * 1.9
    return [
        mip_pyramid(*mosaic(lat, lon, far, zoom_for(lat, far), fetch)),
        mip_pyramid(*mosaic(lat, lon, NEAR_HALF_KM, NEAR_ZOOM, fetch)),
    ]


def render_map_video(
    targets: list[Target],
    out_path: str,
    w: int,
    h: int,
    duration: float,
    my_loc: str,
    my_call: str,
    fps: int = RENDER_FPS,
    fetch=fetch_tile,
) -> int:
    """Render the map panel to its own clip. Its t=0 is the output timeline's
    t=0, like the HUD's and unlike every captured stream, so render() needs no
    -itsoffset for it. Returns the number of frames actually drawn."""
    pyramids = build_pyramids(my_loc, targets, fetch)
    if not pyramids:
        raise ValueError("no worked station has a locator to fly to")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", f"{fps}",
        "-i", "-", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", out_path,
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    drawn, last_key, frame = 0, None, None
    total = max(1, int(duration * fps))
    try:
        with stage_bar("map", total) as bar:
            for i in range(total):
                shot = camera_at(targets, i / fps)
                key = shot.key()
                if key != last_key or frame is None:
                    frame = draw_map_frame(pyramids, shot, w, h, my_call).tobytes()
                    last_key, drawn = key, drawn + 1
                proc.stdin.write(frame)
                bar.update()
    finally:
        proc.stdin.close()
        proc.wait()
    return drawn
