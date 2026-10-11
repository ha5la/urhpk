"""The three ideas, one at a time, on the 030° beam out of JN97TF."""
import math, sys
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, "/home/kod/code/urhpk"); sys.path.insert(0, "/tmp/ticket10")
from urhpk import qth_map as Q
from urhpk.cast_render import CAST_FONT_PATH
from skyline import (render, LAT0, LON0, ALT, TILT, BACK, HORIZON, W, H, R)
from terrain import dem

BRG = 30.0
print(f"camera: alt {ALT:.0f} km  back {BACK:.0f} km  tilt {math.degrees(TILT):.1f}°  "
      f"horizon {HORIZON:.0f} km ({HORIZON-BACK:.0f} from the QTH)")

levels = []
for half, z in ((1250.0, 6), (443.0, 7), (50.0, 11)):
    tex, kmpp, org = Q.mosaic(LAT0, LON0, half, z)
    print(f"  imagery half {half:6.0f} km  z{z}  {tex.shape[1]}x{tex.shape[0]} "
          f"@ {kmpp*1000:.0f} m/px")
    levels.append((tex.astype("float32"), kmpp, org))

hgt, hk, horg = dem(LAT0, LON0, half_km=1250.0, z=7)
far = min(1200.0, (min(hgt.shape) / 2 - 2) * hk)
print(f"  DEM reaches {far:.0f} km; peaks stay visible to "
      f"{HORIZON + math.sqrt(2*2.655*R):.0f} km\n")

panels = [render(BRG, levels, ss=1, tag="1 — curvature only, no supersampling"),
          render(BRG, levels, ss=3, tag="2 — + 3x supersampling"),
          render(BRG, levels, ss=3, terrain=(hgt, hk, horg, far), tag="3 — + terrain")]
# the march is the expensive part; panel 4 is the same ground with borders on it
panels.append(render(BRG, levels, ss=3, borders=True, tag="4 — + country borders",
                     ground=render.last_ground))
sheet = Image.new("RGB", (W * 2 + 8, H * 2 + 8), (0, 0, 0))
for i, p in enumerate(panels):
    sheet.paste(p, ((i % 2) * (W + 8), (i // 2) * (H + 8)))
sheet.save("/tmp/ticket10/skyline.png")
for i, p in enumerate(panels):
    p.save(f"/tmp/ticket10/skyline-{i+1}.png")

# the horizon band, 3x, for 1 vs 3
band = (0, 20, W, 80)
zoom = Image.new("RGB", (W * 3, (band[3] - band[1]) * 3 * 2 + 44), (0, 0, 0))
d = ImageDraw.Draw(zoom)
f13 = ImageFont.truetype(CAST_FONT_PATH, 13)
for i, (p, tag) in enumerate(((panels[0], "no supersampling, no terrain — 3x"),
                              (panels[2], "supersampled + terrain — 3x"))):
    y = i * ((band[3] - band[1]) * 3 + 22)
    d.text((8, y + 3), tag, font=f13, fill=(255, 255, 160))
    zoom.paste(p.crop(band).resize((W * 3, (band[3] - band[1]) * 3), Image.LANCZOS),
               (0, y + 22))
zoom.save("/tmp/ticket10/skyline-zoom.png")
print("wrote /tmp/ticket10/skyline.png and skyline-zoom.png")
