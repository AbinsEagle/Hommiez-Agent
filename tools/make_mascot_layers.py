#!/usr/bin/env python3
"""One-off asset builder for the desktop character (run with the venv python; needs Pillow and numpy).
Takes the flat sticker picture and writes assets/mascot/ for tools/mascot.py:
  base.png            the figure with both hands lifted out (so they can move without leaving a ghost behind)
  hand_up.png         a strip of the top hand bent to every angle in its range (1 degree apart; see layers.json)
  hand_low.png        the same for the lower hand
  sparkle1/2.png, dash1/2.png   the four loose bits, each with its own white sticker border
  layers.json         where everything goes (all in the 300 x 293 frame of assets/mascot.png)
A hand is bent with a smooth twist that fades into the arm, so there is no cut line through the black outline.
Usage:  .venv/bin/python tools/make_mascot_layers.py [source.png]"""
import json, math, os, sys
from collections import deque
import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "assets", "mascot_source.png")
OUT = os.path.join(HERE, "assets", "mascot")
S, OX, OY, FW, FH = 300 / 410, 75, 81, 300, 293        # 512 px source -> the 300 x 293 frame: x300 = (x512 - OX) * S
SS = 2                                                  # supersampling for clean edges

# a hand: where it pivots (the wrist), the points that make up the hand, the window we re-render, and how far the bend reaches
HANDS = {
    # tilting the top hand up (+) pushes the fist into the head, so it may only tilt a little that way
    "hand_up": dict(pivot=(232, 44), parts=[((232, 44), (172, 26), 20, 8), ((232, 44), (216, 6), 13, 8)], rect=(158, 0, 252, 70),
                    arm=(1.0, 0.0), ramp=(-4, 14), floor_y=(47, 57), angles=(-16, 8)),
    # the lower hand: only the fist and finger bend. The thumb shares its outline with the face, so it stays put.
    "hand_low": dict(pivot=(123, 210), parts=[((123, 210), (88, 184), 20, 7)], rect=(72, 150, 150, 236),
                     arm=(0.8, 0.6), ramp=(-10, 2), floor_y=None, angles=(-3, 18)),
}

def smooth(t):
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)

def silhouette(rgb):
    white = rgb.min(axis=2) >= 238
    ybot, xmin, xmax = 477, 240, 424                      # the drawing is cut off at the bottom: seal that edge so the body stays filled
    cand = white.copy(); cand[ybot - 1:ybot + 3, xmin - 4:xmax + 5] = False
    m = Image.new("L", (512, 512)); m.putdata([255 if v else 0 for v in cand.flatten().tolist()])
    from PIL import ImageDraw
    ImageDraw.floodfill(m, (0, 0), 128)
    sil = np.array(m) != 128
    sil[ybot:] = False
    return sil

def components(sil):
    lab = np.zeros(sil.shape, np.int32); n = 0; out = []
    H, W = sil.shape
    for y in range(H):
        for x in range(W):
            if sil[y, x] and lab[y, x] == 0:
                n += 1; q = deque([(y, x)]); lab[y, x] = n; pts = []
                while q:
                    cy, cx = q.popleft(); pts.append((cy, cx))
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ny, nx = cy + dy, cx + dx
                            if 0 <= ny < H and 0 <= nx < W and sil[ny, nx] and lab[ny, nx] == 0:
                                lab[ny, nx] = n; q.append((ny, nx))
                out.append((len(pts), n))
    return lab, sorted(out, reverse=True)

def to_rgba(rgb, mask):
    """The drawing inside `mask`, with a white sticker border (about 4 px) around it."""
    ring = np.array(Image.fromarray((mask * 255).astype("uint8")).filter(ImageFilter.MaxFilter(9))) > 0
    rgba = np.full((512, 512, 4), 255, np.uint8); rgba[..., 3] = 0
    rgba[ring, 3] = 255
    rgba[mask, :3] = rgb[mask]
    img = Image.fromarray(rgba, "RGBA")
    img.putalpha(img.getchannel("A").filter(ImageFilter.GaussianBlur(0.8)))
    return np.array(img)

def sample(pm, xs, ys):
    """Bilinear sample of a premultiplied float RGBA image at float source coordinates."""
    H, W = pm.shape[:2]
    x0 = np.floor(xs).astype(int); y0 = np.floor(ys).astype(int); fx = (xs - x0)[..., None]; fy = (ys - y0)[..., None]
    def at(yy, xx):
        ok = ((xx >= 0) & (xx < W) & (yy >= 0) & (yy < H))[..., None]
        return np.where(ok, pm[np.clip(yy, 0, H - 1), np.clip(xx, 0, W - 1)], 0.0)
    return (at(y0, x0) * (1 - fx) * (1 - fy) + at(y0, x0 + 1) * fx * (1 - fy) + at(y0 + 1, x0) * (1 - fx) * fy + at(y0 + 1, x0 + 1) * fx * fy)

def render(pm, box, weight_fn=None, angle=0.0, pivot=(0, 0)):
    """Render frame-space box (x0, y0, x1, y1) from the 512 source, twisting by `angle` degrees about `pivot` where weight is 1."""
    x0, y0, x1, y1 = box
    w, h = (x1 - x0) * SS, (y1 - y0) * SS
    gx, gy = np.meshgrid(x0 + (np.arange(w) + 0.5) / SS, y0 + (np.arange(h) + 0.5) / SS)
    if weight_fn is not None and angle:
        wgt = weight_fn(gx, gy)
        a = -math.radians(angle) * wgt                         # inverse map: where did this pixel come from?
        dx, dy = gx - pivot[0], gy - pivot[1]
        sx, sy = pivot[0] + dx * np.cos(a) - dy * np.sin(a), pivot[1] + dx * np.sin(a) + dy * np.cos(a)
    else:
        sx, sy = gx, gy
    px = sample(pm, sx / S + OX, sy / S + OY)
    px = px.reshape(h // SS, SS, w // SS, SS, 4).mean(axis=(1, 3))        # box filter down to the frame resolution
    a = px[..., 3:4]
    rgb = np.where(a > 0.05, px[..., :3] * 255.0 / np.maximum(a, 0.05), 255.0)        # un-premultiply (alpha is 0..255)
    return np.clip(np.concatenate([rgb, a], axis=2), 0, 255).astype(np.uint8)

def seg_dist(gx, gy, a, b):
    ax, ay = a; bx, by = b
    vx, vy = bx - ax, by - ay
    t = np.clip(((gx - ax) * vx + (gy - ay) * vy) / (vx * vx + vy * vy), 0, 1)
    return np.hypot(gx - (ax + t * vx), gy - (ay + t * vy))

def make_weight(h):
    x0, y0, x1, y1 = h["rect"]
    def fn(gx, gy):
        w = np.max([1.0 - smooth((seg_dist(gx, gy, a, b) - r) / f) for a, b, r, f in h["parts"]], axis=0)   # inside the hand: 1, then fading out
        if h["ramp"]:                                          # only the side of the wrist that is the hand
            u = (gx - h["pivot"][0]) * h["arm"][0] + (gy - h["pivot"][1]) * h["arm"][1]
            w = w * (1.0 - smooth((u - h["ramp"][0]) / (h["ramp"][1] - h["ramp"][0])))
        if h["floor_y"]:                                       # keep the head's outline below the hand still
            w = w * (1.0 - smooth((gy - h["floor_y"][0]) / (h["floor_y"][1] - h["floor_y"][0])))
        edge = np.minimum.reduce([gx - x0, x1 - gx, gy - y0, y1 - gy])
        return w * smooth((edge - 1.0) / 3.0)                  # exactly 0 at the window edge, so it joins the picture invisibly
    return fn

def main():
    os.makedirs(OUT, exist_ok=True)
    rgb = np.array(Image.open(SRC).convert("RGB"))
    sil = silhouette(rgb)
    lab, sizes = components(sil)
    fig_id = sizes[0][1]
    figure = to_rgba(rgb, lab == fig_id)
    pm = figure.astype(np.float32); pm[..., :3] *= pm[..., 3:4] / 255.0
    meta = {"frame": [FW, FH]}

    full = render(pm, (0, 0, FW, FH))
    base = full.copy()
    for name, h in HANDS.items():
        x0, y0, x1, y1 = h["rect"]
        base[y0 + 3:y1 - 3, x0 + 3:x1 - 3, 3] = 0           # lift the hand out of the base; the strip puts it back
        lo, hi = h["angles"]
        frames = [render(pm, h["rect"], make_weight(h), a, h["pivot"]) for a in range(lo, hi + 1)]
        strip = np.concatenate(frames, axis=1)
        Image.fromarray(strip, "RGBA").save(os.path.join(OUT, name + ".png"))
        meta[name] = {"rect": list(h["rect"]), "pivot": list(h["pivot"]), "frames": len(frames), "amin": lo, "amax": hi}
    Image.fromarray(base, "RGBA").save(os.path.join(OUT, "base.png"))

    loose = [(n, i) for n, i in sizes[1:] if n > 40]
    names = {}
    for n, i in loose:                                         # name the loose bits by where they sit and how big they are
        ys, xs = np.where(lab == i)
        cx, cy = (xs.mean() - OX) * S, (ys.mean() - OY) * S
        kind = "sparkle" if n > 600 else "dash"
        names[i] = (f"{kind}{1 if cy < 120 else 2}", cx, cy)
    for i, (name, cx, cy) in names.items():
        layer = to_rgba(rgb, lab == i)
        p = layer.astype(np.float32); p[..., :3] *= p[..., 3:4] / 255.0
        fr = render(p, (0, 0, FW, FH))
        ys, xs = np.where(fr[..., 3] > 8)
        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
        Image.fromarray(fr[y0:y1, x0:x1], "RGBA").save(os.path.join(OUT, name + ".png"))
        meta[name] = {"box": [int(x0), int(y0), int(x1), int(y1)], "center": [round(cx, 1), round(cy, 1)]}
    with open(os.path.join(OUT, "layers.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print("wrote", sorted(os.listdir(OUT)), "loose bits:", [v[0] for v in names.values()])

if __name__ == "__main__":
    main()
