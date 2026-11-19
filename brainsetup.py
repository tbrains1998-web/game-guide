"""The "Choose your brain" panel: a live status for each brain and the buttons behind it.
Nothing here logs in or sends a question by itself: the status checks only ask the Claude CLI who is
signed in, ask Ollama on this PC which models it has, and (only when Test is pressed) ask Google
whether a Gemini key works. guide.py adds these routes to its web server."""

import asyncio
import json
import subprocess

import httpx
from aiohttp import web

import brains

NAMES = ("claude", "gemini", "local")
CLAUDE_CODE_URL = "https://claude.com/claude-code"
GEMINI_KEY_URL = "https://aistudio.google.com/apikey"
OLLAMA_URL = "https://ollama.com/download"
GEMINI_MODELS = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
LOCAL_HOSTS = ("127.0.0.1", "localhost")


@web.middleware
async def local_only(request, handler):
    """Guard for every route of the app's server: only Game Guide's own window may use it. Without it, any
    web site open in the browser could post to 127.0.0.1 (a plain text or form post needs no permission
    from the browser) or read it through a web address that points at 127.0.0.1.
    So the server name must be this PC, and a POST must be JSON sent from the app's own page."""
    if request.url.host not in LOCAL_HOSTS:
        raise web.HTTPForbidden(text="Only Game Guide's own window can use this server.")
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("Origin")        # browsers always send it on a POST; other programs don't
        if origin is not None and origin != f"http://{request.host}":
            raise web.HTTPForbidden(text="Only Game Guide's own window can use this server.")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON only")
    return await handler(request)


def new_order(chosen: str, order) -> list:
    """The chosen brain first, the others after it as backups (in their old order)."""
    rest = [b for b in (order or []) if b in NAMES and b != chosen]
    return [chosen] + rest + [b for b in NAMES if b != chosen and b not in rest]


def claude_status(find_exe=brains.find_claude_exe, run=subprocess.run) -> dict:
    """Is the Claude Code CLI installed and signed in? Uses "claude auth status" (no question is sent,
    nothing is used up). ok: True = ready, False = needs something, None = could not tell."""
    exe = find_exe()
    if not exe:
        return {"ok": False, "found": False, "text": "Claude Code is not installed on this PC."}
    try:
        p = run([exe, "auth", "status"], capture_output=True, text=True, timeout=20,
                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        out = (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return {"ok": None, "found": True, "text": "Claude Code found, not checked. Press Check again."}
    try:
        logged_in = json.loads(p.stdout).get("loggedIn")
    except (ValueError, AttributeError, TypeError):
        logged_in = None
    if logged_in is None and "not logged in" in out.lower():
        logged_in = False
    if logged_in is None:
        return {"ok": None, "found": True, "text": "Claude Code found, not checked. Press Check again."}
    if logged_in:
        return {"ok": True, "found": True, "text": "Signed in. Ready to use."}
    return {"ok": False, "found": True, "text": "Claude Code found, but you are not signed in. Press Sign in to Claude."}


def gemini_status(cfg: dict, env_key: str = "") -> dict:
    """Only says whether a key is set; the key itself never goes to the window."""
    if cfg.get("gemini_api_key") or env_key:
        return {"ok": None, "has_key": True, "text": "A key is saved. Press Save and test to check it."}
    return {"ok": False, "has_key": False, "text": "No key yet. Get a free key from Google and paste it here."}


def gemini_test(key: str, get=httpx.get) -> dict:
    """Ask Google for its model list with this key: free, no question is sent."""
    if not key:
        return {"ok": False, "has_key": False, "text": "No key yet. Get a free key from Google and paste it here."}
    try:
        r = get(GEMINI_MODELS, headers={"x-goog-api-key": key}, timeout=10)
    except httpx.HTTPError:
        return {"ok": None, "has_key": True, "text": "Could not reach Google. Check your internet and try again."}
    if r.status_code == 200:
        return {"ok": True, "has_key": True, "text": "The key works. Ready to use."}
    if r.status_code in (400, 401, 403):
        return {"ok": False, "has_key": True, "text": "Google says this key does not work. Check it and paste it again."}
    return {"ok": None, "has_key": True, "text": f"Google answered with error {r.status_code}. Try again later."}


def _has_model(names: set, model: str) -> bool:
    return (model if ":" in model else model + ":latest") in names


def local_status(cfg: dict, get=httpx.get) -> dict:
    """Is Ollama running on this PC, and does it have the models Game Guide uses?"""
    model = cfg.get("local_model", "qwen3:4b-instruct")
    vision = cfg.get("vision_model", "qwen3-vl:4b-instruct")
    base = {"pull": [f"ollama pull {m}" for m in dict.fromkeys((model, vision))]}
    try:
        r = get(cfg.get("ollama_url", "http://127.0.0.1:11434") + "/api/tags", timeout=3)
        r.raise_for_status()
        tags = r.json().get("models") or []
    except (httpx.HTTPError, ValueError, AttributeError):
        return {**base, "ok": False, "running": False,
                "text": "Ollama is not running (or not installed). Install it, start it, then pull the models below."}
    names = set()
    for t in tags:
        for k in ("name", "model"):
            n = t.get(k) if isinstance(t, dict) else None
            if isinstance(n, str):
                names.add(n if ":" in n else n + ":latest")
    missing = [m for m in dict.fromkeys((model, vision)) if not _has_model(names, m)]
    if model in missing:
        return {**base, "ok": False, "running": True, "missing": missing,
                "text": "Ollama is running, but the model is missing. Run: " + ", ".join(f"ollama pull {m}" for m in missing)}
    if missing:
        return {**base, "ok": True, "running": True, "missing": missing,
                "text": f"Ready to use. For screenshots also run: ollama pull {vision}"}
    return {**base, "ok": True, "running": True, "missing": [], "text": "Ollama is running and has the models. Ready to use."}


def make_routes(cfg: dict, save_settings, login_cmd: list, on_change=None, env_key=lambda: "",
                find_exe=None, run=subprocess.run, get=httpx.get, popen=subprocess.Popen):
    """The panel's routes. cfg is the live settings dict, save_settings(**kv) writes config.json,
    login_cmd is the command that opens the Claude sign-in window, on_change() refreshes the window.
    The other arguments are only swapped out by the tests."""
    routes = web.RouteTableDef()
    find_exe = find_exe or brains.find_claude_exe

    async def body(request):
        # Only JSON posts: a web page on another site can't send those without the browser asking first.
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON only")
        try:
            d = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="bad JSON")
        return d if isinstance(d, dict) else {}

    def changed():
        if on_change:
            on_change()

    def order():
        return {"order": list(cfg.get("brain_order") or []), "done": bool(cfg.get("brain_setup_done"))}

    @routes.get("/api/brains/status")
    async def status(request):
        which = request.query.get("only")
        out = order()
        if which in (None, "claude"):
            out["claude"] = await asyncio.to_thread(claude_status, find_exe, run)
        if which in (None, "gemini"):
            out["gemini"] = gemini_status(cfg, env_key())
        if which in (None, "local"):
            out["local"] = await asyncio.to_thread(local_status, cfg, get)
        return web.json_response(out)

    @routes.post("/api/brains/gemini")
    async def gemini(request):
        d = await body(request)
        key = (d.get("key") or "").strip() if isinstance(d.get("key"), str) else ""
        if key:
            save_settings(gemini_api_key=key)       # stored in config.json on this PC only
        r = await asyncio.to_thread(gemini_test, key or cfg.get("gemini_api_key") or env_key(), get)
        return web.json_response({**order(), "gemini": r})

    @routes.post("/api/brains/use")
    async def use(request):
        d = await body(request)
        chosen = d.get("brain")
        if chosen not in NAMES:
            return web.json_response({"ok": False, "error": "Unknown brain."}, status=400)
        save_settings(brain_order=new_order(chosen, cfg.get("brain_order")), brain_setup_done=True)
        changed()
        return web.json_response({"ok": True, **order()})

    @routes.post("/api/brains/skip")
    async def skip(request):
        await body(request)
        save_settings(brain_setup_done=True)
        changed()
        return web.json_response({"ok": True, **order()})

    @routes.post("/api/brains/claude-login")
    async def claude_login(request):
        await body(request)
        if not find_exe():
            return web.json_response({"ok": False, "error": "Install Claude Code first, then press Check again."})
        try:
            popen(login_cmd, creationflags=NEW_CONSOLE)    # its own window: the user signs in there
        except OSError as e:
            return web.json_response({"ok": False, "error": f"Could not open the sign-in window: {e}"})
        return web.json_response({"ok": True})

    return routes
