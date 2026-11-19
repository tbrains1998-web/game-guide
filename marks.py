"""Visual answers: the AI says where each step's target is on the player's screenshot
(a hidden <marks> block with percentages), and we draw numbered circles on the image.
A mark can also be a "path" (a route on a map): a list of points drawn as a line with an arrow."""

import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MARKS_RULE = (
    "\n- VISUAL MARKS: screenshot sizes in pixels: {sizes} ({n} screenshot(s), numbered as listed/attached: "
    "1 = the first one in the list). If any of your steps refer to something visible in a screenshot (a button, "
    "switch, lever, valve, door, "
    "item, symbol, number), then AFTER the <notes> block output "
    '<marks>[{{"step": 1, "img": 2, "x": 43.5, "y": 62.0, "label": "Start"}}, ...]</marks> - one entry per step '
    "that points at something on screen: \"step\" (required) is that step's number in your list, \"img\" is which "
    "screenshot it is on (its number in the list, 1 = the first one listed; omit it if there is only one), x/y are "
    "PERCENTAGES (0-100, never pixels) "
    "of that image's width/height measured from the top-left corner to the CENTRE of the target, \"label\" is 1-3 "
    "words. Before giving coordinates, count the rows and columns of the panel/grid and double-check each target's "
    "row and column. If you are not sure WHICH thing is the right one (e.g. which symbol means Filter), say so in "
    "the step text and end the label with '?'. For a ROUTE on a map (the way from the player to a place) output "
    '"path": [[x, y], [x, y], ...] from the player\'s position to the target, 3-8 points following walkable ground '
    "(roads, bridges, open ground - not through walls, cliffs or water), instead of x/y, e.g. "
    '{{"step": 3, "path": [[41.0, 70.5], [48.2, 61.0], [55.0, 52.3]], "label": "Route"}}. '
    "Only mark things that are actually visible; if nothing is, output no <marks> block."
)

# A route may have at most this many points (the rule asks for 3-8; extra ones are cut off).
PATH_MAX_POINTS = 12

_MARKS_RE = re.compile(r"<marks>\s*(\[.*?\])\s*(</marks>|$)", re.S)


def _percent_point(x, y, size) -> tuple[float, float] | None:
    """One (x, y) as percentages of the image. Pixel values (> 100) are converted with `size`
    (w, h) when known; returns None when the point is unusable."""
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if x > 100 or y > 100:
        if not size:
            return None
        x, y = x / size[0] * 100, y / size[1] * 100
    if not (0 <= x <= 100 and 0 <= y <= 100):
        return None
    return x, y


def split_marks(answer: str, image_sizes=None) -> tuple[str, list[dict]]:
    """Remove the <marks> block from the answer and return it as a list of marks. A POINT mark is
    {step, img, x, y, label} with x/y as percentages of image `img` (1-based, its number in the attached list);
    a PATH mark (a route drawn on a map) is {step, img, path: [[x, y], ...], label} with 2-12 points
    and no x/y. `image_sizes` is a list of (w, h) per screenshot (a single (w, h) is accepted too). The AI
    sometimes answers in pixels despite the rule; values above 100 are converted with that image's size."""
    if image_sizes and isinstance(image_sizes, (tuple, list)) and image_sizes and isinstance(image_sizes[0], (int, float)):
        image_sizes = [tuple(image_sizes)]
    sizes = list(image_sizes or [])
    n = max(1, len(sizes))
    m = _MARKS_RE.search(answer)
    if not m:
        return answer, []
    try:
        raw = json.loads(m.group(1))
    except ValueError:
        raw = []
    marks = []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            img = int(it.get("img") or n)
        except (TypeError, ValueError):
            img = n
        img = max(1, min(img, n))
        size = sizes[img - 1] if img - 1 < len(sizes) else None
        mk = {"img": img}
        if isinstance(it.get("path"), list):
            # route: keep the usable points, in order; fewer than 2 is not a line
            pts = []
            for p in it["path"][:PATH_MAX_POINTS]:
                if isinstance(p, dict):                 # tolerate [{"x":..,"y":..}, ...]
                    p = (p.get("x"), p.get("y"))
                if not isinstance(p, (list, tuple)) or len(p) < 2:
                    continue
                q = _percent_point(p[0], p[1], size)
                if q:
                    pts.append([q[0], q[1]])
            if len(pts) < 2:
                continue
            mk["path"] = pts
        else:
            if "x" not in it or "y" not in it:
                continue
            q = _percent_point(it["x"], it["y"], size)
            if not q:
                continue
            mk["x"], mk["y"] = q
        try:
            step = int(it.get("step", len(marks) + 1))
        except (TypeError, ValueError):
            step = len(marks) + 1
        mk["step"] = step
        mk["label"] = str(it.get("label", ""))[:40]
        marks.append(mk)
    return (answer[:m.start()] + answer[m.end():]).strip(), marks


def find_buttons(image_path: str) -> list[tuple[int, int, int, int]]:
    """Rectangles that look like buttons/keys/switches (x, y, w, h), found with OpenCV: light or
    edge-bounded blobs of plausible size and shape. Returns [] if OpenCV isn't available."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []
    img = cv2.imread(image_path)
    if img is None:
        return []
    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    area_lo, area_hi = W * H * 0.00008, W * H * 0.02
    rects = []

    def collect(mask):
        cnts, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            a = w * h
            if a < area_lo or a > area_hi or w < 10 or h < 10:
                continue
            if not 0.3 <= w / h <= 4.0:
                continue
            if cv2.contourArea(c) / a < 0.55:       # must fill its box (keys are solid rectangles)
                continue
            rects.append((x, y, w, h))

    # bright keys on a dark panel
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    thr = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, -8)
    thr = cv2.morphologyEx(thr, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    collect(thr)
    # edge-bounded shapes (dark keys, outlined controls)
    edges = cv2.Canny(blur, 40, 120)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    collect(edges)
    # de-duplicate near-identical boxes
    out = []
    for r in sorted(rects, key=lambda r: r[2] * r[3]):
        if not any(abs(r[0] - o[0]) < 6 and abs(r[1] - o[1]) < 6 and abs(r[2] - o[2]) < 10 and abs(r[3] - o[3]) < 10
                   for o in out):
            out.append(r)
    return out


def snap_marks(image_path: str, marks: list[dict]) -> list[dict]:
    """Move each mark onto the button it most likely meant: the smallest detected rectangle that contains
    the point, else the nearest one within about a button's width. Adds "r" (ring radius, % of width)
    so the ring hugs the button. Marks with no button nearby are left as they are."""
    rects = find_buttons(image_path)
    if not rects:
        return marks
    from PIL import Image
    W, H = Image.open(image_path).size
    # Keys/buttons, not the icons printed on them (too small) or panel plates (too big).
    lo, hi = W * H * 0.0001, W * H * 0.015
    buttons = [r for r in rects if lo <= r[2] * r[3] <= hi and r[2] <= W * 0.2 and r[3] <= H * 0.2]
    if not buttons:
        return marks
    out = []
    for mk in marks:
        # Routes (path marks) have no x/y and must not be pulled onto buttons.
        if mk.get("path") or "x" not in mk or "y" not in mk:
            out.append(mk)
            continue
        # A hint may point at a region ("the left two columns") on purpose: leave those alone.
        if re.search(r"column|row|area|section|group|side|region|left two|right two|part of", mk.get("label", ""), re.I):
            out.append(mk)
            continue
        px, py = mk["x"] / 100 * W, mk["y"] / 100 * H
        inside = [r for r in buttons if r[0] <= px <= r[0] + r[2] and r[1] <= py <= r[1] + r[3]]
        best = max(inside, key=lambda r: r[2] * r[3]) if inside else None
        if best is None:
            def dist(r):
                cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
                return ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
            near = min(buttons, key=dist)
            if dist(near) <= 0.9 * max(near[2], near[3]) + W * 0.01:
                best = near
        if best is None:
            out.append(mk)
            continue
        x, y, w, h = best
        out.append({**mk, "x": (x + w / 2) / W * 100, "y": (y + h / 2) / H * 100,
                    "r": max(w, h) * 0.62 / W * 100, "snapped": True})
    return out


def _font(size: int):
    for name in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _draw_mark(d: ImageDraw.ImageDraw, w: int, h: int, mk: dict, strong: bool, show_label: bool = True):
    cx, cy = mk["x"] / 100 * w, mk["y"] / 100 * h
    r = int(mk["r"] / 100 * w) if mk.get("r") else max(16, int(w * 0.03))
    r = max(14, min(r, int(w * 0.08)))
    unsure = mk.get("label", "").endswith("?")
    # yellow = sure, orange = the AI said it's a best guess
    base = (255, 140, 0) if unsure else (255, 212, 0)
    col = base if strong else base + (110,)
    width = max(4, int(w * 0.006)) if strong else max(2, int(w * 0.003))
    # dark halo so the ring reads on any background, then the ring
    d.ellipse((cx - r - width, cy - r - width, cx + r + width, cy + r + width), outline=(0, 0, 0, 200 if strong else 90), width=width + 2)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=col, width=width)
    # number badge at the top-left of the ring
    br = max(12, int(r * 0.55))
    bx, by = cx - r * 0.75, cy - r * 0.75
    d.ellipse((bx - br, by - br, bx + br, by + br), fill=col if strong else base + (140,), outline=(0, 0, 0, 220), width=2)
    f = _font(int(br * 1.3))
    d.text((bx, by), str(mk["step"]), font=f, fill=(20, 20, 20), anchor="mm")
    # small target -> the zoomed inset carries the number; a text label would cover the neighbours
    if strong and show_label and mk.get("label") and r > w * 0.028:
        f2 = _font(max(14, int(w * 0.018)))
        tw = d.textlength(mk["label"], font=f2)
        lx, ly = cx, cy + r + max(8, int(w * 0.012))
        if ly + br * 1.6 > h:
            ly = cy - r - max(8, int(w * 0.012)) - br * 1.4
        d.rounded_rectangle((lx - tw / 2 - 8, ly, lx + tw / 2 + 8, ly + br * 1.5), radius=6, fill=(0, 0, 0, 190))
        d.text((lx, ly + br * 0.75), mk["label"], font=f2, fill=base, anchor="mm")


def _draw_path(d: ImageDraw.ImageDraw, w: int, h: int, mk: dict, strong: bool, show_label: bool = True):
    """A route on a map: a yellow line through the points with a dark halo under it, an arrowhead
    at the last point and the step number badge at the FIRST point (where the player starts)."""
    pts = [(p[0] / 100 * w, p[1] / 100 * h) for p in mk.get("path", []) if len(p) >= 2]
    if len(pts) < 2:
        return
    unsure = mk.get("label", "").endswith("?")
    base = (255, 140, 0) if unsure else (255, 212, 0)
    col = base if strong else base + (110,)
    width = max(4, int(w * 0.0035)) if strong else max(2, int(w * 0.002))
    halo = (0, 0, 0, 200 if strong else 90)
    # arrowhead: a triangle pointing along the last segment
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    dx, dy = x2 - x1, y2 - y1
    seg = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / seg, dy / seg
    ah = max(12, width * 3.5)                 # arrow length
    tip = (x2, y2)
    left = (x2 - ux * ah - uy * ah * 0.6, y2 - uy * ah + ux * ah * 0.6)
    right = (x2 - ux * ah + uy * ah * 0.6, y2 - uy * ah - ux * ah * 0.6)
    # the line stops a bit short of the tip so the arrowhead is a clean point
    end = (x2 - ux * ah * 0.6, y2 - uy * ah * 0.6)
    line = pts[:-1] + [end]
    # halo first (line + arrow), then the colour on top
    d.line(line, fill=halo, width=width + 4, joint="curve")
    d.polygon([tip, left, right], outline=halo, width=3)
    d.line(line, fill=col, width=width, joint="curve")
    d.polygon([tip, left, right], fill=col)
    # number badge at the start of the route
    br = max(12, int(w * 0.016))
    bx, by = pts[0]
    d.ellipse((bx - br, by - br, bx + br, by + br), fill=col if strong else base + (140,), outline=(0, 0, 0, 220), width=2)
    f = _font(int(br * 1.3))
    d.text((bx, by), str(mk["step"]), font=f, fill=(20, 20, 20), anchor="mm")
    if strong and show_label and mk.get("label"):
        # label beside the middle of the route, where it covers the least of the line
        mx, my = pts[len(pts) // 2]
        f2 = _font(max(14, int(w * 0.018)))
        tw = d.textlength(mk["label"], font=f2)
        lx = min(max(mx, tw / 2 + 8), w - tw / 2 - 8)
        ly = my + max(8, int(w * 0.012))
        if ly + br * 1.6 > h:
            ly = my - max(8, int(w * 0.012)) - br * 1.4
        d.rounded_rectangle((lx - tw / 2 - 8, ly, lx + tw / 2 + 8, ly + br * 1.5), radius=6, fill=(0, 0, 0, 190))
        d.text((lx, ly + br * 0.75), mk["label"], font=f2, fill=base, anchor="mm")


def _draw_any(d: ImageDraw.ImageDraw, w: int, h: int, mk: dict, strong: bool, show_label: bool = True):
    """Point or path, whichever this mark is."""
    if mk.get("path"):
        _draw_path(d, w, h, mk, strong, show_label)
    elif "x" in mk and "y" in mk:
        _draw_mark(d, w, h, mk, strong, show_label)


def _add_inset(img: Image.Image, clean: Image.Image, mk: dict) -> Image.Image:
    """Small target (tiny button, small text)? Paste a magnified crop of it in the corner furthest away,
    with the ring drawn inside the crop too, so the player can actually see what is circled."""
    w, h = img.size
    r = int(mk["r"] / 100 * w) if mk.get("r") else int(w * 0.03)
    if r > w * 0.028:                 # big enough to see as it is
        return img
    cx, cy = mk["x"] / 100 * w, mk["y"] / 100 * h
    side = int(max(r * 7, w * 0.11))
    box = (int(max(0, cx - side / 2)), int(max(0, cy - side / 2)),
           int(min(w, cx + side / 2)), int(min(h, cy + side / 2)))
    crop = clean.crop(box).convert("RGBA")
    zoom = max(2.0, min(4.0, (w * 0.3) / side))
    crop = crop.resize((int(crop.width * zoom), int(crop.height * zoom)), Image.LANCZOS)
    # ring on the zoomed crop
    d = ImageDraw.Draw(crop)
    zx, zy, zr = (cx - box[0]) * zoom, (cy - box[1]) * zoom, max(r * zoom, 12)
    lw = max(3, int(zr * 0.12))
    d.ellipse((zx - zr - lw, zy - zr - lw, zx + zr + lw, zy + zr + lw), outline=(0, 0, 0, 220), width=lw + 2)
    d.ellipse((zx - zr, zy - zr, zx + zr, zy + zr), outline=(255, 212, 0), width=lw)
    # frame + "zoom" tag
    frame = Image.new("RGBA", (crop.width + 8, crop.height + 8), (255, 212, 0, 255))
    frame.paste(crop, (4, 4))
    fd = ImageDraw.Draw(frame)
    f = _font(max(12, int(w * 0.014)))
    tag = f"{mk['step']}  zoomed"
    tw = fd.textlength(tag, font=f)
    fd.rectangle((0, 0, tw + 12, int(w * 0.014) + 10), fill=(255, 212, 0, 255))
    fd.text((6, 5), tag, font=f, fill=(20, 20, 20))
    # corner furthest from the target
    m = int(w * 0.015)
    px = m if cx > w / 2 else w - frame.width - m
    py = m if cy > h / 2 else h - frame.height - m
    out = img.convert("RGBA")
    out.alpha_composite(frame, (px, py))
    # thin leader line from the inset to the target
    ld = ImageDraw.Draw(out)
    ld.line((px + frame.width / 2, py + frame.height / 2, cx, cy), fill=(255, 212, 0, 160), width=max(2, int(w * 0.0015)))
    out.alpha_composite(frame, (px, py))
    return out.convert("RGB")


def render(image_paths, marks: list[dict]) -> dict:
    """Draw the marks (points and paths) on the screenshot(s) they belong to ("img", 1-based; default = the
    last one). Writes <image>-m<step>.jpg (that step highlighted, zoomed inset when a small point) per marked step and
    <image>-mall.jpg per image. Returns {"all": [paths], "steps": {step: path},
    "marks": the snapped marks, each with "img" and "shot" (its image's path)} - the overlay uses those."""
    paths = [image_paths] if isinstance(image_paths, str) else list(image_paths)
    n = len(paths)
    out = {"all": [], "steps": {}, "marks": []}
    by_img: dict[int, list] = {}
    for mk in marks:
        try:
            i = int(mk.get("img") or n)
        except (TypeError, ValueError):
            i = n
        i = max(1, min(i, n))
        by_img.setdefault(i, []).append({**mk, "img": i})
    for i in sorted(by_img):
        path = paths[i - 1]
        group = by_img[i]
        try:
            group = snap_marks(path, group)
        except Exception:
            pass
        for mk in group:
            mk["shot"] = path
        out["marks"] += group
        src = Image.open(path).convert("RGBA")
        w, h = src.size
        base = Path(path)

        def make(strong_step, group=group, src=src, w=w, h=h):
            layer = Image.new("RGBA", src.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            for mk in group:
                if strong_step is not None and mk["step"] != strong_step:
                    _draw_any(d, w, h, mk, strong=False)
            for mk in group:                       # highlighted ring/route drawn last, on top
                if strong_step is None or mk["step"] == strong_step:
                    # the all-steps picture keeps just the numbers; labels would overlap each other
                    _draw_any(d, w, h, mk, strong=True, show_label=strong_step is not None)
            img = Image.alpha_composite(src, layer).convert("RGB")
            if strong_step is not None:
                # zoomed inset only for small point targets; a route is big enough as it is
                cur = [mk for mk in group if mk["step"] == strong_step and not mk.get("path") and "x" in mk]
                if cur:
                    img = _add_inset(img, src.convert("RGB"), cur[0])
            return img

        p = base.with_name(base.stem + "-mall.jpg")
        make(None).save(p, quality=88)
        out["all"].append(str(p))
        for step in sorted({mk["step"] for mk in group}):
            if step in out["steps"]:
                continue
            p = base.with_name(f"{base.stem}-m{step}.jpg")
            make(step).save(p, quality=88)
            out["steps"][step] = str(p)
    return out
