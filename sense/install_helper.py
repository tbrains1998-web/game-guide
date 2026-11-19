"""Install / repair / uninstall the Sense helper for Phantom Blade Zero.

install_pbz (also used by repair_pbz and install_game for Unreal games) picks one of two paths:

KEEP path - UE4SS is already in the game folder and working. "Working" means all of these:
  ue4ss\\UE4SS.dll is there, dwmapi.dll or dwmapi.dll.off is there, ue4ss\\Mods\\mods.txt is there, and
  ue4ss\\UE4SS_Signatures\\GNatives.lua + GUObjectArray.lua are there and are NOT the zip's stock copies
  (the stock ones only know an old game build, so they count as not working). mods.txt must also be plain
  UTF-8 text (a UTF-16 / "Unicode" mods.txt takes the full path). See _ue4ss_ready().
  If the zip is damaged, the folder still counts as working when its signature files are exactly the
  build-aware ones in sig_src (the keep path needs nothing from the zip). On the full path a damaged zip
  stops the install before any backup or change.
  On this path nothing is deleted and nothing comes out of the zip. Only:
  1. refuse if the game is running
  2. back up dwmapi.dll / dwmapi.dll.off, ue4ss\\Mods\\Sense\\, mods.txt and mods.json (not the whole
     ue4ss folder, it holds big object dumps and crash dumps)
  3. replace ue4ss\\Mods\\Sense (learned\\ and out\\ are never deleted, so a failed copy can not lose them;
     game.txt written)
  4. mods.txt: every line stays as it is; only makes sure there is exactly one "Sense : 1" line
     (a "Sense : 0" becomes 1, or a new line goes in just above the keybinds part, or at the end)
  5. mods.json is rewritten to match mods.txt
  6. dwmapi.dll.off -> dwmapi.dll when only the .off is there (repair after uninstall);
     when both are there the .off is deleted
  The signature files, the settings and every other ue4ss file stay byte for byte the same.

FULL path - anything else (fresh game, no ue4ss, broken or old ue4ss, stock signatures):
  1. refuse if the game is running
  2. back up dwmapi.dll, dwmapi.dll.off and the whole ue4ss\\ folder to backup_dir
  3. extract the UE4SS-for-PBZ zip into the game folder (dwmapi.dll + ue4ss\\, the old ue4ss\\ is replaced),
     then right away write a crash-safe mods.txt + mods.json (the CRASH_OFF_MODS off)
  4. copy the user's own mod folders (anything not in the zip) back from the backup
  5. copy the Sense mod folder into ue4ss\\Mods\\Sense (Scripts\\*.lua, profiles\\, README ...); the old
     learned\\ and out\\ folders were already put back from the backup right after step 3 (what the helper
     learned survives a reinstall, even when a later step fails); write Sense\\game.txt (the game key)
  6. write mods.txt + mods.json: zip mods (keeping the user's old on/off state, except the mods in
     CRASH_OFF_MODS, which crash the game and are always written as 0), user mods, Sense, Keybinds last
  7. copy the signature files that know the current game build (sig_src, default
     sense\\ue4ss-signatures-pbz\\) over ue4ss\\UE4SS_Signatures\\ (last on purpose, see below)
  8. delete dwmapi.dll.off
  If anything fails after the old ue4ss\\ was touched, dwmapi.dll is renamed to dwmapi.dll.off, so a half-done
  install never loads UE4SS. Because the build-aware signatures only go in at step 7, a half-done install still
  has the zip's stock ones, so the next Install/Repair takes the full path again (not the keep path).
Running it twice is fine (the 2nd run takes the KEEP path). Every step is appended to the returned "log"
list, and result["mode"] says which path ran ("keep" or "full").
Only ever call this with the game closed; the caller shows the log to the user in plain words.

RE Engine games (REFramework already installed by the user, dinput8.dll): install_re() copies
  autorun\\sense_re.lua + autorun\\sense_re\\ + data\\sense_re\\profiles\\ into <game>\\reframework and writes
  data\\sense_re\\game.txt. Everything else under reframework\\data (learned files, out file) is kept.
  sense_file = <game>\\reframework\\data\\sense\\sense.json.
Per-game table: GAMES (slug as guide.py's slugify(name) makes it -> engine, key, exe, folder).
install_game(slug) / uninstall_game(slug) / sense_file_for(slug) / is_installed_game(slug) pick the right one.
"""
import json
import os
import re
import shutil
import time
import zipfile
import zlib
from pathlib import Path

BASE = Path(__file__).resolve().parent                      # ...\game-guide\sense
GAME_DIR = None    # no default game folder: it always comes from the caller (see resolve_game_dir)
ZIP_PATH = BASE / "downloads" / "ue4ss-pbz-1.0.2.zip"
HELPER_SRC = BASE / "helper" / "ue4ss" / "Sense"
BACKUP_ROOT = BASE / "backup"
GAME_EXE = "PBZ.exe"
MOD_NAME = "Sense"
PROXY = "dwmapi.dll"
KEYBINDS_COMMENT = "; Built-in keybinds, do not move up!"
MOD_LINE = re.compile(r"^\s*([^;:\s][^:]*?)\s*:\s*([01])\s*$")
UE_KEEP = ("learned", "out")                     # Sense sub-folders that survive a reinstall
GAME_TXT = "game.txt"
SIG_SRC = BASE / "ue4ss-signatures-pbz"           # signature files that also know the current game build
SIG_FILES = ("GNatives.lua", "GUObjectArray.lua")  # ue4ss\UE4SS_Signatures\<name>
# These zip mods crash the game, so the FULL path always writes them as 0 (the KEEP path never changes them).
CRASH_OFF_MODS = ("BPModLoaderMod", "BPML_GenericFunctions", "CheatManagerEnablerMod", "ConsoleCommandsMod",
                  "ConsoleEnablerMod")

RE_HELPER_SRC = BASE / "helper" / "reframework"  # autorun\sense_re.lua, autorun\sense_re\, data\sense_re\profiles\
RE_LOADER = "sense_re.lua"
RE_MODDIR = "sense_re"
RE_PROXY = "dinput8.dll"                          # REFramework's loader dll

# slug (guide.py slugify(game name)) -> how to install the helper for that game. "dir" stays None here:
# each user's game folder comes from the settings (config.json "sense_game_dirs", see resolve_game_dir).
GAMES = {
    "phantom-blade-zero": {"engine": "ue4ss", "key": "pbz", "exe": "PBZ.exe", "title": "Phantom Blade Zero",
                           "dir": None},
    "resident-evil-requiem": {"engine": "reframework", "key": "re9", "exe": "re9.exe",
                              "title": "Resident Evil Requiem", "dir": None},
    "dragons-dogma-2": {"engine": "reframework", "key": "dd2", "exe": "DD2.exe", "title": "Dragon's Dogma 2",
                        "dir": None},
}


# ---------------------------------------------------------------- small helpers
def is_game_running(exe_name=GAME_EXE):
    """True if a process with that exe name is running (psutil; tasklist as fallback)."""
    want = exe_name.lower()
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info.get("name") or "").lower() == want:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False
    except ImportError:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}"],
                             capture_output=True, text=True).stdout
        return want in out.lower()


def sense_file_path(game_dir=GAME_DIR):
    """Where the helper writes its JSON (the value guide.py stores as game.json "sense_file"); None = no folder."""
    if not game_dir:
        return None
    return Path(game_dir) / "ue4ss" / "Mods" / MOD_NAME / "out" / "sense.json"


def is_installed(game_dir=GAME_DIR):
    """True when the proxy dll and the Sense mod folder are both there."""
    if not game_dir:
        return False
    g = Path(game_dir)
    return (g / PROXY).exists() and (g / "ue4ss" / "Mods" / MOD_NAME / "Scripts" / "main.lua").exists()


def _new_backup_dir():
    return BACKUP_ROOT / time.strftime("%Y-%m-%d-%H%M%S")


def _parse_mods_txt(text):
    """-> list of (name, enabled) in file order; comments and blanks are skipped."""
    out = []
    for line in text.lstrip("﻿").splitlines():
        m = MOD_LINE.match(line)
        if m:
            out.append((m.group(1).strip(), m.group(2) == "1"))
    return out


def _mods_txt_bytes_plain(data):
    """True when mods.txt bytes are plain UTF-8 style text (no UTF-16 byte order mark, no NUL bytes)."""
    return not (data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data)


def _decode_mods_txt(data):
    """mods.txt bytes -> text for reading the old on/off state. Handles a UTF-16 ('Unicode') file too."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data:                                   # UTF-16 without a byte order mark
        le = data[1::2].count(0) > data[0::2].count(0)    # NULs in the odd bytes -> little endian
        return data.decode("utf-16-le" if le else "utf-16-be", errors="replace")
    return data.decode("utf-8", errors="replace")


def _zip_mod_names(zf):
    """Mod folder names shipped in the zip (ue4ss/Mods/<Name>/...), 'shared' excluded."""
    names = set()
    for n in zf.namelist():
        parts = n.replace("\\", "/").split("/")
        if len(parts) >= 4 and parts[0] == "ue4ss" and parts[1] == "Mods" and parts[2] != "shared":
            names.add(parts[2])
    return names


def _build_mods_txt(zip_text, old_entries, user_mods, force_off=CRASH_OFF_MODS):
    """New mods.txt: the zip's lines (user's old on/off state wins), then the user's own mods, Sense, Keybinds last.
    Mods named in force_off are always written as 0 (they crash the game)."""
    old_state = dict(old_entries)
    for name in force_off:
        old_state[name] = False
    nl = "\r\n" if "\r\n" in zip_text else "\n"
    lines = []
    for line in zip_text.splitlines():
        m = MOD_LINE.match(line)
        if not m:
            continue                                      # blanks and comments are re-added below
        name = m.group(1).strip()
        if name == "Keybinds" or name == MOD_NAME:
            continue
        on = old_state.get(name, m.group(2) == "1")       # keep what the user had (cheat/console mods stay off)
        lines.append(f"{name} : {1 if on else 0}")
    lines.append("")
    for name in user_mods:
        on = old_state.get(name, True)
        lines.append(f"{name} : {1 if on else 0}")
    lines.append(f"{MOD_NAME} : 1")
    lines.append("")
    lines.append(KEYBINDS_COMMENT)
    lines.append("Keybinds : 1")
    return nl.join(lines) + nl


def _build_mods_json(mods_txt_text):
    """mods.json mirrors mods.txt (UE4SS reads either)."""
    return json.dumps([{"mod_name": n, "mod_enabled": on} for n, on in _parse_mods_txt(mods_txt_text)], indent=4)


def _ue4ss_ready(game_dir, zip_path, sig_src=None):
    """-> (True, reason) when UE4SS is already in the game folder and working, so the KEEP path can be used
    (nothing deleted, nothing extracted); (False, reason) when the FULL path is needed. reason is plain words.
    If the zip can not be read, the folder still counts as ready when its signature files are byte for byte
    the build-aware ones in sig_src (the keep path needs nothing from the zip)."""
    g = Path(game_dir)
    ue = g / "ue4ss"
    if not (ue / "UE4SS.dll").is_file():
        return False, "UE4SS is not installed yet (ue4ss\\UE4SS.dll missing)"
    if not ((g / PROXY).is_file() or (g / (PROXY + ".off")).is_file()):
        return False, "the UE4SS loader (dwmapi.dll) is missing"
    if not (ue / "Mods" / "mods.txt").is_file():
        return False, "ue4ss\\Mods\\mods.txt is missing"
    try:
        mods_bytes = (ue / "Mods" / "mods.txt").read_bytes()
    except OSError as e:
        return False, f"ue4ss\\Mods\\mods.txt could not be read ({type(e).__name__})"
    if not _mods_txt_bytes_plain(mods_bytes):
        return False, ("ue4ss\\Mods\\mods.txt is not saved as plain UTF-8 text (it looks like UTF-16 / "
                       "'Unicode'), so it is written fresh")
    sig_dir = ue / "UE4SS_Signatures"
    have = {}
    for name in SIG_FILES:
        f = sig_dir / name
        if not f.is_file():
            return False, f"the signature file UE4SS_Signatures\\{name} is missing"
        try:
            data = f.read_bytes()
        except OSError as e:
            return False, f"the signature file UE4SS_Signatures\\{name} could not be read ({type(e).__name__})"
        if not data:
            return False, f"the signature file UE4SS_Signatures\\{name} is empty"
        have[name] = data
    stock = {}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in SIG_FILES:
                try:
                    stock[name] = zf.read(f"ue4ss/UE4SS_Signatures/{name}")
                except KeyError:
                    stock[name] = None
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, RuntimeError, EOFError, ValueError) as e:
        # The keep path needs nothing from the zip. If the folder's signature files are exactly the
        # build-aware ones from sig_src, they are known good, so a damaged zip must not block the keep path.
        if sig_src is not None:
            try:
                same = all((Path(sig_src) / n).read_bytes() == have[n] for n in SIG_FILES)
            except OSError:
                same = False
            if same:
                return True, ("UE4SS is already installed and its signature files are the ones for this game "
                              f"build (the UE4SS zip could not be read: {type(e).__name__}, but it is not needed)")
        return False, f"could not read the UE4SS zip to compare the signature files ({type(e).__name__})"
    for name in SIG_FILES:
        data = have[name]
        if stock.get(name) is not None and data == stock[name]:
            return False, (f"UE4SS_Signatures\\{name} is the zip's stock copy, which does not work "
                           f"on the current game build")
    return True, "UE4SS is already installed and set up for this game build"


def _ensure_sense_on(text):
    """mods.txt text -> (new text, changed). Every line stays as it is; only makes sure there is exactly one
    "Sense : 1" line: the first Sense line is turned on, extra Sense lines go, or a new "Sense : 1" goes in
    just above the keybinds part (above the blank lines before the keybinds comment / Keybinds line), or at
    the end when there is neither. The file's line endings are kept."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)

    def body(line):
        return line.rstrip("\r\n")

    def name_of(line):
        m = MOD_LINE.match(body(line).lstrip("﻿"))
        return (m.group(1).strip(), m) if m else (None, None)

    out, found, changed = [], False, False
    for line in lines:
        name, m = name_of(line)
        if name != MOD_NAME:
            out.append(line)
            continue
        if found:                                          # a duplicate Sense line
            changed = True
            continue
        found = True
        if m.group(2) == "1":
            out.append(line)
        else:
            b = body(line)
            off = len(b) - len(b.lstrip("﻿"))
            ending = line[len(b):]
            out.append(b[:off + m.start(2)] + "1" + b[off + m.end(2):] + ending)
            changed = True
    if found:
        return "".join(out), changed

    at = None
    for i, line in enumerate(out):
        if body(line).lstrip("﻿").strip() == KEYBINDS_COMMENT:
            at = i
            break
    if at is None:
        for i, line in enumerate(out):
            if name_of(line)[0] == "Keybinds":
                at = i
                break
    if at is None:
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += nl
        out.append(f"{MOD_NAME} : 1{nl}")
    else:
        while at > 0 and not body(out[at - 1]).lstrip("﻿").strip():   # go above the blank lines first
            at -= 1
        new_line = f"{MOD_NAME} : 1{nl}"
        if at == 0 and out and out[0].startswith("﻿"):   # the BOM must stay at the very start of the file
            out[0] = out[0][1:]
            new_line = "﻿" + new_line
        out.insert(at, new_line)
    return "".join(out), True


def _read_text_exact(path):
    """Read a text file so that writing it back gives the same bytes (odd bytes are kept, not replaced)."""
    return Path(path).read_bytes().decode("utf-8", errors="surrogateescape")


def _write_text_exact(path, text):
    Path(path).write_bytes(text.encode("utf-8", errors="surrogateescape"))


def _safe_extract(zf, dest):
    """Extract every entry under dest; refuse absolute or '..' paths (zip is a download)."""
    dest = Path(dest).resolve()
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"):
            (dest / name).mkdir(parents=True, exist_ok=True)
            continue
        target = (dest / name).resolve()
        if dest not in target.parents:
            raise ValueError(f"unsafe path in zip: {info.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _rm(path):
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _copy_tree(src, dst):
    shutil.copytree(src, dst, dirs_exist_ok=True)


_SKIP = shutil.ignore_patterns("__pycache__", "*.pyc")


def _put(src, dst):
    """Replace dst with a fresh copy of src (file or folder). Old files that are no longer shipped go away."""
    src, dst = Path(src), Path(dst)
    _rm(dst)
    if src.is_dir():
        shutil.copytree(src, dst, ignore=_SKIP)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _write_game_key(folder, key):
    """game.txt = the game key the helper uses to pick profiles\\<key>.json and learned\\<key>.json."""
    Path(folder).mkdir(parents=True, exist_ok=True)
    (Path(folder) / GAME_TXT).write_text(str(key or "").strip(), "utf-8")


def _restore_ue_keep(sense_dir, old_sense_dir, log):
    """Copy learned\\ and out\\ from old_sense_dir (the backup) into sense_dir when sense_dir lacks them."""
    if not old_sense_dir:
        return
    for keep in UE_KEEP:
        old = Path(old_sense_dir) / keep
        if old.is_dir() and not (Path(sense_dir) / keep).exists():
            Path(sense_dir).mkdir(parents=True, exist_ok=True)
            shutil.copytree(old, Path(sense_dir) / keep)
            log.append(f"Kept what the helper had in {keep}\\")


def _clear_sense_but_keep(sense_dir):
    """Delete everything in Sense\\ except the learned\\ and out\\ folders (the user's data)."""
    sense_dir = Path(sense_dir)
    if (sense_dir.exists() or sense_dir.is_symlink()) and (not sense_dir.is_dir() or sense_dir.is_symlink()):
        _rm(sense_dir)
        return
    if sense_dir.is_dir():
        for child in list(sense_dir.iterdir()):
            if child.name in UE_KEEP and child.is_dir() and not child.is_symlink():
                continue
            _rm(child)


def _install_ue_sense(sense_dir, helper_src, game_key, old_sense_dir, log):
    """Copy the multi-file UE helper into sense_dir. Everything the source ships (Scripts\\, profiles\\,
    README, enabled.txt ...) is replaced; learned\\ and out\\ are never taken from the source and come back
    from old_sense_dir (the backup) when they were there before."""
    sense_dir, helper_src = Path(sense_dir), Path(helper_src)
    sense_dir.mkdir(parents=True, exist_ok=True)
    # learned\ and out\ go back FIRST, before the source copy: if a copy below fails, the user's data is
    # already in the game folder again (a later run backs it up again instead of a stripped Sense).
    _restore_ue_keep(sense_dir, old_sense_dir, log)
    for child in sorted(helper_src.iterdir()):
        if child.name in UE_KEEP or child.name == "__pycache__":
            continue
        _put(child, sense_dir / child.name)
    n_lua = len(list((sense_dir / "Scripts").glob("*.lua")))
    has_prof = (sense_dir / "profiles").is_dir()
    log.append(f"Installed the Sense mod ({n_lua} script file(s){', profiles' if has_prof else ''})")
    for keep in UE_KEEP:
        (sense_dir / keep).mkdir(exist_ok=True)
    (sense_dir / "enabled.txt").touch(exist_ok=True)
    _write_game_key(sense_dir, game_key)
    log.append(f"Wrote {GAME_TXT} (game key '{game_key}')")


# ---------------------------------------------------------------- install
def install_pbz(game_dir=GAME_DIR, zip_path=ZIP_PATH, helper_src=HELPER_SRC, backup_dir=None, game_key="pbz",
                sig_src=SIG_SRC):
    """-> {"ok": bool, "mode": "keep" | "full" | None, "sense_file": str, "log": [str, ...],
    "error": str (only when not ok)}. Never raises. See the module text for the KEEP and FULL paths."""
    if not game_dir:
        return dict(_no_dir("Phantom Blade Zero"), mode=None, sense_file="")
    log = []
    game_dir = Path(game_dir)
    zip_path = Path(zip_path)
    helper_src = Path(helper_src)
    sig_src = Path(sig_src) if sig_src else SIG_SRC
    backup_dir = Path(backup_dir) if backup_dir else _new_backup_dir()
    result = {"ok": False, "mode": None, "sense_file": str(sense_file_path(game_dir)), "log": log}

    def fail(msg):
        log.append("STOP: " + msg)
        result["error"] = msg
        return result

    try:
        if is_game_running(GAME_EXE):
            return fail("The game is still running. Close Phantom Blade Zero first, then try again.")
        if not (game_dir / GAME_EXE).exists():
            return fail(f"Game not found: {game_dir / GAME_EXE}")
        if not zip_path.exists():
            return fail(f"UE4SS package missing: {zip_path}. Download 'UE4SS For Phantom Blade Zero' from Nexus "
                        f"and put the zip there.")
        if not (helper_src / "Scripts" / "main.lua").exists():
            return fail(f"Sense mod files missing: {helper_src}")

        ready, why = _ue4ss_ready(game_dir, zip_path, sig_src)
        if ready:
            result["mode"] = "keep"
            log.append(f"Keeping the UE4SS that is already there: {why}. Only the Sense mod is replaced.")
            ok = _install_keep(game_dir, helper_src, backup_dir, game_key, log)
        else:
            result["mode"] = "full"
            log.append(f"Full UE4SS install: {why}.")
            missing = [n for n in SIG_FILES if not (sig_src / n).is_file() or (sig_src / n).stat().st_size == 0]
            if missing:
                return fail("The signature files for the current game build are missing: "
                            + ", ".join(str(sig_src / n) for n in missing) + ". Nothing was changed.")
            ok = _install_full(game_dir, zip_path, helper_src, backup_dir, game_key, sig_src, log, fail)
        if ok is not True:
            return ok                                         # a fail() result from the full path
        result["ok"] = True
        log.append("Done. Start the game once; the helper writes " + result["sense_file"])
        return result
    except Exception as e:                                   # never raise into the web route
        return fail(f"Install failed: {type(e).__name__}: {e}")


def _backup_proxy(game_dir, backup_dir, log):
    for f in (Path(game_dir) / PROXY, Path(game_dir) / (PROXY + ".off")):
        if f.exists():
            shutil.copy2(f, Path(backup_dir) / f.name)
            log.append(f"Backed up {f.name}")


def _disable_proxy(game_dir, log):
    """After a failed full install: dwmapi.dll -> dwmapi.dll.off, so the game starts without UE4SS. Never raises."""
    proxy = Path(game_dir) / PROXY
    proxy_off = Path(game_dir) / (PROXY + ".off")
    try:
        if proxy.exists():
            if proxy_off.exists():
                proxy_off.unlink()
            proxy.rename(proxy_off)
            log.append("The install did not finish, so UE4SS was turned off (dwmapi.dll -> dwmapi.dll.off). "
                       "The game starts without mods; run Repair to try again.")
    except OSError as e:
        log.append(f"Could not turn UE4SS off after the failed install ({type(e).__name__}: {e}). "
                   "Rename dwmapi.dll in the game folder to dwmapi.dll.off by hand before playing.")


def _install_keep(game_dir, helper_src, backup_dir, game_key, log):
    """KEEP path: UE4SS is already there and working. Nothing is deleted or extracted; only Sense, the Sense
    line in mods.txt, mods.json and the proxy name are touched. -> True."""
    mods_dir = game_dir / "ue4ss" / "Mods"
    sense_dir = mods_dir / MOD_NAME
    proxy = game_dir / PROXY
    proxy_off = game_dir / (PROXY + ".off")
    txt = mods_dir / "mods.txt"
    js = mods_dir / "mods.json"

    # 1. backup (small things only, not the whole ue4ss folder) ----------------------
    backup_dir.mkdir(parents=True, exist_ok=True)
    _backup_proxy(game_dir, backup_dir, log)
    b_mods = backup_dir / "ue4ss" / "Mods"
    b_mods.mkdir(parents=True, exist_ok=True)
    had_sense = sense_dir.is_dir()
    if had_sense:
        _rm(b_mods / MOD_NAME)
        _copy_tree(sense_dir, b_mods / MOD_NAME)
        log.append("Backed up the Sense mod folder")
    for f in (txt, js):
        if f.is_file():
            shutil.copy2(f, b_mods / f.name)
            log.append(f"Backed up {f.name}")
    log.append(f"Backup folder: {backup_dir}")

    # 2. the Sense mod. learned\ and out\ are never deleted from the game folder (so a failed copy can
    # not lose them); everything else in Sense\ is replaced from the source. -------------------------
    _clear_sense_but_keep(sense_dir)
    _install_ue_sense(sense_dir, helper_src, game_key, (b_mods / MOD_NAME) if had_sense else None, log)

    # 3. mods.txt: only the Sense line; mods.json mirrors it --------------------------
    old_text = _read_text_exact(txt)
    new_text, changed = _ensure_sense_on(old_text)
    if changed:
        _write_text_exact(txt, new_text)
        log.append("Turned Sense on in mods.txt (every other line left as it was)")
    else:
        log.append("mods.txt already had Sense on; left it as it was")
    js.write_text(_build_mods_json(new_text) + "\n", "utf-8")
    log.append("Updated mods.json to match mods.txt")

    # 4. the proxy: back on after an uninstall, or the stale .off goes ----------------
    if not proxy.exists() and proxy_off.exists():
        proxy_off.rename(proxy)
        log.append("Turned the UE4SS proxy back on (dwmapi.dll.off -> dwmapi.dll)")
    elif proxy.exists() and proxy_off.exists():
        proxy_off.unlink()
        log.append("Removed dwmapi.dll.off")
    log.append("UE4SS itself, its settings and the signature files were not changed")
    return True


def _install_full(game_dir, zip_path, helper_src, backup_dir, game_key, sig_src, log, fail):
    """FULL path: back up and replace ue4ss\\ from the zip, then the build-aware signatures, the user's mods,
    Sense and mods.txt/json (crash mods off). -> True, or the fail() result."""
    ue4ss_dir = game_dir / "ue4ss"
    mods_dir = ue4ss_dir / "Mods"
    proxy = game_dir / PROXY
    proxy_off = game_dir / (PROXY + ".off")

    # 0. check the zip BEFORE anything is backed up or changed: a damaged download must not cost a big
    # backup copy of ue4ss\ (object dumps, crash dumps) on every click -----------------------------------
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            names = set(zf.namelist())
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, RuntimeError, EOFError, ValueError) as e:
        return fail(f"The UE4SS zip could not be opened ({type(e).__name__}: {e}). Download it again: "
                    f"{zip_path}. Nothing was changed.")
    if bad:
        return fail(f"The zip is damaged ({bad}). Download it again. Nothing was changed.")
    if "ue4ss/Mods/mods.txt" not in names:
        return fail("The zip has no ue4ss/Mods/mods.txt. Wrong package? Nothing was changed.")

    # 1. backup -----------------------------------------------------------
    backup_dir.mkdir(parents=True, exist_ok=True)
    _backup_proxy(game_dir, backup_dir, log)
    if ue4ss_dir.exists():
        _rm(backup_dir / "ue4ss")
        _copy_tree(ue4ss_dir, backup_dir / "ue4ss")
        log.append("Backed up the ue4ss folder")
    log.append(f"Backup folder: {backup_dir}")

    # remember the user's old mod list before anything is replaced
    old_txt = mods_dir / "mods.txt"
    old_entries = _parse_mods_txt(_decode_mods_txt(old_txt.read_bytes())) if old_txt.is_file() else []
    old_folders = {p.name for p in mods_dir.iterdir() if p.is_dir() and p.name != "shared"} if mods_dir.exists() else set()

    # 2. extract the zip (old ue4ss\ goes away first, it is in the backup) --
    # From the moment the old ue4ss\ is touched, any error turns the proxy off (dwmapi.dll -> .off), so a
    # half-done install never loads UE4SS on the next game start. Repair runs the full path again.
    touched = False
    try:
        with zipfile.ZipFile(zip_path) as zf:              # already tested in step 0
            zip_mods = _zip_mod_names(zf)
            zip_mods_txt = zf.read("ue4ss/Mods/mods.txt").decode("utf-8", errors="replace")
            touched = True
            if ue4ss_dir.exists():
                _rm(ue4ss_dir)
                log.append("Removed the old ue4ss folder")
            _safe_extract(zf, game_dir)
        log.append(f"Extracted {zip_path.name} into {game_dir}")
        # what the helper learned goes back right away, so a failure in a later step can not lose it
        _restore_ue_keep(mods_dir / MOD_NAME, backup_dir / "ue4ss" / "Mods" / MOD_NAME, log)
        if not proxy.exists() or not (ue4ss_dir / "UE4SS.dll").exists():
            _disable_proxy(game_dir, log)
            return fail("The zip did not contain dwmapi.dll and ue4ss\\UE4SS.dll. Wrong package?")

        # 3. right away: a crash-safe mods.txt/json (crash mods off), before the riskier steps
        safe_txt = _build_mods_txt(zip_mods_txt, old_entries, [])
        (mods_dir / "mods.txt").write_text(safe_txt, "utf-8", newline="")
        (mods_dir / "mods.json").write_text(_build_mods_json(safe_txt) + "\n", "utf-8")
        log.append("Wrote a crash-safe mods.txt and mods.json (" + ", ".join(CRASH_OFF_MODS) + " off)")

        # 4. the user's own mods come back from the backup ----------------------
        user_mods = []
        for name in sorted(old_folders - zip_mods - {MOD_NAME}):
            src = backup_dir / "ue4ss" / "Mods" / name
            if src.is_dir():
                _copy_tree(src, mods_dir / name)
                user_mods.append(name)
                log.append(f"Kept your mod: {name}")
        for name, on in old_entries:                   # mods listed in the old file but without a folder
            if name not in zip_mods and name not in user_mods and name not in (MOD_NAME, "Keybinds"):
                user_mods.append(name)

        # 5. the Sense mod (learned\ and out\ come back from the backup) ----------
        sense_dir = mods_dir / MOD_NAME
        _clear_sense_but_keep(sense_dir)              # learned\ and out\ (restored above) stay
        _install_ue_sense(sense_dir, helper_src, game_key, backup_dir / "ue4ss" / "Mods" / MOD_NAME, log)

        # 6. mods.txt + mods.json (now with the user's mods and Sense) ----------
        new_txt = _build_mods_txt(zip_mods_txt, old_entries, user_mods)
        (mods_dir / "mods.txt").write_text(new_txt, "utf-8", newline="")
        (mods_dir / "mods.json").write_text(_build_mods_json(new_txt) + "\n", "utf-8")
        log.append("Updated mods.txt and mods.json (Sense on; " + ", ".join(CRASH_OFF_MODS)
                   + " off because they crash the game)")

        # 7. signature files that know the current game build. Done last on purpose: until this step the
        # folder still has the zip's stock signatures, so if anything above failed, the next Install/Repair
        # sees "not ready" and runs the full path again (it does not take the keep path on a half install).
        sig_dir = ue4ss_dir / "UE4SS_Signatures"
        sig_dir.mkdir(parents=True, exist_ok=True)
        for name in SIG_FILES:
            shutil.copy2(sig_src / name, sig_dir / name)
        log.append("Copied the signature files for the current game build (" + ", ".join(SIG_FILES)
                   + ") into ue4ss\\UE4SS_Signatures")

        # 8. the old disabled proxy is gone now ---------------------------------
        if proxy_off.exists():
            proxy_off.unlink()
            log.append("Removed dwmapi.dll.off")
    except Exception:
        if touched:
            _disable_proxy(game_dir, log)
        raise
    return True


def repair_pbz(game_dir=GAME_DIR, zip_path=ZIP_PATH, helper_src=HELPER_SRC, backup_dir=None, game_key="pbz",
               sig_src=SIG_SRC):
    """Repair = install again (it is safe to run on top of an install)."""
    return install_pbz(game_dir, zip_path, helper_src, backup_dir, game_key, sig_src)


# ---------------------------------------------------------------- uninstall
def uninstall_pbz(game_dir=GAME_DIR, backup_dir=None):
    """Turn the proxy off (dwmapi.dll -> dwmapi.dll.off, the game then starts clean) and remove Mods\\Sense.
    Everything else (UE4SS, the user's mods) stays. -> {"ok", "log", "error"?}."""
    if not game_dir:
        return _no_dir("Phantom Blade Zero")
    log = []
    game_dir = Path(game_dir)
    result = {"ok": False, "log": log}

    def fail(msg):
        log.append("STOP: " + msg)
        result["error"] = msg
        return result

    try:
        if is_game_running(GAME_EXE):
            return fail("The game is still running. Close Phantom Blade Zero first, then try again.")
        proxy = game_dir / PROXY
        proxy_off = game_dir / (PROXY + ".off")
        mods_dir = game_dir / "ue4ss" / "Mods"
        sense_dir = mods_dir / MOD_NAME

        if backup_dir and sense_dir.exists():              # keep the discovery dump etc. if asked
            backup_dir = Path(backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)
            _copy_tree(sense_dir, backup_dir / MOD_NAME)
            log.append(f"Backed up the Sense folder to {backup_dir}")

        if proxy.exists():
            if proxy_off.exists():
                proxy_off.unlink()
            proxy.rename(proxy_off)
            log.append("Turned the UE4SS proxy off (dwmapi.dll -> dwmapi.dll.off)")
        else:
            log.append("dwmapi.dll was not there (already off)")

        if sense_dir.exists():
            _rm(sense_dir)
            log.append("Removed the Sense mod folder")
        else:
            log.append("Sense mod folder was not there")

        txt = mods_dir / "mods.txt"
        if txt.exists():
            old = _read_text_exact(txt)                    # exact read: the file's \r\n line endings stay
            bom = "﻿" if old.startswith("﻿") else ""
            if bom:
                old = old[1:]                              # a BOM (Notepad) is not part of the first mod name

            def is_sense(l):
                m = MOD_LINE.match(l.rstrip("\r\n"))
                return bool(m) and m.group(1).strip() == MOD_NAME

            kept = [l for l in old.splitlines(keepends=True) if not is_sense(l)]
            _write_text_exact(txt, bom + "".join(kept))    # the BOM stays at the very start of the file
            log.append("Removed Sense from mods.txt")
        js = mods_dir / "mods.json"
        if js.exists():
            try:
                data = [m for m in json.loads(js.read_text("utf-8")) if m.get("mod_name") != MOD_NAME]
                js.write_text(json.dumps(data, indent=4) + "\n", "utf-8")
                log.append("Removed Sense from mods.json")
            except (ValueError, AttributeError):
                log.append("mods.json could not be read; left as is")

        result["ok"] = True
        log.append("Done. The game starts without UE4SS now.")
        return result
    except Exception as e:
        return fail(f"Uninstall failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- RE Engine (REFramework)
def sense_file_re(game_dir):
    """Where the RE helper writes its JSON: <game>\\reframework\\data\\sense\\sense.json. None = no folder."""
    if not game_dir:
        return None
    return Path(game_dir) / "reframework" / "data" / "sense" / "sense.json"


def is_installed_re(game_dir):
    """True when REFramework's dll and our loader script are both there."""
    if not game_dir:
        return False
    g = Path(game_dir)
    return (g / RE_PROXY).exists() and (g / "reframework" / "autorun" / RE_LOADER).is_file()


def install_re(game_dir, game_key, exe, helper_src=RE_HELPER_SRC, backup_dir=None):
    """Copy the RE helper into <game>\\reframework. REFramework itself must already be installed (we never
    install or change it). Replaced: autorun\\sense_re.lua, autorun\\sense_re\\, data\\sense_re\\profiles\\
    (+ any other file the source ships under data\\sense_re except learned). Kept: everything else, e.g.
    data\\sense_re\\learned\\, data\\sense\\ and the user's other scripts.
    -> {"ok", "sense_file", "log", "error"?} (same shape as install_pbz)."""
    if not game_dir:
        return dict(_no_dir(exe), sense_file="")
    log = []
    game_dir = Path(game_dir)
    helper_src = Path(helper_src)
    backup_dir = Path(backup_dir) if backup_dir else _new_backup_dir()
    result = {"ok": False, "sense_file": str(sense_file_re(game_dir)), "log": log}

    def fail(msg):
        log.append("STOP: " + msg)
        result["error"] = msg
        return result

    try:
        if is_game_running(exe):
            return fail(f"The game is still running. Close it first ({exe}), then try again.")
        if not (game_dir / exe).exists():
            return fail(f"Game not found: {game_dir / exe}")
        rf = game_dir / "reframework"
        if not (game_dir / RE_PROXY).exists():
            return fail(f"REFramework is not installed in {game_dir} ({RE_PROXY} missing). Install REFramework "
                        f"for this game first, start the game once, then try again.")
        src_loader = helper_src / "autorun" / RE_LOADER
        src_mods = helper_src / "autorun" / RE_MODDIR
        src_data = helper_src / "data" / RE_MODDIR
        if not src_loader.is_file() or not src_mods.is_dir():
            return fail(f"Sense helper files missing: {src_loader} / {src_mods}")

        autorun = rf / "autorun"
        data = rf / "data"
        dst_loader = autorun / RE_LOADER
        dst_mods = autorun / RE_MODDIR
        dst_data = data / RE_MODDIR

        # 1. backup our old files (only ours; REFramework and other scripts are not touched) ----------
        backup_dir.mkdir(parents=True, exist_ok=True)
        if dst_loader.exists():
            shutil.copy2(dst_loader, backup_dir / RE_LOADER)
        if dst_mods.is_dir():
            _rm(backup_dir / "autorun" / RE_MODDIR)
            _copy_tree(dst_mods, backup_dir / "autorun" / RE_MODDIR)
        if dst_data.is_dir():
            _rm(backup_dir / "data" / RE_MODDIR)
            _copy_tree(dst_data, backup_dir / "data" / RE_MODDIR)
        log.append(f"Backup folder: {backup_dir}")

        # 2. scripts ------------------------------------------------------------------------------------
        autorun.mkdir(parents=True, exist_ok=True)
        _put(src_mods, dst_mods)
        _put(src_loader, dst_loader)                 # loader last: it never runs without its modules
        n_lua = len(list(dst_mods.rglob("*.lua")))
        log.append(f"Installed the Sense scripts (autorun\\{RE_LOADER} + {n_lua} module file(s))")

        # 3. profiles (+ other shipped data files, never learned\) ------------------------------------
        dst_data.mkdir(parents=True, exist_ok=True)
        if src_data.is_dir():
            for child in sorted(src_data.iterdir()):
                if child.name in ("learned", "__pycache__", GAME_TXT):
                    continue
                _put(child, dst_data / child.name)
            if (dst_data / "profiles").is_dir():
                log.append("Installed the game profiles")
        if (dst_data / "learned").exists() or any(dst_data.glob("*learned*")):
            log.append("Kept what the helper learned before")
        _write_game_key(dst_data, game_key)
        log.append(f"Wrote data\\{RE_MODDIR}\\{GAME_TXT} (game key '{game_key}')")

        # 4. the folder the helper writes sense.json into (guide.py only reads when it exists) ----------
        sense_file_re(game_dir).parent.mkdir(parents=True, exist_ok=True)

        result["ok"] = True
        log.append("Done. Start the game once; the helper writes " + result["sense_file"])
        return result
    except Exception as e:
        return fail(f"Install failed: {type(e).__name__}: {e}")


def uninstall_re(game_dir, exe, backup_dir=None):
    """Remove autorun\\sense_re.lua + autorun\\sense_re\\ (the game then runs without the helper).
    REFramework, the user's other scripts and reframework\\data (profiles, learned) stay.
    -> {"ok", "log", "error"?}."""
    if not game_dir:
        return _no_dir(exe)
    log = []
    game_dir = Path(game_dir)
    result = {"ok": False, "log": log}

    def fail(msg):
        log.append("STOP: " + msg)
        result["error"] = msg
        return result

    try:
        if is_game_running(exe):
            return fail(f"The game is still running. Close it first ({exe}), then try again.")
        autorun = game_dir / "reframework" / "autorun"
        loader = autorun / RE_LOADER
        mods = autorun / RE_MODDIR
        if backup_dir and (loader.exists() or mods.exists()):
            backup_dir = Path(backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)
            if loader.exists():
                shutil.copy2(loader, backup_dir / RE_LOADER)
            if mods.is_dir():
                _copy_tree(mods, backup_dir / "autorun" / RE_MODDIR)
            log.append(f"Backed up the Sense scripts to {backup_dir}")
        if loader.exists() or mods.exists():
            _rm(loader)                              # loader first: no half-loaded helper
            _rm(mods)
            log.append("Removed the Sense scripts from reframework\\autorun")
        else:
            log.append("The Sense scripts were not there")
        result["ok"] = True
        log.append("Done. REFramework and your other scripts were left as they were.")
        return result
    except Exception as e:
        return fail(f"Uninstall failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- per-game dispatch
def _slug(name):
    # same rule as guide.py's slugify: apostrophes go, other runs of non-letters become one dash
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"['’]", "", str(name or "").lower())).strip("-")


def game_info(name_or_slug):
    """-> a copy of GAMES[slug] plus "slug", or None when there is no helper for that game.
    Takes the slug or the game name (slugified the same way as guide.py's slugify)."""
    slug = _slug(name_or_slug)
    g = GAMES.get(slug)
    if not g:
        return None
    out = dict(g)
    out["slug"] = slug
    return out


def resolve_game_dir(name_or_slug, dirs):
    """The game folder for that game from the settings ({slug: folder}) as a Path, or None when it is not set."""
    d = dirs.get(_slug(name_or_slug)) if isinstance(dirs, dict) else None
    return Path(d.strip()) if isinstance(d, str) and d.strip() else None


def _no_dir(name_or_slug):
    msg = (f"The game folder for {name_or_slug or 'this game'} is not set. Add it to config.json under "
           f"\"sense_game_dirs\" (game slug -> the folder that holds the game's exe).")
    return {"ok": False, "log": ["STOP: " + msg], "error": msg}


def sense_file_for(name_or_slug, game_dir=None):
    """Where that game's helper writes sense.json (str), or None for an unknown game or no game folder."""
    g = game_info(name_or_slug)
    if not g:
        return None
    d = Path(game_dir) if game_dir else g["dir"]
    if not d:
        return None
    return str(sense_file_path(d) if g["engine"] == "ue4ss" else sense_file_re(d))


def is_installed_game(name_or_slug, game_dir=None):
    g = game_info(name_or_slug)
    if not g:
        return False
    d = Path(game_dir) if game_dir else g["dir"]
    if not d:
        return False
    return is_installed(d) if g["engine"] == "ue4ss" else is_installed_re(d)


def install_game(name_or_slug, game_dir=None, backup_dir=None, zip_path=ZIP_PATH, helper_src=None,
                 sig_src=SIG_SRC):
    """Install (or repair) the right helper for that game. Same result shape as install_pbz."""
    g = game_info(name_or_slug)
    if not g:
        msg = f"There is no Sense helper for {name_or_slug or 'this game'} yet."
        return {"ok": False, "log": ["STOP: " + msg], "error": msg}
    d = Path(game_dir) if game_dir else g["dir"]
    if not d:
        return _no_dir(name_or_slug)
    if g["engine"] == "ue4ss":
        return install_pbz(game_dir=d, zip_path=zip_path, helper_src=helper_src or HELPER_SRC,
                           backup_dir=backup_dir, game_key=g["key"], sig_src=sig_src)
    return install_re(d, g["key"], g["exe"], helper_src=helper_src or RE_HELPER_SRC, backup_dir=backup_dir)


def uninstall_game(name_or_slug, game_dir=None, backup_dir=None):
    """Remove the helper for that game. Same result shape as uninstall_pbz."""
    g = game_info(name_or_slug)
    if not g:
        msg = f"There is no Sense helper for {name_or_slug or 'this game'} yet."
        return {"ok": False, "log": ["STOP: " + msg], "error": msg}
    d = Path(game_dir) if game_dir else g["dir"]
    if not d:
        return _no_dir(name_or_slug)
    if g["engine"] == "ue4ss":
        return uninstall_pbz(game_dir=d, backup_dir=backup_dir)
    return uninstall_re(d, g["exe"], backup_dir=backup_dir)


if __name__ == "__main__":       # python install_helper.py [install|uninstall|status] [game slug, default PBZ]
    import sys
    what = sys.argv[1] if len(sys.argv) > 1 else "status"
    which = sys.argv[2] if len(sys.argv) > 2 else "phantom-blade-zero"
    if what == "install":
        r = install_game(which)
    elif what == "uninstall":
        r = uninstall_game(which)
    else:
        gi = game_info(which)
        r = {"ok": bool(gi), "log": [f"game: {which} ({gi['engine'] if gi else 'no helper'})",
                                     f"installed: {is_installed_game(which)}", f"sense_file: {sense_file_for(which)}",
                                     f"game running: {is_game_running(gi['exe']) if gi else '-'}"]}
    print("\n".join(r["log"]))
    sys.exit(0 if r["ok"] else 1)
