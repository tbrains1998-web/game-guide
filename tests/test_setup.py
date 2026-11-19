"""Setup and settings checks: no fixed folders of one PC, plugins are switched off for the hidden
Claude runs without naming any, settings have plain defaults, the launchers find Python themselves.
Uses temp folders only (no window, no game, no app, no real Claude settings).
Run: python tests/test_setup.py   (exit 0 = all PASS)"""
import ast
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        fails.append(name)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tmp = Path(tempfile.mkdtemp(prefix="gg_setup_"))
try:
    # ------------------------------------------------------------ Claude plugins off, none named
    import brains
    fn = getattr(brains, "no_plugins_settings", None)
    check("brains.no_plugins_settings exists", callable(fn))
    if callable(fn):
        cfg = tmp / "claude-config"
        (cfg / "plugins").mkdir(parents=True)
        (cfg / "settings.json").write_text(json.dumps({"enabledPlugins": {"talker@market-a": True,
                                                                          "helper@market-b": False}}))
        (cfg / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"extra@market-c": [{"scope": "user"}]}}))
        got = json.loads(fn(cfg))
        check("every listed plugin is switched off",
              got == {"enabledPlugins": {"talker@market-a": False, "helper@market-b": False, "extra@market-c": False}},
              got)
        empty = tmp / "empty-config"
        empty.mkdir()
        check("no Claude settings -> an empty off list", json.loads(fn(empty)) == {"enabledPlugins": {}})
        (empty / "settings.json").write_text("{ not json")
        try:
            check("broken settings file -> no crash", json.loads(fn(empty)) == {"enabledPlugins": {}})
        except Exception as e:
            check("broken settings file -> no crash", False, repr(e))
    src = (ROOT / "brains.py").read_text(encoding="utf-8")
    check("brains.py names no plugin (no 'name@market' strings)",
          not any("@" in s.value and "." not in s.value.split("@")[1]
                  for s in ast.walk(ast.parse(src)) if isinstance(s, ast.Constant) and isinstance(s.value, str)
                  and " " not in s.value and s.value.count("@") == 1))

    # ------------------------------------------------------------ Sense game folders come from settings
    ih = load(ROOT / "sense" / "install_helper.py", "gg_install_helper")
    check("no game entry has a fixed folder", all(not g.get("dir") for g in ih.GAMES.values()),
          [g.get("dir") for g in ih.GAMES.values()])
    check("no default game folder", not ih.GAME_DIR)
    rd = getattr(ih, "resolve_game_dir", None)
    check("install_helper.resolve_game_dir exists", callable(rd))
    if callable(rd):
        game = tmp / "Some Game"
        game.mkdir()
        check("folder not set -> None", rd("phantom-blade-zero", {}) is None)
        check("folder not set (no settings) -> None", rd("phantom-blade-zero", None) is None)
        check("folder from the settings", rd("Phantom Blade Zero", {"phantom-blade-zero": str(game)}) == game)
        check("blank folder -> None", rd("phantom-blade-zero", {"phantom-blade-zero": "  "}) is None)
    for what, call in (("install", ih.install_game), ("uninstall", ih.uninstall_game)):
        try:
            r = call("phantom-blade-zero")
            check(f"{what} without a game folder stops with a plain message",
                  r.get("ok") is False and "folder" in (r.get("error") or "").lower(), r.get("error"))
        except Exception as e:
            check(f"{what} without a game folder stops with a plain message", False, repr(e))
    check("sense_file_for without a folder -> None", ih.sense_file_for("phantom-blade-zero") is None)
    check("is_installed_game without a folder -> False", ih.is_installed_game("phantom-blade-zero") is False)

    # ------------------------------------------------------------ offline voice model folder
    import screenread
    kp = getattr(screenread, "kokoro_paths", None)
    check("screenread.kokoro_paths exists", callable(kp))
    if callable(kp):
        onnx, voices = kp(None)
        check("default model folder is models\\ in the app folder",
              onnx == ROOT / "models" / "kokoro-v1.0.onnx" and voices == ROOT / "models" / "voices-v1.0.bin",
              (onnx, voices))
        onnx, voices = kp(str(tmp))
        check("model folder from the settings", onnx.parent == tmp and voices.parent == tmp)

    # ------------------------------------------------------------ settings: defaults + example file
    tree = ast.parse((ROOT / "guide.py").read_text(encoding="utf-8"))
    defaults = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", "") == "DEFAULTS" for t in n.targets))
    for key, want in (("kokoro_models_dir", ""), ("extra_vocab_file", ""), ("sense_game_dirs", {})):
        check(f"setting {key} has a plain default", defaults.get(key, "missing") == want, defaults.get(key, "missing"))
    ex = ROOT / "config.example.json"
    check("config.example.json exists", ex.is_file())
    if ex.is_file():
        example = json.loads(ex.read_text(encoding="utf-8"))
        check("example keys are all known settings", set(example) <= set(defaults), set(example) - set(defaults))
        check("example holds no key", example.get("gemini_api_key", "") == "")

    # ------------------------------------------------------------ launchers find Python themselves
    for f in ("Start-GameGuide.bat", "install.ps1"):
        t = (ROOT / f).read_text(encoding="utf-8")
        check(f"{f}: no fixed Python folder", "Python312" not in t)
        check(f"{f}: uses the py launcher or PATH", "pyw" in t.lower() or "where " in t.lower() or "get-command" in t.lower())
    req = ROOT / "requirements.txt"
    check("requirements.txt exists", req.is_file())
    if req.is_file():
        lines = [l.strip() for l in req.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
        check("every requirement is pinned", lines and all("==" in l for l in lines), [l for l in lines if "==" not in l])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("RESULT: " + ("ALL PASS" if not fails else f"{len(fails)} FAIL"))
sys.exit(1 if fails else 0)
