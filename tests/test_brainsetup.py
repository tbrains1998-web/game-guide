"""The "Choose your brain" panel: status checks and web routes, with fakes only.
No real Claude CLI call, no network (the routes run on a test server on 127.0.0.1), no window,
no real settings file. guide.py is read as text, never imported.
Run: python tests/test_brainsetup.py   (exit 0 = all PASS)"""
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        fails.append(name)


try:
    import brainsetup as bs
except Exception as e:          # the module is missing: every check below fails
    bs = None
    check("brainsetup.py imports", False, repr(e))


class FakeResp:
    def __init__(self, code=200, data=None):
        self.status_code, self._data = code, data

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=httpx.Request("GET", "http://127.0.0.1"),
                                        response=httpx.Response(self.status_code))


def fake_run(stdout="", code=0, exc=None, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, code, stdout, "")
    return run


def no_net(*a, **kw):
    raise httpx.ConnectError("no network in tests")


KEY = "test-key-" + "0" * 8      # a fake key, never sent anywhere

if bs:
    # ------------------------------------------------------------ brain order
    check("chosen brain goes first, the rest stay as backups",
          bs.new_order("local", ["claude", "gemini", "local"]) == ["local", "claude", "gemini"])
    check("gemini first", bs.new_order("gemini", ["claude", "gemini", "local"]) == ["gemini", "claude", "local"])
    check("missing brains are added as backups", bs.new_order("claude", ["local"]) == ["claude", "local", "gemini"])
    check("empty or odd order is fixed", bs.new_order("gemini", None) == ["gemini", "claude", "local"]
          and bs.new_order("claude", ["claude", "x", "claude"]) == ["claude", "gemini", "local"])

    # ------------------------------------------------------------ Claude status (fake CLI)
    r = bs.claude_status(find_exe=lambda: None, run=fake_run(exc=AssertionError("must not run")))
    check("Claude not installed -> not ok, not found", r["ok"] is False and r["found"] is False, r)
    calls = []
    r = bs.claude_status(find_exe=lambda: "claude.exe",
                         run=fake_run(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), 0, calls=calls))
    check("signed in -> ok", r["ok"] is True and r["found"] is True, r)
    check("the check only runs 'auth status' (no question, no login)", calls == [["claude.exe", "auth", "status"]], calls)
    r = bs.claude_status(lambda: "claude.exe", fake_run(json.dumps({"loggedIn": False}), 1))
    check("not signed in -> not ok, found", r["ok"] is False and r["found"] is True and "sign in" in r["text"].lower(), r)
    r = bs.claude_status(lambda: "claude.exe", fake_run("Not logged in. Run claude auth login.", 1))
    check("plain-text 'Not logged in' -> not ok", r["ok"] is False, r)
    r = bs.claude_status(lambda: "claude.exe", fake_run("something new", 0))
    check("unknown answer -> 'found, not checked'", r["ok"] is None and "not checked" in r["text"], r)
    r = bs.claude_status(lambda: "claude.exe", fake_run(exc=subprocess.TimeoutExpired("claude", 20)))
    check("CLI hangs -> 'found, not checked'", r["ok"] is None and r["found"] is True, r)
    r = bs.claude_status(lambda: "claude.exe", fake_run(exc=OSError("gone")))
    check("CLI can't start -> 'found, not checked'", r["ok"] is None, r)
    r = bs.claude_status(lambda: "claude.exe", fake_run(json.dumps({"loggedIn": True, "email": "a@example.com"}), 0))
    check("account details never reach the window", "example" not in json.dumps(r), r)

    # ------------------------------------------------------------ Gemini
    r = bs.gemini_status({"gemini_api_key": ""})
    check("no Gemini key -> not ok", r["ok"] is False and r["has_key"] is False, r)
    r = bs.gemini_status({"gemini_api_key": KEY})
    check("saved key -> has_key, key not shown", r["has_key"] is True and KEY not in json.dumps(r), r)
    check("key from the environment counts", bs.gemini_status({}, env_key=KEY)["has_key"] is True)
    seen = []

    def g200(url, headers=None, timeout=None):
        seen.append((url, headers))
        return FakeResp(200, {"models": []})
    r = bs.gemini_test(KEY, get=g200)
    check("working key -> ok", r["ok"] is True, r)
    check("key goes in a header, not in the address", seen and KEY not in seen[0][0]
          and seen[0][1] == {"x-goog-api-key": KEY}, seen)
    check("only the free model list is asked", seen and "/models" in seen[0][0] and "generate" not in seen[0][0], seen)
    r = bs.gemini_test(KEY, get=lambda *a, **k: FakeResp(400))
    check("bad key -> not ok", r["ok"] is False and "does not work" in r["text"], r)
    r = bs.gemini_test(KEY, get=lambda *a, **k: FakeResp(503))
    check("Google error -> unknown", r["ok"] is None, r)
    r = bs.gemini_test(KEY, get=no_net)
    check("no internet -> unknown, plain message", r["ok"] is None and "internet" in r["text"], r)
    r = bs.gemini_test("", get=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    check("no key -> nothing is sent", r["ok"] is False)

    # ------------------------------------------------------------ Local (fake Ollama)
    cfg = {"local_model": "qwen3:4b-instruct", "vision_model": "qwen3-vl:4b-instruct"}
    r = bs.local_status(cfg, get=no_net)
    check("Ollama not running -> not ok", r["ok"] is False and r["running"] is False, r)
    check("pull commands for both models",
          r["pull"] == ["ollama pull qwen3:4b-instruct", "ollama pull qwen3-vl:4b-instruct"], r["pull"])
    urls = []

    def tags(models):
        def get(url, **kw):
            urls.append(url)
            return FakeResp(200, {"models": [{"name": m} for m in models]})
        return get
    r = bs.local_status(cfg, get=tags(["qwen3:4b-instruct", "qwen3-vl:4b-instruct"]))
    check("both models -> ok, none missing", r["ok"] is True and r["missing"] == [], r)
    check("only Ollama on this PC is asked", urls == ["http://127.0.0.1:11434/api/tags"], urls)
    r = bs.local_status(cfg, get=tags(["qwen3:4b-instruct"]))
    check("main model only -> ok, vision model listed", r["ok"] is True and r["missing"] == ["qwen3-vl:4b-instruct"], r)
    r = bs.local_status(cfg, get=tags(["llama3:latest"]))
    check("main model missing -> not ok", r["ok"] is False and "qwen3:4b-instruct" in r["missing"]
          and "ollama pull qwen3:4b-instruct" in r["text"], r)
    r = bs.local_status({"local_model": "mymodel", "vision_model": "mymodel"}, get=tags(["mymodel"]))
    check("model without a tag = :latest, one pull line", r["ok"] is True and r["pull"] == ["ollama pull mymodel"], r)
    r = bs.local_status(cfg, get=lambda *a, **k: FakeResp(200, None))
    check("odd Ollama answer -> not ok, no crash", r["ok"] is False, r)
    r = bs.local_status({**cfg, "ollama_url": "http://127.0.0.1:9"}, get=tags([]))
    check("ollama_url setting is used", urls[-1] == "http://127.0.0.1:9/api/tags", urls[-1])

    # ------------------------------------------------------------ web routes (test server, fakes)
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def routes_check():
        tmp = Path(tempfile.mkdtemp(prefix="gg_brains_"))
        conf = tmp / "config.json"
        cfg = {"brain_order": ["claude", "gemini", "local"], "gemini_api_key": "",
               "local_model": "qwen3:4b-instruct", "vision_model": "qwen3-vl:4b-instruct"}
        conf.write_text(json.dumps(cfg))

        def save(**kv):                      # same job as guide.save_settings, in a temp folder
            cfg.update(kv)
            saved = json.loads(conf.read_text())
            saved.update(kv)
            conf.write_text(json.dumps(saved))
        opened, changes, exe = [], [], ["claude.exe"]

        def popen(cmd, **kw):
            opened.append(cmd)
        app = web.Application()
        app.add_routes(bs.make_routes(cfg, save, ["login-cmd"], on_change=lambda: changes.append(1),
                                      find_exe=lambda: exe[0],
                                      run=fake_run(json.dumps({"loggedIn": False}), 1),
                                      get=tags(["qwen3:4b-instruct"]), popen=popen))
        client = TestClient(TestServer(app, host="127.0.0.1"))
        await client.start_server()
        try:
            r = await client.get("/api/brains/status")
            d = await r.json()
            check("status route answers all three", r.status == 200 and {"claude", "gemini", "local"} <= set(d), d)
            check("status: Claude found but not signed in", d["claude"]["ok"] is False and d["claude"]["found"], d["claude"])
            check("status: local ok", d["local"]["ok"] is True, d["local"])
            check("status: not done yet, current order", d["done"] is False and d["order"] == cfg["brain_order"], d)
            check("a status check never opens the sign-in", opened == [])
            r = await client.get("/api/brains/status?only=local")
            d = await r.json()
            check("status for one brain only", "local" in d and "claude" not in d, list(d))

            r = await client.post("/api/brains/gemini", json={"key": "  " + KEY + " "})
            d = await r.json()
            check("Gemini key is saved to config.json (trimmed)", json.loads(conf.read_text())["gemini_api_key"] == KEY)
            check("key test result comes back, without the key", "gemini" in d and KEY not in json.dumps(d), d)
            r = await client.get("/api/brains/status")
            check("status never returns the saved key", KEY not in await r.text())
            r = await client.post("/api/brains/gemini", json={})
            check("empty key field keeps the saved key", json.loads(conf.read_text())["gemini_api_key"] == KEY)

            r = await client.post("/api/brains/use", json={"brain": "local"})
            d = await r.json()
            saved = json.loads(conf.read_text())
            check("Use this: chosen first, others as backups",
                  saved["brain_order"] == ["local", "claude", "gemini"] and d["order"] == saved["brain_order"], saved)
            check("Use this: panel marked done, window refreshed", saved.get("brain_setup_done") is True and changes, saved)
            r = await client.post("/api/brains/use", json={"brain": "nonsense"})
            check("unknown brain -> 400, nothing saved", r.status == 400
                  and json.loads(conf.read_text())["brain_order"] == ["local", "claude", "gemini"])

            save(brain_setup_done=False)
            r = await client.post("/api/brains/skip", json={})
            check("Skip marks it done", r.status == 200 and json.loads(conf.read_text())["brain_setup_done"] is True)

            r = await client.post("/api/brains/claude-login", json={})
            d = await r.json()
            check("Sign in runs the login command when pressed", d["ok"] is True and opened == [["login-cmd"]], opened)
            exe[0] = None
            r = await client.post("/api/brains/claude-login", json={})
            d = await r.json()
            check("Sign in without Claude Code -> plain error, nothing opened",
                  d["ok"] is False and "Install Claude Code" in d["error"] and len(opened) == 1, d)

            for path in ("/api/brains/claude-login", "/api/brains/use", "/api/brains/skip", "/api/brains/gemini"):
                r = await client.post(path, data="{}", headers={"Content-Type": "text/plain"})
                check(f"{path}: non-JSON post refused", r.status == 415, r.status)
            check("refused posts changed nothing", len(opened) == 1)
            r = await client.post("/api/brains/use", data="not json", headers={"Content-Type": "application/json"})
            check("broken JSON -> 400", r.status == 400, r.status)
        finally:
            await client.close()
            shutil.rmtree(tmp, ignore_errors=True)

    try:
        asyncio.run(routes_check())
    except Exception as e:
        check("routes run without errors", False, repr(e))

# ------------------------------------------------------------ guide.py wiring (read as text)
g = (ROOT / "guide.py").read_text(encoding="utf-8")
check("guide.py has the brain_setup_done setting (off by default)", '"brain_setup_done": False' in g)
check("guide.py sends brain_setup_done to the window", '"brain_setup_done": bool(CFG.get("brain_setup_done"))' in g)
check("guide.py adds the panel routes", "brainsetup.make_routes(" in g)
check("guide.py opens the sign-in with login-claude.ps1", "login-claude.ps1" in g)

# ------------------------------------------------------------ the window (web/index.html)
html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
for el in ("brainSetup", "brainBtn", "bsClaudeLogin", "bsGeminiKey", "bsGeminiTest", "bsSkip", "bsLocalPull"):
    check(f"index.html has #{el}", f'id="{el}"' in html)
check("panel opens on first run", "brain_setup_done" in html)
check("each card has a Use this button", html.count('data-use="') == 3)
for url in ("/api/brains/status", "/api/brains/use", "/api/brains/skip", "/api/brains/gemini", "/api/brains/claude-login"):
    check(f"index.html calls {url}", url in html)
script = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", html))
node = shutil.which("node")
if node and script:
    tmp_js = Path(tempfile.mkdtemp(prefix="gg_js_")) / "page.js"
    tmp_js.write_text(script, encoding="utf-8")
    p = subprocess.run([node, "--check", str(tmp_js)], capture_output=True, text=True)
    check("the page script has no syntax errors (node --check)", p.returncode == 0, p.stderr[:300])
    shutil.rmtree(tmp_js.parent, ignore_errors=True)
else:
    print("SKIP page script syntax check (node not found)")

print("RESULT: " + ("ALL PASS" if not fails else f"{len(fails)} FAIL"))
sys.exit(1 if fails else 0)
