"""In-game overlay for Game Guide.

A transparent, click-through, always-on-top window the size of the game's monitor. It shows the
current step in a corner and draws that step's circle (or route line, for "path" marks) directly over
the game, at the same spot as on the screenshot (the screenshot is a capture of the same monitor, so
percentages map 1:1).

Nothing is drawn when the game isn't in front, when the overlay is turned off, or while a screenshot
is being taken (so it never captures itself). Tk runs in its own thread; the public methods are
thread-safe (they queue work for that thread).

A third layer ("qpath", fed by set_path()) draws the quest path: glowing dots on the ground toward the
target, a small end arrow, and an edge arrow + label when the target is off screen. It sits under
everything else and, like Sense, ignores the Overlay switch.

A second, separate layer ("Sense", fed by set_sense()) pulses a ring over every nearby item and
draws a small arrow at the screen edge for items that are off screen. It carries the canvas tag
"sense" and is the only thing redrawn on every 50 ms tick, so the step layer stays still.
"""

import ctypes
import math
import queue
import re
import threading
import time

KEY = "#010203"                    # colour key: pixels of exactly this colour are see-through
YELLOW, ORANGE = "#ffd400", "#ff8c00"
PANEL, PANEL_EDGE, TEXT, DIM = "#14171c", "#3a4150", "#f2f4f8", "#9aa3b2"
SENSE, SENSE_HALO = "#e8f6ff", "#2f8fb0"   # Sense rings/arrows: cool white on a faint cyan halo
SENSE_MAX = 8                              # most items the Sense layer draws at once
QPATH_MAX = 40                             # most quest-path dots drawn at once
QPATH_DOT, QPATH_GLOW = "#d6eef7", "#4b9fba"   # quest-path dots: soft pale-cyan core on a muted cyan glow
QPATH_CORE = "#a9d3e2"                     # core colour away from the shimmer (softer than the Sense white)
QPATH_R_MIN, QPATH_R_MAX = 0.1, 0.8        # dot radius clamp, % of screen width (questpath sends 0.1..0.8)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80, 0x20, 0x80000, 0x08000000
HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = -1, 0x2, 0x1, 0x10


def strip_md(t: str) -> str:
    t = re.sub(r"\[\d+\]|https?://\S+", "", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"[*_`#>]", "", t)
    return re.sub(r"\s+", " ", t).strip()


_ITEM = re.compile(r"^\s*(?:(\d+)[.)]|[-•*])\s+(.*\S)")


def path_points(m) -> list:
    """The usable points of a "path" mark as [(x%, y%), ...], or [] if it isn't a drawable path
    (fewer than 2 good points). Bad entries are skipped, never raised on: the model wrote them."""
    raw = m.get("path") if isinstance(m, dict) else None
    if not isinstance(raw, (list, tuple)):
        return []
    pts = []
    for p in raw:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            pts.append((x, y))
    return pts if len(pts) >= 2 else []


def mark_kind(m):
    """"path" for a route mark, "point" for a circle, None for a mark we can't draw (skip it)."""
    if not isinstance(m, dict):
        return None
    if path_points(m):
        return "path"
    try:
        x, y = float(m["x"]), float(m["y"])
    except (KeyError, TypeError, ValueError):
        return None
    return "point" if math.isfinite(x) and math.isfinite(y) else None


def split_steps(markdown: str) -> list[dict]:
    """The same split as the window uses: every list item is a step (numbered in order), any other
    paragraph is a step too (num None). Marks refer to steps by that number."""
    steps, num, para = [], 0, []

    def flush():
        if para:
            steps.append({"text": strip_md(" ".join(para)), "num": None})
            para.clear()

    for line in markdown.splitlines():
        m = _ITEM.match(line)
        if m:
            flush()
            num += 1
            steps.append({"text": strip_md(m.group(2)), "num": num})
        elif line.strip():
            if steps and steps[-1]["num"] is not None and not para and line.startswith("  "):
                steps[-1]["text"] = strip_md(steps[-1]["text"] + " " + line)   # wrapped list item
            else:
                para.append(line.strip())
        else:
            flush()
    flush()
    return [s for s in steps if s["text"]]


def _num(d: dict, key: str, default=None):
    """d[key] as a finite float, else default (sense.py's numbers; never raise on a bad one)."""
    try:
        v = float(d.get(key))
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def clean_sense(points) -> list:
    """The drawable Sense points out of what sense.py sent ([{"x","y","dist","kind","edge","angle"}, ...],
    nearest first). Bad entries are skipped, at most SENSE_MAX are kept. A ring needs x and y; an edge
    arrow needs an angle or an x/y spot on the border."""
    if not isinstance(points, (list, tuple)):
        return []
    out = []
    for p in points:
        if not isinstance(p, dict):
            continue
        edge = bool(p.get("edge"))
        x, y, angle = _num(p, "x"), _num(p, "y"), _num(p, "angle")
        spot = x is not None and y is not None
        if (edge and not spot and angle is None) or (not edge and not spot):
            continue
        dist = _num(p, "dist")                  # None when missing/bad: the arrow then shows no "N m" label
        out.append({"x": x, "y": y, "dist": max(0.0, dist) if dist is not None else None,
                    "kind": str(p.get("kind") or ""),
                    "edge": edge, "angle": angle})
        if len(out) >= SENSE_MAX:
            break
    return out


def _pt(d):
    """{"x","y","angle"} with finite numbers out of a questpath "end"/"edge" dict, else None (angle may be None)."""
    if not isinstance(d, dict):
        return None
    x, y, a = _num(d, "x"), _num(d, "y"), _num(d, "angle")
    if x is None or y is None:
        return None
    return {"x": max(-1e4, min(1e4, x)), "y": max(-1e4, min(1e4, y)), "angle": a}


def mask_slice(box, w, h):
    """A normalised (x0, y0, x1, y1) box -> (row0, row1, col0, col1) slice bounds inside a w x h grid, or None
    when the box is bad or lies fully off the grid. Clamped to 0..1 first, so a box that ends above or left
    of the screen can't turn into a negative slice end (mask[0:-1] would hide almost everything)."""
    try:
        x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in box)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or x1 <= 0 or y1 <= 0 or x0 >= 1 or y0 >= 1:
        return None
    r0, r1 = int(y0 * h), min(h, int(y1 * h) + 1)
    c0, c1 = int(x0 * w), min(w, int(x1 * w) + 1)
    if r1 <= r0 or c1 <= c0:
        return None
    return r0, r1, c0, c1


def clean_path(data):
    """The drawable quest path out of what questpath.compute() sent (via sense.py's on_path):
    {"dots":[{"x","y","r","s"}], "end":{"x","y","angle"}|None, "edge":{"x","y","angle"}|None,
    "mark":{"x","y"}|None, "dist", "floors", "label", "kind", "src"} (x/y in % of the screen, r in % of W).
    Returns None when there is nothing to draw (None, not a dict, no good dot, no edge and no mark). Bad dots are skipped, at most QPATH_MAX kept; never raises."""
    if not isinstance(data, dict):
        return None
    dots = []
    raw = data.get("dots")
    if isinstance(raw, (list, tuple)):
        for d in raw:
            if not isinstance(d, dict):
                continue
            x, y = _num(d, "x"), _num(d, "y")
            if x is None or y is None or not (-20 <= x <= 120 and -20 <= y <= 120):
                continue
            r = _num(d, "r", 0.3)
            dots.append({"x": x, "y": y, "r": max(QPATH_R_MIN, min(QPATH_R_MAX, r)), "s": _num(d, "s", float(len(dots) + 1))})
            if len(dots) >= QPATH_MAX:
                break
    end = _pt(data.get("end")) if dots else None
    edge = _pt(data.get("edge"))
    if edge is not None and edge["angle"] is None:
        edge["angle"] = math.degrees(math.atan2(edge["y"] - 50.0, edge["x"] - 50.0))
    # "mark" = the target's own spot when it is on screen (questpath always sends the key: dict or None).
    # It keeps the path alive when every trail dot is off screen (far/high target, camera pitched up).
    mark = _pt(data.get("mark")) if edge is None else None
    if mark is not None and not (-20 <= mark["x"] <= 120 and -20 <= mark["y"] <= 120):
        mark = None
    if not dots and edge is None and mark is None:
        return None
    dist = _num(data, "dist")
    label = data.get("label")
    if isinstance(label, bool) or not isinstance(label, (str, int, float)):
        label = ""
    label = str(label).strip()[:40]
    if not label and dist is not None:
        label = f"{max(0.0, dist):.0f} m"
    try:
        floors = int(data.get("floors") or 0)
    except (TypeError, ValueError, OverflowError):
        floors = 0
    return {"dots": dots, "end": end, "edge": edge, "mark": mark, "dist": max(0.0, dist) if dist is not None else None,
            "floors": floors, "label": label, "kind": str(data.get("kind") or ""), "src": str(data.get("src") or "")}


class Overlay:
    def __init__(self, log=print, keys=None):
        self.log = log
        self.keys = keys or {"next": "F7", "toggle": "F6"}
        self.q: queue.Queue = queue.Queue()
        self.enabled = True
        self.game_front = False
        self.mon = None                  # (left, top, right, bottom) of the game's monitor
        self.answer = None               # {"steps", "marks", "pos", "shot"}
        self.dismissed = False
        self.status = ""
        self.suspended = False
        self.view_ok = True
        self._refs = {}                  # per screenshot: thumbnail for the "did the view change" check
        self._boxes: list = []           # what we drew (normalised), masked out of the view check
        self.sense = None                # Sense layer: cleaned points from set_sense(), None = nothing near
        self._sense_boxes: list = []     # the Sense layer's boxes, masked out of the view check too
        self._sense_drawn = False        # True while "sense" items are on the canvas (so None can clear them)
        self.qpath = None                # quest-path layer: clean_path() result, None = no target / switch off
        self._qpath_boxes: list = []     # the quest path's boxes, masked out of the view check
        self._qpath_drawn = False        # True while "qpath" items are on the canvas
        self._root = None
        self._dirty = True
        self._hide_at = None
        self._last_tick = 0.0
        self.ready = threading.Event()
        threading.Thread(target=self._run, name="overlay", daemon=True).start()

    # ------------------------------------------------------------- public (any thread)
    def _post(self, fn):
        self.q.put(fn)

    def _set(self, **kv):
        def f():
            for k, v in kv.items():
                setattr(self, k, v)
            self._dirty = True
        self._post(f)

    def set_enabled(self, on: bool):
        self._set(enabled=bool(on))

    def set_game_front(self, front: bool):
        if front != self.game_front:
            self._set(game_front=bool(front))

    def set_monitor(self, rect):
        if rect and tuple(rect) != self.mon:
            self._post(lambda: self._set_monitor(tuple(rect)))

    def set_status(self, text: str):
        self._set(status=text or "")

    def show_answer(self, steps: list, marks: list, shots=None, pos: int = 0):
        """marks: snapped marks, each with "step" and "shot" (the path of the screenshot it was made on)."""
        if isinstance(shots, str):
            shots = [shots]
        def f():
            self.answer = {"steps": steps, "marks": marks or [], "pos": pos, "shots": list(shots or [])}
            self.dismissed = False
            self.status = ""
            self._refs = {}
            self.view_ok = True
            self._hide_at = None
            self._dirty = True
        self._post(f)

    def set_pos(self, pos: int):
        def f():
            if self.answer:
                self.answer["pos"] = pos
                self.dismissed = False
                self._hide_at = None
                self._dirty = True
        self._post(f)

    def finished(self):
        """Past the last step: say so briefly, then hide."""
        def f():
            self.status = "That was the last step."
            self._hide_at = time.time() + 4
            self._dirty = True
        self._post(f)

    def clear(self):
        """Forget the current answer and hide everything (the Game Guide window was closed)."""
        def f():
            self.answer = None
            self.status = ""
            self._dirty = True
        self._post(f)

    def toggle(self):
        def f():
            if self.answer:
                self.dismissed = not self.dismissed
                self._dirty = True
        self._post(f)

    def suspend(self):
        """Blocking: clear the drawing so a screenshot taken right after doesn't include us."""
        if not self.ready.is_set():
            return
        done = threading.Event()

        def f():
            self.suspended = True
            self._draw()
            try:
                self._root.update()
            except Exception:
                pass
            done.set()
        self._post(f)
        done.wait(0.5)
        time.sleep(0.03)        # one frame for the compositor

    def resume(self):
        self._set(suspended=False)

    def set_sense(self, points):
        """The Sense layer: nearby items as [{"x": %, "y": %, "dist": m, "kind", "edge": bool, "angle": deg}, ...]
        (nearest first) or None/[] for nothing near. Comes up to 10x a second from sense.py: it does NOT
        mark the step layer dirty, the pump redraws just the "sense" tag on its next tick."""
        pts = clean_sense(points) or None

        def f():
            self.sense = pts
        self._post(f)

    def set_path(self, data):
        """The quest-path layer: questpath.compute()'s dict, or None for no target / switch off. Like
        set_sense it does NOT mark the step layer dirty; the pump redraws the "qpath" tag on its next tick.
        Not tied to the Overlay switch (same gate as Sense: suspended / game in front / monitor)."""
        d = clean_path(data)

        def f():
            self.qpath = d
        self._post(f)

    # ------------------------------------------------------------- Tk thread
    def _run(self):
        try:
            import tkinter as tk
        except Exception as e:
            self.log(f"overlay: tkinter unavailable ({e})")
            return
        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", KEY)
        root.configure(bg=KEY)
        root.geometry("8x8+0+0")
        self.canvas = tk.Canvas(root, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        root.deiconify()
        root.update_idletasks()
        self._apply_styles()
        self.ready.set()
        root.after(50, self._pump)
        root.mainloop()

    def _hwnds(self):
        """The real top-level window only. (Tk's inner client window must NOT get WS_EX_LAYERED:
        a layered child window is never painted.)"""
        try:
            h = self._root.winfo_id()
            top = ctypes.windll.user32.GetAncestor(h, 2)      # GA_ROOT
            return [top or h]
        except Exception:
            return []

    def _apply_styles(self):
        u = ctypes.windll.user32
        u.GetWindowLongW.restype = ctypes.c_long
        for h in self._hwnds():
            ex = u.GetWindowLongW(h, GWL_EXSTYLE)
            u.SetWindowLongW(h, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE)
            u.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _set_monitor(self, rect):
        self.mon = rect
        l, t, r, b = rect
        self._root.geometry(f"{r - l}x{b - t}+{l}+{t}")
        self._dirty = True

    def _pump(self):
        try:
            while True:
                fn = self.q.get_nowait()
                try:
                    fn()
                except Exception as e:
                    self.log(f"overlay task: {e}")
        except queue.Empty:
            pass
        now = time.time()
        if self._hide_at and now > self._hide_at:
            self._hide_at, self.status, self.dismissed, self._dirty = None, "", True, True
        if now - self._last_tick > 2:
            self._last_tick = now
            try:
                self._apply_styles()          # some games re-assert topmost; stay above them
                self._view_check()
            except Exception as e:
                self.log(f"overlay tick: {e}")
        if self._dirty:
            self._draw()
        elif self.sense or self._sense_drawn or self.qpath or self._qpath_drawn:
            self._draw_qpath()            # animate the trail and the Sense rings without touching the step layer
            self._draw_sense()
        self._root.after(50, self._pump)

    # ------------------------------------------------------------- drawing
    def _draw(self):
        """Everything from scratch: the step layer, then the Sense layer on top of it."""
        self._dirty = False
        c = self.canvas
        c.delete("all")
        self._boxes = []
        self._sense_boxes = []
        self._sense_drawn = False
        self._qpath_boxes = []
        self._qpath_drawn = False
        if self.suspended or not self.game_front or not self.mon:
            return
        W, H = self.mon[2] - self.mon[0], self.mon[3] - self.mon[1]
        if self.enabled:                  # the Overlay switch hides only the step layer
            self._draw_steps(W, H)
        self._draw_qpath()                # quest path: own switch in sense.py (sends None when off), lowest layer
        self._draw_sense()                # Sense has its own switch (sense.py sends [] when off)

    def _draw_steps(self, W, H):
        """The status panel, or the current step's circle/line and its panel."""
        if self.status:
            self._panel(W, H, "Game Guide", self.status, small=True)
            return
        a = self.answer
        if not a or self.dismissed or not a["steps"]:
            return
        steps = a["steps"]
        pos = max(0, min(a["pos"], len(steps) - 1))
        step = steps[pos]
        marks = self._current_marks()
        rings = []                      # (cx, cy, r) spots the step panel must not cover
        if self.view_ok:
            for m in marks:
                try:
                    if mark_kind(m) == "path":
                        rings.extend(self._path(W, H, m))
                    else:
                        rings.append(self._ring(W, H, m))
                except Exception as e:
                    self.log(f"overlay mark: {e}")      # one bad mark must not hide the whole step
        title = f"Step {pos + 1} of {len(steps)}    {self.keys['next']} = next    {self.keys['toggle']} = hide"
        note = ""
        if marks and not self.view_ok:
            what = "Line" if all(mark_kind(m) == "path" for m in marks) else "Circle"
            note = f"{what} hidden: the view changed. Look back at where you took the screenshot."
        self._panel(W, H, title, step["text"], note=note, avoid=rings)

    # ------------------------------------------------------------- Sense layer (tag "sense")
    def _draw_sense(self):
        """A breathing ring on every nearby item, an edge arrow for the ones off screen. Only the "sense"
        tag is deleted and redrawn, so this is cheap enough to run every 50 ms tick."""
        c = self.canvas
        c.delete("sense")
        self._sense_boxes = []
        self._sense_drawn = False
        pts = self.sense
        if not pts or self.suspended or not self.game_front or not self.mon:
            return
        W, H = self.mon[2] - self.mon[0], self.mon[3] - self.mon[1]
        breathe = 0.5 - 0.5 * math.cos(2 * math.pi * (time.time() % 1.0))     # 0 -> 1 -> 0 over 1 s
        for p in pts:
            try:
                if p["edge"]:
                    self._sense_arrow(W, H, p)
                else:
                    self._sense_ring(W, H, p, breathe)
            except Exception as e:
                self.log(f"overlay sense: {e}")        # one bad point must not hide the others
        self._sense_drawn = True

    def _sense_ring(self, W, H, p, breathe):
        """Cool white ring with a faint cyan halo; the radius breathes 1.2 % -> 2.6 % of W, a bit smaller
        the farther the item is (30 m = 3/4 size)."""
        cx, cy = p["x"] / 100 * W, p["y"] / 100 * H
        near = 1.0 - 0.25 * min(1.0, (p.get("dist") or 0.0) / 30.0)
        r = (1.2 + 1.4 * breathe) / 100 * W * near
        lw = max(3, int(W * 0.0025))
        c = self.canvas
        # halo first (a little wider), then the white ring on top of it
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=SENSE_HALO, width=lw + 4, tags="sense")
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=SENSE, width=lw, tags="sense")
        pad = 2.6 / 100 * W + lw + 6          # the biggest the ring gets, so the mask box holds still
        self._sense_boxes.append(((cx - pad) / W, (cy - pad) / H, (cx + pad) / W, (cy + pad) / H))

    # ------------------------------------------------------------- quest-path layer (tag "qpath")
    def _draw_qpath(self):
        """Glowing dots on the ground toward the target with a shimmer running along them, a small arrow
        at the far end, and an edge arrow + label when the target is off screen. Only the "qpath" tag is
        redrawn, then lowered under everything else (step panel, Sense rings)."""
        c = self.canvas
        c.delete("qpath")
        self._qpath_boxes = []
        self._qpath_drawn = False
        q = getattr(self, "qpath", None)      # (getattr: tests build Overlay with __new__)
        if not q or self.suspended or not self.game_front or not self.mon:
            return
        W, H = self.mon[2] - self.mon[0], self.mon[3] - self.mon[1]
        try:
            self._qpath_dots(W, H, q)
        except Exception as e:
            self.log(f"overlay qpath: {e}")
        if q.get("end") and not q.get("edge"):
            try:
                self._qpath_end(W, H, q)
            except Exception as e:
                self.log(f"overlay qpath: {e}")
        if q.get("mark") and not q.get("edge"):
            try:
                self._qpath_mark(W, H, q, with_label=not (q.get("end") and q.get("dots")))
            except Exception as e:
                self.log(f"overlay qpath: {e}")
        if q.get("edge"):
            try:
                e = q["edge"]
                self._sense_arrow(W, H, {"x": e["x"], "y": e["y"], "angle": e["angle"], "dist": q.get("dist")},
                                  tag="qpath", text=q.get("label") or None, boxes=self._qpath_boxes)
            except Exception as ex:
                self.log(f"overlay qpath: {ex}")
        try:
            c.tag_lower("qpath")
        except Exception:
            pass
        self._qpath_drawn = True

    def _qpath_dots(self, W, H, q):
        """Each dot: a thin muted-cyan glow ring, then a soft pale core with a thin outline. A gentle bright wave
        runs from the feet outward (one pass every 1.6 s, about 6 dots wide), so the trail reads as "this way"."""
        dots = q["dots"]
        if not dots:
            return
        c = self.canvas
        phase = (time.time() % 1.6) / 1.6 * (len(dots) + 6) - 3
        xs, ys, pad = [], [], 0.0
        for i, d in enumerate(dots):
            cx, cy = d["x"] / 100 * W, d["y"] / 100 * H
            base = max(2.0, d["r"] / 100 * W)
            wave = max(0.0, 1.0 - abs(i - phase) / 3.0)            # 0..1, peaks where the shimmer is
            r = base * (1.0 + 0.25 * wave)
            g = r * (1.5 + 0.4 * wave)
            c.create_oval(cx - g, cy - g, cx + g, cy + g, fill="", outline=QPATH_GLOW,
                          width=max(1, min(4, int(r * (0.18 + 0.2 * wave)))), tags="qpath")
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=QPATH_DOT if wave > 0.15 else QPATH_CORE,
                          outline=QPATH_GLOW, width=1, tags="qpath")
            xs.append(cx)
            ys.append(cy)
            pad = max(pad, base * 1.25 * 1.9 + 4)        # the biggest glow, so the mask box holds still
        self._qpath_boxes.append(((min(xs) - pad) / W, (min(ys) - pad) / H, (max(xs) + pad) / W, (max(ys) + pad) / H))

    def _qpath_end(self, W, H, q):
        """A small filled arrow at the last dot, pointing on along the trail, with the label beside it
        (the label carries the floor hint, e.g. "↑ 2 floors · 42 m")."""
        e = q["end"]
        ex, ey = e["x"] / 100 * W, e["y"] / 100 * H
        ang = e["angle"]
        if ang is None:
            ds = q["dots"]
            if len(ds) >= 2:
                ang = math.degrees(math.atan2((ds[-1]["y"] - ds[-2]["y"]) * H, (ds[-1]["x"] - ds[-2]["x"]) * W))
            else:
                ang = -90.0
        a = math.radians(ang)
        dx, dy = math.cos(a), math.sin(a)
        L = max(18.0, W * 0.014)
        tipx, tipy = ex + dx * L, ey + dy * L
        nx, ny = -dy * L * 0.5, dx * L * 0.5
        lw = max(2, int(W * 0.0015))
        c = self.canvas
        c.create_polygon(tipx, tipy, ex + nx, ey + ny, ex - nx, ey - ny,
                         fill="", outline="#000000", width=lw + 4, joinstyle="round", tags="qpath")
        c.create_polygon(tipx, tipy, ex + nx, ey + ny, ex - nx, ey - ny,
                         fill=QPATH_DOT, outline=QPATH_GLOW, width=lw, joinstyle="round", tags="qpath")
        txt = q.get("label") or ""
        pad = L + lw + 6
        if txt:
            fs = max(12, int(W * 0.009))
            lx, ly = ex + L * 1.2, ey - L * 1.2
            for ox, oy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                c.create_text(lx + ox, ly + oy, text=txt, anchor="w", fill="#000000",
                              font=("Segoe UI", -fs, "bold"), tags="qpath")
            c.create_text(lx, ly, text=txt, anchor="w", fill=QPATH_DOT, font=("Segoe UI", -fs, "bold"), tags="qpath")
            tw = fs * 0.6 * len(txt)
            self._qpath_boxes.append(((lx - 4) / W, (ly - fs) / H, (lx + tw + 4) / W, (ly + fs) / H))
        self._qpath_boxes.append(((ex - pad) / W, (ey - pad) / H, (ex + pad) / W, (ey + pad) / H))

    def _qpath_mark(self, W, H, q, with_label=True):
        """The target itself, when it is on screen: a small ring with a down-pointing tick above it, and the
        label beside it (only when the end arrow did not already draw the label)."""
        m = q["mark"]
        mx, my = m["x"] / 100 * W, m["y"] / 100 * H
        R = max(9.0, W * 0.006)
        lw = max(2, int(W * 0.0012))
        c = self.canvas
        for col, wd in (("#000000", lw + 3), (QPATH_GLOW, lw)):
            c.create_oval(mx - R, my - R, mx + R, my + R, fill="", outline=col, width=wd, tags="qpath")
        T = R * 1.4                                      # tick: a small triangle above the ring, tip pointing down
        tip = my - R - 3
        pts = (mx, tip, mx - T * 0.55, tip - T, mx + T * 0.55, tip - T)
        c.create_polygon(*pts, fill="", outline="#000000", width=lw + 3, joinstyle="round", tags="qpath")
        c.create_polygon(*pts, fill=QPATH_DOT, outline=QPATH_GLOW, width=lw, joinstyle="round", tags="qpath")
        pad = R + lw + 6
        top = tip - T - lw - 4
        txt = (q.get("label") or "") if with_label else ""
        if txt:
            fs = max(12, int(W * 0.009))
            lx, ly = mx + R + 8, my
            for ox, oy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                c.create_text(lx + ox, ly + oy, text=txt, anchor="w", fill="#000000",
                              font=("Segoe UI", -fs, "bold"), tags="qpath")
            c.create_text(lx, ly, text=txt, anchor="w", fill=QPATH_DOT, font=("Segoe UI", -fs, "bold"), tags="qpath")
            tw = fs * 0.6 * len(txt)
            self._qpath_boxes.append(((lx - 4) / W, (ly - fs) / H, (lx + tw + 4) / W, (ly + fs) / H))
        self._qpath_boxes.append(((mx - pad) / W, top / H, (mx + pad) / W, (my + pad) / H))

    def _sense_arrow(self, W, H, p, tag="sense", text=None, boxes=None):
        """A big outlined triangle at the screen border pointing out toward an off-screen item.
        angle is in degrees in screen (pixel) space, the same convention sense.py sends: 0 = right,
        90 = down, -90 = up, +-180 = left (y grows downward, so dy = +sin). The spot (x, y) is where
        sense.py clamped the direction to the border; without one we walk from the centre along the
        angle to a 4 % margin ourselves."""
        x, y, ang = p["x"], p["y"], p["angle"]
        if x is not None and y is not None:
            px, py = x / 100 * W, y / 100 * H
            if ang is None:
                ang = math.degrees(math.atan2(py - H / 2, px - W / 2))
            a = math.radians(ang)
            dx, dy = math.cos(a), math.sin(a)
        else:
            a = math.radians(ang)
            dx, dy = math.cos(a), math.sin(a)
            hw, hh = W * 0.46, H * 0.46
            t = min(hw / abs(dx) if dx else float("inf"), hh / abs(dy) if dy else float("inf"))
            px, py = W / 2 + dx * t, H / 2 + dy * t
        # Big and easy to spot: ~4.5 % of the screen
        # width, a thick black outline so it reads on bright and dark scenes, a gentle size pulse, and the
        # distance written next to it.
        breathe = 0.5 - 0.5 * math.cos(2 * math.pi * (time.time() % 1.0))
        L = max(36.0, W * 0.045) * (1.0 + 0.12 * breathe)
        px, py = min(max(px, L), W - L), min(max(py, L), H - L)      # the whole arrow stays on screen
        tipx, tipy = px + dx * L / 2, py + dy * L / 2
        bx, by = px - dx * L / 2, py - dy * L / 2
        nx, ny = -dy * L * 0.45, dx * L * 0.45                         # half the base, across the arrow
        lw = max(3, int(W * 0.0022))
        c = self.canvas
        c.create_polygon(tipx, tipy, bx + nx, by + ny, bx - nx, by - ny,
                         fill="", outline="#000000", width=lw + 6, joinstyle="round", tags=tag)
        c.create_polygon(tipx, tipy, bx + nx, by + ny, bx - nx, by - ny,
                         fill=SENSE, outline=SENSE_HALO, width=lw, joinstyle="round", tags=tag)
        dist = p.get("dist")
        if text or dist is not None:
            # label sits on the screen side of the arrow (behind its base), so it never leaves the screen
            lx, ly = px - dx * L * 1.15, py - dy * L * 1.15
            fs = max(14, int(W * 0.011))
            txt = text or f"{dist:.0f} m"
            for ox, oy in ((-2, 0), (2, 0), (0, -2), (0, 2)):           # black outline around the text
                c.create_text(lx + ox, ly + oy, text=txt, fill="#000000", font=("Segoe UI", -fs, "bold"), tags=tag)
            c.create_text(lx, ly, text=txt, fill=SENSE, font=("Segoe UI", -fs, "bold"), tags=tag)
        pad = L * 1.6 + lw + 8
        (self._sense_boxes if boxes is None else boxes).append(((px - pad) / W, (py - pad) / H, (px + pad) / W, (py + pad) / H))

    def _badge(self, bx, by, br, col, step):
        """The numbered dot that says which step a circle or line belongs to."""
        c = self.canvas
        c.create_oval(bx - br - 2, by - br - 2, bx + br + 2, by + br + 2, fill="#000000", outline="")
        c.create_oval(bx - br, by - br, bx + br, by + br, fill=col, outline="")
        c.create_text(bx, by, text=str(step), font=("Segoe UI", -int(br * 1.3), "bold"), fill="#141414")

    def _path(self, W, H, m):
        """A route: a yellow line through the mark's points with a black halo under it, an arrowhead at
        the end and the step badge at the start. Returns (x, y, r) spots for the panel to avoid."""
        pts = [(x / 100 * W, y / 100 * H) for x, y in path_points(m)]
        unsure = str(m.get("label", "")).endswith("?")
        col = ORANGE if unsure else YELLOW
        lw = max(4, int(W * 0.003))
        ah = max(12, int(lw * 3.5))                   # arrowhead length
        flat = [v for p in pts for v in p]
        c = self.canvas
        # halo first (wider, black), then the colour line on top; both carry the arrow so the halo
        # outlines the head too
        c.create_line(*flat, fill="#000000", width=lw + 4, capstyle="round", joinstyle="round",
                      arrow="last", arrowshape=(ah + 2, ah * 1.3 + 3, ah * 0.6 + 2))
        c.create_line(*flat, fill=col, width=lw, capstyle="round", joinstyle="round",
                      arrow="last", arrowshape=(ah, ah * 1.3, ah * 0.6))
        br = max(13, int(W * 0.012))
        bx, by = pts[0]
        self._badge(bx, by, br, col, m.get("step", ""))
        # mask box for the view check: the line's bounding box plus room for halo, arrow and badge
        pad = lw + 4 + max(br + 2, ah)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self._boxes.append(((min(xs) - pad) / W, (min(ys) - pad) / H, (max(xs) + pad) / W, (max(ys) + pad) / H))
        return [(x, y, br + lw) for x, y in pts]

    def _ring(self, W, H, m):
        cx, cy = float(m["x"]) / 100 * W, float(m["y"]) / 100 * H
        r = (m.get("r") or 3.0) / 100 * W
        r = max(18, min(r, W * 0.08))
        unsure = str(m.get("label", "")).endswith("?")
        col = ORANGE if unsure else YELLOW
        lw = max(5, int(W * 0.0035))
        c = self.canvas
        c.create_oval(cx - r - lw, cy - r - lw, cx + r + lw, cy + r + lw, outline="#000000", width=lw + 4)
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=col, width=lw)
        br = max(13, int(r * 0.5))
        bx, by = cx - r * 0.8, cy - r * 0.8
        self._badge(bx, by, br, col, m.get("step", ""))
        pad = r + lw + br + 6
        self._boxes.append(((cx - pad) / W, (cy - pad) / H, (cx + pad) / W, (cy + pad) / H))
        return (cx, cy, r)

    def _panel(self, W, H, title, text, small=False, note="", avoid=()):
        c = self.canvas
        margin = int(W * 0.018)
        width = int(W * (0.30 if small else 0.36))
        big = max(16, int(H * (0.020 if small else 0.026)))
        sml = max(12, int(H * 0.016))
        pad = int(big * 0.8)
        # measure first (draw at 0,0), then place
        items = []
        y = pad
        items.append(c.create_text(pad, y, text=title, anchor="nw", width=width, fill=DIM, font=("Segoe UI", -sml)))
        y = c.bbox(items[-1])[3] + int(sml * 0.5)
        items.append(c.create_text(pad, y, text=text, anchor="nw", width=width, fill=TEXT, font=("Segoe UI", -big)))
        y = c.bbox(items[-1])[3]
        if note:
            y += int(sml * 0.5)
            items.append(c.create_text(pad, y, text=note, anchor="nw", width=width, fill=ORANGE, font=("Segoe UI", -sml)))
            y = c.bbox(items[-1])[3]
        pw, ph = width + 2 * pad, y + pad
        # corner: bottom-left unless a ring sits there, then top-left, then bottom-right
        corners = [(margin, H - margin - ph), (margin, margin), (W - margin - pw, H - margin - ph), (W - margin - pw, margin)]
        px, py = corners[0]
        for cx0, cy0 in corners:
            if not any(cx0 - r < x < cx0 + pw + r and cy0 - r < y_ < cy0 + ph + r for x, y_, r in avoid):
                px, py = cx0, cy0
                break
        rect = c.create_rectangle(px, py, px + pw, py + ph, fill=PANEL, outline=PANEL_EDGE, width=2)
        c.tag_lower(rect)
        for it in items:
            c.move(it, px, py)
        self._boxes.append((px / W, py / H, (px + pw) / W, (py + ph) / H))

    # ------------------------------------------------------------- "did the view change?" check
    def _thumb(self, img):
        return img.convert("L").resize((96, 54))

    def _current_marks(self):
        a = self.answer
        if not a or not a["steps"]:
            return []
        step = a["steps"][max(0, min(a["pos"], len(a["steps"]) - 1))]
        if step["num"] is None:
            return []
        # points and paths of this step; anything we can't draw (no x/y, no usable path) is left out
        return [m for m in a["marks"] if isinstance(m, dict) and m.get("step") == step["num"] and mark_kind(m)]

    def _view_check(self):
        a = self.answer
        if not a or not self.mon or self.suspended or not self.enabled or not self.game_front or self.dismissed:
            return
        cur_marks = self._current_marks()
        shot = next((m.get("shot") for m in cur_marks if m.get("shot")), None)
        if not shot:
            shot = (a.get("shots") or [None])[-1]
        if not shot or not cur_marks:
            return                      # nothing circled on this step: nothing to compare against
        import numpy as np
        from PIL import Image
        if shot not in self._refs:
            try:
                self._refs[shot] = np.asarray(self._thumb(Image.open(shot)), dtype=np.float32)
            except Exception:
                return
        ref = self._refs[shot]
        try:
            import mss
            l, t, r, b = self.mon
            with mss.mss() as sct:
                raw = sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
            cur = np.asarray(self._thumb(Image.frombytes("RGB", raw.size, raw.rgb)), dtype=np.float32)
        except Exception:
            return
        diff = np.abs(ref - cur)
        mask = np.ones_like(diff, dtype=bool)
        h, w = diff.shape
        for box in self._boxes + self._sense_boxes + self._qpath_boxes:     # ignore what we drew ourselves
            r = mask_slice(box, w, h)
            if r is not None:
                mask[r[0]:r[1], r[2]:r[3]] = False
        if mask.sum() < 200:
            return
        score = float(diff[mask].mean())
        ok = score < (26 if self.view_ok else 16)   # hysteresis so it doesn't flicker
        if ok != self.view_ok:
            self.view_ok = ok
            self._dirty = True
            self.log(f"overlay: view {'matches again' if ok else 'changed'} (diff {score:.0f})")
