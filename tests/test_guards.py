"""Guard checks: the local server refuses requests from other web sites, every hidden Claude run has the
plugins switched off, a bad start gives a plain message, the test runner's exit code is honest, game names
with an apostrophe find their Sense helper, the GPU speech packages are listed.
Temp folders only. guide.py is only started as a --child in a temp copy that stops while loading its
settings (no window, no hotkeys, no tray). No real Claude settings are read.
Run: python tests/test_guards.py   (exit 0 = all PASS)"""
import ast
import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from _repo_files import repo_files  # noqa: E402

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


def guide_func(name):
    """One plain function from guide.py, without importing guide.py (that would start the app)."""
    tree = ast.parse((ROOT / "guide.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {"re": re, "os": os, "Path": Path, "tempfile": tempfile, "time": __import__("time")}
    exec(compile(ast.Module([node], []), "guide.py", "exec"), ns)
    return ns[name]


def app_copy(dest):
    """The app files (no tests, no user data) in a temp folder."""
    shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns(
        "tests", "games", "models", ".edge-profile", "__pycache__", ".git", ".venv", "venv", "config.json*",
        "*.log", "startup_diag.txt", "backup", "downloads"))
    return dest


tmp = Path(tempfile.mkdtemp(prefix="gg_guards_"))
os.environ["CLAUDE_CONFIG_DIR"] = str(tmp / "claude-config")      # never the real Claude settings
try:
    # ------------------------------------------------------------ every hidden Claude run: plugins off
    cfgdir = tmp / "claude-config"
    cfgdir.mkdir()
    (cfgdir / "settings.json").write_text(json.dumps({"enabledPlugins": {"talker@market-a": True}}))
    import screenread
    seen = {}

    class Done:
        stdout = json.dumps({"result": "Hello there, traveller.", "is_error": False})

    real_run = screenread.subprocess.run
    screenread.subprocess.run = lambda cmd, **kw: (seen.setdefault("cmd", cmd), Done())[1]
    try:
        text = screenread.text_via_claude("claude", str(tmp / "shot.png"), "Some Game")
    finally:
        screenread.subprocess.run = real_run
    cmd = seen.get("cmd") or []
    check("screen read with Claude returns the text", text == "Hello there, traveller.", text)
    check("screen read with Claude switches plugins off",
          "--settings" in cmd and json.loads(cmd[cmd.index("--settings") + 1]) ==
          {"enabledPlugins": {"talker@market-a": False}}, cmd[-4:])
    # every claude command list in the app (marked by --strict-mcp-config) also has --settings
    missing = []
    for f, rel in repo_files(".py"):                     # only files git would add: never a .venv or user data
        if "tests" in rel.parts:
            continue
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8", errors="replace"))):
            if isinstance(n, ast.List):
                consts = [e.value for e in n.elts if isinstance(e, ast.Constant)]
                if "--strict-mcp-config" in consts and "--settings" not in consts:
                    missing.append(f"{f.name}:{n.lineno}")
    check("every hidden Claude run passes --settings (plugins off)", not missing, missing)
    # that scan (and the privacy scan) only reads files git would add: a .venv or user folder in the clone is skipped
    rf = load(ROOT / "tests" / "_repo_files.py", "_repo_files_copy")
    fake = tmp / "fake-clone"
    for rel, data in ((".gitignore", (ROOT / ".gitignore").read_bytes()), ("guide.py", b"x = 1\n"),
                      (".venv/Lib/site-packages/bad.py", b"s = '\xff\xfe'\n"), ("venv/x.py", b"\xff"),
                      ("games/some-game/notes.py", b"\xff"), ("sense/backup/old.py", b"\xff"),
                      ("config.json", b"{}"),
                      ("env/pyvenv.cfg", b"home = x\n"), ("env/Lib/site-packages/bad.py", b"\xff")):  # venv, any name
        (fake / rel).parent.mkdir(parents=True, exist_ok=True)
        (fake / rel).write_bytes(data)
    rf.ROOT = fake
    got = sorted(r.as_posix() for r in rf.walk_files())
    check("repo file list without git skips .venv, venv, a venv under any name, games, sense/backup, config.json",
          got == [".gitignore", "guide.py"], got)
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=fake, capture_output=True)
        got = sorted(r.as_posix() for _p, r in rf.repo_files())
        check("repo file list with git skips the same", got == [".gitignore", "guide.py"], got)

    # ------------------------------------------------------------ local server: only its own window
    import brainsetup
    from aiohttp import web
    from aiohttp.test_utils import TestServer, TestClient
    guard = getattr(brainsetup, "local_only", None)
    check("brainsetup.local_only exists", guard is not None)

    async def server_checks():
        app = web.Application(middlewares=[guard])
        hits = []

        async def act(request):
            hits.append(request.path)
            return web.json_response({"ok": True})
        app.router.add_post("/api/act", act)
        app.router.add_get("/api/look", act)
        async with TestClient(TestServer(app, host="127.0.0.1")) as c:
            own = f"127.0.0.1:{c.port}"
            res = {}
            r = await c.post("/api/act", data="{}", headers={"Content-Type": "text/plain",
                                                             "Origin": "https://evil.example"})
            res["other site, text/plain"] = r.status
            r = await c.post("/api/act", json={}, headers={"Origin": "https://evil.example"})
            res["other site, JSON"] = r.status
            r = await c.post("/api/act", json={}, headers={"Origin": "null"})
            res["origin null"] = r.status
            r = await c.post("/api/act", data="{}", headers={"Content-Type": "text/plain"})
            res["no origin, text/plain"] = r.status
            r = await c.post("/api/act", data="{}", headers={"Content-Type": "application/x-www-form-urlencoded",
                                                             "Origin": "http://" + own})
            res["own window, form post"] = r.status
            r = await c.get("/api/look", headers={"Host": f"evil.example:{c.port}"})
            res["other host name (DNS rebinding)"] = r.status
            r = await c.post("/api/act", json={}, headers={"Host": f"evil.example:{c.port}",
                                                           "Origin": f"http://evil.example:{c.port}"})
            res["other host name, POST"] = r.status
            r = await c.post("/api/act", json={}, headers={"Origin": "http://" + own})
            res["own window, JSON"] = r.status
            r = await c.post("/api/act", json={}, headers={"Host": f"localhost:{c.port}",
                                                           "Origin": f"http://localhost:{c.port}"})
            res["own window via localhost"] = r.status
            r = await c.post("/api/act", json={})
            res["no origin (not a browser), JSON"] = r.status
            r = await c.get("/api/look")
            res["own GET"] = r.status
            return res, hits

    if guard is not None:
        res, hits = asyncio.run(server_checks())
        for k in ("other site, text/plain", "other site, JSON", "origin null", "other host name (DNS rebinding)",
                  "other host name, POST"):
            check(f"refused: {k}", res[k] == 403, res[k])
        for k in ("no origin, text/plain", "own window, form post"):
            check(f"refused (JSON only): {k}", res[k] == 415, res[k])
        for k in ("own window, JSON", "own window via localhost", "no origin (not a browser), JSON", "own GET"):
            check(f"allowed: {k}", res[k] == 200, res[k])
        check("a refused request never reaches the route", len(hits) == 4, hits)
    gsrc = (ROOT / "guide.py").read_text(encoding="utf-8")
    check("guide.py's server uses the guard for every route",
          re.search(r"web\.Application\(\s*middlewares=\[\s*brainsetup\.local_only\s*\]", gsrc) is not None)

    # ------------------------------------------------------------ a bad start gives a plain message
    def child(app_dir):
        p = subprocess.run([sys.executable, "-B", str(app_dir / "guide.py"), "--child", "--no-window"],
                           cwd=app_dir, capture_output=True, text=True, timeout=120, encoding="utf-8",
                           errors="replace")
        diag = app_dir / "startup_diag.txt"
        return p.returncode, diag.read_text(encoding="utf-8") if diag.exists() else ""

    a = app_copy(tmp / "app-badconfig")
    (a / "config.json").write_text(r'{"kokoro_models_dir": "\models\voices"}', encoding="utf-8")  # \m = bad JSON
    code, msg = child(a)
    check("broken config.json: stops with the start-error code (3)", code == 3, code)
    check("broken config.json: plain message names the file and the fix",
          "config.json" in msg and "/" in msg and "line 1" in msg, msg[:200])
    check("broken config.json is left as it was",
          (a / "config.json").read_text(encoding="utf-8") == r'{"kokoro_models_dir": "\models\voices"}')

    b = app_copy(tmp / "app-nopackage")
    (b / "numpy.py").write_text("raise ModuleNotFoundError(\"No module named 'numpy'\", name='numpy')\n")
    code, msg = child(b)
    check("missing package: stops with the start-error code (3)", code == 3, code)
    check("missing package: message names it and the pip command",
          "numpy" in msg and "pip install -r requirements.txt" in msg, msg[:200])

    c = app_copy(tmp / "app-badgamesmap")                # crash while loading, before main(): still in guide.log
    (c / "config.json").write_text("{}", encoding="utf-8")
    (c / "games").mkdir()
    (c / "games" / "games.json").write_text('{"somegame.exe": "Some Ga', encoding="utf-8")   # cut off
    code, _msg = child(c)
    glog = (c / "guide.log").read_text(encoding="utf-8") if (c / "guide.log").exists() else ""
    check("crash while loading: exits with an error code", code not in (0, 3), code)
    check("crash while loading: guide.log has the error and where it happened",
          "FATAL" in glog and "JSONDecodeError" in glog and "games_map" in glog, glog[-300:])
    write_atomic = guide_func("write_atomic")
    wa = tmp / "atomic" / "x.json"
    wa.parent.mkdir()
    wa.write_text("old", encoding="utf-8")
    write_atomic(wa, '{"a": 1}')
    check("write_atomic replaces the file and leaves no temp file",
          wa.read_text(encoding="utf-8") == '{"a": 1}' and sorted(x.name for x in wa.parent.iterdir()) == ["x.json"])
    check("guide.py writes its JSON files atomically", "write_text(json.dumps" not in gsrc)
    # two threads saving the same file at once (answer stream + route worker): no error, a whole file, no temp left
    import threading
    errs = []

    def saver(n):
        for i in range(150):
            try:
                write_atomic(wa, json.dumps({"n": n, "i": i}))
            except Exception as e:
                errs.append(repr(e))
    ts = [threading.Thread(target=saver, args=(n,)) for n in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("write_atomic from 4 threads at once: no error", not errs, errs[:2])
    check("write_atomic from 4 threads at once: the file is whole and no temp file is left",
          "n" in json.loads(wa.read_text(encoding="utf-8")) and sorted(x.name for x in wa.parent.iterdir()) == ["x.json"],
          sorted(x.name for x in wa.parent.iterdir()))
    # a reader while another thread saves: Windows can refuse the open for a moment while os.replace swaps the file
    read_json = guide_func("read_json")
    read_json.__globals__["json"] = json
    rerrs, stop = [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                read_json(wa)
            except Exception as e:
                rerrs.append(repr(e))
            time.sleep(0.002)
    ts = [threading.Thread(target=saver, args=(n,)) for n in range(2)]
    rt_ = threading.Thread(target=reader)
    rt_.start()
    [t.start() for t in ts]
    [t.join() for t in ts]
    stop.set()
    rt_.join()
    check("read_json while 2 threads save the same file: no read error", not rerrs, rerrs[:2])
    check("read_json while 2 threads save the same file: no save error", not errs, errs[:2])
    check("guide.py reads its atomically saved JSON files through read_json",
          not re.search(r"json\.loads\([^)\n]*read_text\(", gsrc.replace(
              'return json.loads(p.read_text(encoding="utf-8-sig"))', "")), "a plain json.loads(...read_text) read")

    sup = gsrc[gsrc.index("def supervise"):gsrc.index('if __name__ == "__main__" and "--child"')]
    check("supervisor does not restart after a start error and shows the message",
          "STARTUP_ERROR" in sup and "message_box(" in sup and "DIAG.read_text" in sup and "MessageBoxW" in gsrc)

    # ------------------------------------------------------------ test runner exit code
    rt = tmp / "runner" / "tests"
    rt.mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "_run_all.py", rt)
    (rt / "test_ok.py").write_text("print('RESULT: ALL PASS')\n")
    p = subprocess.run([sys.executable, "-B", str(rt / "_run_all.py")], capture_output=True, text=True, timeout=120)
    check("runner: all pass -> exit 0", p.returncode == 0, p.returncode)
    (rt / "test_bad.py").write_text("import sys\nprint('FAIL something')\nsys.exit(1)\n")
    p = subprocess.run([sys.executable, "-B", str(rt / "_run_all.py")], capture_output=True, text=True, timeout=120)
    check("runner: a failing file -> nonzero exit", p.returncode != 0, p.returncode)

    # ------------------------------------------------------------ launchers
    ps = (ROOT / "install.ps1").read_text(encoding="utf-8")
    check("install.ps1: the py launcher probe can't stop the script (inside try, PATH fallback still runs)",
          re.search(r"try\s*\{[^}]*&\s*py\b[^}]*\}\s*catch", ps) is not None)
    for f in ("Start-GameGuide.bat", "install.ps1"):          # README sets up a .venv: the launchers use it first
        check(f"{f} uses the project's .venv Python when there is one",
              ".venv\\Scripts\\pythonw.exe" in (ROOT / f).read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("README sets up a venv in the project folder",
          "py -3.12 -m venv .venv" in readme and ".venv\\Scripts\\python -m pip install -r requirements.txt" in readme)
    for f in ("Start-GameGuide.bat", "install.ps1"):          # the "not found" help gives the same venv steps
        msg = (ROOT / f).read_text(encoding="utf-8").split("was not found", 1)[-1]
        check(f"{f}: the Python-not-found message gives the README's venv steps",
              "py -3.12 -m venv .venv" in msg and ".venv\\Scripts\\python -m pip install -r requirements.txt" in msg)
    run = readme.split("## Run", 1)[-1].split("\n## ", 1)[0]
    check("README Run section says the launcher uses the .venv first", ".venv" in run)
    check("README single-test example uses the venv Python",
          "Single test: `.venv\\Scripts\\python tests\\" in readme)
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8") if (ROOT / ".gitattributes").exists() else ""
    for ext in ("bat", "cmd", "ps1", "vbs"):
        check(f".gitattributes keeps .{ext} files CRLF",
              re.search(rf"^\*\.{ext}\s+.*eol=crlf", attrs, re.M) is not None)
    for script, rel in repo_files():
        if script.suffix.lower() in (".bat", ".cmd", ".ps1", ".vbs"):
            b = script.read_bytes()
            check(f"{rel.as_posix()} has CRLF line endings", b.count(b"\n") == b.count(b"\r\n"))

    # ------------------------------------------------------------ game names with an apostrophe
    ih = load(ROOT / "sense" / "install_helper.py", "gg_install_helper_guards")
    slugify = guide_func("slugify")
    for title in ("Dragon's Dogma 2", "Dragon’s Dogma 2"):
        gi = ih.game_info(title)
        check(f"Sense helper found for {title!r}", gi is not None and gi["slug"] == "dragons-dogma-2", gi)
        check(f"guide.py and the installer make the same slug for {title!r}",
              slugify(title) == ih._slug(title) == "dragons-dogma-2", (slugify(title), ih._slug(title)))
    game = tmp / "Some Game"
    game.mkdir()
    check("game folder setting found under the real name",
          ih.resolve_game_dir("Dragon's Dogma 2", {"dragons-dogma-2": str(game)}) == game)
    check("every helper's title finds its own entry",
          all((ih.game_info(g["title"]) or {}).get("slug") == s for s, g in ih.GAMES.items()),
          [g["title"] for s, g in ih.GAMES.items() if (ih.game_info(g["title"]) or {}).get("slug") != s])

    # ------------------------------------------------------------ helper calls without a game folder
    for what, call, want in (("sense_file_path", lambda: ih.sense_file_path(), None),
                             ("is_installed", lambda: ih.is_installed(), False),
                             ("sense_file_re", lambda: ih.sense_file_re(None), None),
                             ("is_installed_re", lambda: ih.is_installed_re(None), False)):
        try:
            got = call()
            check(f"{what} without a game folder -> {want}", got is want, got)
        except Exception as e:
            check(f"{what} without a game folder -> {want}", False, repr(e))
    for what, call in (("repair_pbz", lambda: ih.repair_pbz()), ("install_re", lambda: ih.install_re(None, "x", "x.exe")),
                       ("uninstall_re", lambda: ih.uninstall_re(None, "x.exe"))):
        try:
            r = call()
            check(f"{what} without a game folder stops with a plain message",
                  r.get("ok") is False and "folder" in (r.get("error") or "").lower(), r.get("error"))
        except Exception as e:
            check(f"{what} without a game folder stops with a plain message", False, repr(e))

    # ------------------------------------------------------------ games map lives in games\ (not part of git)
    check("the exe -> game map is saved under games\\ (ignored by git), not in the shipped games.json",
          'BASE / "games" / "games.json"' in gsrc and re.search(r"^games/$", (ROOT / ".gitignore").read_text(
              encoding="utf-8"), re.M) is not None)

    # ------------------------------------------------------------ brain panel checks in the page (node)
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def block(start, end):
        i = page.find(start)
        j = page.find(end, i)
        return page[i:j + len(end)] if i >= 0 and j >= 0 else ""
    check_js = block("async function bsCheck(", "\n}")
    login_js = block('$("#bsClaudeLogin").onclick', "\n};")
    node = shutil.which("node")
    if node and check_js and login_js:
        js = r"""
const BS_STATUS = {claude: 1, gemini: 1, local: 1};
const shown = [], applied = [];
let open = true, inFlight = 0, most = 0, calls = 0, claudeOk = false;
const bsShow = (n, st) => shown.push(n);
const bsApply = d => applied.push(Object.keys(d).filter(k => k in BS_STATUS).join(","));
const toast = () => {};
const $ = () => ({classList: {contains: () => open}});
const wait = ms => new Promise(r => realTimeout(r, ms));
const realTimeout = setTimeout;
const realInterval = setInterval;
globalThis.setTimeout = (fn, ms) => realTimeout(fn, 0);          // 5 s steps run at once here
globalThis.setInterval = (fn, ms) => realInterval(fn, 1);
let bsPoll = null;
const bsPost = async () => ({ok: true});
globalThis.fetch = async url => {
  calls++; inFlight++; most = Math.max(most, inFlight);
  const brain = url.split("only=")[1];
  await wait(brain === "claude" ? 20 : 2);                          // the Claude check is the slow one
  inFlight--;
  return {json: async () => ({order: [], [brain]: {ok: brain === "claude" ? claudeOk : true}})};
};
""" + check_js + "\n" + login_js.replace('$("#bsClaudeLogin").onclick =', "const login =") + r"""
(async () => {
  await bsCheck();
  const order = applied.join(" | ");
  calls = 0; most = 0;
  await login();
  for (let t = 0; t < 400 && (bsPoll !== null || inFlight); t++) await wait(25);   // up to 10 s
  console.log(JSON.stringify({order, pollCalls: calls, most}));
})();
"""
        jf = tmp / "panel_check.js"
        jf.write_text(js, encoding="utf-8")
        p = subprocess.run([node, str(jf)], capture_output=True, text=True, timeout=60)
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            out = {}
            check("page panel script runs", False, (p.stdout + p.stderr)[-300:])
        if out:
            check("Gemini and Local show before the slow Claude check is done",
                  out["order"].split(" | ")[-1] == "claude", out["order"])
            check("Sign in re-checks never overlap (one at a time)", out["most"] == 1, out["most"])
            check("Sign in re-checks stop after 3 minutes (36 tries)", out["pollCalls"] == 36, out["pollCalls"])
    else:
        print("SKIP brain panel page checks (node not found)" if not node else "FAIL brain panel code not found")
        if node:
            fails.append("brain panel code not found")

    # ------------------------------------------------------------ no third-party warning noise on import
    p = subprocess.run([sys.executable, "-B", "-W", "default", "-c",
                        f"import sys; sys.path.insert(0, {str(ROOT)!r}); import research"],
                       capture_output=True, text=True, timeout=120, errors="replace")
    check("importing research.py prints no urllib3 SOCKS DependencyWarning",
          p.returncode == 0 and "DependencyWarning" not in p.stderr, p.stderr[-300:])

    # ------------------------------------------------------------ GPU speech packages
    tree = ast.parse(gsrc)
    defaults = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", "") == "DEFAULTS" for t in n.targets))
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    if defaults.get("whisper_device") == "cuda":
        for pkg in ("nvidia-cublas-cu12", "nvidia-cudnn-cu12", "ctranslate2"):
            check(f"requirements.txt lists {pkg} (speech-to-text on the GPU is the default)",
                  re.search(rf"^{pkg}==", req, re.M) is not None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("RESULT: " + ("ALL PASS" if not fails else f"{len(fails)} FAIL"))
sys.exit(1 if fails else 0)
