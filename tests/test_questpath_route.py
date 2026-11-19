"""Ground-route quest path tests: questpath.compute() following msg.target.route
(the game's walking-map path sent by the UE helper), the straight-line fallback, hiding dots that
are drawn over the player character, the 100 m floor-hint rule, the smaller dots and hard input
checks on the route. Pure maths plus the Sense reader fed through a temp sense.json (no window,
no game, no app).
Run: python tests/test_questpath_route.py"""
import json
import math
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sense      # noqa: E402
import questpath  # noqa: E402

fails = []
count = [0]


def check(name, ok, info=""):
    count[0] += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""), flush=True)
    if not ok:
        fails.append(name)


def near(a, b, tol=0.05):
    return a is not None and b is not None and abs(a - b) <= tol


SCR = [2560, 1440]
KEYS = {"dots", "end", "edge", "mark", "dist", "floors", "label", "kind", "src", "trail"}
LIFT = 0.05


def ue_msg(player=(0, 0, 0.9), target=(40, 0, 0), rot=(-10, 0, 0), cam_pos=(-3, 0, 2), fov=90,
           has_z=True, route=None, route_src="nav", **extra):
    t = None
    if target is not None:
        t = {"pos": list(target), "has_z": has_z, "kind": "quest", "id": "q1", "label": "", "src": "tracked"}
        if route is not None:
            t["route"] = route
            if route_src is not None:
                t["route_src"] = route_src
    m = {"coords": "ue", "player": list(player), "screen": SCR,
         "cam": {"pos": list(cam_pos), "rot": list(rot), "quat": None, "fov": fov}, "target": t}
    m.update(extra)
    return m


def poly_at(pts, s):
    """Independent walk along a polyline: the point s metres from pts[0] (3D length)."""
    for p, q in zip(pts, pts[1:]):
        seg = math.dist(p, q)
        if s <= seg + 1e-9:
            t = s / seg if seg else 0.0
            return [p[k] + (q[k] - p[k]) * t for k in range(3)]
        s -= seg
    return list(pts[-1])


def lift(w, up=2):
    w = list(w)
    w[up] += LIFT
    return w


def dots_match_route(r, pts, cam, up=2, project=sense.project_ue, tol=0.03):
    """Every dot sits where the route point s metres along (+5 cm) projects."""
    bad = []
    for d in r["dots"]:
        x, y, _f = project(cam, SCR, lift(poly_at(pts, d["s"]), up))
        if not (near(d["x"], x, tol) and near(d["y"], y, tol)):
            bad.append((d, round(x, 2), round(y, 2)))
    return bad


# ================================================================ geometry (bare trail)
questpath.HIDE_PLAYER = False

# 1. A straight route along +X: same dots as the line, but at route z + 5 cm, trail "nav".
route = [[0, 0, 0], [20, 0, 0], [40, 0, 0]]
m = ue_msg(route=route)
r = questpath.compute(m, None)
check("straight route: result with the contract keys", isinstance(r, dict) and set(r) == KEYS, r and sorted(r))
check("straight route: trail 'nav'", r and r["trail"] == "nav", r and r["trail"])
check("straight route 40 m: dots every 1 m from 1 to 30",
      r and [d["s"] for d in r["dots"]] == [float(i) for i in range(1, 31)], r and [d["s"] for d in r["dots"]][-3:])
check("straight route: dots at route z + 0.05 m", r and not dots_match_route(r, route, m["cam"]),
      r and dots_match_route(r, route, m["cam"])[:2])
check("straight route: x = 50 (centre line)", r and all(near(d["x"], 50) for d in r["dots"]))
check("straight route: dist/label unchanged (flat straight distance)", r and r["dist"] == 40.0 and r["label"] == "40 m")
check("JSON-safe", json.dumps(r, allow_nan=False) is not None)

# 2. A bend: 10 m ahead, then 20 m to the right. Dots follow the corner, not the straight line.
route = [[0, 0, 0], [10, 0, 0], [10, 8, 0]]
m = ue_msg(target=(10, 8, 0), route=route)
r = questpath.compute(m, None)
ss = [d["s"] for d in r["dots"]] if r else []
check("bend: route length 18, goal at the end -> dots 1..17", ss == [float(i) for i in range(1, 18)], ss[-3:])
check("bend: every dot on the polyline", r and not dots_match_route(r, route, m["cam"]),
      r and dots_match_route(r, route, m["cam"])[:2])
line = questpath.compute(ue_msg(target=(10, 8, 0)), None)
check("bend: differs from the straight line toward the target",
      r and line and abs(r["dots"][4]["x"] - line["dots"][4]["x"]) > 1.0, (r and r["dots"][4], line and line["dots"][4]))
check("bend: first 10 dots straight ahead (x = 50), later ones move right",
      r and all(near(d["x"], 50) for d in r["dots"][:10]) and all(d["x"] > 50.5 for d in r["dots"][11:]))
# end arrow follows the polyline (the 2nd leg goes +Y = screen right)
last = r["dots"][-1]
p0 = sense.project_ue(m["cam"], SCR, lift(poly_at(route, last["s"])))
p1 = sense.project_ue(m["cam"], SCR, lift(poly_at(route, last["s"] + 0.5)))
want = math.degrees(math.atan2(p1[1] - p0[1], (p1[0] - p0[0]) * SCR[0] / SCR[1]))
check("bend: end arrow at the last dot, along the polyline", r["end"] and near(r["end"]["angle"], want, 0.5)
      and r["end"]["x"] == last["x"], (r["end"], round(want, 1)))
check("bend: end arrow points right-ish (-60..60), not up the screen like the straight line",
      r["end"] and -60 < r["end"]["angle"] < 60, r["end"])

# 3. End arrow on the last leg near a turn
route = [[0, 0, 0], [20, 0, 0], [20, 15, 0]]
m = ue_msg(target=(20, 15, 0), route=route)
r = questpath.compute(m, None)
check("turn: last dot at 30 m of route (route 35 m long)", r and r["dots"] and r["dots"][-1]["s"] == 30.0,
      r and r["dots"][-1])

# 4. Re-anchor: the player is halfway along the route -> the part behind is dropped.
route = [[-10, 0, 0], [0, 0, 0], [10, 0, 0], [40, 0, 0]]
m = ue_msg(player=(5, 0.4, 0.9), route=route, cam_pos=(2, 0.4, 2))
r = questpath.compute(m, None)
anchored = [[5, 0, 0], [10, 0, 0], [40, 0, 0]]
check("re-anchor: trail starts at the closest route point to the feet",
      r and not dots_match_route(r, anchored, m["cam"]), r and dots_match_route(r, anchored, m["cam"])[:2])
check("re-anchor: first dot 1 m past the anchor (s = 1)", r and r["dots"] and r["dots"][0]["s"] == 1.0,
      r and r["dots"][:1])
check("re-anchor: no dot behind the player (all dots ahead on screen, top half side)",
      r and len(r["dots"]) == 30, r and len(r["dots"]))
# route sent while the player already walked past its first 12 m (helper is ~1 s late)
route = [[0, 0, 0], [6, 0, 0], [12, 0, 0], [50, 0, 0]]
m = ue_msg(player=(12.3, 0.2, 0.9), target=(50, 0, 0), route=route, cam_pos=(9.3, 0, 2))
r = questpath.compute(m, None)
check("re-anchor late route: dots start 1 m past the feet, not at the old start",
      r and r["dots"] and not dots_match_route(r, [[12.3, 0, 0], [50, 0, 0]], m["cam"]),
      r and r["dots"][:2])

# 5. Stairs: dots follow the route height.
route = [[0, 0, 0], [5, 0, 0], [10, 0, 3.5], [30, 0, 3.5], [40, 0, 7]]
m = ue_msg(target=(40, 0, 7), route=route)
r = questpath.compute(m, None)
check("stairs: dots follow route heights (+5 cm)", r and not dots_match_route(r, route, m["cam"]),
      r and dots_match_route(r, route, m["cam"])[:2])
check("stairs: s is the walking length along the slope (3D)", r and r["dots"] and r["dots"][-1]["s"] == 30.0)
flat = questpath.compute(ue_msg(target=(40, 0, 7)), None)
check("stairs: differs from the straight interpolated line", r and flat and
      abs(r["dots"][6]["y"] - flat["dots"][6]["y"]) > 0.3, (r and r["dots"][6], flat and flat["dots"][6]))
check("stairs: floors from the target dz still (7 m up -> 2 floors)", r and r["floors"] == 2
      and r["label"] == "↑ 2 floors · 40 m", r and r["label"])

# 6. route_src passes through; unknown/missing -> "nav".
for src, want in (("nav", "nav"), ("nav_partial", "nav_partial"), ("ground", "ground"), ("xyz", "nav"),
                  (None, "nav"), (7, "nav"), (["nav"], "nav")):
    rr = questpath.compute(ue_msg(route=[[0, 0, 0], [40, 0, 0]], route_src=src), None)
    check(f"route_src {src!r} -> trail {want!r}", rr and rr["trail"] == want, rr and rr["trail"])

# 7. Partial route that ends short of the goal: dots go to its very end; arrow still set.
route = [[0, 0, 0], [12, 0, 0]]
r = questpath.compute(ue_msg(target=(40, 0, 0), route=route, route_src="nav_partial"), None)
check("partial route 12 m: dots 1..12 (to its end, not d-1)", r and [d["s"] for d in r["dots"]][-1] == 12.0,
      r and [d["s"] for d in r["dots"]][-2:])
check("partial route: end arrow pointing up (-90)", r and r["end"] and near(r["end"]["angle"], -90, 1.0),
      r and r["end"])
check("partial route: trail nav_partial, dist still 40", r and r["trail"] == "nav_partial" and r["dist"] == 40.0)
# Too-short route that stops short of the goal (nav_partial dead end: the player already stands at the
# closest reachable point) -> the straight line, never an empty trail.
straight40 = questpath.compute(ue_msg(target=(40, 0, 0)), None)
questpath.HIDE_PLAYER = True
straight40_hide = questpath.compute(ue_msg(target=(40, 0, 0)), None)   # camera right behind: dots over the body go
questpath.HIDE_PLAYER = False
for name, rt in (("0.4 m", [[0, 0, 0], [0.4, 0, 0]]), ("behind + 0.3 m", [[-3, 0, 0], [0.3, 0, 0]]),
                 ("1.5 m", [[0, 0, 0], [1.5, 0, 0]]), ("3.9 m", [[0, 0, 0], [3.9, 0, 0]])):
    for hide in (False, True):
        questpath.HIDE_PLAYER = hide
        r = questpath.compute(ue_msg(target=(40, 0, 0), route=rt, route_src="nav_partial"), None)
        questpath.HIDE_PLAYER = False
        same = straight40_hide if hide else straight40
        ok = (r and r["trail"] == "line" and len(r["dots"]) >= (1 if hide else 20) and r["end"] is not None
              and r["dots"] == same["dots"] and r["end"] == same["end"])
        check(f"nav_partial route {name} (dead end), hide {hide}: straight line with dots + end arrow", ok,
              r and (r["trail"], len(r["dots"]), r["end"]))
r = questpath.compute(ue_msg(target=(40, 0, 0), route=[[0, 0, 0], [4.5, 0, 0]], route_src="nav_partial"), None)
check("nav_partial route 4.5 m (>= 4 m): still used, dots 1..4", r and r["trail"] == "nav_partial"
      and [d["s"] for d in r["dots"]] == [1.0, 2.0, 3.0, 4.0], r and (r["trail"], [d["s"] for d in r["dots"]]))
r = questpath.compute(ue_msg(target=(5.5, 0, 0), route=[[0, 0, 0], [3.6, 0, 0]]), None)
check("3.6 m route that ends within 2 m of a near target is still used (dots 1, 2)", r and r["trail"] == "nav"
      and [d["s"] for d in r["dots"]] == [1.0, 2.0], r and (r["trail"], [d["s"] for d in r["dots"]]))
# Mid-goal route (helper projects 55 m ahead for far targets): trail is 30 m along it
route = [[0, 0, 0], [55, 0, 0]]
r = questpath.compute(ue_msg(target=(300, 0, 0), route=route), None)
check("far target, 55 m mid-goal route: 30 dots, label just the distance",
      r and len(r["dots"]) == 30 and r["label"] == "300 m", r and (len(r["dots"]), r["label"]))
# Ground fallback: few points 3 m apart
route = [[0, 0, 0.1], [3, 0, 0.2], [6, 0, 0.4], [9, 0, 0.3]]
m = ue_msg(target=(40, 0, 0), route=route, route_src="ground")
r = questpath.compute(m, None)
check("ground route 9 m: dots 1..9 on it, trail 'ground'",
      r and r["trail"] == "ground" and [d["s"] for d in r["dots"]][-1] == 9.0 and not dots_match_route(r, route, m["cam"]),
      r and (r["trail"], [d["s"] for d in r["dots"]][-2:]))

# 8. Hard input checks: junk -> straight line, never raise.
nan, inf = float("nan"), float("inf")
straight = questpath.compute(ue_msg(), None)
junk = [
    ("route None", None), ("route []", []), ("one point", [[0, 0, 0]]), ("dict", {"a": 1}), ("string", "0,0,0"),
    ("number", 5), ("bool", True), ("NaN point", [[0, 0, 0], [nan, 0, 0], [40, 0, 0]]),
    ("inf point", [[0, 0, 0], [inf, 0, 0]]), ("bool coord", [[0, 0, 0], [True, 0, 0], [40, 0, 0]]),
    ("string coords", [["0", "0", "0"], ["40", "0", "0"]]), ("short point", [[0, 0, 0], [40, 0]]),
    ("point is a dict", [[0, 0, 0], {"x": 1}]), ("point None", [[0, 0, 0], None]),
    ("huge coords", [[0, 0, 0], [1e308, 0, 0]]), ("all same point", [[1, 0, 0]] * 10),
    ("step > 500 m", [[0, 0, 0], [900, 0, 0]]), ("route 20 m away from the player", [[0, 25, 0], [40, 25, 0]]),
    ("nested junk", [[[0, 0, 0]], [[1, 1, 1]]]),
]
for name, rt in junk:
    m = ue_msg()
    m["target"]["route"] = rt
    try:
        out = questpath.compute(m, SCR)
        ok = (out is not None and out["trail"] == "line" and out["dots"] == straight["dots"]
              and json.dumps(out, allow_nan=False) is not None)
        check(f"junk route ({name}): straight line, no raise", ok, out and out["trail"])
    except Exception as e:
        check(f"junk route ({name}): no raise", False, repr(e))
# NaN not at the start but beyond the 64-point cap is ignored (only the first 64 are read)
rt = [[i * 0.5, 0, 0] for i in range(64)] + [[nan, nan, nan]] * 1000
r = questpath.compute(ue_msg(route=rt), None)
check("route > 64 points: extra ignored (junk past 64 not read), route used, ends at 31.5 m",
      r and r["trail"] == "nav" and r["dots"] and r["dots"][-1]["s"] <= 31.5, r and r["dots"][-1:])
rt = [[i * 0.5, 0, 0] for i in range(100000)]
t0 = time.perf_counter()
r = questpath.compute(ue_msg(route=rt), None)
check("route of 100,000 points: fast (< 50 ms) and capped", r and r["trail"] == "nav"
      and time.perf_counter() - t0 < 0.05, round(time.perf_counter() - t0, 4))
# route as a tuple of tuples is fine
r = questpath.compute(ue_msg(route=((0, 0, 0), (40, 0, 0))), None)
check("route as tuples works", r and r["trail"] == "nav")
# route with 4+ numbers per point: first 3 used
r = questpath.compute(ue_msg(route=[[0, 0, 0, 9], [40, 0, 0, 9]]), None)
check("route points with extra numbers: first 3 used", r and r["trail"] == "nav")
# route given but target within 5 m -> still None
check("route + target within 5 m -> None",
      questpath.compute(ue_msg(target=(4, 0, 0), route=[[0, 0, 0], [4, 0, 0]]), None) is None)
# has_z False + route: route heights still used
route = [[0, 0, 0], [10, 0, 2], [40, 0, 2]]
m = ue_msg(target=(40, 0, 30), has_z=False, route=route)
r = questpath.compute(m, None)
check("has_z False + route: dots on the route, floors 0", r and r["floors"] == 0
      and not dots_match_route(r, route, m["cam"]), r and r["floors"])

# 9. RE frame (Y up): route heights on Y.
def re_msg(player=(0, 0, 0), target=(0, 0, -40), cam_pos=(0, 2, 3), route=None, src="nav"):
    t = {"pos": list(target), "has_z": True, "kind": "lock", "id": "g", "src": "unlocked"}
    if route is not None:
        t["route"] = route
        t["route_src"] = src
    return {"coords": "re", "player": list(player), "screen": SCR,
            "cam": {"pos": list(cam_pos), "rot": None, "quat": [0, 0, 0, 1], "fov": 60}, "target": t}


route = [[0, 0, 0], [0, 0, -10], [0, 3.5, -20], [0, 3.5, -40]]
m = re_msg(target=(0, 3.5, -40), route=route)
r = questpath.compute(m, None)
check("re route: trail nav, dots on the route at Y + 0.05",
      r and r["trail"] == "nav" and not dots_match_route(r, route, m["cam"], up=1, project=sense.project_re),
      r and dots_match_route(r, route, m["cam"], up=1, project=sense.project_re)[:2])
check("re route: 1 floor up label", r and r["label"] == "↑ 1 floor · 40 m", r and r["label"])

# 10. Dot size: smaller (world radius 0.04, max 0.8 % W).
check("constants: DOT_R_M 0.04, DOT_R_MAX 0.8", questpath.DOT_R_M == 0.04 and questpath.DOT_R_MAX == 0.8)
r = questpath.compute(ue_msg(), None)
cam = ue_msg()["cam"]
tan_h = math.tan(math.radians(45))
ok = True
for d in r["dots"]:
    f = sense.camera_space_ue(cam, [d["s"], 0, 0])[0]
    want = min(max(0.04 / (f * tan_h) * 50, 0.10), 0.8)
    ok = ok and near(d["r"], want, 0.002)
check("dot radius = 0.04 m in the world, clamped 0.10..0.8 % W", ok, [d["r"] for d in r["dots"][:3]])
# Spec item 4 also for the far dots: with the old sizes (0.06 m, min 0.15 % W) every dot past ~20 m sat on
# the 0.15 floor, so the dots that stay on screen did not get smaller. Each dot must now be smaller than
# its old size, except where the new min clamp holds it.
check("constant: DOT_R_MIN 0.10 (was 0.15)", questpath.DOT_R_MIN == 0.10, questpath.DOT_R_MIN)
for name, m in (("camera right behind", ue_msg(target=(60, 0, 0))),
                ("higher camera behind", ue_msg(target=(60, 0, 0), cam_pos=(-3.5, 0, 2.2), rot=(-12, 0, 0)))):
    r = questpath.compute(m, None)
    cam = m["cam"]
    old = [min(max(0.06 / (sense.camera_space_ue(cam, [d["s"], 0, 0])[0] * tan_h) * 50, 0.15), 1.2) for d in r["dots"]]
    far = [(d["r"], o) for d, o in zip(r["dots"], old) if d["s"] >= 15]
    check(f"{name}: far dots (s >= 15) are smaller than before, at most 2/3 of the old size",
          len(far) >= 10 and all(rn <= o * 0.67 + 0.002 for rn, o in far), far[:3])
r = questpath.compute(ue_msg(cam_pos=(-0.5, 0, 0.4), rot=(-30, 0, 0)), None)
check("close camera: radius capped at 0.8", r and r["dots"] and max(d["r"] for d in r["dots"]) <= 0.8,
      r and max(d["r"] for d in r["dots"]))

# 11. Floor hint only within 100 m (flat).
for d, want_label, want_floors in ((42, "↑ 2 floors · 42 m", 2), (100, "↑ 2 floors · 100 m", 2),
                                   (100.4, "100 m", 0), (150, "150 m", 0), (1256, "1256 m", 0)):
    rr = questpath.compute(ue_msg(target=(d, 0, 7.0)), None)
    ti = questpath.target_info(ue_msg(target=(d, 0, 7.0)))
    check(f"target {d} m away, 7 m up: label {want_label!r}, floors {want_floors}",
          rr and rr["label"] == want_label and rr["floors"] == want_floors
          and ti["label"] == want_label and ti["floors"] == want_floors, rr and (rr["label"], rr["floors"]))
rr = questpath.compute(ue_msg(target=(60, 80, -10.0)), None)        # 100 m flat exactly, 10 m below
check("100 m flat (diagonal), 10 m below: '↓ 3 floors · 100 m'", rr and rr["label"] == "↓ 3 floors · 100 m",
      rr and rr["label"])
check("floor hint uses FLAT distance: 99 m across, 500 m up still shows floors",
      questpath.target_info(ue_msg(target=(99, 0, 500)))["floors"] > 0)

# ================================================================ hiding dots over the character
questpath.HIDE_PLAYER = True


def player_rect_ue(m):
    """Independent screen box of the character (UE, no camera roll): feet .. +1.8 m, +-0.45 m
    along the flat camera right (-sin yaw, cos yaw, 0)."""
    cam = m["cam"]
    fdz = m.get("player_foot_dz", 0.9)
    px, py, pz = m["player"]
    yaw = math.radians(cam["rot"][1])
    rx, ry = -math.sin(yaw), math.cos(yaw)
    xs, ys = [], []
    for side in (-0.45, 0.45):
        for h in (0.0, 1.8):
            x, y, f = sense.project_ue(cam, SCR, [px + rx * side, py + ry * side, pz - fdz + h])
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def overlaps(d, rect):
    w, h = SCR[0] / 100, SCR[1] / 100
    cx, cy, rr = d["x"] * w, d["y"] * h, d["r"] * w
    dx = max(rect[0] * w - cx, 0, cx - rect[2] * w)
    dy = max(rect[1] * h - cy, 0, cy - rect[3] * h)
    return dx * dx + dy * dy < rr * rr


scenes = [("camera right behind", ue_msg()),
          ("over the shoulder", ue_msg(cam_pos=(-3, 0.9, 2))),
          ("high camera", ue_msg(cam_pos=(-4, 0, 5), rot=(-35, 0, 0))),
          ("target to the right, camera turned", ue_msg(target=(20, 25, 0), rot=(-12, 30, 0), cam_pos=(-2.6, -1.5, 2))),
          ("route bend", ue_msg(target=(10, 20, 0), route=[[0, 0, 0], [10, 0, 0], [10, 20, 0]]))]
RANGE = math.inf  # dots over the character go at
                  # ANY distance; the old 3 m limit is gone
check("body-hide: the 3 m hide limit is gone (no PLAYER_HIDE_RANGE_M)", not hasattr(questpath, "PLAYER_HIDE_RANGE_M"))


def dot_world(m, s):
    """Independent world point s metres along the bare trail: the route, else the straight line."""
    rt = m["target"].get("route")
    if rt:
        return poly_at(rt, s)
    px, py, pz = m["player"]
    tx, ty, _tz = m["target"]["pos"]
    d = math.hypot(tx - px, ty - py)
    return [px + (tx - px) / d * s, py + (ty - py) / d * s, 0.0]


def near_body(m, d):
    w = dot_world(m, d["s"])
    return math.hypot(w[0] - m["player"][0], w[1] - m["player"][1]) <= RANGE


for name, m in scenes:
    on = questpath.compute(m, None)
    questpath.HIDE_PLAYER = False
    off = questpath.compute(m, None)
    questpath.HIDE_PLAYER = True
    rect = player_rect_ue(m)
    hidden = [d for d in off["dots"] if d not in on["dots"]]
    check(f"{name}: no kept dot overlaps the character box",
          on and not any(overlaps(d, rect) and near_body(m, d) for d in on["dots"]),
          [d for d in on["dots"] if overlaps(d, rect) and near_body(m, d)][:2])
    check(f"{name}: exactly the overlapping dots were left out",
          all(overlaps(d, rect) and near_body(m, d) for d in hidden) and len(on["dots"]) + len(hidden) == len(off["dots"]),
          (len(on["dots"]), len(hidden), len(off["dots"])))
# body-hide with a camera right behind the player: the trail lines up with the body on screen. EVERY dot
# over the body goes, also the far ones (the trail passes behind the character); the dots that are
# left are exactly the ones clear of the box, and the end arrow sits on the last drawn dot. When no
# dot is left the target mark (on screen) shows instead.
for name, cp, rot in (("camera right behind", (-3, 0, 2), (-10, 0, 0)), ("low centred camera", (-3.5, 0, 1.5), (-10, 0, 0)),
                      ("higher camera behind", (-3.5, 0, 2.2), (-12, 0, 0)),
                      ("camera 0.5 m off centre", (-3, 0.5, 2), (-10, 0, 0))):
    m = ue_msg(target=(60, 0, 0), cam_pos=cp, rot=rot)
    on = questpath.compute(m, None)
    questpath.HIDE_PLAYER = False
    off = questpath.compute(m, None)
    questpath.HIDE_PLAYER = True
    rect = player_rect_ue(m)
    ss = [d["s"] for d in on["dots"]] if on else []
    far_over = [d for d in off["dots"] if overlaps(d, rect) and d["s"] > 3.0]
    check(f"{name}: no drawn dot overlaps the body box (any distance)",
          on is not None and not any(overlaps(d, rect) for d in on["dots"]), ss)
    check(f"{name}: dots past 3 m that overlap the box are hidden too (setup has some)",
          on is not None and far_over and not any(d in on["dots"] for d in far_over), (len(far_over), ss))
    check(f"{name}: kept dots = the bare trail's dots clear of the box (beyond 0.8 m)",
          on is not None and on["dots"] == [d for d in off["dots"] if not overlaps(d, rect)], ss)
    if on and on["dots"]:
        check(f"{name}: end arrow on the last drawn dot",
              on["end"] is not None and (on["end"]["x"], on["end"]["y"]) == (on["dots"][-1]["x"], on["dots"][-1]["y"]),
              on["end"])
    else:
        check(f"{name}: no dot left, target on screen -> mark, no end arrow",
              on is not None and on["mark"] is not None and on["end"] is None and on["edge"] is None, on)
# Last bare dot is over the box while nearer dots are not: a route that first goes off to the left
# (clear of the body) and then bends back to a goal straight ahead, far behind the body on screen.
# The end arrow moves to the last visible dot.
m = ue_msg(target=(25, 0, 0), route=[[0, 0, 0], [2, -3, 0], [25, 0, 0]])
on = questpath.compute(m, None)
questpath.HIDE_PLAYER = False
off = questpath.compute(m, None)
questpath.HIDE_PLAYER = True
rect = player_rect_ue(m)
check("body-hide end arrow: setup has the bare trail's last dot over the box",
      off and off["dots"] and overlaps(off["dots"][-1], rect), off and off["dots"][-1:])
check("body-hide end arrow: the last bare dot is gone, arrow on the last visible dot",
      on and on["dots"] and on["dots"][-1] != off["dots"][-1] and on["end"] is not None
      and (on["end"]["x"], on["end"]["y"]) == (on["dots"][-1]["x"], on["dots"][-1]["y"]),
      on and (on["dots"][-1:], on["end"]))
check("body-hide end arrow: far dots over the box (> 3 m) are hidden, no drawn dot overlaps",
      on and not any(overlaps(d, rect) for d in on["dots"])
      and any(overlaps(d, rect) and d["s"] > 3.0 for d in off["dots"]), on and [d["s"] for d in on["dots"]])
on = questpath.compute(ue_msg(cam_pos=(-3, 0.9, 2)), None)
check("over the shoulder: most dots stay (only the first few are behind the body)",
      on and len(on["dots"]) >= 25, on and [d["s"] for d in on["dots"]][:4])
# a dot drawn left of the body, right next to it, is kept
on = questpath.compute(ue_msg(target=(0, -40, 0), rot=(-10, 0, 0)), None)
check("trail going sideways (left) is kept outside the body box", on and len(on["dots"]) >= 1,
      on and len(on["dots"]))
# Only dots, none hidden -> mark appears when the target is on screen and every dot is hidden
# a route that stays within 3 m of the feet straight ahead of the camera (all dots over the body)
m = ue_msg(target=(20, 0, 0), route=[[0, 0, 0], [2.9, 0, 0], [2.9, 0.3, 0], [0.9, 0.3, 0]], route_src="nav_partial")
on = questpath.compute(m, None)
check("all dots hidden behind the body, target on screen -> mark at the target",
      on and on["dots"] == [] and on["mark"] is not None and on["end"] is None, on and (len(on["dots"]), on["mark"]))
# First person (camera inside the body box): box not used, dots stay
on = questpath.compute(ue_msg(cam_pos=(0.1, 0, 1.7), rot=(-30, 0, 0)), None)
check("first-person camera: box skipped, dots stay", on and len(on["dots"]) >= 20, on and len(on["dots"]))

# Near-feet rule: a route that loops back past the feet. Far high camera so the body box is tiny.
route = [[0, 0, 0], [5, 0, 0], [5, 0.6, 0], [-5, 0.6, 0]]
m = ue_msg(target=(-5, 0.6, 0), route=route, cam_pos=(-20, 0, 15), rot=(-35, 0, 0))
on = questpath.compute(m, None)
questpath.HIDE_PLAYER = False
off = questpath.compute(m, None)
questpath.HIDE_PLAYER = True
s_on = [d["s"] for d in on["dots"]] if on else []
s_off = [d["s"] for d in off["dots"]] if off else []
check("near feet: the dot 0.72 m from the feet (s 11) is left out, it is drawn with the switch off",
      11.0 not in s_on and 11.0 in s_off, (s_on, s_off))
check("near feet: no kept dot within 0.8 m (flat) of the feet",
      all(math.hypot(*poly_at(route, s)[:2]) > 0.8 for s in s_on))
check("near feet: dots outside 0.8 m are kept (s 9, 10, 12, 13)", all(s in s_on for s in (9.0, 10.0, 12.0, 13.0)),
      s_on)
check("re frame: hiding works (no raise, path)", questpath.compute(re_msg()) is not None)

# ================================================================ fuzz
rng = random.Random(11)
pool = [0, 1, -1, 0.5, 40, -40, 1e6, 1e308, nan, inf, -inf, True, None, "x", [], {}]


def v3():
    return [rng.choice(pool) if rng.random() < 0.1 else rng.uniform(-60, 60) for _ in range(3)]


bad = []
for i in range(3000):
    questpath.HIDE_PLAYER = rng.random() < 0.8
    coords = rng.choice(["ue", "re", None])
    cam = {"pos": v3(), "rot": v3() if rng.random() < 0.9 else None,
           "quat": [rng.uniform(-1, 1) for _ in range(4)] if rng.random() < 0.9 else None,
           "fov": rng.choice([rng.uniform(1, 170), nan, 90, None]), "fov_axis": rng.choice(["h", "v", None])}
    k = rng.choice([0, 1, 2, 5, 30, 64, 80])
    rt = rng.choice([None, "x", 5, {}, [v3() for _ in range(k)],
                     [[rng.uniform(-3, 3) + j * rng.uniform(0, 4), rng.uniform(-3, 3), rng.uniform(-2, 2)]
                      for j in range(k)]])
    m = {"coords": coords, "player": v3(), "cam": cam, "screen": rng.choice([SCR, None, [0, 0]]),
         "player_foot_dz": rng.choice([0.9, nan, None, 0.0]),
         "target": {"pos": v3(), "has_z": rng.choice([True, False]), "kind": "quest", "route": rt,
                    "route_src": rng.choice(["nav", "ground", "nav_partial", None, 3])}}
    try:
        out = questpath.compute(m, rng.choice([SCR, None]))
        if out is not None:
            json.dumps(out, allow_nan=False)
            if set(out) != KEYS or len(out["dots"]) > 30 or out["dist"] <= questpath.HIDE_M:
                bad.append(("contract", out))
            if out["trail"] not in ("line", "nav", "nav_partial", "ground"):
                bad.append(("trail", out["trail"]))
            for d in out["dots"]:
                if not (-5 <= d["x"] <= 105 and -5 <= d["y"] <= 105 and 0.10 <= d["r"] <= 0.8 and 0 < d["s"] <= 30):
                    bad.append(("dot", d))
    except Exception as e:
        bad.append((repr(e), str(m)[:200]))
questpath.HIDE_PLAYER = True
check("3000 random messages with routes: no raise, JSON-safe, dots in range", not bad, str(bad[:2])[:300])

# ================================================================ through the Sense reader
tmp = Path(tempfile.mkdtemp(prefix="qg_qroute_"))
try:
    f = tmp / "sense.json"
    m = ue_msg(cam_pos=(-3, 0.9, 2), target=(10, 20, 0), route=[[0, 0, 0], [10, 0, 0], [10, 20, 0]])
    m.update(v=1, seq=1, t=100, items=[])
    (tmp / "s.tmp").write_text(json.dumps(m), encoding="utf-8")
    os.replace(tmp / "s.tmp", f)
    s = sense.Sense(log=lambda *_: None)
    s.path = str(f)
    paths = []
    s.on_path = paths.append
    s._poll(str(f))
    check("Sense reader: route message -> path with trail 'nav' reaches on_path",
          paths and isinstance(paths[-1], dict) and paths[-1]["trail"] == "nav" and paths[-1]["dots"],
          str(paths[-1:])[:120])
    s.stop()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"{count[0] - len(fails)}/{count[0]} checks pass")
print("RESULT: " + ("ALL PASS" if not fails else "FAIL " + ", ".join(fails)))
sys.exit(1 if fails else 0)
