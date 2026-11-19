"""
Sense - "something is near" for pickups and collectibles.

A small helper mod inside the game (UE4SS for Phantom Blade Zero) writes sense.json ten times a
second: the player, the camera and the items near the player. This module reads that file, works
out where each item sits on the screen, plays a soft sound when an item is in range and hands the
overlay the points to pulse. It only reads; it never touches the game.

Used by guide.py:
    APP.sense = sense.Sense(log)
    APP.sense.on_draw = lambda pts: APP.overlay.set_sense(pts)
    APP.sense.on_status = lambda text: ...toast...
    APP.sense.on_path = lambda path: APP.overlay.set_path(path)
    APP.sense.on_target = lambda target: ...push_state...   # same dict as state()["target"]
    APP.sense.on_elsewhere = lambda info: ...toast + push_state...  # same dict as state()["elsewhere"]
    APP.sense.start(r"...\\ue4ss\\Mods\\Sense\\out\\sense.json")
"""

import json
import math
import os
import threading
import time
from pathlib import Path

try:
    import winsound
except ImportError:                      # not Windows: sounds are simply skipped
    winsound = None

BASE = Path(__file__).resolve().parent
SOUNDS_DIR = BASE / "sense" / "sounds"
SOUND_NAMES = ("chime", "knock", "harp")
KINDS = {"pickup", "collectible"}        # what counts; bells, gates, enemies never arrive here

POLL_HZ = 20          # how often we look at the file
DRAW_HZ = 10          # how often on_draw may be called
STALE_S = 1.5         # no fresh message for this long = helper not running
# "Main quest is in another area" is kept through gaps that say nothing about the quest: a stale helper
# (game-thread stall) or a helper message with an error / no player (loading screen, pawn respawn). Only a
# normal message (player present, no error) without "target_elsewhere" clears it at once. After this long
# without a normal message that confirms it, it is cleared anyway (game closed, main menu).
ELSEWHERE_HOLD_S = 60.0
MAX_POINTS = 8        # most rings the overlay has to animate
RANGE_MIN, RANGE_MAX = 3.0, 30.0
EDGE_MARGIN = 4.0     # percent: arrows sit this far inside the screen border
MUTE_MAX_S = 24 * 3600  # longest mute we accept (a day); keeps muted_until a sane finite number


# ------------------------------------------------------------------ projection (pure maths)
def _ue_axes(rot):
    """Camera axes in world space from a UE rotator [pitch, yaw, roll] (degrees).
    Same numbers as UE's FRotationMatrix: X forward, Y right, Z up; yaw about Z, pitch about Y,
    roll about X, applied yaw -> pitch -> roll."""
    p, y, r = (math.radians(float(v)) for v in rot)
    sp, cp = math.sin(p), math.cos(p)
    sy, cy = math.sin(y), math.cos(y)
    sr, cr = math.sin(r), math.cos(r)
    fwd = (cp * cy, cp * sy, sp)
    right = (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp)
    up = (-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp)
    return fwd, right, up


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def camera_space_ue(cam: dict, pos) -> tuple:
    """(forward, right, up) distances of a world point as seen from the camera."""
    cpos = cam.get("pos") or (0.0, 0.0, 0.0)
    rot = cam.get("rot") or (0.0, 0.0, 0.0)
    d = (float(pos[0]) - float(cpos[0]), float(pos[1]) - float(cpos[1]), float(pos[2]) - float(cpos[2]))
    fwd, right, up = _ue_axes(rot)
    return _dot(d, fwd), _dot(d, right), _dot(d, up)


def project_ue(cam: dict, screen, pos) -> tuple:
    """World point -> (x_pct, y_pct, in_front) for coords "ue".

    cam = {"pos": [x,y,z], "rot": [pitch, yaw, roll], "fov": horizontal fov in degrees};
    screen = [w, h] (only the aspect is used; 0/None -> 16:9); pos = [x,y,z] in the same units
    as cam.pos. x_pct/y_pct are percent of the screen, 0,0 = top-left, 50,50 = the centre.
    Items behind the camera give in_front False and a far-off point in the item's direction, so
    the caller can still tell which way to point an arrow.
    cam.fov_axis "v" (optional) says cam.fov is the vertical angle instead."""
    return _project_cs(camera_space_ue(cam, pos), cam, screen, "h")


def _tan_halves(cam: dict, screen, default_axis: str = "h") -> tuple:
    """(tan of half the horizontal fov, tan of half the vertical fov) from cam.fov.
    cam.fov_axis "h" or "v" says which angle cam.fov is; anything else -> default_axis
    ("h" for UE, "v" for RE Engine). fov is clamped to 1..179 degrees (default 90, also for NaN)."""
    try:
        fov = float(cam.get("fov") or 90.0)
    except (TypeError, ValueError, OverflowError):
        fov = 90.0
    if math.isnan(fov):
        fov = 90.0
    fov = min(max(fov, 1.0), 179.0)
    axis = cam.get("fov_axis")
    if axis not in ("h", "v"):
        axis = default_axis
    t = math.tan(math.radians(fov) / 2)
    aspect = _aspect(screen)
    if axis == "v":
        return t * aspect, t
    return t, t / aspect


def _project_cs(cs: tuple, cam: dict, screen, default_axis: str) -> tuple:
    """Camera space (forward, right, up) -> (x_pct, y_pct, in_front); see project_ue."""
    f, r, u = cs
    tan_h, tan_v = _tan_halves(cam, screen, default_axis)
    if f <= 1e-6:                                    # behind (or exactly beside) the camera
        n = math.hypot(r, u) or 1.0
        return 50 + 200 * r / n, 50 - 200 * u / n, False
    x_ndc = r / (f * tan_h)                          # -1..1 across the width
    y_ndc = u / (f * tan_v)                          # -1..1 across the height
    return 50 + 50 * x_ndc, 50 - 50 * y_ndc, True


def _aspect(screen) -> float:
    try:
        w, h = float(screen[0]), float(screen[1])
        if w > 0 and h > 0:
            return w / h
    except (TypeError, ValueError, IndexError):
        pass
    return 16 / 9


def edge_point(angle_deg: float, screen, margin: float = EDGE_MARGIN) -> tuple:
    """Where an arrow for an off-screen item sits: walk from the centre in the given direction
    (pixel angle: 0 = right, 90 = down) until the border box [margin, 100-margin] percent."""
    a = math.radians(angle_deg)
    dx, dy = math.cos(a) / _aspect(screen), math.sin(a)      # pixel direction -> percent direction
    half = 50.0 - margin
    t = min(half / abs(dx) if abs(dx) > 1e-9 else math.inf,
            half / abs(dy) if abs(dy) > 1e-9 else math.inf)
    if t == math.inf:
        t = half
    return 50 + t * dx, 50 + t * dy


def screen_point_ue(cam: dict, screen, pos) -> dict:
    """One overlay point: {"x","y","edge","angle"}; edge True = outside the frame or behind,
    then x/y are on the border and angle (degrees, 0 = right, 90 = down) points at the item."""
    cs = camera_space_ue(cam, pos)
    return _screen_point(cs, _project_cs(cs, cam, screen, "h"), screen)


def _screen_point(cs: tuple, xyf: tuple, screen) -> dict:
    """Overlay point from camera space + its projection (shared by the ue and re frames)."""
    f, r, u = cs
    x, y, front = xyf
    if front and 0 <= x <= 100 and 0 <= y <= 100:
        return {"x": round(x, 2), "y": round(y, 2), "edge": False, "angle": 0.0}
    # Direction on screen from the camera-space right/up (y grows downward on screen).
    if abs(r) < 1e-9 and abs(u) < 1e-9:
        angle = 90.0                                 # straight behind: point down
    else:
        angle = math.degrees(math.atan2(-u, r))
    ex, ey = edge_point(angle, screen)
    return {"x": round(ex, 2), "y": round(ey, 2), "edge": True, "angle": round(angle, 1)}


# ---- RE Engine frame ("re"): metres, Y up, camera looks down its local -Z, cam.quat = [x,y,z,w]
def _quat(q) -> tuple:
    """Unit quaternion (x, y, z, w) from cam.quat; None -> no rotation.
    Raises ValueError for a quaternion that is not 4 finite numbers or has (almost) no length."""
    if q is None:
        return 0.0, 0.0, 0.0, 1.0
    if not (isinstance(q, (list, tuple)) and _finite(q, 4)):
        raise ValueError("bad camera quaternion")
    x, y, z, w = (float(v) for v in q[:4])
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if not math.isfinite(n) or n < 1e-6:
        raise ValueError("camera quaternion has no length")
    return x / n, y / n, z / n, w / n


def _qrot(q: tuple, v: tuple) -> tuple:
    """Rotate vector v by the unit quaternion q = (x, y, z, w)."""
    qx, qy, qz, qw = q
    tx = 2 * (qy * v[2] - qz * v[1])                 # t = 2 * (q.xyz cross v)
    ty = 2 * (qz * v[0] - qx * v[2])
    tz = 2 * (qx * v[1] - qy * v[0])
    return (v[0] + qw * tx + (qy * tz - qz * ty),    # v + w*t + q.xyz cross t
            v[1] + qw * ty + (qz * tx - qx * tz),
            v[2] + qw * tz + (qx * ty - qy * tx))


def _re_axes(quat) -> tuple:
    """Camera axes in world space for coords "re": forward = q*(0,0,-1), right = q*(1,0,0),
    up = q*(0,1,0)."""
    q = _quat(quat)
    return _qrot(q, (0.0, 0.0, -1.0)), _qrot(q, (1.0, 0.0, 0.0)), _qrot(q, (0.0, 1.0, 0.0))


def camera_space_re(cam: dict, pos) -> tuple:
    """(forward, right, up) distances of a world point as seen from an RE Engine camera."""
    cpos = cam.get("pos") or (0.0, 0.0, 0.0)
    d = (float(pos[0]) - float(cpos[0]), float(pos[1]) - float(cpos[1]), float(pos[2]) - float(cpos[2]))
    fwd, right, up = _re_axes(cam.get("quat"))
    return _dot(d, fwd), _dot(d, right), _dot(d, up)


def project_re(cam: dict, screen, pos) -> tuple:
    """Like project_ue for coords "re". cam.fov is the vertical angle unless cam.fov_axis == "h"."""
    return _project_cs(camera_space_re(cam, pos), cam, screen, "v")


def screen_point_re(cam: dict, screen, pos) -> dict:
    """Like screen_point_ue for coords "re" (cam = {"pos", "quat": [x,y,z,w], "fov", "fov_axis"})."""
    cs = camera_space_re(cam, pos)
    return _screen_point(cs, _project_cs(cs, cam, screen, "v"), screen)


PROJECTORS = {"ue": screen_point_ue, "re": screen_point_re}

# Per coordinate frame: the up axis (ue: Z = 2, re: Y = 1), camera-space and projection
# functions, which camera key holds the rotation, and which angle cam.fov is by default.
FRAMES = {
    "ue": {"up": 2, "camera_space": camera_space_ue, "project": project_ue,
           "screen_point": screen_point_ue, "cam_key": "rot", "cam_n": 3, "fov_axis": "h"},
    "re": {"up": 1, "camera_space": camera_space_re, "project": project_re,
           "screen_point": screen_point_re, "cam_key": "quat", "cam_n": 4, "fov_axis": "v"},
}


def frame(coords):
    """The frame entry for msg["coords"] (missing/None/"" -> "ue"); None for an unknown frame."""
    if coords is None or coords == "":
        return FRAMES["ue"]
    return FRAMES.get(coords) if isinstance(coords, str) else None


def up_axis(coords):
    """Index of the up axis: 2 for "ue" (Z up), 1 for "re" (Y up); None for an unknown frame."""
    fr = frame(coords)
    return fr["up"] if fr else None


def cam_ok(cam, coords) -> bool:
    """True when cam has a finite pos and the rotation its frame needs (ue: rot, re: quat)."""
    fr = frame(coords)
    if not fr or not isinstance(cam, dict):
        return False
    key, n = fr["cam_key"], fr["cam_n"]
    if not (isinstance(cam.get("pos"), (list, tuple)) and _finite(cam["pos"])
            and isinstance(cam.get(key), (list, tuple)) and _finite(cam[key], n)):
        return False
    if key == "quat":
        try:
            _quat(cam[key])
        except ValueError:
            return False
    return True


# ------------------------------------------------------------------ sounds
def sound_path(name: str) -> Path:
    return SOUNDS_DIR / f"{name}.wav"


def play_wav(path) -> bool:
    """Play a WAV file without waiting for it (own thread; PlaySound itself is async)."""
    if winsound is None:
        return False
    p = str(path)
    if not os.path.exists(p):
        return False

    def f():
        try:
            winsound.PlaySound(p, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            pass
    threading.Thread(target=f, name="sense-sound", daemon=True).start()
    return True


def sound_interval(dist: float, range_m: float) -> float:
    """Seconds between two sounds: quicker the closer the item is (0.5 s at hand, 1.5 s at the edge)."""
    try:
        dist, range_m = float(dist), float(range_m)
    except (TypeError, ValueError):
        return 1.5
    if range_m <= 0 or not math.isfinite(dist) or not math.isfinite(range_m):
        return 1.5
    return min(max(1.5 * dist / range_m, 0.5), 1.5)


def _finite(values, n: int = 3) -> bool:
    """True when the first n entries are real, finite numbers (no NaN, no inf, no strings)."""
    try:
        return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in values[:n]) and len(values) >= n
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------ the reader
_UNSET = object()                         # "on_path never called yet"
_QP = []                                  # [module or None] once imported


def _stopped(stop) -> bool:
    """True when a reader thread's own stop event is given and set (None = not a reader call)."""
    return stop is not None and stop.is_set()


def _questpath():
    """questpath, imported on first use (questpath imports sense). None if it is missing."""
    if not _QP:
        try:
            import questpath
            _QP.append(questpath)
        except ImportError:
            _QP.append(None)
    return _QP[0]


class Sense:
    def __init__(self, log=print, enabled: bool = True, range_m: float = 10.0, sound: str = "chime",
                 sound_on: bool = False, path_enabled: bool = True):
        self.log = log
        self.enabled = bool(enabled)
        self.path_enabled = bool(path_enabled)   # quest path switch (own switch, not tied to enabled)
        self.range_m = self._clamp_range(range_m)
        self.sound = sound if sound in SOUND_NAMES else "chime"
        self.sound_on = bool(sound_on)    # False = no beeps near items (pulses/arrows still show)
        self.muted_until = 0.0            # epoch seconds; 0 = not muted
        self.screen = (1920, 1080)        # fallback when the helper sends no viewport size
        self.on_draw = None               # fn(points: list) - set by guide.py (overlay.set_sense)
        self.on_status = None             # fn(text: str) - set by guide.py (toast + state push)
        self.on_path = None               # fn(path: dict | None) - set by guide.py (overlay.set_path)
        self.on_target = None             # fn(target: dict | None) - set by guide.py (state push);
                                          # same dict as state()["target"]; at most once a second
        self.on_elsewhere = None          # fn(info: dict | None) - set by guide.py (toast + state push);
                                          # same dict as state()["elsewhere"]; only when it changes
        self.play = play_wav              # swapped in tests
        self.path = None
        self._thread = None
        self._stop = threading.Event()    # the running reader thread's own event (new one per start)
        self._lock = threading.Lock()
        self._life = threading.Lock()     # start()/stop() never run at the same time
        self._no_cam_said = False         # logged once: helper sends items but no camera
        self._msg = None                  # last good message (dict)
        self._msg_at = 0.0                # monotonic time it arrived
        self._last_key = None             # ("t", "seq") of the last message, to see it moved
        self._stamp = None                # (mtime_ns, size) of the file we last read
        self._helper_ok = False
        self._status = None               # last text handed to on_status
        self._nearest = None
        self._count = 0
        self._next_sound_at = 0.0
        self._last_draw_at = 0.0
        self._last_drawn = None           # what on_draw got last (to skip repeats of "nothing")
        self._bad_reads = 0
        self._last_error = None
        self._missing_sound_said = set()
        self._target = None               # {"dist","floors","kind"} of the helper's target, or None
        self._path_src = None             # the message the cached path was worked out from
        self._path_screen = None          # ... and the fallback screen size used then
        self._path_now = None             # cached questpath.compute() result
        self._last_path = _UNSET          # what on_path got last
        self._last_path_at = 0.0
        self._path_err_said = False
        self._target_sent = None          # what on_target got last (None = nothing / no target)
        self._target_sent_at = -1e9       # monotonic time of that call
        self._elsewhere = None            # {"label","map"}: the tracked main quest is in another area
        self._elsewhere_sent = None       # what on_elsewhere got last (None = not elsewhere)
        self._elsewhere_seen_at = 0.0     # monotonic time a message last carried target_elsewhere

    # ------------------------------------------------------------- start / stop
    def start(self, path):
        """Watch this sense.json (None = no helper for this game). Safe to call again."""
        with self._life:
            self._stop_locked()
            self.path = str(path) if path else None
            with self._lock:
                self._msg, self._msg_at, self._last_key, self._stamp = None, 0.0, None, None
                self._helper_ok, self._nearest, self._count = False, None, 0
            self._status = None
            self._last_drawn = None
            self._no_cam_said = False
            self._path_src, self._path_now = None, None
            if not self.path:
                return
            # Each reader thread gets its own stop event. An old thread that was still busy when
            # stop() gave up waiting keeps its own (set) event and ends by itself; it can never
            # pick up the new thread's event and run on next to it.
            ev = threading.Event()
            self._stop = ev
            self._thread = threading.Thread(target=self._run, args=(ev, self.path),
                                            name="sense", daemon=True)
            self._thread.start()
            self.log(f"Sense: watching {self.path}")

    def stop(self):
        with self._life:
            self._stop_locked()

    def _stop_locked(self):
        """stop() with self._life already held."""
        t = self._thread
        if t and t.is_alive():
            self._stop.set()
            t.join(1.0)
            if t.is_alive():
                self.log("Sense: old reader is slow to stop; it will end on its own")
        self._thread = None
        self._draw([])
        self._draw_path(None)
        with self._lock:
            self._helper_ok, self._nearest, self._count = False, None, 0
            self._target = None
            self._elsewhere = None
        # The target went away with the reader: tell the window now (no reader is left to do
        # it later, so this one call skips the once-a-second limit).
        self._check_target(force=True)
        self._check_elsewhere()

    # ------------------------------------------------------------- settings (any thread)
    @staticmethod
    def _clamp_range(m) -> float:
        try:
            v = float(m)
        except (TypeError, ValueError):
            return 10.0
        if not math.isfinite(v):
            # NaN slips past min/max (comparisons are always False) and would
            # end up as a bare NaN token in the state JSON; inf is clamped.
            if math.isnan(v):
                return 10.0
            return RANGE_MAX if v > 0 else RANGE_MIN
        return min(max(v, RANGE_MIN), RANGE_MAX)

    def set_enabled(self, on: bool):
        self.enabled = bool(on)
        if not self.enabled:
            self._draw([])

    def set_path_enabled(self, on: bool):
        """Quest path switch. Off = the overlay's path is cleared now (the target is still read)."""
        self.path_enabled = bool(on)
        if not self.path_enabled:
            self._draw_path(None)

    def set_range(self, m):
        self.range_m = self._clamp_range(m)

    def set_sound(self, name: str) -> bool:
        if name not in SOUND_NAMES:
            self.log(f"Sense: unknown sound {name!r}, keeping {self.sound}")
            return False
        self.sound = name
        return True

    def set_sound_on(self, on: bool):
        """Sound switch: False = never play the near-item sound (the Test button still plays)."""
        self.sound_on = bool(on)

    def set_screen(self, w, h):
        """Fallback viewport size (the game's monitor) when the helper sends [0, 0]."""
        try:
            if int(w) > 0 and int(h) > 0:
                self.screen = (int(w), int(h))
        except (TypeError, ValueError):
            pass

    def mute(self, seconds):
        """Quiet for this many seconds (0 = unmute now). The pulse keeps showing."""
        try:
            s = float(seconds)
        except (TypeError, ValueError, OverflowError):
            s = 0.0
        if not math.isfinite(s) or s <= 0:      # inf/nan would break state() and the JSON
            s = 0.0
        s = min(s, MUTE_MAX_S)
        self.muted_until = time.time() + s if s > 0 else 0.0

    def muted(self) -> bool:
        return self.muted_until > time.time()

    def play_sound(self, name=None) -> bool:
        """Play one sound now (the window's Test button). Ignores mute and enabled."""
        name = name if name in SOUND_NAMES else self.sound
        p = sound_path(name)
        ok = bool(self.play(p))
        if not ok and name not in self._missing_sound_said:
            self._missing_sound_said.add(name)
            self.log(f"Sense: cannot play {p}")
        return ok

    # ------------------------------------------------------------- state for the window
    def state(self) -> dict:
        with self._lock:
            ok, nearest, count, at = self._helper_ok, self._nearest, self._count, self._msg_at
            target = dict(self._target) if self._target else None
            elsewhere = dict(self._elsewhere) if self._elsewhere else None
        age = round(time.monotonic() - at, 2) if at else None
        left = self.muted_until - time.time()
        return {
            "enabled": self.enabled,
            "range": self.range_m,
            "sound": self.sound,
            "sound_on": self.sound_on,
            "sounds": list(SOUND_NAMES),
            "muted_until": self.muted_until if left > 0 else 0,
            "muted_left_s": int(left) if left > 0 else 0,
            "helper_ok": ok,
            "helper_age_s": age,
            "helper_file": self.path,
            "nearest": dict(nearest) if nearest else None,
            "count": count,
            "path_enabled": self.path_enabled,
            "target": target,
            "elsewhere": elsewhere,
        }

    # ------------------------------------------------------------- reader thread
    def _run(self, stop: threading.Event, path: str):
        tick = 1.0 / POLL_HZ
        while not stop.is_set():
            try:
                self._poll(path, stop)
            except Exception as e:                  # never let the thread die
                if not stop.is_set():
                    self.log(f"Sense: {e!r}")
            stop.wait(tick)

    def _poll(self, path=None, stop=None):
        """One look at the file. stop = the calling reader thread's own stop event: once it is
        set (stop() gave up waiting and maybe a new game's reader runs), this call changes no
        shared state and calls no callback (on_draw/on_path/on_status/on_target)."""
        if _stopped(stop):
            return
        now = time.monotonic()
        msg = self._read_file(path or self.path)
        if msg is not None:
            key = (msg.get("t"), msg.get("seq"))
            if key == (None, None):                      # helper sends no clock: every rewrite counts
                key = self._stamp
            if key != self._last_key:                    # moved (or the helper restarted)
                with self._lock:
                    if _stopped(stop):
                        return
                    self._last_key = key
                    self._msg, self._msg_at = msg, now
                err = msg.get("error")
                if err and err != self._last_error:
                    self._last_error = err
                    self.log(f"Sense: helper says: {err}")
        with self._lock:
            if _stopped(stop):
                return
            fresh = self._msg is not None and now - self._msg_at < STALE_S
            cur = self._msg
        if fresh != self._helper_ok:
            with self._lock:
                if _stopped(stop):
                    return
                self._helper_ok = fresh
            self._say_status("Helper running" if fresh else "Helper not running", stop)
        if not fresh:
            with self._lock:
                if _stopped(stop):
                    return
                self._nearest, self._count = None, 0
                self._target = None
                self._hold_elsewhere_locked(now)     # a stall is no "it cleared" (one toast)
            self._draw([], stop)
            self._draw_path(None, stop)
            self._check_target(stop=stop)
            self._check_elsewhere(stop)
            return
        # The quest path has its own switch: it is worked out before the Sense switch returns.
        self._update_path(cur, stop)
        self._check_target(stop=stop)
        self._update_elsewhere(cur, stop)
        points, can_draw = self._points(cur)
        nearest = points[0] if points else None
        with self._lock:
            if _stopped(stop):
                return
            self._nearest = ({"dist": nearest["dist"], "kind": nearest["kind"], "id": nearest["id"]}
                             if nearest else None)
            self._count = len(points)
        if not self.enabled:
            self._draw([], stop)
            return
        if (nearest and self.sound_on and not self.muted() and now >= self._next_sound_at
                and not _stopped(stop)):
            self.play_sound()
            self._next_sound_at = now + sound_interval(nearest["dist"], self.range_m)
        if now - self._last_draw_at >= 1.0 / DRAW_HZ:
            # No camera in the message = the spots on screen cannot be worked out, so the
            # overlay gets nothing (the sound and the nearest/count numbers still work).
            self._draw(points if can_draw else [], stop)

    def _read_file(self, path=None):
        """The file, parsed, when it changed since last time; None otherwise (or when half-written)."""
        path = path or self.path
        if not path:
            return None
        try:
            st = os.stat(path)
        except OSError:                                 # missing, or mid-rename
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return None
        try:
            with open(path, "rb") as f:
                raw = f.read()
            # A stray non-UTF-8 byte (ids come straight from the game) costs one odd character
            # in that id, not the whole message. The ids are only ever shown as text.
            msg = json.loads(raw.decode("utf-8-sig", errors="replace"))
            if not isinstance(msg, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            self._bad_reads += 1                        # keep the last good one; try again next tick
            if self._bad_reads in (1, 50, 500):
                self.log(f"Sense: could not read {path} ({self._bad_reads}x)")
            return None
        self._stamp = stamp
        self._bad_reads = 0
        return msg

    def _points(self, msg: dict) -> tuple:
        """(points, can_draw): items in range as overlay points, nearest first, at most MAX_POINTS.
        can_draw is False when the message has no usable camera; the points then still carry
        dist/kind/id for the sound and the window, but their x/y mean nothing."""
        items = msg.get("items")
        if not isinstance(items, list):
            return [], True
        player = msg.get("player")
        player_ok = isinstance(player, (list, tuple)) and _finite(player)
        found = []
        for it in items:
            if not isinstance(it, dict) or it.get("kind") not in KINDS:
                continue
            pos = it.get("pos")
            if not (isinstance(pos, (list, tuple)) and _finite(pos)):
                continue
            dist = it.get("dist")
            dist_ok = isinstance(dist, (int, float)) and not isinstance(dist, bool) and math.isfinite(dist)
            if not dist_ok and player_ok:
                dist = math.dist([float(v) for v in pos[:3]], [float(v) for v in player[:3]])
                dist_ok = math.isfinite(dist)
            if not dist_ok or dist > self.range_m:        # NaN/inf dist: the item is skipped
                continue
            found.append((float(dist), it, pos))
        found.sort(key=lambda t: t[0])
        coords = msg.get("coords") or "ue"
        proj = PROJECTORS.get(coords) if isinstance(coords, str) else None
        cam = msg.get("cam") if isinstance(msg.get("cam"), dict) else {}
        cam_good = cam_ok(cam, coords)
        if found and proj and not cam_good and not self._no_cam_said:
            self._no_cam_said = True
            self.log("Sense: helper sends items but no camera; spots on screen are off until it does")
        screen = msg.get("screen")
        if not (isinstance(screen, (list, tuple)) and len(screen) >= 2 and screen[0] and screen[1]):
            screen = self.screen
        out = []
        for dist, it, pos in found[:MAX_POINTS]:
            pt = {"x": 50.0, "y": 50.0, "edge": False, "angle": 0.0}
            if proj and cam_good:
                try:
                    pt = proj(cam, screen, pos)
                except (TypeError, ValueError, IndexError):
                    continue
            pt.update(dist=round(dist, 2), kind=it["kind"], id=str(it.get("id") or ""))
            out.append(pt)
        return out, bool(cam_good or not proj)

    # ------------------------------------------------------------- quest path
    def _update_path(self, msg: dict, stop=None):
        """Work out the target summary and the quest path for a new message (cached per message)
        and hand the path to on_path (None when the path switch is off). stop: see _poll."""
        if _stopped(stop):
            return
        if msg is not self._path_src or self.screen != self._path_screen:
            self._path_src, self._path_screen = msg, self.screen
            info = path = None
            qp = _questpath()
            if qp is not None:
                try:
                    info = qp.target_info(msg)
                    path = qp.compute(msg, self.screen)
                except Exception as e:              # a maths bug must not stop the items
                    info = path = None
                    if not self._path_err_said:
                        self._path_err_said = True
                        self.log(f"Sense: quest path failed: {e!r}")
            elif not self._path_err_said:
                self._path_err_said = True
                self.log("Sense: questpath.py missing; no quest path")
            with self._lock:
                if _stopped(stop):
                    return
                self._target = ({"dist": info["dist"], "floors": info["floors"], "kind": info["kind"]}
                                if info else None)
            self._path_now = path
        self._draw_path(self._path_now if self.path_enabled else None, stop)

    def _check_target(self, stop=None, force: bool = False):
        """Call on_target(state()["target"]) when the target appeared or went away, its distance
        moved 1 m or more, or its floors/kind changed, since the last call. At most once a
        second (a change inside that second is sent once the second is over, on a later poll);
        force skips that limit (used by stop, when no reader is left to send it later)."""
        if _stopped(stop):
            return
        with self._lock:
            cur = dict(self._target) if self._target else None
        last = self._target_sent
        if cur is None and last is None:
            return
        if cur is not None and last is not None:
            try:
                moved = abs(float(cur.get("dist")) - float(last.get("dist"))) >= 1.0
            except (TypeError, ValueError):
                moved = cur.get("dist") != last.get("dist")
            if not (moved or cur.get("floors") != last.get("floors") or cur.get("kind") != last.get("kind")):
                return
        now = time.monotonic()
        if not force and now - self._target_sent_at < 1.0:
            return
        self._target_sent = cur
        self._target_sent_at = now
        cb = self.on_target
        if cb and not _stopped(stop):
            try:
                cb(dict(cur) if cur else None)
            except Exception as e:
                self.log(f"Sense: on_target failed: {e!r}")

    # ------------------------------------------------------------- main quest in another area
    @staticmethod
    def elsewhere_info(msg) -> dict | None:
        """sense.json "target_elsewhere" -> {"label": str, "map": str} or None.
        The UE helper sends {"label": <quest label>, "map": <the quest's map id>} (top-level key)
        when the tracked MAIN quest is on another map than the player; the key is absent otherwise.
        Only a JSON object counts; label/map that are not plain text become "" (capped in length)."""
        if not isinstance(msg, dict):
            return None
        v = msg.get("target_elsewhere")
        if not isinstance(v, dict):
            return None

        def text(x, n):
            if isinstance(x, bool) or not isinstance(x, (str, int, float)):
                return ""
            x = " ".join(str(x).split())          # no newlines/control spacing in the window text
            return "" if x.lower() == "none" else x[:n]
        return {"label": text(v.get("label"), 80), "map": text(v.get("map"), 60)}

    @staticmethod
    def _is_gap_message(msg) -> bool:
        """True for a helper message that says nothing about the quest: an "error" message (the helper's
        "no player yet" during a loading screen / pawn respawn, or a write error) or one without a player.
        Such a message carries no target_elsewhere even while the main quest is still in another area."""
        if not isinstance(msg, dict):
            return True
        return bool(msg.get("error")) or not isinstance(msg.get("player"), (list, tuple, dict))

    def _hold_elsewhere_locked(self, now):
        """self._lock held. Keep the last "another area" info through a gap, but not longer than
        ELSEWHERE_HOLD_S since a message last carried it."""
        if self._elsewhere and now - self._elsewhere_seen_at >= ELSEWHERE_HOLD_S:
            self._elsewhere = None

    def _update_elsewhere(self, msg, stop=None):
        """A message with target_elsewhere sets it; a normal message without the key clears it at once;
        an error / no-player message keeps the last one (see ELSEWHERE_HOLD_S), so a loading screen does
        not count as "cleared and came back" (that would show the toast again)."""
        if _stopped(stop):
            return
        info = self.elsewhere_info(msg)
        gap = self._is_gap_message(msg)
        now = time.monotonic()
        with self._lock:
            if _stopped(stop):
                return
            if info is not None:
                self._elsewhere = info
                self._elsewhere_seen_at = now
            elif gap:
                self._hold_elsewhere_locked(now)
            else:
                self._elsewhere = None
        self._check_elsewhere(stop)

    def _check_elsewhere(self, stop=None):
        """Call on_elsewhere(state()["elsewhere"]) when it appeared, went away or its label/map
        changed since the last call (never for "still the same")."""
        if _stopped(stop):
            return
        with self._lock:
            cur = dict(self._elsewhere) if self._elsewhere else None
        if cur == self._elsewhere_sent:
            return
        self._elsewhere_sent = cur
        cb = self.on_elsewhere
        if cb and not _stopped(stop):
            try:
                cb(dict(cur) if cur else None)
            except Exception as e:
                self.log(f"Sense: on_elsewhere failed: {e!r}")

    def _draw_path(self, path, stop=None):
        """Hand the overlay the quest path (None = clear). At most DRAW_HZ times a second; an
        unchanged path is repeated at most once a second; None is sent once until a path comes.
        stop: see _poll (a set event = do nothing)."""
        if _stopped(stop):
            return
        now = time.monotonic()
        if path is None:
            if self._last_path is None:
                return
        else:
            if self._last_path not in (None, _UNSET) and now - self._last_path_at < 1.0 / DRAW_HZ:
                return
            if path == self._last_path and now - self._last_path_at < 1.0:
                return
        self._last_path = path
        self._last_path_at = now
        cb = self.on_path
        if cb and not _stopped(stop):
            try:
                cb(path)
            except Exception as e:
                self.log(f"Sense: on_path failed: {e!r}")

    # ------------------------------------------------------------- callbacks
    def _draw(self, points: list, stop=None):
        """Hand the overlay the points. Unchanged points (nothing moved, or still nothing) are
        repeated at most once a second, so the overlay gets them again after a clear.
        stop: see _poll (a set event = do nothing)."""
        if _stopped(stop):
            return
        if points == self._last_drawn and (not points or time.monotonic() - self._last_draw_at < 1.0):
            return
        self._last_drawn = list(points)
        self._last_draw_at = time.monotonic()
        cb = self.on_draw
        if cb and not _stopped(stop):
            try:
                cb(points)
            except Exception as e:
                self.log(f"Sense: on_draw failed: {e!r}")

    def _say_status(self, text: str, stop=None):
        if _stopped(stop) or text == self._status:
            return
        self._status = text
        self.log(f"Sense: {text}")
        cb = self.on_status
        if cb and not _stopped(stop):
            try:
                cb(text)
            except Exception as e:
                self.log(f"Sense: on_status failed: {e!r}")
