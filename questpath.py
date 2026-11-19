"""
Quest path - the dotted trail on the ground toward the game's current target.

Pure maths, no threads, no files. sense.py calls compute() with the latest sense.json message;
the overlay draws what comes back. Works for both coordinate frames the helpers send:
    "ue"  Unreal: metres (UE cm / 100), Z up, cam.rot = [pitch, yaw, roll], fov horizontal
    "re"  RE Engine: metres, Y up, cam.quat = [x, y, z, w] (camera looks -Z), fov vertical
          unless cam.fov_axis says "h"

The trail follows msg.target.route when the helper sends one (a list of [x, y, z] points in the
same frame and units as "player", first point near the player's floor, last at the goal; the UE
helper gets it from the game's walking map, route_src "nav" / "nav_partial" / "ground"). Without a
usable route it is the old straight line from the feet to the target with a linear height.

compute(msg, screen) -> None | {
    "dots":  [{"x": %, "y": %, "r": % of screen width, "s": metres along the trail}, ...] near->far,
    "end":   {"x": %, "y": %, "angle": deg} | None     small arrow at the last dot on screen,
    "edge":  {"x": %, "y": %, "angle": deg} | None     target off screen or behind,
    "mark":  {"x": %, "y": %} | None     target on screen while no dot and no edge is drawn
             (the projected target point; the key is always present),
    "dist":  metres left (horizontal, 1 decimal),
    "floors": int (signed, + = target above; 0 = same floor, or farther than FLOOR_HINT_M flat),
    "label": "42 m" | "↑ 2 floors · 42 m" (the floor part only within FLOOR_HINT_M),
    "kind":  target kind ("quest", "lock", "door", "pin", ...),
    "src":   target src ("tracked", "unlocked", ...),
    "trail": "line" | "nav" | "nav_partial" | "ground"   (what the dots follow)}
Every dot whose circle overlaps the player character's screen box (a feet .. feet+PLAYER_H_M box,
+-PLAYER_HALF_W_M across the screen) is left out, at ANY distance along the trail (the
trail should look like it passes behind the character; with the camera right behind the
player this can hide a long stretch of a straight trail). Dots within NEAR_FEET_M (flat) of the
feet are left out too. The end arrow sits on the last dot that is still drawn; when no dot is
left, "mark" (target on screen) or "edge" (target off screen) still shows where to go.
A route that stops short of the target and is shorter than ROUTE_MIN_M, or one that would give
no dot at all (e.g. a "nav_partial" dead end where the player already stands at the closest
reachable point), is not used: the trail is the straight line then.
x/y are percent of the screen (0,0 = top-left); angles are pixel angles (0 = right, 90 = down).
None = nothing to draw: no target, bad numbers, unknown frame, no usable camera, or the player
is within HIDE_M (horizontal) of the target.
"""

import math

import sense as _sense

TRAIL_M = 30.0        # how far ahead the dots go
STEP_M = 1.0          # one dot every metre
START_M = 1.0         # first dot this far from the feet
HIDE_M = 5.0          # closer than this (horizontal) to the target: no path at all
FRONT_MIN_M = 0.3     # a dot must be at least this far in front of the camera
VIEW_MIN, VIEW_MAX = -5.0, 105.0   # dots kept only inside this box (percent)
FOOT_DZ = {"ue": 0.9, "re": 0.0}   # default metres from the player point down to the feet
FOOT_DZ_MAX = 5.0     # a sent player_foot_dz outside 0..this is ignored
DOT_R_M = 0.04        # dot radius in the world (metres)
DOT_R_MIN, DOT_R_MAX = 0.10, 0.8   # dot radius on screen, percent of the screen width (min was 0.15:
                                   # far dots sat on it, so they did not get smaller)
FLOOR_SAME_M = 2.5    # |height difference| under this = same floor
FLOOR_M = 3.5         # one floor is this tall
ARROW_LOOK_M = 0.5    # the end arrow points along the trail over this length
MAX_COORD_M = 1e7     # player/target numbers beyond this (10,000 km) are junk -> no path
FLOOR_HINT_M = 100.0  # the floor hint ("↓ 2 floors") only within this flat distance
ROUTE_MAX_PTS = 64    # route points past this many are ignored
ROUTE_LIFT_M = 0.05   # route dots sit this far above the route point (walking-map floor)
ROUTE_MAX_OFF_M = 12.0   # route farther than this from the feet = stale/junk -> straight line
ROUTE_SEG_MAX_M = 500.0  # a single route step longer than this = junk -> straight line
ROUTE_END_NEAR_M = 2.0   # route end this close (flat) to the target: dots stop 1 m before it
ROUTE_SRCS = ("nav", "nav_partial", "ground")
PLAYER_H_M = 1.8      # player box for hiding dots drawn over the character: feet .. feet + this
PLAYER_HALF_W_M = 0.45   # ... and this far to each side (camera right, kept flat)
                      # Dots over this box are hidden at ANY distance: the old 3 m limit
                      # (PLAYER_HIDE_RANGE_M) was removed on purpose.
NEAR_FEET_M = 0.8     # dots this close (flat) to the feet are left out
ROUTE_MIN_M = 4.0     # a route shorter than this (after re-anchoring) that stops short of the target is not
                      # used (dead end: its few dots would sit behind the body): straight line instead
HIDE_PLAYER = True    # the two rules above; False = the bare trail (tests that check its geometry)


def _num(v):
    """float(v) when v is a real finite number (no bool, no str), else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _vec3(v):
    """[x, y, z] floats from a list/tuple of 3+ finite numbers within +-MAX_COORD_M, else None."""
    if not isinstance(v, (list, tuple)) or len(v) < 3:
        return None
    out = [_num(c) for c in v[:3]]
    if any(c is None or abs(c) > MAX_COORD_M for c in out):
        return None
    return out


def _screen(msg, screen):
    """Viewport [w, h]: the message's own size, else the caller's, else 16:9."""
    for s in (msg.get("screen"), screen):
        if isinstance(s, (list, tuple)) and len(s) >= 2:
            w, h = _num(s[0]), _num(s[1])
            if w and h and w > 0 and h > 0:
                return [w, h]
    return [1920.0, 1080.0]


def floors_for(dz) -> int:
    """Signed floor count for a height difference in metres: |dz| < 2.5 -> 0, else
    round(|dz| / 3.5), at least 1, with the sign of dz."""
    d = _num(dz)
    if d is None or abs(d) < FLOOR_SAME_M:
        return 0
    n = max(1, int(round(abs(d) / FLOOR_M)))
    return n if d > 0 else -n


def label_for(dist, floors) -> str:
    """"42 m", or "↑ 2 floors · 42 m" / "↓ 1 floor · 42 m" when the target is on another floor."""
    m = f"{int(round(dist))} m"
    if not floors:
        return m
    arrow = "↑" if floors > 0 else "↓"
    n = abs(int(floors))
    return f"{arrow} {n} floor{'s' if n != 1 else ''} · {m}"


def _parts(msg):
    """Shared checks: (frame, up, feet, target_ground_point, has_z, target dict) or None."""
    if not isinstance(msg, dict):
        return None
    fr = _sense.frame(msg.get("coords"))
    if not fr:
        return None
    up = fr["up"]
    tgt = msg.get("target")
    if not isinstance(tgt, dict):
        return None
    tpos = _vec3(tgt.get("pos"))
    player = _vec3(msg.get("player"))
    if tpos is None or player is None:
        return None
    coords = msg.get("coords") or "ue"
    fdz = _num(msg.get("player_foot_dz"))
    if fdz is None or not 0.0 <= fdz <= FOOT_DZ_MAX:
        fdz = FOOT_DZ.get(coords, 0.0)
    feet = list(player)
    feet[up] -= fdz
    has_z = tgt.get("has_z", True) is not False
    goal = list(tpos)
    if not has_z:
        goal[up] = feet[up]                  # 2D target: the line stays at foot height
    return fr, up, feet, goal, has_z, tgt


def _horizontal(up):
    return [i for i in range(3) if i != up]


def target_info(msg):
    """{"dist", "floors", "kind", "src", "label"} for the message's target, or None.
    Needs only the player and the target (no camera); also works within HIDE_M.
    floors is 0 when the target is farther than FLOOR_HINT_M (flat): the label is then just
    the distance."""
    p = _parts(msg)
    if not p:
        return None
    fr, up, feet, goal, has_z, tgt = p
    a, b = _horizontal(up)
    dist = math.hypot(goal[a] - feet[a], goal[b] - feet[b])
    if not math.isfinite(dist):
        return None
    floors = floors_for(goal[up] - feet[up]) if has_z and dist <= FLOOR_HINT_M else 0
    return {"dist": round(dist, 1), "floors": floors,
            "kind": str(tgt.get("kind") or "")[:24], "src": str(tgt.get("src") or "")[:24],
            "label": label_for(dist, floors)}


def _pixel_angle(p0, p1, aspect):
    """Pixel angle (0 = right, 90 = down) from screen point p0 to p1 (both in percent)."""
    dx = (p1[0] - p0[0]) * aspect          # percent of width -> same scale as percent of height
    dy = p1[1] - p0[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return None
    return math.degrees(math.atan2(dy, dx))


def _dist3(p, q):
    return math.sqrt((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2)


def _route_points(tgt):
    """The helper's route as a list of [x, y, z] floats (2..ROUTE_MAX_PTS points, repeated points
    dropped), or None when there is no usable route. Points past ROUTE_MAX_PTS are ignored. Any
    junk point (NaN, inf, bool, string, short, beyond MAX_COORD_M) or a step longer than
    ROUTE_SEG_MAX_M throws the whole route away (the caller then draws the straight line)."""
    r = tgt.get("route")
    if not isinstance(r, (list, tuple)) or len(r) < 2:
        return None
    pts = []
    for raw in r[:ROUTE_MAX_PTS]:
        v = _vec3(raw)
        if v is None:
            return None
        if pts:
            d = _dist3(v, pts[-1])
            if not math.isfinite(d) or d > ROUTE_SEG_MAX_M:
                return None
            if d < 1e-3:
                continue
        pts.append(v)
    return pts if len(pts) >= 2 else None


def _closest_on_segment(p, a, b):
    """(distance, t) of the point on segment a-b closest to p (t in 0..1, 3D)."""
    d = [b[k] - a[k] for k in range(3)]
    dd = d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
    t = 0.0 if dd <= 1e-12 else max(0.0, min(1.0, sum((p[k] - a[k]) * d[k] for k in range(3)) / dd))
    q = [a[k] + d[k] * t for k in range(3)]
    return _dist3(p, q), t


def _reanchor(pts, feet):
    """Polyline starting at the route point closest to the feet (the part behind is dropped), or
    None when even the closest point is farther than ROUTE_MAX_OFF_M (stale or junk route)."""
    best = None
    for i in range(len(pts) - 1):
        d, t = _closest_on_segment(feet, pts[i], pts[i + 1])
        if not math.isfinite(d):
            return None
        if best is None or d < best[0] - 1e-6:          # ties: the earliest segment wins
            best = (d, i, t)
    if best is None or best[0] > ROUTE_MAX_OFF_M:
        return None
    _, i, t = best
    a, b = pts[i], pts[i + 1]
    anchor = [a[k] + (b[k] - a[k]) * t for k in range(3)]
    out = [anchor]
    for q in pts[i + 1:]:
        if _dist3(q, out[-1]) >= 1e-3:
            out.append(q)
    return out if len(out) >= 2 else None


def _player_rect(cam, scr, feet, up, cam_space, project):
    """Screen box (x0, y0, x1, y1 in percent) of the player character: feet .. feet + PLAYER_H_M,
    +-PLAYER_HALF_W_M along the camera's right axis (kept flat). None when part of it is behind
    or right at the camera (first person) or the numbers are not usable."""
    o = cam_space(cam, [0.0, 0.0, 0.0])
    right = []
    for k in range(3):
        e = [0.0, 0.0, 0.0]
        e[k] = 1.0
        right.append(cam_space(cam, e)[1] - o[1])     # camera space is linear: the "right" row
    right[up] = 0.0
    n = math.sqrt(sum(c * c for c in right))
    if not math.isfinite(n) or n < 1e-6:
        return None
    right = [c / n for c in right]
    xs, ys = [], []
    for side in (-PLAYER_HALF_W_M, PLAYER_HALF_W_M):
        for h in (0.0, PLAYER_H_M):
            w = [feet[k] + right[k] * side for k in range(3)]
            w[up] += h
            if not cam_space(cam, w)[0] > FRONT_MIN_M:
                return None
            x, y, front = project(cam, scr, w)
            if not (front and math.isfinite(x) and math.isfinite(y)):
                return None
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _over_rect(x, y, r, rect, scr):
    """True when the dot circle (centre x/y in percent, radius r in percent of the width)
    overlaps the screen box rect; worked out in pixels."""
    w, h = scr[0] / 100.0, scr[1] / 100.0
    cx, cy, rr = x * w, y * h, r * w
    x0, y0, x1, y1 = rect[0] * w, rect[1] * h, rect[2] * w, rect[3] * h
    dx = max(x0 - cx, 0.0, cx - x1)
    dy = max(y0 - cy, 0.0, cy - y1)
    return dx * dx + dy * dy < rr * rr


def compute(msg, screen=None):
    """The quest path for one sense.json message (see the module doc). Never raises for bad
    data in msg; returns None instead."""
    try:
        return _compute(msg, screen)
    except (TypeError, ValueError, IndexError, KeyError, OverflowError, ZeroDivisionError,
            AttributeError):
        return None


def _compute(msg, screen):
    p = _parts(msg)
    if not p:
        return None
    fr, up, feet, goal, has_z, tgt = p
    coords = msg.get("coords") or "ue"
    cam = msg.get("cam")
    if not _sense.cam_ok(cam, coords):
        return None
    info = target_info(msg)
    if info is None:
        return None
    a, b = _horizontal(up)
    dh = math.hypot(goal[a] - feet[a], goal[b] - feet[b])
    if not math.isfinite(dh) or dh <= HIDE_M:
        return None
    scr = _screen(msg, screen)
    aspect = scr[0] / scr[1]
    tan_h, _tan_v = _sense._tan_halves(cam, scr, fr["fov_axis"])
    cam_space, project = fr["camera_space"], fr["project"]

    # ---- the trail as world(s): s = metres along it from its start, 0 <= s <= s_max
    route = _route_points(tgt)
    line = _reanchor(route, feet) if route else None
    trail = "line"
    cum = [0.0]
    if line:
        for i in range(1, len(line)):
            cum.append(cum[-1] + _dist3(line[i - 1], line[i]))
        if not (math.isfinite(cum[-1]) and cum[-1] > 1e-3):
            line = None
    if line:
        total = cum[-1]
        src = tgt.get("route_src")
        trail = src if isinstance(src, str) and src in ROUTE_SRCS else "nav"
        s_max = total
        tail = line[-1]
        near_goal = math.hypot(goal[a] - tail[a], goal[b] - tail[b]) <= ROUTE_END_NEAR_M
        last_s = min(TRAIL_M, total - 1.0) if near_goal else min(TRAIL_M, total)
        if (total < ROUTE_MIN_M and not near_goal) or last_s < START_M:
            line = None                  # too short to draw (dead end, player at the route end): straight line
            trail = "line"
    if line:

        def world(s):
            s = max(0.0, min(s, total))
            i = 1
            while i < len(cum) - 1 and cum[i] < s:
                i += 1
            seg = cum[i] - cum[i - 1]
            t = (s - cum[i - 1]) / seg if seg > 1e-12 else 0.0
            p0, p1 = line[i - 1], line[i]
            w = [p0[k] + (p1[k] - p0[k]) * t for k in range(3)]
            w[up] += ROUTE_LIFT_M
            return w
    else:
        ux, uy = (goal[a] - feet[a]) / dh, (goal[b] - feet[b]) / dh
        climb = (goal[up] - feet[up]) / dh                 # height change per horizontal metre
        s_max = math.inf
        last_s = min(TRAIL_M, dh - 1.0)

        def world(s):
            w = [0.0, 0.0, 0.0]
            w[a] = feet[a] + ux * s
            w[b] = feet[b] + uy * s
            w[up] = feet[up] + climb * s
            return w

    def on_screen(s):
        """(x, y, forward) of the trail point s metres out when it is in front, else None."""
        w = world(s)
        cs = cam_space(cam, w)
        if not cs[0] > FRONT_MIN_M:
            return None
        x, y, front = project(cam, scr, w)
        if not (front and math.isfinite(x) and math.isfinite(y)):
            return None
        return x, y, cs[0]

    rect = _player_rect(cam, scr, feet, up, cam_space, project) if HIDE_PLAYER else None
    dots = []
    n = int(math.floor((last_s - START_M) / STEP_M + 1e-9)) + 1 if last_s >= START_M else 0
    for i in range(n):
        s = START_M + i * STEP_M
        w = world(s)
        flat = math.hypot(w[a] - feet[a], w[b] - feet[b])
        if HIDE_PLAYER and flat <= NEAR_FEET_M:
            continue                                   # right at the feet: under the character
        pt = on_screen(s)
        if pt is None:
            continue
        x, y, f = pt
        if not (VIEW_MIN <= x <= VIEW_MAX and VIEW_MIN <= y <= VIEW_MAX):
            continue
        r = DOT_R_M / (f * tan_h) * 50.0
        r = min(max(r, DOT_R_MIN), DOT_R_MAX) if math.isfinite(r) else DOT_R_MIN
        if rect is not None and _over_rect(x, y, r, rect, scr):
            continue                                   # dot drawn over the character (any distance)
        dots.append({"x": round(x, 2), "y": round(y, 2), "r": round(r, 3), "s": round(s, 2)})

    end = None
    if dots:
        last = dots[-1]
        s = last["s"]
        here = (last["x"], last["y"])
        angle = None
        s_ahead = min(s + ARROW_LOOK_M, s_max)         # route: look along the polyline, not past it
        if s_ahead - s > 0.05:
            ahead = on_screen(s_ahead)
            if ahead is not None:
                angle = _pixel_angle(here, ahead[:2], aspect)
        if angle is None:
            behind = on_screen(max(s - ARROW_LOOK_M, 0.05))
            if behind is not None:
                angle = _pixel_angle(behind[:2], here, aspect)
        if angle is not None and math.isfinite(angle):
            end = {"x": last["x"], "y": last["y"], "angle": round(angle, 1)}

    edge = None
    mark = None
    ep = fr["screen_point"](cam, scr, goal)
    ex, ey = _num(ep.get("x")), _num(ep.get("y"))
    if ep.get("edge"):
        ea = _num(ep.get("angle"))
        if ex is not None and ey is not None and ea is not None:
            edge = {"x": ex, "y": ey, "angle": ea}
    elif not dots and ex is not None and ey is not None:
        # Target on screen but no trail dot is drawn (all off screen or behind the character):
        # mark the target itself so the label still has somewhere to sit.
        mark = {"x": ex, "y": ey}

    return {"dots": dots, "end": end, "edge": edge, "mark": mark, "dist": info["dist"],
            "floors": info["floors"], "label": info["label"], "kind": info["kind"], "src": info["src"],
            "trail": trail}
