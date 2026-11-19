"""
Game Guide - ask for help when you're stuck in a game, by voice or by typing.

  Hold F10 (in game)   -> talk, release to send the question
  F11 (in game)        -> grab a screenshot to attach to the next question
  Window               -> chat box, mic button, game picker, hint level

Brains (config.json "brain_order"): claude (your Claude subscription, no GPU) ->
gemini (free API key, no GPU) -> local (small Ollama model, unloads after 2 min).
"""

import asyncio
import ctypes
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG = BASE / "guide.log"
DIAG = BASE / "startup_diag.txt"
STARTUP_ERROR = 3   # child exit code: it could not start (bad config.json, missing package); don't restart it


def startup_error(msg: str):
    """Stop the child with a plain message. The supervisor shows it in a message box (pythonw has no console)."""
    try:
        DIAG.write_text(msg, encoding="utf-8")
    except OSError:
        pass
    print(msg, file=sys.stderr) if sys.stderr else None
    sys.exit(STARTUP_ERROR)


def message_box(text: str, title: str = "Game Guide"):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        pass


def supervise():
    """Keep the real app (a --child process) running: if it crashes, start it again (without
    popping a new window - the open one reconnects). A clean exit (tray > Quit) ends this too."""
    import socket
    port = 47130
    try:
        # read_json is defined further down, after supervise() has already run
        port = int(json.loads((BASE / "config.json").read_text(encoding="utf-8-sig")).get("port", port))
    except Exception:
        pass
    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) == 0:        # already running: child just opens the window
            subprocess.call([sys.executable, str(BASE / "guide.py"), "--child", *sys.argv[1:]])
            return
    args, crashes = sys.argv[1:], []
    while True:
        code = subprocess.call([sys.executable, str(BASE / "guide.py"), "--child", *args])
        if code == 0:
            return
        if code == STARTUP_ERROR:
            try:
                msg = DIAG.read_text(encoding="utf-8")
            except OSError:
                msg = "Game Guide could not start. guide.log in its folder has the details."
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} Could not start: {msg}\n")
            message_box(msg, "Game Guide could not start")
            return
        crashes = [t for t in crashes if time.time() - t < 120] + [time.time()]
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} App exited with code {code} - restarting\n")
        if len(crashes) > 5:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} Crashed 6 times in 2 minutes - giving up\n")
            message_box("Game Guide crashed 6 times in 2 minutes and stopped.\n\n"
                        "guide.log in the Game Guide folder has the details.")
            return
        time.sleep(2)
        args = [a for a in sys.argv[1:] if a != "--no-window"] + ["--no-window"]


if __name__ == "__main__" and "--child" not in sys.argv and "--no-supervisor" not in sys.argv:
    supervise()
    sys.exit(0)


def _log_crash(kind, value, tb):
    """Any crash of the app (also while loading, before main) goes to guide.log: pythonw has no console."""
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} FATAL "
                    + "".join(traceback.format_exception(kind, value, tb)))
    except OSError:
        pass
    sys.__excepthook__(kind, value, tb)


sys.excepthook = _log_crash


def write_atomic(p: Path, text: str):
    """Write a file so it is never left half-written (a cut-off JSON file would stop the next start)."""
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")   # own temp file per call
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        for i in range(20):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:           # Windows: another save or a reader has the file open right now
                if i == 19:
                    raise
                time.sleep(0.01 * (i + 1))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(p: Path):
    """Read a JSON file that write_atomic saves (it may be swapped in by another thread right now)."""
    for i in range(20):
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except PermissionError:               # Windows: os.replace is swapping the file in at this moment
            if i == 19:
                raise
            time.sleep(0.01 * (i + 1))

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# GPU speech-to-text: the CUDA DLLs come as pip packages (nvidia-cublas-cu12 ...) wherever pip put them
# (the Python folder, a venv or the per-user folder).
import importlib.util
_nvidia = importlib.util.find_spec("nvidia")
for _root in (_nvidia.submodule_search_locations or []) if _nvidia else []:
    for _bin in Path(_root).glob("*/bin"):
        os.add_dll_directory(str(_bin))
        os.environ["PATH"] = str(_bin) + os.pathsep + os.environ.get("PATH", "")

try:
    import numpy as np
    from aiohttp import web

    import brains
    import brainsetup
    import mapnav
    import marks
    import overlay
    import screenread
except ImportError as _e:
    startup_error(f"Game Guide could not start: a Python package is missing ({_e}).\n\n"
                  "Open a command prompt in the Game Guide folder and run:\n"
                  "py -3.12 -m venv .venv\n"
                  ".venv\\Scripts\\python -m pip install -r requirements.txt")

try:
    import sense                 # nearby-item sound + pulse (sense.py reads the game helper's sense.json)
    SENSE_IMPORT_ERROR = None
except Exception as _e:          # the app must still start without it (missing or broken sense.py)
    sense = None
    SENSE_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True) if sys.stdout else None
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


DEFAULTS = {
    "port": 47130,
    "brain_order": ["claude", "gemini", "local"],
    "brain_setup_done": False,  # the "Choose your brain" panel was answered (Use this / Skip); False = show it
    "claude_model": "sonnet",
    "claude_model_careful": "opus",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "local_model": "qwen3:4b-instruct",
    "vision_model": "qwen3-vl:4b-instruct",
    "num_ctx": 8192,
    "keep_alive": "2m",
    "hotkey_talk": "f10",
    "hotkey_talk_enabled": True,
    "hotkey_screenshot": "f11",
    "whisper_model": "large-v3-turbo",
    "whisper_device": "cuda",
    "whisper_compute": "int8_float16",
    "whisper_unload_after_s": 600,
    "speak_answers": False,
    "tts_voice": "af_heart",
    "hotkey_read": "f8",
    "hotkey_next": "f7",
    "hotkey_overlay": "f6",
    "hotkey_where": "f4",
    "overlay": True,
    "dossier": True,
    "read_mode": "auto",
    "read_model": "haiku",
    "voice": "en-US-AndrewMultilingualNeural",
    "voice_rate": "+0%",
    "default_level": "hint",
    "open_window": True,
    "auto_detect": True,
    "sense": True,            # nearby-item sound + pulse (when the game has a helper installed)
    "sense_range": 10,        # metres, 3-30
    "sense_sound": "chime",   # chime | knock | harp (sense\sounds\<name>.wav)
    "sense_sound_on": False,  # play the near-item sound? (off by default; pulses/arrows always show)
    "sense_path": True,       # quest path: dotted trail on the ground toward the game's target (own switch)
    "sense_route": True,      # quest path follows the game's walking map (UE helper; off = <Sense mod>/route_off.txt)
    "sense_game_dirs": {},    # game slug -> game folder, for installing the Sense helper (e.g. "phantom-blade-zero")
    "kokoro_models_dir": "",  # folder with kokoro-v1.0.onnx + voices-v1.0.bin ("" = models\ next to guide.py)
    "extra_vocab_file": "",   # optional text file with extra words for speech-to-text (comma separated, # = comment)
}


def load_config() -> dict:
    p = BASE / "config.json"
    cfg = dict(DEFAULTS)
    if p.exists():
        try:
            saved = read_json(p)
        except ValueError as e:              # a hand edit went wrong, most often a folder path with single \
            saved = e
        if not isinstance(saved, dict):
            startup_error(f"Game Guide could not start: config.json is not valid ({saved}).\n\n"
                          "In folder paths write / instead of \\ (or \\\\ for each \\). Fix the file, "
                          "or delete config.json to start again with the default settings.")
        cfg.update(saved)
    else:
        write_atomic(p, json.dumps(DEFAULTS, indent=2))
    return cfg


CFG = load_config()
BOOT_ID = str(int(time.time()))   # changes on every app start; open windows reload to pick up new UI


def save_settings(**kv):
    """Persist settings changed from the window into config.json."""
    CFG.update(kv)
    p = BASE / "config.json"
    saved = read_json(p) if p.exists() else {}
    saved.update(kv)
    write_atomic(p, json.dumps(saved, indent=2))


def slugify(name: str) -> str:
    # "Dragon's Dogma 2" -> "dragons-dogma-2" (same rule as sense\install_helper.py _slug)
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"['’]", "", name.lower())).strip("-") or "unknown"


# ------------------------------------------------------------------ Sense (nearby items) paths
SENSE_DIR = BASE / "sense"
SENSE_SOUNDS = SENSE_DIR / "sounds"                                   # chime.wav, knock.wav, harp.wav
SENSE_SOUND_NAMES = ("chime", "knock", "harp")
SENSE_ZIP = SENSE_DIR / "downloads" / "ue4ss-pbz-1.0.2.zip"          # UE4SS for Phantom Blade Zero (Nexus mod 5)
SENSE_HELPER_SRC = SENSE_DIR / "helper" / "ue4ss" / "Sense"           # the Lua helper mod we ship
_INSTALL_HELPER = None


def load_install_helper():
    """Load sense\\install_helper.py by path. `import sense.install_helper` can't work: `sense` is already
    the sense.py module (a module wins over a folder of the same name)."""
    global _INSTALL_HELPER
    if _INSTALL_HELPER is None:
        import importlib.util
        p = SENSE_DIR / "install_helper.py"
        spec = importlib.util.spec_from_file_location("sense_install_helper", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _INSTALL_HELPER = mod
    return _INSTALL_HELPER


def play_sense_sound(name: str) -> bool:
    """Play one Sense sound once (the window's Test button). Never blocks: plays on its own thread."""
    import winsound
    p = SENSE_SOUNDS / f"{name}.wav"
    if not p.exists():
        return False
    threading.Thread(target=winsound.PlaySound, daemon=True,
                     args=(str(p), winsound.SND_FILENAME | winsound.SND_NODEFAULT)).start()
    return True


# ------------------------------------------------------------------ per-game storage
class GameStore:
    _notes_lock = threading.Lock()   # notes.md is written from the answer loop AND the clue-catcher thread

    def __init__(self, name: str):
        self.name = name
        self.dir = BASE / "games" / slugify(name)
        (self.dir / "shots").mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "game.json"
        self.meta = read_json(self.meta_path) if self.meta_path.exists() else {}
        self.meta["name"] = name

    @property
    def dossier(self) -> str:
        """One-time research about the game (structure, bosses, missables...) - see App.ensure_dossier."""
        p = self.dir / "dossier.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    @property
    def notes(self) -> str:
        p = self.dir / "notes.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    @notes.setter
    def notes(self, text: str):
        (self.dir / "notes.md").write_text(text.strip() + "\n", encoding="utf-8")

    def history(self) -> list[dict]:
        p = self.dir / "history.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def append(self, msg: dict):
        with open(self.dir / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def save_meta(self):
        write_atomic(self.meta_path, json.dumps(self.meta, indent=2))

    def replace_notes(self, bullets: str, seen: str | None = None):
        """The AI hands back the full, rewritten progress list; it replaces the old one.
        Clue lines (from the clue catcher) have their own budget so they don't push progress lines out.
        `seen` = the notes text the AI was given. A clue the clue catcher saved AFTER that (it runs in
        its own thread, so it can land while the answer is still running) is not in the AI's list;
        it is kept instead of being wiped. When `seen` is None every clue missing from the list is kept."""
        lines = [l.strip().lstrip("-*• ").strip() for l in bullets.splitlines()]
        lines = [l for l in lines if l and "<" not in l]
        clues = [l for l in lines if _is_clue(l)]
        rest = [l for l in lines if not _is_clue(l)][:8]
        if not (rest or clues):
            return
        with self._notes_lock:
            known = {_clue_key(l) for l in clues}
            seen_keys = None
            if seen is not None:
                seen_keys = {_clue_key(l) for l in _clue_lines(seen)}
            late = []
            for l in _clue_lines(self.notes):      # clue lines in the file right now
                k = _clue_key(l)
                if k in known or (seen_keys is not None and k in seen_keys):
                    continue                       # the AI had it (kept or dropped it on purpose)
                known.add(k)
                late.append(l)
            clues = (clues + late)[-MAX_CLUES:]    # over the limit: the oldest go first
            self.notes = "\n".join("- " + l for l in rest + clues)

    # ---- last map (open-world games: the map only opens at a checkpoint, so we keep the newest one)
    @property
    def last_map(self) -> str | None:
        """Local path of the newest screenshot that looked like the map, or None (missing file = None)."""
        name = self.meta.get("last_map")
        if not name:
            return None
        p = self.dir / "shots" / Path(name).name
        return str(p) if p.exists() else None

    def set_last_map(self, path, strength: int | None = None) -> None:
        """Remember `path` (a file in this game's shots folder) as the last map; saved in game.json.
        `strength` (mapnav.STRONG / WEAK) and the time are saved next to it, so a later weak map shot
        knows whether it may replace this one (see mapnav.may_replace_last_map)."""
        if not path:
            return
        self.meta["last_map"] = Path(path).name
        if strength is None:
            self.meta.pop("last_map_strength", None)
        else:
            self.meta["last_map_strength"] = int(strength)
        self.meta["last_map_ts"] = time.time()
        self.save_meta()

    @property
    def last_map_strength(self) -> int | None:
        """mapnav strength of the saved last map, or None when there is no last map (unknown -> WEAK)."""
        if not self.last_map:
            return None
        try:
            return int(self.meta.get("last_map_strength") or mapnav.WEAK)
        except (TypeError, ValueError):
            return mapnav.WEAK

    @property
    def last_map_age(self) -> float | None:
        """Seconds since the last map was saved (from game.json, else the file's time), or None."""
        lm = self.last_map
        if not lm:
            return None
        ts = self.meta.get("last_map_ts")
        try:
            if ts:
                return max(0.0, time.time() - float(ts))
            return max(0.0, time.time() - Path(lm).stat().st_mtime)
        except (TypeError, ValueError, OSError):
            return None

    # ---- map icons: what the icons on this game's map mean, learned from a legend screenshot or guides
    def map_icons(self) -> str:
        return dossier_section(self.dossier, "Map icons")

    def set_map_icons(self, legend: str) -> bool:
        """Write the icon list into dossier.md under '## Map icons' (replace that section, or append it).
        Returns True when something was written. "unknown" / "I don't know..." and lines that are not
        '<icon>: <meaning>' are ignored, so a guess never becomes a junk icon list."""
        if not legend or LEGEND_UNKNOWN_RE.match(legend):
            return False
        lines = [l.strip().lstrip("-*• ").strip() for l in legend.splitlines()]
        lines = [l for l in lines if l and "<" not in l and LEGEND_LINE_RE.match(l)
                 and not LEGEND_UNKNOWN_RE.match(l)]
        if not lines:
            return False
        section = "## Map icons\n" + "\n".join("- " + l for l in lines) + "\n"
        text = self.dossier
        pat = re.compile(r"^##\s*Map icons\s*\n.*?(?=^##\s|\Z)", re.S | re.M | re.I)
        if pat.search(text):
            text = pat.sub(lambda _m: section + "\n", text, count=1)
        else:
            text = (text.rstrip() + "\n\n" if text.strip() else "") + section
        (self.dir / "dossier.md").write_text(text.rstrip() + "\n", encoding="utf-8")
        return True

    # ---- clues caught from on-screen text (F8): kept in the progress notes until done
    def add_clues(self, clues: list[str], source: str = "note") -> list[str]:
        """Append '- Clue (from <source>): <clue>' lines to notes.md, skipping ones already there.
        At most MAX_CLUES clue lines are kept (oldest dropped). Returns the clues that were new."""
        with self._notes_lock:
            lines = [l.rstrip() for l in self.notes.splitlines() if l.strip()]
            known = {_clue_key(l) for l in lines if _is_clue(l.lstrip("-*• ").strip())}
            added = []
            for c in clues:
                c = " ".join(str(c or "").split()).strip(" -")
                if not c or "<" in c or _clue_key(c) in known:
                    continue
                known.add(_clue_key(c))
                lines.append(f"- Clue (from {source}): {c}")
                added.append(c)
            if not added:
                return []
            n_clues = sum(1 for l in lines if _is_clue(l.lstrip("-*• ").strip()))
            kept = []
            for l in lines:           # over the limit: drop the oldest clue lines (they come first)
                if n_clues > MAX_CLUES and _is_clue(l.lstrip("-*• ").strip()):
                    n_clues -= 1
                    continue
                kept.append(l)
            self.notes = "\n".join(kept)
            return added


MAX_CLUES = 10


def _is_clue(line: str) -> bool:
    """A notes line written by the clue catcher ('Clue (from note): ...'), bullet already stripped."""
    return bool(re.match(r"clue\s*(\(|:)", line, re.I))


def _clue_key(text: str) -> str:
    """Dedupe key for a clue: the clue text only, lowercased, without punctuation."""
    text = re.sub(r"^[-*• ]*clue\s*(\([^)]*\))?\s*:\s*", "", text.strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _clue_lines(text: str | None) -> list[str]:
    """The clue lines of a notes text, bullets stripped, in file order."""
    out = []
    for l in (text or "").splitlines():
        l = l.strip().lstrip("-*• ").strip()
        if l and _is_clue(l):
            out.append(l)
    return out


LEGEND_LINE_RE = re.compile(r"^[^:\n]{2,}:\s*\S")      # "<icon>: <meaning>"
LEGEND_UNKNOWN_RE = re.compile(r"^\s*(unknown|i don'?t know|not sure|no legend)", re.I)


def dossier_section(text: str, title: str) -> str:
    """The body of a '## <title>' section of the dossier markdown, or '' when there is none."""
    m = re.search(rf"^##\s*{re.escape(title)}\s*\n(.*?)(?=^##\s|\Z)", text or "", re.S | re.M | re.I)
    return m.group(1).strip() if m else ""


def list_games() -> list[str]:
    root = BASE / "games"
    names = []
    if root.exists():
        for d in root.iterdir():
            m = d / "game.json"
            if m.exists():
                try:
                    names.append(read_json(m)["name"])
                except Exception:
                    pass
    return sorted(names)


# ------------------------------------------------------------------ game detection
NOT_GAMES = {"explorer.exe", "msedge.exe", "chrome.exe", "firefox.exe", "discord.exe", "steam.exe",
             "steamwebhelper.exe", "claude.exe", "code.exe", "windowsterminal.exe", "powershell.exe",
             "cmd.exe", "python.exe", "pythonw.exe", "taskmgr.exe", "obs64.exe", "spotify.exe",
             "battle.net.exe", "epicgameslauncher.exe", "upc.exe", "applicationframehost.exe",
             "searchhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
             "lockapp.exe", "textinputhost.exe", "nvidia overlay.exe", "nvcontainer.exe",
             "gamebar.exe", "notepad.exe", "vlc.exe", "ollama app.exe",
             # other browsers / apps that sit maximized next to the game (Brave on the 2nd screen
             # once pulled the overlay off the game)
             "brave.exe", "librewolf.exe", "opera.exe", "vivaldi.exe", "msedgewebview2.exe",
             "systemsettings.exe", "nvngx.dll", "mpc-hc64.exe", "potplayermini64.exe",
             "wmplayer.exe", "telegram.exe", "whatsapp.exe", "slack.exe", "teams.exe", "ms-teams.exe"}


def foreground_window():
    import psutil
    import win32gui
    import win32process
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        proc = psutil.Process(pid)
        exe = proc.name()
    except Exception:
        return None
    try:
        path = proc.exe()
    except Exception:
        path = ""
    return {"hwnd": hwnd, "exe": exe, "path": path, "title": win32gui.GetWindowText(hwnd)}


def monitor_rect(hwnd):
    """(left, top, right, bottom) of the monitor a window is on, in physical pixels."""
    import win32api
    try:
        return tuple(win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd))["Monitor"])
    except Exception:
        return None


def window_is_fullscreenish(hwnd) -> bool:
    import win32api
    import win32gui
    try:
        if ctypes.windll.user32.IsZoomed(hwnd):     # an ordinary maximized app window, not a fullscreen game
            return False
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        mon = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd))["Monitor"]
        return (r - l) >= (mon[2] - mon[0]) * 0.9 and (b - t) >= (mon[3] - mon[1]) * 0.9
    except Exception:
        return False


def _acf_fields(path: Path) -> dict:
    """Minimal parser for Steam's appmanifest_*.acf ("key" "value" lines)."""
    out = {}
    try:
        for m in re.finditer(r'^\s*"(\w+)"\s+"([^"]*)"', path.read_text(encoding="utf-8", errors="replace"), re.M):
            out.setdefault(m.group(1).lower(), m.group(2))
    except OSError:
        pass
    return out


def steam_game_for_exe(exe_path: str) -> dict | None:
    """If the exe is inside ...\\steamapps\\common\\<dir>\\, return {"appid", "name"} from Steam's manifest."""
    p = Path(exe_path)
    for parent in p.parents:
        if parent.name.lower() == "common" and parent.parent.name.lower() == "steamapps":
            installdir = p.relative_to(parent).parts[0].lower()
            for acf in parent.parent.glob("appmanifest_*.acf"):
                f = _acf_fields(acf)
                if f.get("installdir", "").lower() == installdir:
                    return {"appid": f.get("appid"), "name": f.get("name")}
            return None
    return None


def steam_libraries() -> list[Path]:
    libs = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
        libs.append(steam / "steamapps")
        vdf = (steam / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        libs += [Path(m.replace("\\\\", "\\")) / "steamapps" for m in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    except OSError:
        pass
    return list(dict.fromkeys(l for l in libs if l.exists()))


def steam_name_for_appid(appid: str) -> str | None:
    for lib in steam_libraries():
        f = _acf_fields(lib / f"appmanifest_{appid}.acf")
        if f.get("name"):
            return f["name"]
    try:   # not installed: ask the public Steam store API
        import httpx
        r = httpx.get("https://store.steampowered.com/api/appdetails",
                      params={"appids": appid, "filters": "basic"}, timeout=8)
        # The response is keyed by an internal id that doesn't always equal the appid.
        d = next(iter(r.json().values()), {})
        if d.get("success"):
            return d["data"]["name"]
    except Exception:
        pass
    return None


def clean_title(title: str) -> str:
    # "Silent Hill: Townfall  (DX12) - v1.02" -> "Silent Hill: Townfall"
    t = re.split(r"\s+[-|–]\s+|\s{2,}|\s\(|\sv\d", title)[0]
    return t.strip() or title.strip()


def game_name_for(exe_path: str, title: str) -> tuple[str, str]:
    """Best guess of a game's name: Steam manifest if it's a Steam game, else the window title."""
    s = steam_game_for_exe(exe_path) if exe_path else None
    if s and s.get("name"):
        return s["name"], "steam"
    folder = install_folder_name(exe_path) if exe_path else None
    if folder:
        return folder, "folder"
    return clean_title(title) or Path(exe_path).stem, "title"


LIBRARY_DIRS = {"games", "epic games", "gog games", "xboxgames", "program files", "program files (x86)",
                "ubisoft game launcher", "ea games", "battle.net", "riot games", "amazon games", "itch"}


def install_folder_name(exe_path: str) -> str | None:
    """...\\Games\\Some Game\\...\\SomeGame-Win64-Shipping.exe -> "Some Game"."""
    parts = Path(exe_path).parts
    for i in range(len(parts) - 2, 0, -1):
        if parts[i].lower() in LIBRARY_DIRS and i + 1 < len(parts) - 1:
            name = parts[i + 1]
            if name.lower() not in ("common", "games"):
                return name
    return None


def list_windows() -> list[dict]:
    """Visible top-level windows that could be games, with a suggested name for each."""
    import psutil
    import win32gui
    import win32process
    seen, out = set(), []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, 4):   # 4 = GW_OWNER
            return
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in seen:
            return
        try:
            proc = psutil.Process(pid)
            exe, path = proc.name(), proc.exe()
        except Exception:
            return
        if exe.lower() in NOT_GAMES or exe.lower() in ("textinputhost.exe", "systemsettings.exe"):
            return
        seen.add(pid)
        name, how = game_name_for(path, title)
        out.append({"exe": exe, "title": title, "name": name, "how": how,
                    "known": APP.games_map.get(exe.lower())})
    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda w: (w["how"] != "steam", w["known"] is None, w["name"].lower()))
    return out


# ------------------------------------------------------------------ app state
class App:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()
        # exe -> game name. Saved in games\ (your data, not part of git); the games.json next to guide.py is
        # only the starting list (shipped empty), read until the first game is picked.
        self.games_map_path = BASE / "games" / "games.json"
        start = self.games_map_path if self.games_map_path.exists() else BASE / "games.json"
        self.games_map = read_json(start) if start.exists() else {}
        last = (BASE / "last_game.txt")
        self.game = GameStore(last.read_text(encoding="utf-8").strip()) if last.exists() else None
        self.level = CFG["default_level"]
        self.speak = CFG["speak_answers"]
        self.busy = False
        self.pending_shots: list[str] = []   # F11 screenshots waiting for the next question (max 4)
        self.candidate = None          # unknown fullscreen exe the user might want to name
        self.last_game_hwnd = None
        self.recording = False
        self.whisper = None
        self.whisper_used = 0.0
        self.whisper_lock = threading.Lock()
        self.chunks = []
        self.stream = None
        self.cancel = False
        self.auto_detect = bool(CFG["auto_detect"])
        self.checked_exes: set[str] = set()   # unknown exes already looked at by the watcher
        self.tray = None
        self.overlay = None              # in-game overlay (overlay.Overlay), created in main()
        self.cur = None                  # the latest answer: {"steps", "marks", "pos", "shots"}
        self.map_shots: set[str] = set()  # screenshot paths that looked like the game's map (fresh ones)
        self.strong_shots: set[str] = set()  # the ones that are surely the real map screen (not a legend list)
        self.old_map_shots: set[str] = set()  # last maps attached to a question as OLD (the player had moved on)
        self.brain_warning: str | None = None   # why the claude brain is unavailable (shown as a red banner)
        self.map_checks: dict[str, threading.Thread] = {}   # path -> running map check (OCR thread)
        self.map_checked: set[str] = set()   # paths already checked (map or not), no second OCR
        self.dossier_building: set[str] = set()
        self.speaker = screenread.Speaker(log, CFG.get("kokoro_models_dir") or None)
        self.reading = False
        self.read_cancel = False
        self.read_mode = CFG["read_mode"]
        self.voice = CFG["voice"]
        self.sense = None                # nearby-item sound + pulse (sense.Sense); see start_sense()
        self.sense_status = ""           # last helper status text we toasted (toast only when it changes)
        self.sense_busy = False          # helper install/uninstall running
        self.sense_elsewhere = None      # last "main quest is in another area" info from sense.py (None = not)
        if sense is not None:
            try:
                self.sense = self._new_sense()
                self.sense.on_draw = self._sense_draw
                self.sense.on_status = self._sense_status
                self.sense.on_path = self._sense_path        # quest path (sense.py calls it, 10x/s at most)
                if hasattr(self.sense, "on_target"):
                    # target appeared/gone/moved: sense.py calls this (about 1x/s at most) so the window's
                    # " · target 42 m ↑2" text follows the target instead of freezing on an old push
                    self.sense.on_target = self._sense_target
                if hasattr(self.sense, "on_elsewhere"):
                    # the tracked main quest is on another map: one toast when it first shows + state push
                    self.sense.on_elsewhere = self._sense_elsewhere
                if hasattr(self.sense, "set_path_enabled"):
                    # Only a real JSON false turns it off; anything else (missing, junk) = on (the default).
                    self.sense.set_path_enabled(CFG.get("sense_path", True) is not False)
                self.sense.set_enabled(bool(CFG.get("sense", True)))
                # raw value on purpose: set_range() clamps bad input (e.g. "abc" or null -> 10 m)
                # instead of a ValueError here turning Sense off for good
                self.sense.set_range(CFG.get("sense_range", 10))
                self.sense.set_sound(str(CFG.get("sense_sound", "chime")))
                # Sound switch (saved). Only a real JSON true turns it on; anything else = off.
                if hasattr(self.sense, "set_sound_on"):
                    self.sense.set_sound_on(CFG.get("sense_sound_on", False) is True)
                # A mute is only a stopgap: start every run unmuted (the Sound switch is the real control).
                if hasattr(self.sense, "mute"):
                    self.sense.mute(0)
            except Exception as e:
                log(f"sense unavailable: {e}")
                self.sense = None

    @staticmethod
    def _new_sense():
        try:
            return sense.Sense(log)      # preferred: it logs through guide.log
        except TypeError:
            return sense.Sense()

    # ---- Sense: the game helper writes sense.json, sense.py reads it, the overlay pulses the spots
    def _sense_draw(self, points):
        """From sense.py's reader thread, at most 10x/s: the items in range (or None/[] = nothing)."""
        if self.overlay:
            self.overlay.set_sense(points or None)

    def _sense_path(self, data):
        """From sense.py's reader thread, at most 10x/s: the quest path to draw (questpath.compute() output)
        or None = nothing (no target, stale helper, reader stopped, switch off)."""
        ov = self.overlay
        if ov and hasattr(ov, "set_path"):
            try:
                ov.set_path(data or None)
            except Exception as e:
                log(f"quest path draw failed: {e}")

    def _sense_target(self, *_):
        """From sense.py's reader thread when the quest target changes: push the state so the window updates."""
        try:
            self.push_state()
        except Exception as e:
            log(f"sense target push failed: {e}")

    def _sense_elsewhere(self, info=None, *_):
        """From sense.py when "main quest is in another area" appears, changes or clears (info = {"label","map"}
        or None). One toast when it first appears (not again until it clears and comes back), only while the
        Quest path switch is on; always a state push so the Sense panel text follows."""
        info = info if isinstance(info, dict) else None
        was = self.sense_elsewhere
        self.sense_elsewhere = info
        if info and not was and CFG.get("sense_path", True) is not False:
            try:
                self.emit({"type": "toast", "text": "Your main quest is in another area"})
            except Exception as e:
                log(f"sense elsewhere toast failed: {e}")
        try:
            self.push_state()
        except Exception as e:
            log(f"sense elsewhere push failed: {e}")

    def _clear_path(self):
        """Take the quest path off the screen now (game change, path switch off)."""
        ov = self.overlay
        if ov and hasattr(ov, "set_path"):
            try:
                ov.set_path(None)
            except Exception as e:
                log(f"quest path clear failed: {e}")

    def _sense_status(self, text):
        """From sense.py when the helper state changes ("helper not running" ...): toast once + state push."""
        text = (text or "").strip()
        if text and text != self.sense_status:
            self.emit({"type": "toast", "text": text})
        self.sense_status = text
        self.push_state()

    def sense_file(self) -> str | None:
        """Where the current game's helper writes sense.json (game.json "sense_file"), or None = no helper."""
        sf = self.game.meta.get("sense_file") if self.game else None
        return str(sf) if sf else None

    def sense_state(self) -> dict:
        if self.sense:
            try:
                st = dict(self.sense.state())
                # an older sense.py without the quest path: fill the keys so the window can rely on them
                st.setdefault("path_enabled", CFG.get("sense_path", True) is not False)
                st.setdefault("elsewhere", None)
                st.setdefault("target", None)
                st["path_available"] = hasattr(self.sense, "set_path_enabled")
                st["engine"] = self.sense_engine()
                st["route_enabled"] = CFG.get("sense_route", True) is not False
                st["route_available"] = self.sense_mod_dir() is not None
                return st
            except Exception as e:
                log(f"sense state: {e}")
        return {"enabled": False, "range": CFG.get("sense_range", 10), "sound": CFG.get("sense_sound", "chime"),
                "sound_on": CFG.get("sense_sound_on", False) is True, "muted_until": 0, "helper_ok": False, "helper_age_s": None, "nearest": None, "count": 0,
                "path_enabled": CFG.get("sense_path", True) is not False, "target": None, "elsewhere": None,
                "path_available": False,
                "engine": self.sense_engine(),
                "route_enabled": CFG.get("sense_route", True) is not False, "route_available": False}

    def sense_mod_dir(self) -> Path | None:
        """The Sense mod folder of an Unreal (UE4SS) game: the game's sense_file is <mod>/out/sense.json, so
        this is the parent of "out". None for RE Engine games, no helper, an odd sense_file or a missing folder.
        Never raises."""
        try:
            if self.sense_engine() != "ue":
                return None
            sf = self.sense_file()
            if not sf:
                return None
            out = Path(sf).parent
            if out.name.lower() != "out":
                return None
            mod = out.parent
            return mod if mod.is_dir() else None
        except Exception:
            return None

    def apply_route_switch(self, on: bool | None = None) -> str | None:
        """"Quest path follows the ground" switch -> the helper's kill file. on=True deletes
        <Sense mod>/route_off.txt, on=False creates it (the Lua helper checks it every 2 s, no game restart).
        on=None = use the saved setting. Nothing to do (None) when the game has no Unreal Sense mod folder.
        Returns a plain error text when the file could not be written/removed, else None. Never raises."""
        if on is None:
            on = CFG.get("sense_route", True) is not False
        mod = self.sense_mod_dir()
        if mod is None:
            return None
        f = mod / "route_off.txt"
        try:
            if on:
                if f.exists():
                    f.unlink()
                    log(f"quest path ground route on (removed {f})")
            elif not f.exists():
                f.write_text("Game Guide: quest path ground route switched off. Delete this file to turn it on.\n",
                             encoding="utf-8")
                log(f"quest path ground route off (wrote {f})")
        except OSError as e:
            log(f"quest path ground route switch failed: {e}")
            return f"Could not change the ground route switch: {str(e)[:120]}"
        return None

    def sense_engine(self) -> str | None:
        """Which helper the current game uses: "ue" (UE4SS, has the F3 discovery dump), "re" (REFramework, no
        F3) or None (no helper for this game). From the installer's entry (sense_game); never raises."""
        try:
            gi = sense_game(self.game.name) if self.game else None
        except Exception:
            return None
        if not gi:
            return None
        return "re" if gi.get("engine") == "reframework" else "ue"

    def start_sense(self):
        """(Re)start the Sense reader for the current game; stop it when the game has no helper.
        Called on every game change and once in main() (the first game is set before the overlay exists)."""
        if not self.sense:
            return
        sf = self.sense_file()
        try:
            self.sense.stop()
            if self.overlay:
                self.overlay.set_sense(None)
            self._clear_path()           # the old game's quest path must not stay on screen
            if sf and Path(sf).parent.exists():
                self.sense.start(sf)
                log(f"Sense reading {sf}")
                self.apply_route_switch()    # the saved "follows the ground" switch -> this game's route_off.txt
            elif sf:
                log(f"Sense helper folder missing, not reading: {sf}")
        except Exception as e:
            log(f"sense start failed: {e}")

    # ---- events to the UI (Server-Sent Events)
    def emit(self, ev: dict):
        if self.loop is None:
            return
        def _put():
            for q in list(self.clients):
                q.put_nowait(ev)
        self.loop.call_soon_threadsafe(_put)

    def state(self) -> dict:
        return {"type": "state", "boot": BOOT_ID, "game": self.game.name if self.game else None,
                "games": list_games(), "level": self.level, "speak": self.speak,
                "busy": self.busy, "recording": self.recording,
                "shots": ["/shots/" + slugify(self.game.name) + "/" + Path(sp).name for sp in self.pending_shots]
                         if self.game else [],
                "candidate": self.candidate, "notes": self.game.notes if self.game else "",
                "auto_detect": self.auto_detect, "startup": startup_enabled(),
                "voice": self.voice, "voices": screenread.VOICES, "read_mode": self.read_mode,
                "read_key": CFG["hotkey_read"], "next_key": CFG["hotkey_next"],
                "overlay": bool(CFG.get("overlay", True)), "overlay_key": CFG["hotkey_overlay"],
                "where_key": CFG["hotkey_where"], "has_dossier": bool(self.game and self.game.dossier),
                "dossier_building": bool(self.game and slugify(self.game.name) in self.dossier_building),
                "brain_warning": self.brain_warning,
                "brain_setup_done": bool(CFG.get("brain_setup_done")),
                "sense": self.sense_state(), "sense_helper_installed": bool(self.sense_file()),
                "sense_busy": self.sense_busy, "sense_available": self.sense is not None,
                "brains": CFG["brain_order"], "hotkeys": [CFG["hotkey_talk"], CFG["hotkey_screenshot"]]}

    def push_state(self):
        self.emit(self.state())

    def note_brain_error(self, bname: str, failed: str | None) -> bool:
        """A brain gave no answer. When it is the claude brain and it is really gone ("not found", "Not logged
        in", or any error when claude is the first brain), remember why (`brain_warning`, shown as a red banner
        in the window) and say it in a toast, so a silent fall-back to the weak local brain can't happen again.
        Returns True when the warning was set."""
        if bname != "claude":
            return False
        text = (failed or "no answer").strip()
        low = text.lower()
        first = bool(CFG["brain_order"]) and CFG["brain_order"][0] == "claude"
        if not ("not found" in low or "not logged in" in low or first):
            return False
        # The CLI's own text often ends with a full stop ("...not found.") - drop it, the toast adds its own.
        reason = re.sub(r"^claude:\s*", "", text, flags=re.I)[:120].rstrip(" .")
        self.brain_warning = reason
        self.emit({"type": "toast", "text": f"Claude isn't available: {reason}. Answers now come from the next brain "
                                            "in your list (press Brain to set one up)."})
        log(f"brain warning set: {reason}")
        return True

    def set_game(self, name: str, exe: str | None = None):
        name = name.strip()
        if not name or (self.game and self.game.name == name and not exe):
            return
        self.game = GameStore(name)
        self.game.save_meta()
        (BASE / "last_game.txt").write_text(name, encoding="utf-8")
        if exe:
            self.games_map[exe.lower()] = name
            self.games_map_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(self.games_map_path, json.dumps(self.games_map, indent=2))
        if self.candidate and (not exe or self.candidate["exe"].lower() == exe.lower()):
            self.candidate = None
        self.pending_shots = []
        log(f"Game set: {name} ({exe or 'manual'})")
        self.start_sense()               # the new game's helper (or none): stop the old reader first
        self.push_state()
        self.emit({"type": "history", "items": self.game.history()[-40:]})
        self.ensure_dossier()

    def ensure_dossier(self, force: bool = False):
        """Research the game once in the background (chapters, bosses, points of no return, missables, bugs...).
        Every later answer gets this overview, so 'where am I' and progress tracking stay accurate."""
        g = self.game
        if not g or not CFG.get("dossier", True) or "claude" not in CFG["brain_order"]:
            return
        slug = slugify(g.name)
        if slug in self.dossier_building or (g.dossier and not force):
            return
        self.dossier_building.add(slug)
        self.push_state()

        def work():
            try:
                self.emit({"type": "toast", "text": f"Researching {g.name} in the background (one-time, about a minute)..."})
                text = brains.build_dossier(g.name, CFG["claude_model"])
                # Learned map icons must survive a rebuild: read them AFTER the (long) build, so icons
                # saved from a legend screenshot during the build are not lost.
                icons = g.map_icons()
                if icons and not dossier_section(text, "Map icons"):
                    text = text.rstrip() + "\n\n## Map icons\n" + icons + "\n"
                (g.dir / "dossier.md").write_text(text, encoding="utf-8")
                log(f"Dossier built for {g.name} ({len(text)} chars)")
                self.emit({"type": "toast", "text": f"Game dossier ready for {g.name}"})
            except Exception as e:
                log(f"dossier failed for {g.name}: {e}")
            finally:
                self.dossier_building.discard(slug)
                self.push_state()
        threading.Thread(target=work, daemon=True).start()

    # ---- watcher: which game is in front?
    def watch_foreground(self):
        import pythoncom
        pythoncom.CoInitialize()
        while True:
            try:
                tag_new_windows()
            except Exception as e:
                log(f"tagging: {e}")
            try:
                fg = foreground_window()
                if self.overlay:
                    front = bool(fg) and (fg["exe"].lower() in self.games_map or fg["hwnd"] == self.last_game_hwnd)
                    if front:
                        mr = monitor_rect(fg["hwnd"])
                        self.overlay.set_monitor(mr)
                        if mr and self.sense and hasattr(self.sense, "set_screen"):
                            self.sense.set_screen(mr[2] - mr[0], mr[3] - mr[1])   # fallback when the helper sends no viewport size
                    self.overlay.set_game_front(front)
                if fg and fg["exe"].lower() not in NOT_GAMES:
                    exe = fg["exe"].lower()
                    if exe in self.games_map:
                        self.last_game_hwnd = fg["hwnd"]
                        if self.auto_detect and (not self.game or self.game.name != self.games_map[exe]):
                            self.set_game(self.games_map[exe])
                    elif exe not in self.checked_exes:
                        self.checked_exes.add(exe)
                        steam = steam_game_for_exe(fg["path"]) if fg["path"] else None
                        if steam and steam.get("name"):
                            # A Steam game: we know its exact name, no need to ask.
                            self.last_game_hwnd = fg["hwnd"]
                            if self.auto_detect:
                                log(f"Steam game detected: {steam['name']} (appid {steam.get('appid')})")
                                self.set_game(steam["name"], fg["exe"])
                        elif window_is_fullscreenish(fg["hwnd"]):
                            self.last_game_hwnd = fg["hwnd"]
                            if self.auto_detect:
                                self.candidate = {"exe": fg["exe"],
                                                  "title": game_name_for(fg["path"], fg["title"])[0]}
                                self.push_state()
                        else:
                            self.checked_exes.discard(exe)   # windowed now; check again later
            except Exception as e:
                log(f"watcher: {e}")
            if self.whisper and time.time() - self.whisper_used > CFG["whisper_unload_after_s"] \
                    and not self.recording:
                with self.whisper_lock:
                    self.whisper = None
                log("Whisper unloaded (idle) - VRAM freed")
            time.sleep(2)

    # ---- screenshot
    def capture_screen(self, max_width: int, prefix: str = "") -> str | None:
        """Grab the monitor the game is on. Returns the saved path, or None (with a toast) on failure."""
        if not self.game:
            self.emit({"type": "toast", "text": "Pick a game first"})
            beep(300, 250)
            return None
        try:
            import mss
            import win32api
            from PIL import Image
            hwnd = self.last_game_hwnd
            fg = foreground_window()
            if fg and fg["exe"].lower() not in NOT_GAMES:
                hwnd = fg["hwnd"]
            if self.overlay:
                self.overlay.suspend()
            try:
                with mss.mss() as sct:
                    mon = sct.monitors[1]
                    if hwnd:
                        m = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd))["Monitor"]
                        for cand in sct.monitors[1:]:
                            if cand["left"] == m[0] and cand["top"] == m[1]:
                                mon = cand
                    raw = sct.grab(mon)
            finally:
                if self.overlay:
                    self.overlay.resume()
            img = Image.frombytes("RGB", raw.size, raw.rgb)
            if np.asarray(img.resize((64, 36))).mean() < 3:
                self.emit({"type": "toast", "text": "Screenshot came out black - set the game to "
                                                    "borderless/windowed fullscreen"})
                beep(300, 250)
                return None
            if img.width > max_width:
                img = img.resize((max_width, int(img.height * max_width / img.width)))
            # milliseconds in the name: two quick F11 presses must not overwrite each other
            stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"[:-3]
            path = self.game.dir / "shots" / f"{prefix}{stamp}.jpg"
            img.save(path, quality=90)
            log(f"Screenshot {path.name}")
            return str(path)
        except Exception as e:
            log(f"screenshot failed: {e}")
            beep(300, 250)
            return None

    def note_if_map(self, path: str | None) -> bool:
        """After a capture: if the screenshot is the game's map, remember it as the last map.
        OCR runs in a subprocess (~0.3 s), so call this from a thread, not the event loop."""
        if not path or not self.game:
            return False
        if path in self.map_shots:
            return True
        if path in self.map_checked:
            return False
        game = self.game
        strength = mapnav.map_strength(path)
        self.map_checked.add(path)
        if strength <= mapnav.NONE:
            return False
        self.map_shots.add(path)
        if strength >= mapnav.STRONG:
            self.strong_shots.add(path)
        # A strong map signal ("map completion" / "world map") always replaces the saved last map. A weak one
        # (a legend or collectibles list often still says "map" or "legend"; or a real map whose title the OCR
        # missed) replaces it only when the saved map was weak too, or is older than ~10 minutes: the legend
        # screen the guide asks for must not push out the real map taken a moment earlier, but a weak-only
        # map game, or one OCR miss, must not leave an hours-old map in place forever.
        if mapnav.may_replace_last_map(strength, game.last_map_strength, game.last_map_age):
            game.set_last_map(path, strength)
            log(f"Map screenshot: {Path(path).name} saved as the last map ({'strong' if strength >= mapnav.STRONG else 'weak words'})")
        else:
            log(f"Map-like screenshot: {Path(path).name} (weak words only, fresh last map kept)")
        return True

    def check_map_async(self, path: str | None) -> threading.Thread | None:
        """Run note_if_map on its own daemon thread (OCR takes ~0.7 s, and can hang up to 30 s), so the
        hotkey worker is free for F10 talk right after F11. Returns the thread (None when nothing to do)."""
        if not path or not self.game or path in self.map_shots or path in self.map_checked:
            return None
        t = self.map_checks.get(path)
        if t and t.is_alive():
            return t

        def work():
            try:
                self.note_if_map(path)
            except Exception:
                log(f"map check: {traceback.format_exc()}")
            finally:
                self.map_checks.pop(path, None)
        t = threading.Thread(target=work, daemon=True)
        self.map_checks[path] = t
        t.start()
        return t

    def wait_map_checks(self, paths, timeout: float = 3.0) -> None:
        """Start a map check for every path that never had one, then wait at most `timeout` seconds for
        them all. After this, `path in self.map_shots` is the best answer we have (a check still running
        just counts as 'not a map' for now)."""
        threads = [self.check_map_async(p) for p in paths]
        deadline = time.time() + timeout
        for t in threads:
            if t is None:
                continue
            left = deadline - time.time()
            if left <= 0:
                break
            t.join(left)

    def take_screenshot(self):
        path = self.capture_screen(1600)
        if path:
            self.pending_shots.append(path)
            if len(self.pending_shots) > 4:
                self.pending_shots.pop(0)
            beep(1400, 60)
            if len(self.pending_shots) > 1:
                self.emit({"type": "toast", "text": f"{len(self.pending_shots)} screenshots attached to your next question"})
            self.push_state()
            self.check_map_async(path)      # off the hotkey worker: F10 must not wait for the OCR

    # ---- "where am I?" (F4): screenshot + a fixed 3-line answer; with the map open: map directions
    def where_am_i(self, capture: bool = True):
        if self.busy:
            self.emit({"type": "toast", "text": "Still answering the last question..."})
            return
        is_map = False
        if capture:
            path = self.capture_screen(1600, prefix="where-")
            if path:
                self.pending_shots.append(path)
                beep(1400, 60)
                self.wait_map_checks([path])            # at most ~3 s; a slow OCR -> plain "where" mode
                is_map = path in self.map_shots
        elif self.pending_shots:      # from the window: F11 on the map, then "Where am I?"
            self.wait_map_checks([self.pending_shots[-1]])
            is_map = self.pending_shots[-1] in self.map_shots
        if is_map:
            asyncio.run_coroutine_threadsafe(
                self.ask("Where do I go next?", source="where", level="hint", mode="map"), self.loop)
            return
        asyncio.run_coroutine_threadsafe(
            self.ask("Where am I, what is my objective right now, and what do I do next?",
                     source="where", level="hint", mode="where"), self.loop)

    # ---- clue catcher: tasks, places, names and codes in the text F8 just read
    def catch_clues(self, text: str, game: GameStore, source: str = "note"):
        """Runs in a daemon thread after F8: a cheap model pulls clues out of the text and they go
        into the progress notes, so 'Where do I go?' later knows about them."""
        try:
            clues = brains.extract_clues(text, game.name, CFG["read_model"])
        except Exception as e:
            log(f"clue catcher failed: {e}")
            return
        if not clues:
            return
        try:
            added = game.add_clues(list(clues), source)
        except Exception as e:
            log(f"clue save failed: {e}")
            return
        if added:
            log(f"Clues saved ({len(added)}): {added[0]}")
            self.emit({"type": "toast", "text": f"Clue saved: {added[0]}"})
            self.push_state()
        else:
            log("Clues found but all were already in the notes")

    # ---- read the text on screen out loud (F8)
    def read_screen(self):
        if self.reading or self.speaker.speaking:     # second press = stop
            self.read_cancel = True
            self.speaker.stop()
            self.emit({"type": "status", "text": ""})
            return
        self.reading, self.read_cancel = True, False
        try:
            beep(1200, 60)
            path = self.capture_screen(2560, prefix="read-")
            if not path:
                return
            self.emit({"type": "status", "text": "Reading the text on screen..."})
            text, how = None, ""
            mode = self.read_mode
            if mode in ("auto", "fast"):
                try:
                    t0 = time.time()
                    text = screenread.ocr_windows(path)
                    how = "fast"
                    log(f"OCR {time.time() - t0:.2f}s, {len(text)} chars")
                except Exception as e:
                    log(f"OCR failed: {e}")
                if mode == "auto" and not screenread.looks_readable(text):
                    text = None
            if text is None and mode in ("auto", "smart") and not self.read_cancel:
                exe = brains.find_claude_exe()
                if exe:
                    self.emit({"type": "status", "text": "Text is hard to read - asking Claude to read it..."})
                    try:
                        t0 = time.time()
                        text = screenread.text_via_claude(exe, path, self.game.name, CFG["read_model"])
                        how = "smart"
                        log(f"Claude read {time.time() - t0:.1f}s, {len(text or '')} chars")
                    except Exception as e:
                        log(f"Claude read failed: {e}")
            if self.read_cancel:
                return
            if not text:
                self.emit({"type": "status", "text": ""})
                self.emit({"type": "toast", "text": "No readable text found on screen"})
                beep(300, 200)
                return
            msg = {"role": "read", "content": text, "ts": time.time(), "via": how,
                   "shot": "/shots/" + slugify(self.game.name) + "/" + Path(path).name}
            self.game.append(msg)
            self.emit({"type": "read", **msg})
            # Clue catcher runs beside the read-aloud, never delays it.
            threading.Thread(target=self.catch_clues, args=(text, self.game), daemon=True).start()
            self.emit({"type": "status", "text": "Reading aloud... (F8 again to stop)"})
            self.speaker.say(text, self.voice, CFG["voice_rate"])
            self.emit({"type": "status", "text": ""})
        finally:
            self.reading = False

    # ---- microphone + whisper
    def _load_whisper(self):
        with self.whisper_lock:
            if self.whisper:
                return self.whisper
            _ensure_av_importable()
            from faster_whisper import WhisperModel
            t0 = time.time()
            try:
                self.whisper = WhisperModel(CFG["whisper_model"], device=CFG["whisper_device"],
                                            compute_type=CFG["whisper_compute"])
            except Exception as e:
                log(f"Whisper GPU load failed ({e}); using CPU 'small'")
                self.whisper = WhisperModel("small", device="cpu", compute_type="int8")
            log(f"Whisper loaded in {time.time() - t0:.1f}s")
            self.whisper_used = time.time()
            return self.whisper

    def mic_start(self):
        import sounddevice as sd
        if self.recording:
            return
        self.chunks = []
        try:
            self.stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                                         callback=lambda d, *_: self.chunks.append(d.copy()))
            self.stream.start()
        except Exception as e:
            log(f"mic error: {e}")
            self.emit({"type": "toast", "text": f"Mic error: {e}"})
            beep(300, 300)
            return
        self.recording = True
        beep(900, 90)
        self.push_state()
        if not self.whisper:   # load while the player is still talking
            threading.Thread(target=self._load_whisper, daemon=True).start()

    def mic_stop(self, send: bool = True):
        if not self.recording:
            return
        self.recording = False
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        self.stream = None
        beep(600, 90)
        chunks, self.chunks = self.chunks, []
        self.push_state()
        threading.Thread(target=self._transcribe, args=(chunks, send), daemon=True).start()

    def _transcribe(self, chunks, send):
        if not chunks:
            return
        audio = np.concatenate(chunks).flatten()
        if len(audio) < 16000 * 0.4:
            return
        self.emit({"type": "status", "text": "Transcribing..."})
        try:
            model = self._load_whisper()
            vocab = load_vocab(self.game)
            prompt = f"A gamer describing where they are stuck in {self.game.name if self.game else 'a video game'}."
            if vocab:
                prompt += f" Names: {vocab}."
            t0 = time.time()
            with self.whisper_lock:
                segs, _ = model.transcribe(audio, language="en", initial_prompt=prompt,
                                           vad_filter=True, beam_size=5)
                text = " ".join(s.text.strip() for s in segs).strip()
            self.whisper_used = time.time()
            log(f"STT {len(audio) / 16000:.1f}s audio -> {time.time() - t0:.2f}s: {text}")
        except Exception as e:
            log(f"transcribe failed: {traceback.format_exc()}")
            self.emit({"type": "toast", "text": f"Transcription failed: {e}"})
            return
        if not text:
            self.emit({"type": "status", "text": ""})
            self.emit({"type": "toast", "text": "Didn't catch that"})
            return
        if send:
            asyncio.run_coroutine_threadsafe(self.ask(text, source="voice"), self.loop)
        else:
            self.emit({"type": "transcript", "text": text})

    # ---- asking
    async def ask(self, question: str, source: str = "text", level: str | None = None,
                  careful: bool = False, mode: str | None = None):
        if self.busy:
            self.emit({"type": "toast", "text": "Still answering the last question..."})
            return
        if not self.game:
            self.emit({"type": "toast", "text": "Pick or type the game you're playing first (top of the window)."})
            beep(300, 250)
            return
        self.busy = True
        self.cancel = False
        level = level or self.level
        shots, self.pending_shots = list(self.pending_shots), []
        g = self.game
        ask_text = question
        if mode is None and re.search(r"\bboss\b|how (do|can) i beat|how to beat|keeps? killing me", question, re.I):
            mode = "boss"            # boss questions get the compact boss card format
        elif mode is None and WHATSLEFT_RE.search(question):
            mode = "whatsleft"       # "what's left here?": list of things in this area, undone ones circled
        elif mode is None and MAP_Q_RE.search(question):
            mode = "map"             # "where do I go?": directions on the map (point + route)
        prev = next((m for m in reversed(g.history()) if m["role"] == "user"), None)
        shots_from_prev = False          # the shots were re-attached from the previous question
        if (prev and not careful and time.time() - prev.get("ts", 0) < 180
                and difflib.SequenceMatcher(None, question.lower(), prev["content"].lower()).ratio() > 0.85):
            # Same question again right after an answer: treat it as "didn't work" and re-check carefully.
            careful = True
            if not shots:
                for u in (prev.get("shots") or ([prev["shot"]] if prev.get("shot") else [])):
                    p = g.dir / "shots" / Path(u).name
                    if p.exists():
                        shots.append(str(p))
                shots_from_prev = bool(shots)
            ask_text += ("\n\n(I asked this already and the answer didn't work. I may have executed it wrong - "
                         "re-give the steps more precisely first, and tell me what to screenshot so you can check.)")
            log("Repeated question - careful re-check")
        last_map_old = False
        last_map_index = None            # 1-based place of the last map among the attached screenshots
        if mode in ("map", "whatsleft") and shots and not any(sp in self.strong_shots for sp in shots):
            # A fresh F11 shot may still be in its map check (OCR thread); wait briefly so a new map is
            # not treated as missing and the old last map attached as a second image.
            await asyncio.get_running_loop().run_in_executor(None, self.wait_map_checks, shots, 3.0)
        lm = g.last_map if mode in ("map", "whatsleft") else None
        if lm and lm in shots and shots_from_prev and prev and prev.get("last_map_old") is not None:
            # Repeated question with the previous question's shots: the last map in them is whatever it
            # was the first time (the guide added it then), even when it is a strong map of this run.
            last_map_old = bool(prev["last_map_old"])
            last_map_index = shots.index(lm) + 1
        elif lm and not any(sp in self.strong_shots for sp in shots):
            # No fresh REAL map in the question (nothing attached, or only a legend / collectibles list,
            # which is map-like but has no route to draw on). Add the saved last map so the point and the
            # route land on the real map. It is OLD (the player has moved on since; the map only opens at a
            # checkpoint) unless a fresh map-like shot (the legend screen the guide asked for, or a real map
            # whose title the OCR missed) was just taken and the last map is only minutes old: then the
            # player is still at that checkpoint with the map open. The legend screen stays attached so the
            # icons still get learned.
            if lm not in shots:
                fresh_map_like = any(sp in self.map_shots for sp in shots)
                age = g.last_map_age
                at_checkpoint = fresh_map_like and age is not None and age < mapnav.MAX_LAST_MAP_AGE
                shots.append(lm)
                last_map_index = len(shots)
                if at_checkpoint:
                    log(f"Attached the last map (taken {age:.0f} s ago, player still at the checkpoint): {Path(lm).name}")
                else:
                    last_map_old = True
                    self.old_map_shots.add(lm)
                    log(f"Attached the last map (old): {Path(lm).name}")
            elif lm in self.old_map_shots or lm not in self.map_shots:
                # Attached as old before, or re-attached from an earlier run: not a fresh capture.
                last_map_old = True
                last_map_index = shots.index(lm) + 1
        user_msg = {"role": "user", "content": question, "ts": time.time(), "via": source, "level": level,
                    "mode": mode}
        if shots:
            urls = ["/shots/" + slugify(g.name) + "/" + Path(sp).name for sp in shots]
            user_msg["shots"] = urls
            user_msg["shot"] = urls[-1]
        if last_map_index is not None:
            user_msg["last_map_old"] = last_map_old     # a repeat of this question re-attaches the same shots
        g.append(user_msg)
        self.emit({"type": "user", **user_msg})
        self.push_state()
        # The claude brain resumes its last chat session (--resume). A resumed session keeps the system
        # prompt it was started with, so the answer rules of a DIFFERENT mode (map -> whatsleft -> plain)
        # would not take effect. When a question picks a different mode, start a fresh session instead.
        # A question with no mode of its own ("yes", "the left one", "and then?") is a follow-up to whatever
        # was asked before: it resumes the current session, so the model keeps its memory of the chat and of
        # the question it just asked the player.
        session = g.meta.get("claude_session")
        if session and mode is not None and g.meta.get("claude_session_mode") != mode:
            log(f"Claude session not resumed: mode changed ({g.meta.get('claude_session_mode')} -> {mode})")
            session = None
        past = [m for m in g.history()[:-1] if m["role"] in ("user", "assistant")]
        claude_first = bool(CFG["brain_order"]) and CFG["brain_order"][0] == "claude"
        if not session and past and claude_first:
            # The claude brain only remembers through the resumed session, so a fresh one would start
            # blank: hand it the last few turns, so a map -> whatsleft switch still knows what was said.
            # Same when there is no session for any other reason (a brain error dropped it, first ask
            # after a restart): a follow-up like "yes" or "the left one" must not get a blank-slate answer.
            # Only when claude is the brain that answers first: the gemini/local brains get the chat
            # through req["history"] already, so this block would only repeat it for them.
            lines = []
            for m in past[-4:]:
                who = "Player" if m["role"] == "user" else "Guide"
                text = re.sub(r"\s+", " ", (m.get("content") or "")).strip()
                if len(text) > 300:
                    text = text[:300].rstrip() + "..."
                lines.append(f"{who}: {text}")
            ask_text += "\n\nEarlier in this chat (for context):\n" + "\n".join(lines)
        req = {"game": g.name, "level": level, "question": ask_text, "notes": g.notes,
               "history": past,
               "image": shots[-1] if shots else None, "images": shots,
               "cwd": str(g.dir), "claude_session": session,
               "mode": mode, "dossier": g.dossier}
        req["careful"] = careful
        req["last_map_old"] = last_map_old
        req["last_map_index"] = last_map_index      # which attached screenshot is the saved last map (1-based)
        req["map_icons"] = g.map_icons() or None     # "## Map icons" section of the dossier, if learned
        if shots:
            from PIL import Image
            sizes = []
            for sp in shots:
                try:
                    sizes.append(Image.open(sp).size)
                except Exception:
                    sizes.append(None)
            req["image_sizes"] = sizes
            req["image_size"] = sizes[-1]
        answer, sources, used = "", [], None
        stream_vis, first_spoken = "", False
        first_step_re = re.compile(r"^\s*(?:1[.)]|[-•*])\s+(.+?)\n(?=\s*(?:2[.)]|[-•*])\s)", re.M)
        t0 = time.time()
        try:
            for bname in CFG["brain_order"]:
                brain = brains.BRAINS[bname](CFG)
                got_tokens, failed = False, None
                filt = NotesFilter()
                async for ev in brain.ask(req):
                    if self.cancel:
                        break
                    t = ev["type"]
                    if t == "token":
                        got_tokens = True
                        answer += ev["text"]
                        vis = filt.feed(ev["text"])
                        if vis:
                            self.emit({"type": "token", "text": vis})
                            stream_vis += vis
                            if self.speak and not first_spoken:
                                m1 = first_step_re.search(stream_vis)
                                if m1:      # don't wait for the whole answer: read step 1 now
                                    first_spoken = True
                                    threading.Thread(target=self.say, args=(m1.group(1),), daemon=True).start()
                    elif t == "reset":        # brain retracted the text so far (pre-search narration)
                        if answer:
                            answer, filt, stream_vis = "", NotesFilter(), ""
                            self.emit({"type": "reset"})
                    elif t == "status":
                        self.emit(ev)
                        if self.overlay:
                            self.overlay.set_status(ev.get("text", ""))
                    elif t == "sources":
                        sources = ev["items"]
                    elif t == "progress":
                        g.replace_notes("\n".join(ev["items"]), seen=req["notes"])
                        update_vocab(g, "\n".join(ev["items"]))
                    elif t == "claude_session":
                        if ev.get("id"):
                            if ev["id"] != g.meta.get("claude_session"):
                                # A new session: remember the rules it was started with. A resumed one
                                # (same id) keeps its starting mode, so a mode-less follow-up does not
                                # overwrite it with None.
                                g.meta["claude_session_mode"] = mode
                            g.meta["claude_session"] = ev["id"]
                        else:
                            g.meta.pop("claude_session", None)
                            g.meta.pop("claude_session_mode", None)
                        g.save_meta()
                    elif t == "error":
                        failed = ev["text"]
                        log(f"brain {bname}: {failed}")
                        if not got_tokens:
                            self.note_brain_error(bname, failed)
                            break
                if got_tokens or self.cancel:
                    used = bname
                    rest = filt.flush()
                    if rest:
                        self.emit({"type": "token", "text": rest})
                    break
                self.emit({"type": "status", "text": f"{bname} unavailable ({(failed or 'no answer')[:120]}) - trying next..."})
            if used == "claude" and self.brain_warning:
                self.brain_warning = None        # claude answered again: the red banner goes away
                log("Claude brain is back - warning cleared")
            if not used:
                answer = "Sorry - none of the AI brains could answer. Check guide.log (is Ollama running / are you online?)."
                self.emit({"type": "token", "text": answer})
        except Exception as e:
            log(f"ask failed: {traceback.format_exc()}")
            msg = ("\n\nSomething went wrong while answering - please ask again. "
                   f"(Details are in guide.log: {type(e).__name__})")
            answer += msg
            self.emit({"type": "token", "text": msg})
        finally:
            answer, mark_list = marks.split_marks(answer, req.get("image_sizes"))
            # <legend>: what the map icons mean (from a legend screenshot or a guide) -> dossier.
            answer, legend = split_tag(answer, "legend")
            if legend:
                try:
                    # set_map_icons ignores "unknown" / "I don't know" and lines that are not "<icon>: <meaning>".
                    if g.set_map_icons(legend):
                        log(f"Map icons saved to the dossier ({len(legend.splitlines())} line(s))")
                    else:
                        log("Legend block had no usable '<icon>: <meaning>' lines - not saved")
                except Exception as e:
                    log(f"map icons save failed: {e}")
            visible, notes = split_notes(answer)
            visible = strip_preamble(strip_sources(visible), level)
            if notes:
                g.replace_notes(notes, seen=req["notes"])     # keeps clues saved while answering
                update_vocab(g, notes + "\n" + visible)
            steps = overlay.split_steps(visible)
            msg = {"role": "assistant", "content": visible, "ts": time.time(), "brain": used,
                   "sources": sources, "secs": round(time.time() - t0, 1), "first_spoken": first_spoken,
                   "steps": steps}
            snapped = []
            if shots and mark_list:
                try:
                    rendered = marks.render(shots, mark_list)
                    snapped = rendered["marks"]
                    url = lambda p: "/shots/" + slugify(g.name) + "/" + Path(p).name
                    msg["marks"] = {"all": [url(p) for p in rendered["all"]],
                                    "steps": {str(k): url(v) for k, v in rendered["steps"].items()}}
                    msg["mark_points"] = [{k: v for k, v in mk.items() if k != "shot"} for mk in snapped]
                    log(f"Marked {len(mark_list)} spot(s) on {len(rendered['all'])} screenshot(s)")
                except Exception as e:
                    log(f"marks render failed: {e}")
            g.append(msg)
            self.cur = {"steps": steps, "marks": snapped, "pos": 0, "shots": shots}
            self.emit({"type": "done", **msg})
            if self.overlay:
                self.overlay.set_status("")
                if steps and not self.cancel:
                    self.overlay.show_answer(steps, snapped, shots)
            self.busy = False
            self.push_state()
            log(f"Answered via {used} in {msg['secs']}s")
            # Read aloud: step 1 now (unless it was already spoken while streaming); F7 / Next speak the rest.
            if self.speak and not first_spoken and steps and not self.cancel:
                threading.Thread(target=self.say, args=(steps[0]["text"],), daemon=True).start()

    # ---- step navigation: window, overlay and voice move together
    def step(self, delta: int = 0, pos: int | None = None):
        cur = self.cur
        if not cur or not cur["steps"]:
            return
        n = len(cur["steps"])
        new = pos if pos is not None else cur["pos"] + delta
        if new >= n:
            self.emit({"type": "toast", "text": "That was the last step."})
            if self.overlay:
                self.overlay.finished()
            if self.speak:
                self.speaker.stop()
                threading.Thread(target=self.speaker.say, daemon=True,
                                 args=("That was the last step.", self.voice, CFG["voice_rate"])).start()
            return
        cur["pos"] = max(0, new)
        self.emit({"type": "step", "pos": cur["pos"]})
        if self.overlay:
            self.overlay.set_pos(cur["pos"])
        if self.speak:
            self.speaker.stop()
            threading.Thread(target=self.say, args=(cur["steps"][cur["pos"]]["text"],), daemon=True).start()

    # ---- read aloud (Kokoro on CPU as the offline fallback)
    def say(self, text: str):
        """Read a guide answer aloud with the chosen natural voice."""
        try:
            clean = re.sub(r"\[\d+\]|https?://\S+|[*_#`>]", "", text)
            clean = re.sub(r"^\s*(\d+)[.)]\s*", r"Step \1: ", clean, flags=re.M)
            clean = re.sub(r"^\s*[-•]\s*", "", clean, flags=re.M)
            # Each bullet becomes its own sentence so the voice pauses between steps.
            clean = "\n\n".join(l.strip().rstrip(".") + "." for l in clean.splitlines() if l.strip())
            self.speaker.say(clean, self.voice, CFG["voice_rate"])
        except Exception as e:
            log(f"tts failed: {e}")


# Questions that mean "map mode" / "what's left mode" (only when the caller did not pick a mode).
MAP_Q_RE = re.compile(r"where (do|should) i go|what('s| is) next|next objective|where to go|where to now", re.I)
WHATSLEFT_RE = re.compile(r"what('s| is) left (here|in this area)|what did i miss|anything (i )?missed", re.I)


class NotesFilter:
    """Streams tokens to the UI but hides the trailing <notes>...</notes> block
    (and the <marks> / <legend> blocks, which are data for us, not text for the player)."""

    def __init__(self):
        self.buf = ""
        self.hidden = False

    def feed(self, t: str) -> str:
        if self.hidden:
            return ""
        self.buf += t
        i = min([k for k in (self.buf.find("<notes"), self.buf.find("<marks"), self.buf.find("<legend"))
                 if k >= 0], default=-1)
        if i >= 0:
            out, self.buf, self.hidden = self.buf[:i], "", True
            return out
        j = self.buf.rfind("<")
        if j >= 0 and len(self.buf) - j < 7:     # maybe the start of "<notes", hold it back
            out, self.buf = self.buf[:j], self.buf[j:]
            return out
        out, self.buf = self.buf, ""
        return out

    def flush(self) -> str:
        out, self.buf = ("" if self.hidden else self.buf), ""
        return out


def strip_sources(text: str) -> str:
    """Remove citation markers and any trailing 'Sources:' section the model added anyway."""
    text = re.sub(r"\n+\**Sources?\**:?\**\s*\n.*\Z", "", text, flags=re.S | re.I)
    text = re.sub(r"\s?\[\d+\](\[\d+\])*", "", text)
    return text.strip()


def strip_preamble(text: str, level: str) -> str:
    """Drop a leading non-list line ("This is consistent enough to answer...") before the steps."""
    if level == "nudge":
        return text
    paras = text.split("\n\n", 1)
    is_list = lambda s: bool(re.match(r"\s*([-*•]|\d+[.)])\s", s))
    if len(paras) == 2 and not is_list(paras[0]) and len(paras[0]) < 160 and is_list(paras[1]):
        return paras[1].strip()
    return text


def split_tag(answer: str, tag: str) -> tuple[str, str]:
    """Cut the last <tag>...</tag> block out of the answer (a missing closing tag is tolerated).
    Returns (answer without the block, block content)."""
    ms = list(re.finditer(rf"<{tag}>(.*?)(</{tag}>|$)", answer, re.S))
    m = ms[-1] if ms else None
    if not m:
        return answer.strip(), ""
    return (answer[:m.start()] + answer[m.end():]).strip(), m.group(1).strip()


def split_notes(answer: str) -> tuple[str, str]:
    return split_tag(answer, "notes")


def load_vocab(game: GameStore | None) -> str:
    words = []
    shared = CFG.get("extra_vocab_file") or ""
    shared = Path(shared) if isinstance(shared, str) and shared.strip() else None
    if game and (game.dir / "vocab.txt").exists():
        words += [w for w in (game.dir / "vocab.txt").read_text(encoding="utf-8").split(",") if w.strip()]
    if game:
        words.insert(0, game.name)
    if shared and shared.is_file() and len(words) < 40:
        for line in shared.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                words += [w for w in line.split(",") if w.strip()]
    return ", ".join(dict.fromkeys(w.strip() for w in words))[:600]


def update_vocab(game: GameStore, text: str):
    """Collect proper nouns (e.g. 'Haldane Hotel', 'Lion Medallion') so Whisper spells them right."""
    text = re.sub(r"[*_#`\[\]()]", "", text)
    # Capitalised words in the middle of a sentence are names; sentence-initial words are skipped.
    found = re.findall(r"(?<=[a-z0-9,;:] )([A-Z][a-z]+(?:\s+(?:of\s+|the\s+)?[A-Z][a-z]+)*)", text)
    common = {"The", "This", "That", "Then", "When", "After", "Once", "Look", "Head", "Step", "Use",
              "Player", "Chapter", "Part", "You", "Your", "From", "Inside", "Note", "There", "Here",
              "Find", "Take", "Check", "Sources", "Enter", "Turn", "Walk", "Open", "Next", "What",
              "Where", "Keep", "Walkthrough", "Guide", "Wiki", "Puzzles", "Endings", "Collectibles"}
    p = game.dir / "vocab.txt"
    old = [w.strip() for w in p.read_text(encoding="utf-8").split(",")] if p.exists() else []
    new = [w for w in found if w.split()[0] not in common and w.split()[-1] not in common and len(w) > 3]
    merged = list(dict.fromkeys(old + new))[-80:]
    p.write_text(", ".join(w for w in merged if w), encoding="utf-8")


def _ensure_av_importable():
    try:
        import av  # noqa: F401
    except ImportError:
        import types
        stub = types.ModuleType("av")
        stub.__path__ = []
        for sub in ("audio", "audio.frame", "audio.resampler", "audio.fifo", "container", "error"):
            sys.modules["av." + sub] = types.ModuleType("av." + sub)
        stub.audio = sys.modules["av.audio"]
        stub.error = sys.modules["av.error"]
        sys.modules["av"] = stub


def beep(freq: int, ms: int):
    import winsound
    threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()


APP = App()


# ------------------------------------------------------------------ hotkeys
def setup_hotkeys():
    import keyboard
    import queue
    q: queue.Queue = queue.Queue()
    held = {"talk": False}

    # Hook callbacks stay trivial (slow hooks lag the whole system); the work happens on a thread.
    def on_talk(e):
        if e.event_type == "down" and not held["talk"]:
            held["talk"] = True
            q.put("talk_down")
        elif e.event_type == "up" and held["talk"]:
            held["talk"] = False
            q.put("talk_up")

    if CFG.get("hotkey_talk_enabled", True):
        keyboard.hook_key(CFG["hotkey_talk"], on_talk)
    keyboard.on_press_key(CFG["hotkey_screenshot"], lambda e: q.put("shot"))
    keyboard.on_press_key(CFG["hotkey_read"], lambda e: q.put("read"))
    keyboard.on_press_key(CFG["hotkey_next"], lambda e: q.put("next"))
    keyboard.on_press_key(CFG["hotkey_overlay"], lambda e: q.put("overlay"))
    keyboard.on_press_key(CFG["hotkey_where"], lambda e: q.put("where"))

    def worker():
        while True:
            ev = q.get()
            try:
                if ev == "talk_down":
                    APP.mic_start()
                elif ev == "talk_up":
                    APP.mic_stop(send=True)
                elif ev == "shot":
                    APP.take_screenshot()
                elif ev == "next":       # next step: window, overlay and voice follow
                    APP.step(1)
                elif ev == "overlay":    # hide / show the in-game overlay for the current answer
                    if APP.overlay:
                        APP.overlay.toggle()
                elif ev == "where":      # screenshot + "where am I?"
                    APP.where_am_i()
                elif ev == "read":   # own thread, so a second press can stop it
                    threading.Thread(target=APP.read_screen, daemon=True).start()
            except Exception:
                log(f"hotkey worker: {traceback.format_exc()}")
    threading.Thread(target=worker, daemon=True).start()


# ------------------------------------------------------------------ web server
routes = web.RouteTableDef()

# "Sign in to Claude" in the Choose your brain panel: the same login script as Login-Claude.bat, in its own window.
BRAIN_LOGIN_CMD = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BASE / "login-claude.ps1")]


def brain_choice_changed():
    """A brain was picked (or the panel skipped): the old 'Claude isn't available' banner may no longer fit."""
    APP.brain_warning = None
    APP.push_state()


@routes.get("/")
async def index(_):
    return web.FileResponse(BASE / "web" / "index.html", headers={"Cache-Control": "no-cache"})


@routes.get("/events")
async def events(request):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    q: asyncio.Queue = asyncio.Queue()
    APP.clients.add(q)
    try:
        await resp.write(f"data: {json.dumps(APP.state())}\n\n".encode())
        if APP.game:
            await resp.write(f"data: {json.dumps({'type': 'history', 'items': APP.game.history()[-40:]})}\n\n".encode())
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=3)   # short ping: a closed window is noticed fast
                await resp.write(f"data: {json.dumps(ev)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        APP.clients.discard(q)
        if not APP.clients:
            asyncio.get_running_loop().call_later(4, window_gone)
    return resp


def window_gone():
    """The Game Guide window was closed (no client came back within a few seconds, so it was not
    just a reload): take the answer off the game screen and stop talking. A new answer shows again."""
    if APP.clients:
        return
    log("Window closed - overlay cleared, voice stopped")
    if APP.overlay:
        APP.overlay.clear()
    APP.speaker.stop()


@routes.post("/api/ask")
async def api_ask(request):
    d = await request.json()
    text = (d.get("text") or "").strip()
    if APP.game:   # re-attach earlier screenshots of this game (by file name)
        for name in ([d["shot"]] if d.get("shot") else []) + list(d.get("shots") or []):
            p = APP.game.dir / "shots" / Path(name).name
            if p.exists() and str(p) not in APP.pending_shots:
                APP.pending_shots.append(str(p))
    if text:
        asyncio.create_task(APP.ask(text, level=d.get("level"), careful=bool(d.get("careful")),
                                    mode=d.get("mode")))
    return web.json_response({"ok": True})


@routes.post("/api/retry")
async def api_retry(_):
    """'Didn't work': the last answer failed in game. Re-ask with the failed steps spelled out, the same
    screenshot re-attached, and the careful model, so the guide re-checks instead of repeating itself."""
    if not APP.game or APP.busy:
        return web.json_response({"ok": False})
    hist = APP.game.history()
    last_a = next((m for m in reversed(hist) if m["role"] == "assistant"), None)
    last_u = next((m for m in reversed(hist) if m["role"] == "user"), None)
    if not last_a or not last_u:
        return web.json_response({"ok": False})
    if not APP.pending_shots:
        for u in (last_u.get("shots") or ([last_u["shot"]] if last_u.get("shot") else [])):
            p = APP.game.dir / "shots" / Path(u).name
            if p.exists():
                APP.pending_shots.append(str(p))
    q = ("That didn't work in the game. These are the steps I followed:\n" + last_a["content"][:1500] +
         "\n\nI may have pressed something wrong - panels with many buttons confuse me. So: (1) if the walkthroughs "
         "still agree with these steps, give me the SAME steps again but more precisely - for every button its symbol "
         "and its row/column, starting with clearing the panel - and tell me to press F11 on the panel after entering "
         "so you can check what is lit; (2) only if the guides disagree with them, or my screenshot shows I entered it "
         "exactly right, give a different answer - consider codes/IDs that differ per playthrough (tell me where to "
         "read mine), a missed step, or a different order.")
    asyncio.create_task(APP.ask(q, level=last_u.get("level") or APP.level, careful=True))
    return web.json_response({"ok": True})


@routes.post("/api/where")
async def api_where(_):
    await asyncio.to_thread(APP.where_am_i, False)      # from the window: no capture (use F11 first if wanted)
    return web.json_response({"ok": True})


@routes.post("/api/whatsleft")
async def api_whatsleft(_):
    """'What's left here?': things in this area and which are done. ask() attaches the pending map
    screenshot if there is one, else the game's last map (marked as old)."""
    if not APP.game:
        APP.emit({"type": "toast", "text": "Pick or type the game you're playing first (top of the window)."})
        return web.json_response({"ok": False})
    if not APP.pending_shots and not APP.game.last_map:
        APP.emit({"type": "toast", "text": "I have no map yet - open the map and press F4 (or F11), then try again."})
        return web.json_response({"ok": False})
    asyncio.create_task(APP.ask("What's left to do in this area?", source="whatsleft", mode="whatsleft"))
    return web.json_response({"ok": True})


@routes.get("/api/dossier")
async def api_dossier(_):
    g = APP.game
    return web.json_response({"text": g.dossier if g else "", "building": bool(g and slugify(g.name) in APP.dossier_building)})


@routes.post("/api/dossier")
async def api_dossier_rebuild(_):
    APP.ensure_dossier(force=True)
    return web.json_response({"ok": True})


@routes.post("/api/step")
async def api_step(request):
    d = await request.json()
    APP.step(delta=int(d.get("delta", 0)), pos=d.get("pos"))
    return web.json_response({"ok": True})


@routes.post("/api/solution")
async def api_solution(_):
    """'Just tell me': re-ask the last question at full-solution level."""
    if APP.game:
        last = next((m for m in reversed(APP.game.history()) if m["role"] == "user"), None)
        if last:
            asyncio.create_task(APP.ask("Just give me the full solution for this: " + last["content"],
                                        level="solution"))
    return web.json_response({"ok": True})


@routes.post("/api/set")
async def api_set(request):
    d = await request.json()
    if "game" in d:
        name = (d["game"] or "").strip()
        if name.isdigit():   # a Steam App ID -> look up the real name
            resolved = await asyncio.to_thread(steam_name_for_appid, name)
            if not resolved:
                APP.emit({"type": "toast", "text": f"Couldn't find a Steam game with App ID {name}"})
                return web.json_response({"ok": False})
            name = resolved
        APP.set_game(name, d.get("exe"))
    if "overlay" in d:
        save_settings(overlay=bool(d["overlay"]))
        if APP.overlay:
            APP.overlay.set_enabled(bool(d["overlay"]))
    if "startup" in d:
        err = await asyncio.to_thread(set_startup_safe, bool(d["startup"]))
        if APP.tray:
            APP.tray.update_menu()
        if err:
            APP.emit({"type": "toast", "text": f"Start with Windows didn't change: {err}"})
            APP.push_state()
            return web.json_response({"ok": False, "error": err})
    if "voice" in d or "read_mode" in d:
        if d.get("voice"):
            APP.voice = d["voice"]
        if d.get("read_mode") in ("auto", "fast", "smart"):
            APP.read_mode = d["read_mode"]
        save_settings(voice=APP.voice, read_mode=APP.read_mode)
    if d.get("test_voice"):
        threading.Thread(target=APP.speaker.say, daemon=True,
                         args=("Hey. This is how I'll sound when I read notes and hints to you.", APP.voice,
                               CFG["voice_rate"])).start()
    if "auto_detect" in d:
        APP.auto_detect = bool(d["auto_detect"])
        APP.checked_exes.clear()
        if not APP.auto_detect:
            APP.candidate = None
        save_settings(auto_detect=APP.auto_detect)
    if "level" in d:
        APP.level = d["level"]
    if "speak" in d:
        APP.speak = bool(d["speak"])
    if "notes" in d and APP.game:
        APP.game.notes = d["notes"]
    if d.get("dismiss_candidate"):
        APP.candidate = None
    if "drop_shot" in d:            # an index removes that screenshot; anything else clears them all
        i = d["drop_shot"]
        if isinstance(i, int) and 0 <= i < len(APP.pending_shots):
            del APP.pending_shots[i]
        else:
            APP.pending_shots = []
    if d.get("stop"):
        APP.cancel = True
        APP.read_cancel = True
        APP.speaker.stop()
    if d.get("clear_chat") and APP.game:
        hp = APP.game.dir / "history.jsonl"
        if hp.exists():
            hp.rename(APP.game.dir / f"history-{datetime.now():%Y%m%d-%H%M%S}.jsonl")
        APP.game.meta.pop("claude_session", None)
        APP.game.meta.pop("claude_session_mode", None)
        APP.game.save_meta()
        APP.emit({"type": "history", "items": []})
    APP.push_state()
    return web.json_response({"ok": True})


@routes.post("/api/sense")
async def api_sense(request):
    """Sense settings from the window: any of {enabled, range (3-30 m), sound (chime|knock|harp),
    sound_on (true/false: play the near-item sound, saved), path (true/false: the quest path trail, saved),
    route (true/false: the quest path follows the ground = no <Sense mod>/route_off.txt, saved),
    mute_minutes (0 = unmute), test (play the chosen sound once)}."""
    d = await request.json()
    s = APP.sense
    if not s:
        return web.json_response({"ok": False, "error": "Sense is not available (sense.py did not load)"})
    try:
        if "enabled" in d:
            on = bool(d["enabled"])
            s.set_enabled(on)
            save_settings(sense=on)
            if not on and APP.overlay:
                APP.overlay.set_sense(None)
        if "range" in d:
            r = round(min(30.0, max(3.0, float(d["range"]))), 1)
            s.set_range(r)
            save_settings(sense_range=r)
        if "sound" in d:
            name = str(d["sound"])
            if name not in SENSE_SOUND_NAMES:
                return web.json_response({"ok": False, "error": f"Unknown sound: {name}"})
            s.set_sound(name)
            save_settings(sense_sound=name)
        if "sound_on" in d:
            v = d["sound_on"]
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "on", "yes")
            else:
                v = bool(v)
            if hasattr(s, "set_sound_on"):
                s.set_sound_on(v)
            save_settings(sense_sound_on=v)
        if "path" in d:
            v = d["path"]
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "on", "yes")
            else:
                v = bool(v)
            if hasattr(s, "set_path_enabled"):
                s.set_path_enabled(v)
            save_settings(sense_path=v)
            if not v:
                APP._clear_path()
        if "route" in d:
            v = d["route"]
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "on", "yes")
            else:
                v = bool(v)
            # change the kill file first and save only when that worked: a failed write/delete must not
            # leave config.json (and so the button) saying a state the game is not using
            err = APP.apply_route_switch(v)
            if err:
                APP.push_state()
                return web.json_response({"ok": False, "error": err})
            save_settings(sense_route=v)
        if "mute_minutes" in d:
            s.mute(max(0.0, float(d["mute_minutes"])) * 60)
        if d.get("test"):
            name = str(CFG.get("sense_sound", "chime"))
            played = s.play_sound(name) if hasattr(s, "play_sound") else play_sense_sound(name)
            if not played:
                APP.emit({"type": "toast", "text": f"Sound file missing: {name}.wav"})
    except Exception as e:
        log("sense settings failed: " + traceback.format_exc())
        return web.json_response({"ok": False, "error": str(e)[:160]})
    APP.push_state()
    return web.json_response({"ok": True})


def sense_game(name: str) -> dict | None:
    """The installer's entry for this game: {"engine": "ue4ss"|"reframework", "key", "exe", "title", "dir", "slug"},
    or None when we have no helper for it. One helper per engine (sense\\install_helper.py GAMES). "dir" is the
    game folder from config.json "sense_game_dirs" (slug -> folder), or None while it is not set."""
    if not name:
        return None
    try:
        helper = load_install_helper()
        gi = helper.game_info(name)
        if gi:
            gi = dict(gi)
            gi["dir"] = helper.resolve_game_dir(name, CFG.get("sense_game_dirs"))
            gi.setdefault("slug", slugify(name))
        return gi or None
    except Exception as e:
        log(f"sense installer not loadable: {e}")
    return None


def sense_target(name: str) -> tuple[str | None, Path | None]:
    """Which game the helper install is for: (slug, game folder) or (None, None) when we have no helper for it."""
    gi = sense_game(name)
    return (gi["slug"], gi["dir"]) if gi else (None, None)


async def sense_install_check(name: str) -> str | None:
    """Reasons not to touch the game folder now, in plain words (None = fine)."""
    gi = sense_game(name)
    if not gi:
        return f"There is no Sense helper for {name or 'this game'} yet."
    game_dir = gi["dir"]
    if APP.sense_busy:
        return "The helper is already being installed or removed. Wait a moment."
    if game_dir is None:
        return (f"Set the game folder first: in config.json, add \"sense_game_dirs\": {{\"{gi['slug']}\": "
                f"\"<the folder with {gi.get('exe') or 'the game exe'}>\"}} and restart Game Guide.")
    if not game_dir.exists():
        return f"Game folder not found: {game_dir}"
    try:
        helper = load_install_helper()
        if await asyncio.to_thread(helper.is_game_running, gi.get("exe") or "PBZ.exe"):
            return "Close the game first, then try again."
    except Exception as e:
        log("sense install check: " + traceback.format_exc())
        return f"Installer not available: {str(e)[:120]}"
    return None


@routes.post("/api/sense/install")
async def api_sense_install(request):
    """Install (or repair) the game's Sense helper into the game folder, in a thread (Unreal: UE4SS package +
    helper; RE Engine: the helper scripts into an existing REFramework).
    A backup of the old files is made first (sense\\backup\\<time>). Refuses while the game runs."""
    d = await request.json()
    name = (d.get("game") or (APP.game.name if APP.game else "")).strip()
    err = await sense_install_check(name)
    gi = None if err else sense_game(name)
    ue = bool(gi) and gi.get("engine", "ue4ss") == "ue4ss"
    helper_src = SENSE_HELPER_SRC
    if not err and not ue:
        try:
            helper_src = Path(load_install_helper().RE_HELPER_SRC)
        except Exception as e:
            err = f"Installer not available: {str(e)[:120]}"
    if not err and ue and not SENSE_ZIP.exists():
        err = f"The UE4SS download is missing: {SENSE_ZIP}"       # only the Unreal helper needs UE4SS
    if not err and not helper_src.exists():
        err = f"The helper mod folder is missing: {helper_src}"
    if err:
        # The window shows the "error" text of the reply as a toast itself, so no toast from here.
        return web.json_response({"ok": False, "error": err})
    slug, game_dir = gi["slug"], gi["dir"]
    APP.sense_busy = True
    APP.push_state()

    def work() -> str | None:
        """Blocking install on a worker thread. Returns the plain reason it failed, or None when it worked."""
        try:
            APP.emit({"type": "toast", "text": "Installing the Sense helper (backup first, about a minute)..."})
            helper = load_install_helper()
            backup = SENSE_DIR / "backup" / f"{datetime.now():%Y-%m-%d-%H%M%S}"
            if hasattr(helper, "install_game"):      # picks the Unreal or the RE Engine helper
                res = helper.install_game(slug, game_dir=str(game_dir), backup_dir=str(backup),
                                          zip_path=str(SENSE_ZIP), helper_src=str(helper_src))
            else:
                res = helper.install_pbz(game_dir=str(game_dir), zip_path=str(SENSE_ZIP),
                                         helper_src=str(SENSE_HELPER_SRC), backup_dir=str(backup))
            for line in res.get("log") or []:
                log(f"sense install: {line}")
            if res.get("ok") and res.get("sense_file"):
                g = APP.game if (APP.game and slugify(APP.game.name) == slug) else GameStore(name)
                g.meta["sense_file"] = str(res["sense_file"])
                g.save_meta()
                if g is APP.game:
                    APP.start_sense()
                log(f"Sense helper installed for {g.name}: {res['sense_file']}")
                return None
            why = res.get("error") or ((res.get("log") or ["no details"])[-1])
            log(f"sense install failed: {why}")
            return f"Helper install failed: {why}"
        except Exception as e:
            log("sense install failed: " + traceback.format_exc())
            return f"Helper install failed: {str(e)[:160]}"
        finally:
            APP.sense_busy = False
            APP.push_state()
    # The window keeps its button on "Installing..." until this reply arrives and shows the result itself,
    # so we wait for the install here (it is a ~30 MB copy, well under a minute) instead of fire-and-forget.
    why = await asyncio.to_thread(work)
    if why:
        return web.json_response({"ok": False, "error": why})
    return web.json_response({"ok": True})


@routes.post("/api/sense/uninstall")
async def api_sense_uninstall(request):
    """Remove the Sense helper (Unreal: UE4SS loader off, dwmapi.dll -> .off, and the Sense mod; RE Engine: only our
    scripts, REFramework stays). Refuses while the game runs."""
    d = await request.json()
    name = (d.get("game") or (APP.game.name if APP.game else "")).strip()
    err = await sense_install_check(name)
    if err:
        # The window shows the "error" text of the reply as a toast itself, so no toast from here.
        return web.json_response({"ok": False, "error": err})
    slug, game_dir = sense_target(name)
    APP.sense_busy = True
    APP.push_state()

    def work() -> str | None:
        """Blocking removal on a worker thread. Returns the plain reason it failed, or None when it worked."""
        try:
            helper = load_install_helper()
            backup = SENSE_DIR / "backup" / f"{datetime.now():%Y-%m-%d-%H%M%S}-uninstall"
            if hasattr(helper, "uninstall_game"):
                res = helper.uninstall_game(slug, game_dir=str(game_dir), backup_dir=str(backup))
            else:
                res = helper.uninstall_pbz(game_dir=str(game_dir), backup_dir=str(backup))
            for line in (res.get("log") if isinstance(res, dict) else None) or []:
                log(f"sense uninstall: {line}")
            if isinstance(res, dict) and res.get("ok") is False:
                why = res.get("error") or ((res.get("log") or ["no details"])[-1])
                log(f"sense uninstall failed: {why}")
                return f"Helper removal failed: {why}"
            g = APP.game if (APP.game and slugify(APP.game.name) == slug) else GameStore(name)
            g.meta.pop("sense_file", None)
            g.save_meta()
            if g is APP.game:
                APP.start_sense()        # no sense_file any more -> stops the reader, clears the overlay
            log(f"Sense helper removed for {g.name}")
            return None
        except Exception as e:
            log("sense uninstall failed: " + traceback.format_exc())
            return f"Helper removal failed: {str(e)[:160]}"
        finally:
            APP.sense_busy = False
            APP.push_state()
    why = await asyncio.to_thread(work)     # the window waits for the reply and toasts the result itself
    if why:
        return web.json_response({"ok": False, "error": why})
    return web.json_response({"ok": True})


@routes.get("/api/sense/discovery")
async def api_sense_discovery(_):
    """The helper's one-time discovery dump (F3 in game): out\\classes.txt next to sense.json."""
    sf = APP.sense_file()
    p = Path(sf).parent / "classes.txt" if sf else None
    if p and p.exists():
        try:
            return web.json_response({"text": p.read_text(encoding="utf-8", errors="replace"), "found": True})
        except OSError as e:
            return web.json_response({"text": f"Could not read {p}: {e}", "found": False})
    return web.json_response({"text": "", "found": False})


@routes.post("/api/say")
async def api_say(request):
    """Replay a read-out text (or stop if something is already being spoken).
    step=true: speak one answer step now, cutting off whatever is being said."""
    d = await request.json()
    text = (d.get("text") or "").strip()
    if d.get("step"):
        APP.speaker.stop()
        if text:
            threading.Thread(target=APP.say, args=(text,), daemon=True).start()
    elif APP.speaker.speaking:
        APP.speaker.stop()
    elif text:
        threading.Thread(target=APP.speaker.say, args=(text, APP.voice, CFG["voice_rate"]), daemon=True).start()
    return web.json_response({"ok": True})


@routes.post("/api/read")
async def api_read(_):
    threading.Thread(target=APP.read_screen, daemon=True).start()
    return web.json_response({"ok": True})


@routes.get("/api/windows")
async def api_windows(_):
    return web.json_response(await asyncio.to_thread(list_windows))


@routes.post("/api/mic/{what}")
async def api_mic(request):
    what = request.match_info["what"]
    if what == "start":
        await asyncio.to_thread(APP.mic_start)
    else:
        await asyncio.to_thread(APP.mic_stop, request.query.get("send", "1") == "1")
    return web.json_response({"ok": True})


@routes.post("/api/screenshot")
async def api_screenshot(_):
    await asyncio.to_thread(APP.take_screenshot)
    return web.json_response({"ok": True})


@routes.get("/shots/{game}/{name}")
async def shots(request):
    p = BASE / "games" / request.match_info["game"] / "shots" / request.match_info["name"]
    if not p.resolve().is_relative_to((BASE / "games").resolve()) or not p.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(p)


# ------------------------------------------------------------------ taskbar identity
# The window is drawn by Edge, so by default Windows groups it under Edge (Edge icon, pinning
# pins Edge). Giving the window and our shortcuts the same AppUserModelID makes the taskbar treat
# it as its own app: Game Guide icon, and a pinned button that relaunches guide.py.
AUMID = "GameGuide.App"
ICON = BASE / "web" / "icon.ico"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
_tagged_windows: set[int] = set()


def _pv(value: str):
    import pythoncom
    from win32comext.propsys import propsys
    return propsys.PROPVARIANTType(value, pythoncom.VT_LPWSTR)


def find_guide_windows() -> list[int]:
    import psutil
    import win32gui
    import win32process
    found = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd).startswith("Game Guide"):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                if psutil.Process(pid).name().lower() == "msedge.exe":
                    found.append(hwnd)
            except Exception:
                pass
    win32gui.EnumWindows(cb, None)
    return found


def tag_window(hwnd: int):
    import win32con
    import win32gui
    from win32comext.propsys import propsys, pscon
    store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_RelaunchCommand, _pv(f'"{PYTHONW}" "{BASE / "guide.py"}"'))
    store.SetValue(pscon.PKEY_AppUserModel_RelaunchDisplayNameResource, _pv("Game Guide"))
    store.SetValue(pscon.PKEY_AppUserModel_RelaunchIconResource, _pv(f"{ICON},0"))
    store.SetValue(pscon.PKEY_AppUserModel_ID, _pv(AUMID))
    store.Commit()
    flags = win32con.LR_LOADFROMFILE
    big = win32gui.LoadImage(0, str(ICON), win32con.IMAGE_ICON, 48, 48, flags)
    small = win32gui.LoadImage(0, str(ICON), win32con.IMAGE_ICON, 16, 16, flags)
    win32gui.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_BIG, big)
    win32gui.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_SMALL, small)


def tag_new_windows():
    for hwnd in find_guide_windows():
        if hwnd not in _tagged_windows:
            try:
                tag_window(hwnd)
                _tagged_windows.add(hwnd)
                log(f"Taskbar identity set on window {hwnd}")
            except Exception as e:
                log(f"tag window failed: {e}")


def set_shortcut_aumid(lnk: Path):
    """Stamp our AppUserModelID on a .lnk so a pinned/launched shortcut groups with the window."""
    import pythoncom
    from win32com.shell import shell
    from win32comext.propsys import propsys, pscon
    pythoncom.CoInitialize()
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                      shell.IID_IShellLink)
    pf = link.QueryInterface(pythoncom.IID_IPersistFile)
    pf.Load(str(lnk), 2)   # STGM_READWRITE
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    try:
        if str(store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue() or "") == AUMID:
            return
    except Exception:
        pass
    store.SetValue(pscon.PKEY_AppUserModel_ID, _pv(AUMID))
    store.Commit()
    pf.Save(str(lnk), True)
    log(f"AppUserModelID set on {lnk.name} ({lnk.parent.name})")


def fix_shortcuts():
    import pythoncom
    pythoncom.CoInitialize()
    import win32com.shell.shell as sh
    from win32com.shell import shellcon
    dirs = [sh.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0),
            sh.SHGetFolderPath(0, shellcon.CSIDL_PROGRAMS, None, 0),
            str(STARTUP_LNK.parent),
            str(Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Internet Explorer" / "Quick Launch"
                / "User Pinned" / "TaskBar")]
    for d in dirs:
        lnk = Path(d) / "Game Guide.lnk"
        if lnk.exists():
            try:
                set_shortcut_aumid(lnk)
            except Exception as e:
                log(f"shortcut id failed for {lnk}: {e}")


def focus_existing_window() -> bool:
    """If a Game Guide window is already open, bring it to the front instead of opening another."""
    import win32con
    import win32gui
    found = find_guide_windows()
    if not found:
        return False
    try:
        win32gui.ShowWindow(found[0], win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(found[0])
    except Exception:
        pass
    return True


def open_window(url: str):
    try:
        if focus_existing_window():
            return
    except Exception as e:
        log(f"focus window: {e}")
    edge = shutil.which("msedge") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    try:
        subprocess.Popen([edge, f"--app={url}", "--window-size=560,820",
                          f"--user-data-dir={BASE / '.edge-profile'}"])
    except Exception:
        import webbrowser
        webbrowser.open(url)


STARTUP_LNK = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
               / "Startup" / "Game Guide.lnk")


def startup_enabled() -> bool:
    return STARTUP_LNK.exists()


def set_startup(on: bool):
    """Start with Windows = a shortcut in the user's Startup folder that starts quietly (no window)."""
    if on:
        import win32com.client
        sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(STARTUP_LNK))
        sc.TargetPath = str(Path(sys.executable).with_name("pythonw.exe"))
        sc.Arguments = f'"{BASE / "guide.py"}" --no-window'
        sc.WorkingDirectory = str(BASE)
        sc.IconLocation = str(BASE / "web" / "icon.ico")
        sc.Description = "Game Guide (starts in the background; F10 in game to ask)"
        sc.Save()
        try:
            set_shortcut_aumid(STARTUP_LNK)
        except Exception as e:
            log(f"startup shortcut id: {e}")
    elif STARTUP_LNK.exists():
        STARTUP_LNK.unlink()
    log(f"Start with Windows: {'on' if on else 'off'}")


def set_startup_safe(on: bool) -> str | None:
    """set_startup() for a worker thread; returns an error text in plain words, or None when it worked.
    Why: the window's button ran set_startup() on the web server's thread pool, where COM was never set
    up (pythoncom only does that for the thread that imports it first - the game watcher). WScript.Shell
    then failed with "CoInitialize has not been called" and the route answered 500 with no log line."""
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception as e:
        log(f"startup: COM init failed: {e}")
    try:
        set_startup(on)
        return None
    except Exception as e:
        log("startup failed: " + traceback.format_exc())
        reason = getattr(e, "strerror", None) or str(e)
        return f"{reason}"[:160] or type(e).__name__


def run_tray(port: int):
    """Tray icon by the clock: Open / Start with Windows / Quit."""
    try:
        import pystray
        from PIL import Image
    except ImportError:
        log("pystray not installed - no tray icon")
        return
    url = f"http://127.0.0.1:{port}/"

    def toggle_startup(icon, _item):
        set_startup(not startup_enabled())
        APP.push_state()

    def quit_app(icon, _item):
        log("Quit from tray")
        icon.stop()
        os._exit(0)

    k = lambda name: CFG[name].upper()
    menu = pystray.Menu(
        pystray.MenuItem("Open Game Guide", lambda *_: open_window(url), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Hold {k('hotkey_talk')}  -  ask by voice", None, enabled=False),
        pystray.MenuItem(f"{k('hotkey_next')}  -  next step of the answer", None, enabled=False),
        pystray.MenuItem(f"{k('hotkey_overlay')}  -  hide / show the in-game overlay", None, enabled=False),
        pystray.MenuItem(f"{k('hotkey_where')}  -  where am I? (screenshot + 3-line answer)", None, enabled=False),
        pystray.MenuItem(f"{k('hotkey_read')}  -  read text on screen aloud (again = stop)", None, enabled=False),
        pystray.MenuItem(f"{k('hotkey_screenshot')}  -  attach screenshot to next question", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with Windows", toggle_startup, checked=lambda _i: startup_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("GameGuide", Image.open(BASE / "web" / "icon.png"),
                        f"Game Guide - hold {CFG['hotkey_talk'].upper()} in game to ask", menu)
    APP.tray = icon
    icon.run_detached()


async def main():
    APP.loop = asyncio.get_running_loop()
    app = web.Application(middlewares=[brainsetup.local_only])   # other web sites can't use the server
    app.add_routes(routes)
    app.add_routes(brainsetup.make_routes(
        CFG, save_settings, BRAIN_LOGIN_CMD, on_change=brain_choice_changed,
        env_key=lambda: os.environ.get("GEMINI_API_KEY", "")))
    app.router.add_static("/static", BASE / "web")
    runner = web.AppRunner(app)
    await runner.setup()
    port = CFG["port"]
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
    except OSError:
        log("Already running - just opening the window.")
        open_window(f"http://127.0.0.1:{port}/")
        return
    log(f"Game Guide running on http://127.0.0.1:{port}  brains={CFG['brain_order']}")
    threading.Thread(target=APP.watch_foreground, daemon=True).start()
    threading.Thread(target=fix_shortcuts, daemon=True).start()
    try:
        setup_hotkeys()
    except Exception as e:
        log(f"hotkeys unavailable: {e}")
    try:
        run_tray(port)
    except Exception as e:
        log(f"tray unavailable: {e}")
    try:
        APP.overlay = overlay.Overlay(log, {"next": CFG["hotkey_next"].upper(),
                                            "toggle": CFG["hotkey_overlay"].upper()})
        APP.overlay.set_enabled(bool(CFG.get("overlay", True)))
    except Exception as e:
        log(f"overlay unavailable: {e}")
    if SENSE_IMPORT_ERROR:
        log(f"sense unavailable: {SENSE_IMPORT_ERROR}")
    APP.start_sense()           # the game from last_game.txt was set before the overlay existed
    APP.ensure_dossier()        # research the current game in the background if it has no dossier yet
    if CFG["open_window"] and "--no-window" not in sys.argv:
        open_window(f"http://127.0.0.1:{port}/")
        beep(1000, 80)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass                     # any other crash is written to guide.log by _log_crash
