"""Logic test for overlay.py path marks without opening any window (no Tk thread is started:
the Overlay is built with __new__ and given a fake canvas that just records the calls)."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import overlay


class FakeCanvas:
    def __init__(self):
        self.calls = []
        self._n = 0

    def _rec(self, name, *a, **kw):
        self.calls.append((name, a, kw))
        self._n += 1
        return self._n

    def __getattr__(self, name):
        return lambda *a, **kw: self._rec(name, *a, **kw)

    def bbox(self, item):
        return (0, 0, 100, 20)


def make():
    o = overlay.Overlay.__new__(overlay.Overlay)
    o.log = print
    o.keys = {"next": "F7", "toggle": "F6"}
    o.enabled, o.game_front, o.mon = True, True, (0, 0, 1920, 1080)
    o.dismissed, o.status, o.suspended, o.view_ok = False, "", False, True
    o._refs, o._boxes, o._dirty, o._hide_at = {}, [], True, None
    # the Sense and quest-path layers (added to Overlay.__init__ after this test was written)
    o.sense, o._sense_boxes, o._sense_drawn = None, [], False
    o.qpath, o._qpath_boxes, o._qpath_drawn = None, [], False
    o.canvas = FakeCanvas()
    return o


steps = [{"text": "one", "num": 1}, {"text": "two", "num": 2}, {"text": "three", "num": 3}, {"text": "para", "num": None}]
marks = [
    {"step": 2, "img": 1, "x": 40.0, "y": 60.0, "label": "Gate", "shot": "a.jpg"},
    {"step": 3, "img": 1, "path": [[10, 10], [30, 20], [50, 50]], "label": "Route", "shot": "a.jpg"},
    {"step": 3, "img": 1, "label": "broken, nothing to draw"},
    {"step": 3, "img": 1, "path": [[1, 2]], "label": "too short"},
    {"step": 3, "img": 1, "path": [["a", "b"], [5, 5], [None, 3], [8, 9]], "label": "partly bad"},
    {"step": 1, "x": "12", "y": "34", "label": "string numbers"},
    "not a dict",
]

assert overlay.path_points(marks[1]) == [(10.0, 10.0), (30.0, 20.0), (50.0, 50.0)]
assert overlay.path_points(marks[3]) == []
assert overlay.path_points(marks[4]) == [(5.0, 5.0), (8.0, 9.0)]
assert overlay.mark_kind(marks[0]) == "point"
assert overlay.mark_kind(marks[1]) == "path"
assert overlay.mark_kind(marks[2]) is None
assert overlay.mark_kind(marks[3]) is None
assert overlay.mark_kind(marks[5]) == "point"
assert overlay.mark_kind("x") is None
assert overlay.mark_kind({"x": float("nan"), "y": 1}) is None

o = make()
o.answer = {"steps": steps, "marks": marks, "pos": 2, "shots": ["a.jpg"]}
cur = o._current_marks()
assert [m["label"] for m in cur] == ["Route", "partly bad"], cur
o._draw()
lines = [c for c in o.canvas.calls if c[0] == "create_line"]
assert len(lines) == 4, lines            # halo + line for each of the 2 paths
halo, line = lines[0], lines[1]
assert halo[2]["fill"] == "#000000" and line[2]["fill"] == overlay.YELLOW
assert line[2]["width"] == max(4, int(1920 * 0.003)) == 5
assert line[2]["arrow"] == "last"
assert line[1] == (192.0, 108.0, 576.0, 216.0, 960.0, 540.0), line[1]
# badge at the first point: text "3" at (192,108)
texts = [c for c in o.canvas.calls if c[0] == "create_text" and c[2].get("text") == "3"]
assert texts and texts[0][1][:2] == (192.0, 108.0), texts
# mask box = bbox + margin, normalised
box = o._boxes[0]
assert box[0] < 192 / 1920 and box[1] < 108 / 1080 and box[2] > 960 / 1920 and box[3] > 540 / 1080, box
assert len(o._boxes) == 3                # 2 paths + panel

# point step still draws a ring, no lines
o = make()
o.answer = {"steps": steps, "marks": marks, "pos": 1, "shots": ["a.jpg"]}
o._draw()
assert not [c for c in o.canvas.calls if c[0] == "create_line"]
assert len([c for c in o.canvas.calls if c[0] == "create_oval"]) == 4   # halo ring, ring, badge halo, badge

# string numbers point
o = make()
o.answer = {"steps": steps, "marks": marks, "pos": 0, "shots": ["a.jpg"]}
o._draw()
assert len([c for c in o.canvas.calls if c[0] == "create_oval"]) == 4

# paragraph step (num None): no marks, no crash
o = make()
o.answer = {"steps": steps, "marks": marks, "pos": 3, "shots": ["a.jpg"]}
assert o._current_marks() == []
o._draw()

# view changed: note says Line for a path-only step
o = make()
o.view_ok = False
o.answer = {"steps": steps, "marks": marks, "pos": 2, "shots": ["a.jpg"]}
o._draw()
notes = [c for c in o.canvas.calls if c[0] == "create_text" and "hidden" in str(c[2].get("text"))]
assert notes and notes[0][2]["text"].startswith("Line hidden"), notes
assert not [c for c in o.canvas.calls if c[0] == "create_line"]

# game not in front: nothing drawn
o = make()
o.game_front = False
o.answer = {"steps": steps, "marks": marks, "pos": 2, "shots": ["a.jpg"]}
o._draw()
assert o.canvas.calls == [("delete", ("all",), {})], o.canvas.calls
print("overlay path logic: all OK")
