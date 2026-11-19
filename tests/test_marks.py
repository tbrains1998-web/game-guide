"""Path marks in marks.py: parsing the <marks> block, snapping and drawing on a picture.
Run: python tests/test_marks.py"""
import os
import shutil
import sys
import tempfile

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import marks  # noqa: E402

# A made-up 1600x900 "map" picture (dark ground, a road and a few red squares), built at run time.
FIX_DIR = tempfile.mkdtemp(prefix="gg_marks_")
FIX = os.path.join(FIX_DIR, "fake-map.jpg")
_im = Image.new("RGB", (1600, 900), (40, 46, 38))
_d = ImageDraw.Draw(_im)
_d.line([(320, 720), (480, 540), (720, 450), (960, 360)], fill=(170, 160, 120), width=14)
for _x, _y in ((960, 360), (300, 200), (1300, 650)):
    _d.rectangle([_x - 18, _y - 18, _x + 18, _y + 18], fill=(200, 40, 40))
_im.save(FIX, quality=90)

ans = ("1. You're at: Crimson Forest checkpoint\n2. Go to: the red square\n3. How: follow the road north\n"
       '<marks>[{"step": 2, "x": 60, "y": 40, "label": "Story"}, '
       '{"step": 3, "path": [[20, 80], [30, 60], [45, 50], [60, 40]], "label": "Route"}, '
       '{"step": 4, "path": [[320, 720], [800, 700]], "label": "pixels"}, '
       '{"step": 5, "path": [[10, 10]], "label": "too short"}, '
       '{"step": 6, "path": [[10, 10], ["a", 5], [20, 20]], "label": "one bad point"}, '
       '{"step": 7, "x": 1000, "y": 500, "label": "px point"}]</marks>')
vis, mk = marks.split_marks(ans, [(1600, 900)])
print("visible:", repr(vis[-30:]))
for m in mk:
    print(m)
assert "<marks>" not in vis
assert [m["step"] for m in mk] == [2, 3, 4, 6, 7], [m["step"] for m in mk]
p3 = next(m for m in mk if m["step"] == 3)
assert "x" not in p3 and "y" not in p3 and len(p3["path"]) == 4
p4 = next(m for m in mk if m["step"] == 4)
assert abs(p4["path"][0][0] - 20) < 1e-6 and abs(p4["path"][0][1] - 80) < 1e-6
assert abs(p4["path"][1][0] - 50) < 1e-6 and abs(p4["path"][1][1] - 700 / 900 * 100) < 1e-6
p6 = next(m for m in mk if m["step"] == 6)
assert len(p6["path"]) == 2
p7 = next(m for m in mk if m["step"] == 7)
assert abs(p7["x"] - 62.5) < 1e-6
# no sizes known: pixel path dropped, percent path kept
_, mk2 = marks.split_marks(ans, None)
assert [m["step"] for m in mk2] == [2, 3, 6], [m["step"] for m in mk2]
# old single (w, h) tuple form still accepted
_, mk3 = marks.split_marks(ans, (1600, 900))
assert [m["step"] for m in mk3] == [2, 3, 4, 6, 7]

# snap_marks must not crash on path marks and must leave them untouched
snapped = marks.snap_marks(FIX, [dict(m) for m in mk])
assert next(m for m in snapped if m["step"] == 3) == p3

# render on the fixture in a temp dir
tmp = tempfile.mkdtemp()
shot = os.path.join(tmp, "map.jpg")
shutil.copy(FIX, shot)
out = marks.render([shot], [m for m in mk if m["step"] in (2, 3)])
print(out["all"], out["steps"])
assert out["steps"].keys() == {2, 3}
got = next(m for m in out["marks"] if m["step"] == 3)
assert got["img"] == 1 and got["shot"] == shot and got["path"] == p3["path"]
for p in out["all"] + list(out["steps"].values()):
    assert os.path.exists(p), p
print("step-3 image:", out["steps"][3])
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(FIX_DIR, ignore_errors=True)
print("RESULT: ALL PASS")
