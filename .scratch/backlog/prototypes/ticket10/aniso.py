"""Throwaway: why the ground shimmers, and what fixes it.

A screen pixel's footprint on the ground grows with range and is stretched
along the view direction -- in the distance it covers many texels vertically
and about one horizontally. Bilinear averages 2x2 texels regardless, so it
misses most of that footprint, and which texels it happens to land on changes
as the camera moves. That is the flicker.

Two properties of a ground plane under a camera with no roll make the fix
cheap: the footprint depends only on the screen *row*, and its long axis is
screen-vertical. So the mip level and the number of extra taps are per-row
constants, and the taps are just sub-pixel offsets in y.
"""
import math, sys, time
import numpy as np
sys.path.insert(0, "/tmp/ticket10"); sys.path.insert(0, "/home/kod/code/urhpk")
from path_view import W, H, FOV

F = (W / 2) / math.tan(math.radians(FOV) / 2)


def mips(tex, kmpp, org, n=7):
    """Box-halved pyramid; each level's km/px and origin scale with it."""
    out, t, k, o = [], tex.astype(np.float32), kmpp, org
    for _ in range(n):
        out.append((t, k, o))
        h, w = t.shape[0] // 2 * 2, t.shape[1] // 2 * 2
        if h < 4 or w < 4:
            break
        t = t[:h, :w].reshape(h // 2, 2, w // 2, 2, 3).mean((1, 3))
        k, o = k * 2, (o[0] / 2, o[1] / 2)
    return out


def ground(brg, back, alt, tilt, dy_off=0.0):
    """East/north offset from the QTH for every pixel, at sub-pixel row offset."""
    # py kept as a column so a per-row sub-pixel offset broadcasts into it
    px = (np.arange(W) - W / 2)[None, :]
    py = (np.arange(H) - H / 2)[:, None] + dy_off
    dy = F * math.cos(tilt) - py * math.sin(tilt)
    dz = -(F * math.sin(tilt) + py * math.cos(tilt))
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(dz < 0, -alt / dz, np.nan)
    fwd, right = dy * s, px * s
    b = math.radians(brg)
    return (right * math.cos(b) + (fwd - back) * math.sin(b),
            -right * math.sin(b) + (fwd - back) * math.cos(b))


def bilinear(tex, u, v):
    h, w = tex.shape[:2]
    ok = np.isfinite(u) & np.isfinite(v) & (u >= 0) & (v >= 0) & (u < w - 1) & (v < h - 1)
    us, vs = np.where(ok, u, 0.0), np.where(ok, v, 0.0)
    u0, v0 = us.astype(np.int32), vs.astype(np.int32)
    fu, fv = (us - u0)[..., None], (vs - v0)[..., None]
    a, b = tex[v0, u0], tex[v0, u0 + 1]
    c, d = tex[v0 + 1, u0], tex[v0 + 1, u0 + 1]
    return a * (1 - fu) * (1 - fv) + b * fu * (1 - fv) + c * (1 - fu) * fv + d * fu * fv, ok


def row_lod(pyramid, brg, back, alt, tilt, maxtaps=16):
    """Per-row mip index and tap count.

    lx/ly are how many texels one screen pixel spans across and along the view.
    The taps run along the long axis; the mip level is set by the short one --
    except that with only `maxtaps` of them the long axis would still be
    undersampled, so the level is raised to whatever those taps can cover.
    That is the standard bounded-anisotropy formulation."""
    e0, n0 = ground(brg, back, alt, tilt)
    e1, n1 = ground(brg, back, alt, tilt, dy_off=1.0)
    base_kmpp = pyramid[0][1]
    # horizontal footprint: neighbouring columns, mid-row
    dx = np.hypot(np.gradient(e0, axis=1), np.gradient(n0, axis=1)) / base_kmpp
    dyv = np.hypot(e1 - e0, n1 - n0) / base_kmpp
    lx = np.nanmedian(np.where(np.isfinite(dx), dx, np.nan), axis=1)
    ly = np.nanmedian(np.where(np.isfinite(dyv), dyv, np.nan), axis=1)
    lx = np.nan_to_num(lx, nan=1.0)
    ly = np.nan_to_num(ly, nan=1.0)
    taps = np.clip(np.ceil(ly / np.maximum(lx, 1e-6)), 1, maxtaps).astype(int)
    span = np.maximum(lx, ly / taps)
    lod = np.clip(np.floor(np.log2(np.maximum(span, 1.0))).astype(int), 0, len(pyramid) - 1)
    return lod, taps


def render(levels, brg, dist, mode, taps=8):
    """mode: 'bilinear' (what the swing used), 'mip', or 'aniso'."""
    back, alt = 0.45 * dist, 0.38 * dist
    tilt = (math.atan2(alt, back) + math.atan2(alt, back + dist)) / 2
    img = np.zeros((H, W, 3), np.float32)
    covered = np.zeros((H, W), bool)
    for pyramid in sorted(levels, key=lambda P: -P[0][1]):
        lod, _ = row_lod(pyramid, brg, back, alt, tilt, maxtaps=taps)
        if mode == "bilinear":
            lod = np.zeros_like(lod)
        n = 1 if mode in ("bilinear", "mip") else taps
        acc = np.zeros((H, W, 3), np.float32)
        hits = np.zeros((H, W), np.float32)
        # The footprint grows monotonically with range, and range with screen
        # row, so the rows sharing a mip level are a contiguous band: slices,
        # not boolean masks, and no copy in or out.
        edges = [0, *(np.flatnonzero(np.diff(lod)) + 1).tolist(), H]
        for k in range(n):
            # The taps spread across *one screen pixel* in y -- that is the
            # whole footprint, by definition. Spreading them over `aniso`
            # pixels instead smears the ground into mush.
            off = 0.0 if n == 1 else (k + 0.5) / n - 0.5
            e, nn = ground(brg, back, alt, tilt, dy_off=off)
            for lo, hi in zip(edges, edges[1:]):
                tex, kmpp, org = pyramid[lod[lo]]
                vals, ok = bilinear(tex, org[0] + e[lo:hi] / kmpp,
                                    org[1] - nn[lo:hi] / kmpp)
                np.add(acc[lo:hi], np.where(ok[..., None], vals, 0), out=acc[lo:hi])
                np.add(hits[lo:hi], ok, out=hits[lo:hi], casting="unsafe")
        got = hits > 0
        img[got] = acc[got] / hits[got][..., None]
        covered |= got
    return img, covered


if __name__ == "__main__":
    from twolevel import level
    from path_view import worked
    from urhpk import geo
    ME = "JN97TF"
    locs = worked()
    lat0, lon0 = geo.maidenhead_to_latlon(ME)
    lv = [mips(*level(lat0, lon0, half_km=geo.distance_between(ME, locs["HA3KHB"]) * 1.9, z=8)),
          mips(*level(lat0, lon0, half_km=50, z=11))]
    brg = geo.bearing_between(ME, locs["HG5O"])
    dist = geo.distance_between(ME, locs["HG5O"])

    e, n_ = ground(brg, 0.45 * dist, 0.38 * dist,
                   (math.atan2(0.38 * dist, 0.45 * dist)
                    + math.atan2(0.38 * dist, 0.45 * dist + dist)) / 2)
    _, aniso_rows = row_lod(lv[0], brg, 0.45 * dist, 0.38 * dist,
                            (math.atan2(0.38 * dist, 0.45 * dist)
                             + math.atan2(0.38 * dist, 0.45 * dist + dist)) / 2,
                            maxtaps=4096)
    print(f"footprint anisotropy by row: median {np.median(aniso_rows):.0f}x, "
          f"max {aniso_rows.max()}x")
    ref, _ = render(lv, brg, dist, "aniso", taps=128)
    for mode, taps in (("bilinear", 1), ("mip", 1), ("aniso", 4), ("aniso", 8), ("aniso", 16)):
        t0 = time.perf_counter()
        for i in range(5):
            img, _ = render(lv, brg + i * 0.2, dist, mode, taps)
        ms = (time.perf_counter() - t0) / 5 * 1000
        img, _ = render(lv, brg, dist, mode, taps)
        rmse = float(np.sqrt(np.nanmean((img - ref) ** 2)))
        print(f"{mode:9s} taps={taps:2d}  {ms:6.0f} ms/frame   RMSE vs reference {rmse:5.2f}")
