"""Quest path tests: questpath.compute() + the "re" projector + the quest path hook in sense.py.
Pure maths plus the Sense reader fed through a temp sense.json (no window, no game, no app).
Run: python tests/test_questpath.py"""
import json
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sense      # noqa: E402
import questpath  # noqa: E402

# These checks are about the bare trail geometry (straight line, no route). Leaving out dots that
# are drawn over the player character is checked in tests/test_qg_questpath_route.py.
questpath.HIDE_PLAYER = False

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


def ue_msg(player=(0, 0, 0.9), target=(40, 0, 0), rot=(-10, 0, 0), cam_pos=(-3, 0, 2), fov=90,
           has_z=True, **extra):
    m = {"coords": "ue", "player": list(player), "screen": SCR,
         "cam": {"pos": list(cam_pos), "rot": list(rot), "quat": None, "fov": fov},
         "target": None if target is None else {"pos": list(target), "has_z": has_z, "kind": "quest",
                                                "id": "q1", "label": "", "src": "tracked"}}
    m.update(extra)
    return m


# ------------------------------------------------------------------ geometry (ue)
r = questpath.compute(ue_msg(), None)
check("ue straight ahead: result", isinstance(r, dict), str(r)[:120])
keys = {"dots", "end", "edge", "mark", "dist", "floors", "label", "kind", "src",
        "trail"}   # "trail" = the ground route
check("result has exactly the contract keys", set(r) == keys, sorted(r))
ss = [d["s"] for d in r["dots"]]
check("dots every 1 m from 1 to 30", ss == [float(i) for i in range(1, 31)], ss[:5])
check("dots straight ahead stay at x = 50", all(near(d["x"], 50.0) for d in r["dots"]))
ys = [d["y"] for d in r["dots"]]
check("dots climb up the screen near -> far", all(a > b for a, b in zip(ys, ys[1:])), ys[:4])
rs = [d["r"] for d in r["dots"]]
check("dot radius shrinks with distance and stays in 0.10..0.8 % W",
      all(a >= b for a, b in zip(rs, rs[1:])) and all(0.10 <= v <= 0.8 for v in rs), rs[:3] + rs[-2:])
check("no route sent -> trail 'line'", r["trail"] == "line", r["trail"])
check("end arrow at the last dot, pointing up (-90)",
      r["end"] and near(r["end"]["x"], r["dots"][-1]["x"]) and near(r["end"]["y"], r["dots"][-1]["y"])
      and near(r["end"]["angle"], -90.0, 1.0), r["end"])
check("target in front on screen -> no edge arrow", r["edge"] is None, r["edge"])
check("dist = horizontal metres, 1 decimal", r["dist"] == 40.0, r["dist"])
check("label '40 m', floors 0", r["label"] == "40 m" and r["floors"] == 0, (r["label"], r["floors"]))
check("kind/src passed through", (r["kind"], r["src"]) == ("quest", "tracked"))
check("all numbers JSON-safe", json.dumps(r, allow_nan=False) is not None)

# Feet: player z 0.9 (UE capsule centre) -> feet at 0 -> the first dot is at ground level z 0.
cam = ue_msg()["cam"]
x0, y0, _ = sense.project_ue(cam, SCR, [1, 0, 0.0])
check("first dot = ground point 1 m ahead of the feet (default foot dz 0.9)",
      near(r["dots"][0]["y"], y0, 0.02), (r["dots"][0]["y"], round(y0, 2)))
r2 = questpath.compute(ue_msg(player_foot_dz=0.5), None)
x1, y1, _ = sense.project_ue(cam, SCR, [1, 0, 0.4 - 0.4 / 40])   # feet z 0.4 -> target z 0
check("player_foot_dz from the helper is used", near(r2["dots"][0]["y"], y1, 0.02),
      (r2["dots"][0]["y"], round(y1, 2)))

# Short target: dots stop 1 m before it.
r = questpath.compute(ue_msg(target=(12, 0, 0)), None)
check("target 12 m: dots 1..11", [d["s"] for d in r["dots"]] == [float(i) for i in range(1, 12)],
      [d["s"] for d in r["dots"]])
r = questpath.compute(ue_msg(target=(9.5, 0, 0)), None)
check("target 9.5 m: dots 1..8 (stop at d-1)", [d["s"] for d in r["dots"]][-1] == 8.0,
      [d["s"] for d in r["dots"]])

# Right side: UE +Y is to the right when looking down +X.
r = questpath.compute(ue_msg(target=(20, 20, 0)), None)
check("target ahead-right: dots move right", r["dots"] and r["dots"][-1]["x"] > r["dots"][0]["x"] >= 50,
      [(d["x"], d["y"]) for d in r["dots"][:3]])
check("ahead-right: end arrow points up-right (-90..0)", r["end"] and -90 < r["end"]["angle"] < 0,
      r["end"])
check("ahead-right: dist = 28.3", r["dist"] == round(math.hypot(20, 20), 1), r["dist"])

# Every kept dot is in front (>0.3 m) and inside -5..105 %.
for name, m in (("low camera looking up", ue_msg(cam_pos=(-1, 0, 0.5), rot=(25, 0, 0))),
                ("camera turned 60 deg", ue_msg(rot=(-10, 60, 0))),
                ("narrow fov", ue_msg(fov=20))):
    rr = questpath.compute(m, None)
    ok = rr is not None and all(-5 <= d["x"] <= 105 and -5 <= d["y"] <= 105 for d in rr["dots"])
    for d in (rr or {}).get("dots", []):
        w = [d["s"], 0, 0.0]
        if sense.camera_space_ue(m["cam"], w)[0] <= 0.3:
            ok = False
    check(f"{name}: kept dots in front and inside -5..105", ok, len((rr or {}).get("dots", [])))

# ------------------------------------------------------------------ edge arrow
r = questpath.compute(ue_msg(target=(-40, 0, 0)), None)
check("target behind: edge arrow", r is not None and r["edge"] is not None, r and r["edge"])
check("target behind: edge arrow points down-ish (60..120)", r and 60 <= r["edge"]["angle"] <= 120,
      r and r["edge"])
check("target behind: no dot is on screen top half", r and all(d["y"] > 50 for d in r["dots"]),
      r and r["dots"][:3])
r = questpath.compute(ue_msg(target=(5, 60, 0)), None)
check("target far right off screen: edge arrow on the right border, angle ~0",
      r["edge"] and r["edge"]["x"] > 90 and -30 <= r["edge"]["angle"] <= 30, r["edge"])
check("edge carries x/y/angle only", r["edge"] and set(r["edge"]) == {"x", "y", "angle"})

# ------------------------------------------------------------------ hide < 5 m
check("4.9 m -> None", questpath.compute(ue_msg(target=(4.9, 0, 0)), None) is None)
check("5.0 m -> None (within 5 m)", questpath.compute(ue_msg(target=(5.0, 0, 0)), None) is None)
check("5.2 m -> path", questpath.compute(ue_msg(target=(5.2, 0, 0)), None) is not None)
check("3 m across, 20 m up -> None (horizontal counts)",
      questpath.compute(ue_msg(target=(3, 0, 20)), None) is None)
ti = questpath.target_info(ue_msg(target=(3, 0, 0)))
check("target_info still reports a near target", ti and ti["dist"] == 3.0, ti)

# ------------------------------------------------------------------ floors + label
for dz, want in ((0.0, 0), (2.4, 0), (-2.4, 0), (2.5, 1), (3.0, 1), (5.0, 1), (7.0, 2), (7.5, 2),
                 (-8.0, -2), (-3.0, -1), (10.6, 3)):
    check(f"floors_for({dz}) = {want}", questpath.floors_for(dz) == want, questpath.floors_for(dz))
r = questpath.compute(ue_msg(target=(42, 0, 7.0)), None)          # feet at 0 -> dz 7
check("target 7 m above: floors 2, label '↑ 2 floors · 42 m'",
      r["floors"] == 2 and r["label"] == "↑ 2 floors · 42 m", (r["floors"], r["label"]))
r = questpath.compute(ue_msg(target=(42, 0, -3.0)), None)
check("target 3 m below: label '↓ 1 floor · 42 m'", r["label"] == "↓ 1 floor · 42 m", r["label"])
r = questpath.compute(ue_msg(target=(42, 0, 2.4)), None)
check("target 2.4 m above feet: same floor", r["floors"] == 0 and r["label"] == "42 m", r["label"])
r = questpath.compute(ue_msg(target=(42, 0, 30.0), has_z=False), None)
check("has_z false: floors 0, line at foot height",
      r["floors"] == 0 and near(r["dots"][4]["y"], questpath.compute(ue_msg(target=(42, 0, 0)), None)
                                ["dots"][4]["y"], 0.02), (r["floors"], r["dots"][4]))
r = questpath.compute(ue_msg(target=(20, 0, 10.0)), None)
w = [10.0, 0.0, 5.0]                                              # halfway: feet z 0 -> 10, at 10 m -> 5
xh, yh, _ = sense.project_ue(cam, SCR, w)
check("dot height is interpolated feet -> target", near(r["dots"][9]["y"], yh, 0.02),
      (r["dots"][9], round(yh, 2)))
check("label rounds metres: 1234.4 -> '1234 m'", questpath.label_for(1234.4, 0) == "1234 m")

# ------------------------------------------------------------------ stale / none
check("no target key -> None", questpath.compute({k: v for k, v in ue_msg().items() if k != "target"}) is None)
check("target null -> None", questpath.compute(ue_msg(target=None)) is None)
check("target_info(null) -> None", questpath.target_info(ue_msg(target=None)) is None)
check("no player -> None", questpath.compute({**ue_msg(), "player": None}) is None)
check("no camera -> None", questpath.compute({**ue_msg(), "cam": None}) is None)
check("camera without rot -> None", questpath.compute({**ue_msg(), "cam": {"pos": [0, 0, 0], "fov": 90}}) is None)
check("unknown coords -> None", questpath.compute({**ue_msg(), "coords": "unity"}) is None)
check("coords as a list -> None", questpath.compute({**ue_msg(), "coords": ["ue"]}) is None)
check("missing coords = ue", questpath.compute({k: v for k, v in ue_msg().items() if k != "coords"}) is not None)
check("msg not a dict -> None", questpath.compute(None) is None and questpath.compute([1, 2]) is None)

# ------------------------------------------------------------------ NaN / junk input
nan, inf = float("nan"), float("inf")
bad = [
    ("player NaN", {**ue_msg(), "player": [nan, 0, 0]}),
    ("player inf", {**ue_msg(), "player": [0, inf, 0]}),
    ("player bool", {**ue_msg(), "player": [True, 0, 0]}),
    ("player strings", {**ue_msg(), "player": ["1", "2", "3"]}),
    ("player short", {**ue_msg(), "player": [1, 2]}),
    ("target pos NaN", ue_msg(target=(nan, 0, 0))),
    ("target pos inf", ue_msg(target=(inf, 0, 0))),
    ("target pos missing", {**ue_msg(), "target": {"kind": "quest"}}),
    ("target not a dict", {**ue_msg(), "target": [40, 0, 0]}),
    ("cam pos NaN", ue_msg(cam_pos=(nan, 0, 0))),
    ("cam rot NaN", ue_msg(rot=(nan, 0, 0))),
    ("huge numbers", ue_msg(target=(1e308, 1e308, 0))),
]
for name, m in bad:
    try:
        out = questpath.compute(m, SCR)
        check(f"{name}: no raise, None", out is None, str(out)[:80])
    except Exception as e:
        check(f"{name}: no raise", False, repr(e))
for name, m in (("fov NaN -> default 90", ue_msg(fov=nan)), ("fov string", ue_msg(fov="wide")),
                ("screen NaN", {**ue_msg(), "screen": [nan, nan]}), ("screen zero", {**ue_msg(), "screen": [0, 0]}),
                ("foot dz NaN", ue_msg(player_foot_dz=nan)), ("foot dz -3", ue_msg(player_foot_dz=-3)),
                ("kind/src junk", {**ue_msg(), "target": {"pos": [40, 0, 0], "kind": {"x": 1}, "src": None}})):
    try:
        out = questpath.compute(m, [nan, 5])
        ok = out is not None and json.dumps(out, allow_nan=False) is not None
        check(f"{name}: still a valid path, JSON-safe", ok, str(out)[:80])
    except Exception as e:
        check(f"{name}: no raise", False, repr(e))
r = questpath.compute(ue_msg(fov=nan), None)
check("fov NaN behaves like fov 90", r == questpath.compute(ue_msg(fov=90), None))

# Random junk: never raises, output always JSON-safe and inside the contract.
import random  # noqa: E402
rng = random.Random(7)
pool = [0, 1, -1, 0.5, 3.7, 40, -40, 1e6, -1e6, 1e308, -1e308, nan, inf, -inf, True, None, "x", [], {}]
bad_out = []
for i in range(3000):
    def v3():
        return [rng.choice(pool) if rng.random() < 0.15 else rng.uniform(-60, 60) for _ in range(3)]
    coords = rng.choice(["ue", "re", None, "zz"])
    cam = {"pos": v3(), "rot": v3() if rng.random() < 0.8 else None,
           "quat": [rng.uniform(-1, 1) for _ in range(4)] if rng.random() < 0.8 else rng.choice(pool),
           "fov": rng.choice([rng.uniform(1, 170), nan, 0, -5, 400, "a", None]),
           "fov_axis": rng.choice(["h", "v", None, 3])}
    m = {"coords": coords, "player": v3(), "cam": cam, "screen": rng.choice([SCR, [0, 0], [nan, 1], None]),
         "player_foot_dz": rng.choice([0.9, nan, -1, 100, None, "a"]),
         "target": {"pos": v3(), "has_z": rng.choice([True, False, None]), "kind": "quest"}}
    try:
        out = questpath.compute(m, rng.choice([SCR, None, [nan, nan]]))
        if out is not None:
            json.dumps(out, allow_nan=False)
            if set(out) != keys or len(out["dots"]) > 30 or out["dist"] <= questpath.HIDE_M:
                bad_out.append(("contract", out))
            for d in out["dots"]:
                if not (-5 <= d["x"] <= 105 and -5 <= d["y"] <= 105 and 0.10 <= d["r"] <= 0.8):
                    bad_out.append(("dot", d))
    except Exception as e:
        bad_out.append((repr(e), m))
check("3000 random messages: no raise, JSON-safe, dots in range", not bad_out, str(bad_out[:2])[:300])

# ------------------------------------------------------------------ re frame
def q_axis(axis, deg):
    h = math.radians(deg) / 2
    s = math.sin(h)
    return [axis[0] * s, axis[1] * s, axis[2] * s, math.cos(h)]


def re_msg(player=(0, 0, 0), target=(0, 0, -40), quat=(0, 0, 0, 1), cam_pos=(0, 2, 3), fov=60,
           fov_axis=None, **extra):
    c = {"pos": list(cam_pos), "rot": None, "quat": list(quat), "fov": fov}
    if fov_axis:
        c["fov_axis"] = fov_axis
    m = {"coords": "re", "player": list(player), "screen": SCR, "cam": c,
         "target": {"pos": list(target), "has_z": True, "kind": "lock", "id": "g", "src": "unlocked"}}
    m.update(extra)
    return m


C = {"pos": [0, 0, 0], "quat": [0, 0, 0, 1], "fov": 90}
x, y, f = sense.project_re(C, SCR, [0, 0, -10])
check("re: identity quat looks down -Z (centre, in front)", near(x, 50) and near(y, 50) and f, (x, y, f))
x, y, f = sense.project_re(C, SCR, [0, 0, 10])
check("re: +Z is behind", f is False)
x, y, f = sense.project_re(C, SCR, [10, 0, -10])
check("re: +X is right", x > 50 and near(y, 50), (x, y))
x, y, f = sense.project_re(C, SCR, [0, 10, -10])
check("re: +Y is up; fov vertical by default -> top edge at 45 deg", near(y, 0.0) and near(x, 50), (x, y))
x, y, f = sense.project_re({**C, "fov_axis": "h"}, SCR, [10, 0, -10])
check("re: fov_axis 'h' -> 45 deg right is the right edge", near(x, 100.0), (x, y))
x, y, f = sense.project_re({**C, "fov_axis": "v"}, SCR, [10, 0, -10])
check("re: fov_axis 'v' -> 45 deg right is inside (aspect)", near(x, 50 + 50 / (16 / 9)), x)
x, y, f = sense.project_ue({"pos": [0, 0, 0], "rot": [0, 0, 0], "fov": 90, "fov_axis": "v"}, SCR, [10, 0, 10])
check("ue honours fov_axis 'v' too", near(y, 0.0), y)
yaw = {**C, "quat": q_axis((0, 1, 0), 90)}                       # turn left 90 deg about +Y
x, y, f = sense.project_re(yaw, SCR, [-10, 0, 0])
check("re: quat yaw +90 about Y -> looks down -X", near(x, 50) and near(y, 50) and f, (x, y, f))
x, y, f = sense.project_re({**C, "quat": [0, 0, 0, 2]}, SCR, [0, 0, -10])
check("re: non-unit quat is normalised", near(x, 50) and f, (x, y))
pt = sense.screen_point_re(C, SCR, [0, 0, 10])
check("screen_point_re: behind -> edge, angle 90 (down)", pt["edge"] and pt["angle"] == 90.0, pt)
pt = sense.screen_point_re(C, SCR, [-20, 0, 5])
check("screen_point_re: behind-left -> left border", pt["edge"] and pt["x"] < 10, pt)
check("PROJECTORS has ue and re", sense.PROJECTORS.get("re") is sense.screen_point_re
      and sense.PROJECTORS.get("ue") is sense.screen_point_ue)
check("up_axis ue=2 re=1, unknown None", (sense.up_axis("ue"), sense.up_axis("re"), sense.up_axis(None),
                                          sense.up_axis("x")) == (2, 1, 2, None))
for name, q in (("zero quat", [0, 0, 0, 0]), ("NaN quat", [nan, 0, 0, 1]), ("3 values", [0, 0, 1]),
                ("strings", ["0", "0", "0", "1"])):
    check(f"cam_ok re rejects {name}", not sense.cam_ok({"pos": [0, 0, 0], "quat": q}, "re"))
    try:
        sense.screen_point_re({"pos": [0, 0, 0], "quat": q, "fov": 90}, SCR, [0, 0, -5])
        check(f"screen_point_re {name} raises ValueError (skipped by _points)", False)
    except (ValueError, TypeError, IndexError):
        check(f"screen_point_re {name} raises a skippable error", True)
check("cam_ok re accepts a quat, ue needs rot", sense.cam_ok(C, "re") and not sense.cam_ok(C, "ue"))

r = questpath.compute(re_msg(), None)
check("re path: straight ahead along -Z", r and all(near(d["x"], 50) for d in r["dots"]), r and r["dots"][:2])
check("re path: 30 dots, climb up the screen", r and len(r["dots"]) == 30
      and all(a["y"] > b["y"] for a, b in zip(r["dots"], r["dots"][1:])))
check("re path: end arrow points up", r and r["end"] and near(r["end"]["angle"], -90, 1), r and r["end"])
check("re: default foot dz 0 (player point = feet)",
      r and near(r["dots"][0]["y"], sense.project_re(re_msg()["cam"], SCR, [0, 0, -1])[1], 0.02))
r = questpath.compute(re_msg(target=(0, 7.0, -42)), None)
check("re floors use Y: 7 m up -> ↑ 2 floors · 42 m", r and r["label"] == "↑ 2 floors · 42 m", r and r["label"])
r = questpath.compute(re_msg(target=(0, 30.0, -4)), None)
check("re hide uses X/Z distance: 4 m away, 30 m up -> None", r is None)
r = questpath.compute(re_msg(target=(30, 0, -30)), None)
check("re: target ahead on +X side -> dots go right", r and r["dots"] and r["dots"][-1]["x"] > r["dots"][0]["x"] > 50, r and r["dots"][:2])
r = questpath.compute(re_msg(target=(0, 0, 40)), None)
check("re: target behind (+Z) -> edge arrow", r and r["edge"] and 60 <= r["edge"]["angle"] <= 120,
      r and r["edge"])
check("re: quat missing -> None", questpath.compute({**re_msg(), "cam": {"pos": [0, 2, 3], "fov": 60}}) is None)
check("re: quat zero -> None", questpath.compute(re_msg(quat=(0, 0, 0, 0))) is None)

# Same scene in both frames gives the same picture. UE (X fwd, Y right, Z up) -> RE (Y, Z, -X).
def ue2re(v):
    return [v[1], v[2], -v[0]]


for pitch, yaw_deg in ((-10, 0), (-20, 35), (5, -70)):
    um = ue_msg(player=(3, 1, 0.9), target=(30, 18, 4), rot=(pitch, yaw_deg, 0), cam_pos=(0, 0, 2.5),
                fov=80, player_foot_dz=0.9)
    # UE yaw (about Z, toward +Y = right) is a turn right = negative rotation about RE +Y;
    # UE pitch up is a positive rotation about the camera's right axis (RE +X), applied after yaw.
    qy = q_axis((0, 1, 0), -yaw_deg)
    qp = q_axis((1, 0, 0), pitch)

    def qmul(a, b):
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        return [aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
                aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz]
    rm = {"coords": "re", "player": ue2re([3, 1, 0.9]), "screen": SCR, "player_foot_dz": 0.9,
          "cam": {"pos": ue2re([0, 0, 2.5]), "quat": qmul(qy, qp), "fov": 80, "fov_axis": "h"},
          "target": {"pos": ue2re([30, 18, 4]), "has_z": True, "kind": "quest", "src": "tracked"}}
    a, b = questpath.compute(um), questpath.compute(rm)
    same = (a is not None and b is not None and len(a["dots"]) == len(b["dots"])
            and all(near(p["x"], q["x"], 0.05) and near(p["y"], q["y"], 0.05) and near(p["r"], q["r"], 0.002)
                    for p, q in zip(a["dots"], b["dots"]))
            and a["dist"] == b["dist"] and a["floors"] == b["floors"]
            and (a["edge"] is None) == (b["edge"] is None))
    check(f"ue and re frames agree (pitch {pitch}, yaw {yaw_deg})", same,
          (a and len(a["dots"]), b and len(b["dots"]), a and a["dots"][:1], b and b["dots"][:1]))

# ------------------------------------------------------------------ Sense hook (reader, temp file)
tmp = Path(tempfile.mkdtemp(prefix="eh_qpath_"))
try:
    f = tmp / "sense.json"
    seq = [0]

    def write(m):
        seq[0] += 1
        m = dict(m, v=1, seq=seq[0], t=seq[0] * 100, items=m.get("items", []))
        (tmp / "s.tmp").write_text(json.dumps(m), encoding="utf-8")
        os.replace(tmp / "s.tmp", f)
        time.sleep(0.02)                               # new mtime_ns on coarse clocks

    logs = []
    s = sense.Sense(log=logs.append)
    s.path = str(f)
    paths, draws = [], []
    s.on_path = paths.append
    s.on_draw = draws.append
    write(ue_msg(items=[{"id": "a", "kind": "pickup", "pos": [2, 0, 0.9], "dist": 2.0}]))
    s._poll(str(f))
    check("hook: fresh message with target -> on_path got a path",
          paths and isinstance(paths[-1], dict) and len(paths[-1]["dots"]) == 30, str(paths[-1:])[:80])
    st = s.state()
    check("state: path_enabled True and target {dist,floors,kind}",
          st["path_enabled"] is True and st["target"] == {"dist": 40.0, "floors": 0, "kind": "quest"}, st["target"])
    check("state: items still work (nearest)", st["nearest"] and st["nearest"]["id"] == "a", st["nearest"])

    s.set_enabled(False)
    n0 = len(paths)
    write(ue_msg(target=(30, 5, 0)))
    s._last_path_at = 0
    s._poll(str(f))
    check("Sense switch off: path still sent", len(paths) > n0 and paths[-1] and paths[-1]["dist"] == 30.4,
          str(paths[-1:])[:80])
    check("Sense switch off: on_draw got [] (rings off)", draws[-1] == [])
    s.set_enabled(True)

    s.set_path_enabled(False)
    check("set_path_enabled(False) -> on_path(None) now", paths[-1] is None)
    n0 = len(paths)
    write(ue_msg(target=(25, 0, 0)))
    s._poll(str(f))
    check("path off: no new path sent", len(paths) == n0 or paths[-1] is None, paths[n0:])
    check("path off: state still reports the target", s.state()["target"]["dist"] == 25.0
          and s.state()["path_enabled"] is False, s.state()["target"])
    s.set_path_enabled(True)
    s._poll(str(f))
    check("path on again: path sent on the next poll", isinstance(paths[-1], dict), str(paths[-1:])[:60])

    write(ue_msg(target=None))
    s._last_path_at = 0
    s._poll(str(f))
    check("target gone -> on_path(None), state target None", paths[-1] is None and s.state()["target"] is None)
    n0 = len(paths)
    s._poll(str(f))
    s._poll(str(f))
    check("None is not repeated", len(paths) == n0, paths[n0:])

    write(ue_msg(target=(4, 0, 0)))
    s._poll(str(f))
    check("within 5 m: no path, but state target dist 4.0", paths[-1] is None
          and s.state()["target"] == {"dist": 4.0, "floors": 0, "kind": "quest"}, s.state()["target"])

    write(re_msg(items=[{"id": "r", "kind": "collectible", "pos": [0, 0, -3], "dist": 3.0}]))
    s._last_path_at = 0
    s._poll(str(f))
    check("re message: path sent", isinstance(paths[-1], dict) and len(paths[-1]["dots"]) == 30,
          str(paths[-1:])[:60])
    check("re message: item ring projected with the re projector (on screen, below centre)",
          draws and draws[-1] and draws[-1][0]["edge"] is False and draws[-1][0]["y"] > 50, draws[-1:])

    # Throttle: a new path every poll for 1 s -> at most ~10 calls.
    n0 = len(paths)
    t_end = time.monotonic() + 1.0
    k = 0
    while time.monotonic() < t_end:
        k += 1
        write(ue_msg(target=(20 + k * 0.1, 0, 0)))
        s._poll(str(f))
    sent = len(paths) - n0
    check("on_path throttled to <= ~10 per second", 1 <= sent <= 11, f"{sent} calls for {k} messages")

    # Stale: helper stops writing.
    with s._lock:
        s._msg_at = time.monotonic() - 5
    s._last_path_at = 0
    s._poll(str(f))
    check("stale -> on_path(None) and state target None", paths[-1] is None and s.state()["target"] is None)

    # Bad message (NaN in it): Python's json writes NaN; the reader must not crash or send junk.
    (tmp / "s.tmp").write_text('{"v":1,"seq":999,"t":99900,"coords":"ue","player":[NaN,0,0],'
                               '"cam":{"pos":[0,0,0],"rot":[0,0,0],"fov":90},'
                               '"target":{"pos":[40,0,0],"has_z":true,"kind":"quest"}}', encoding="utf-8")
    os.replace(tmp / "s.tmp", f)
    time.sleep(0.02)
    s._last_path_at = 0
    try:
        s._poll(str(f))
        check("NaN player in sense.json: no raise, no path, no target",
              paths[-1] is None and s.state()["target"] is None, (str(paths[-1])[:60], s.state()["target"]))
    except Exception as e:
        check("NaN player in sense.json: no raise", False, repr(e))
    check("no 'quest path failed' log line", not any("quest path failed" in str(x) for x in logs), logs)

    # stop() clears the path.
    write(ue_msg())
    s._last_path_at = 0
    s._poll(str(f))
    had = isinstance(paths[-1], dict)
    s.stop()
    check("stop() -> on_path(None)", had and paths[-1] is None)
    check("state() JSON-safe", json.dumps(s.state(), allow_nan=False) is not None)

    # on_path raising does not break the reader.
    s2 = sense.Sense(log=logs.append)
    s2.on_path = lambda p: 1 / 0
    write(ue_msg())
    try:
        s2._poll(str(f))
        check("on_path raising: logged, reader goes on", any("on_path failed" in str(x) for x in logs))
    except Exception as e:
        check("on_path raising: no raise", False, repr(e))

    # Constructor switch.
    s3 = sense.Sense(log=logs.append, path_enabled=False)
    got = []
    s3.on_path = got.append
    write(ue_msg())
    s3._poll(str(f))
    check("Sense(path_enabled=False): no path sent", all(g is None for g in got)
          and s3.state()["path_enabled"] is False, got)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ------------------------------------------------------------------ body-hide: hide every dot over the body
# (at any distance, not only within 3 m). Independent box: feet .. +1.8 m,
# +-0.45 m along the flat camera right, projected with sense.project_ue.
questpath.HIDE_PLAYER = True


def _body_box(m):
    cam = m["cam"]
    px, py, pz = m["player"]
    yaw = math.radians(cam["rot"][1])
    rx, ry = -math.sin(yaw), math.cos(yaw)
    xs, ys = [], []
    for side in (-0.45, 0.45):
        for h in (0.0, 1.8):
            x, y, _f = sense.project_ue(cam, SCR, [px + rx * side, py + ry * side, pz - 0.9 + h])
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _over(d, box):
    w, h = SCR[0] / 100, SCR[1] / 100
    cx, cy, rr = d["x"] * w, d["y"] * h, d["r"] * w
    dx = max(box[0] * w - cx, 0, cx - box[2] * w)
    dy = max(box[1] * h - cy, 0, cy - box[3] * h)
    return dx * dx + dy * dy < rr * rr


for name, m in (("camera right behind", ue_msg(target=(60, 0, 0))),
                ("higher camera behind", ue_msg(target=(60, 0, 0), cam_pos=(-3.5, 0, 2.2), rot=(-12, 0, 0))),
                ("over the shoulder", ue_msg(cam_pos=(-3, 0.9, 2))),
                ("high camera", ue_msg(cam_pos=(-4, 0, 5), rot=(-35, 0, 0)))):
    on = questpath.compute(m, None)
    questpath.HIDE_PLAYER = False
    off = questpath.compute(m, None)
    questpath.HIDE_PLAYER = True
    box = _body_box(m)
    check(f"body-hide {name}: no drawn dot overlaps the body box", on is not None
          and not any(_over(d, box) for d in on["dots"]), on and [d["s"] for d in on["dots"]][:6])
    check(f"body-hide {name}: every bare dot clear of the box (and past 0.8 m) is kept", on is not None
          and on["dots"] == [d for d in off["dots"] if not _over(d, box)], on and len(on["dots"]))
    if on and on["dots"]:
        check(f"body-hide {name}: end arrow on the last drawn dot", on["end"] is not None
              and (on["end"]["x"], on["end"]["y"]) == (on["dots"][-1]["x"], on["dots"][-1]["y"]), on["end"])
m = ue_msg(target=(60, 0, 0))
questpath.HIDE_PLAYER = False
off = questpath.compute(m, None)
questpath.HIDE_PLAYER = True
check("body-hide camera right behind: far dots (s > 3 m) over the body are hidden too",
      any(_over(d, _body_box(m)) and d["s"] > 3.0 for d in off["dots"])
      and not any(_over(d, _body_box(m)) for d in questpath.compute(m, None)["dots"]))
m = ue_msg(target=(60, 0, 0), cam_pos=(-3.5, 0, 1.5), rot=(-10, 0, 0))
on = questpath.compute(m, None)
check("body-hide low centred camera: whole trail behind the body -> no dots, mark at the target, no end arrow",
      on is not None and on["dots"] == [] and on["mark"] is not None and on["end"] is None, on and on["mark"])
questpath.HIDE_PLAYER = False

print()
print(f"{count[0] - len(fails)}/{count[0]} checks pass")
print("RESULT: " + ("ALL PASS" if not fails else "FAIL " + ", ".join(fails)))
sys.exit(1 if fails else 0)
