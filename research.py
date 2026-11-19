"""Free web research: DuckDuckGo search -> fetch guide pages -> pick the relevant passages."""

import asyncio
import math
import re
import warnings
from urllib.parse import urlparse

import httpx

# trafilatura tries urllib3's optional SOCKS proxy support on import; without PySocks urllib3 warns (harmless)
warnings.filterwarnings("ignore", message="SOCKS support in urllib3")
import trafilatura  # noqa: E402
from ddgs import DDGS

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Sites that write real, hand-made walkthroughs. Higher = preferred.
TRUSTED = {
    "powerpyx.com": 5, "fextralife.com": 5, "game8.co": 5, "ign.com": 4,
    "gamefaqs.gamespot.com": 5, "gamerant.com": 3, "thegamer.com": 3,
    "eurogamer.net": 4, "gamesradar.com": 4, "pcgamer.com": 4, "polygon.com": 4,
    "rockpapershotgun.com": 4, "pushsquare.com": 4, "purexbox.com": 3,
    "vg247.com": 3, "dexerto.com": 3, "shacknews.com": 3, "gameranx.com": 2,
    "fandom.com": 4, "reddit.com": 3, "steamcommunity.com": 3, "gosunoob.com": 3,
    "segmentnext.com": 2, "primagames.com": 3, "trueachievements.com": 3,
    "truetrophies.com": 3, "psnprofiles.com": 3, "gamepressure.com": 4,
    "guides.gamepressure.com": 4, "neoseeker.com": 3, "strategywiki.org": 3,
    "destructoid.com": 3, "thesixthaxis.com": 2, "hardcoregamer.com": 2,
}
BLOCKED = ("youtube.com", "youtu.be", "tiktok.com", "facebook.com", "instagram.com",
           "vercel.app", "netlify.app", "pinterest.", "x.com", "twitter.com",
           "amazon.", "store.steampowered.com", "playstation.com", "xbox.com")

_WORD = re.compile(r"[a-z0-9']+")
STOP = set("the a an and or of to in on at for is it i im i'm my me with what where how do does"
           " can cant can't this that there get go stuck help next now after before from by be"
           " are was you your just game walkthrough guide".split())


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def score_result(url: str, title: str, game: str) -> float:
    d = _domain(url)
    if any(b in d for b in BLOCKED):
        return -99
    s = 0.0
    for site, w in TRUSTED.items():
        if d == site or d.endswith("." + site):
            s += w
            break
    # SEO-spam sites named after the game ("silenthilltownfall.org") are often AI-written guesses.
    g = _slug(game)
    if g and len(g) > 6 and g[:10] in _slug(d):
        s -= 4
    if game and _slug(game)[:8] in _slug(title):
        s += 1
    return s


_A = re.compile(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG = re.compile(r"<[^>]+>")


def _ddg_html(q: str, n: int) -> list[dict]:
    """DuckDuckGo's no-JS page: ~1 s, no key."""
    import html as _html
    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": q, "kl": "us-en"},
                   headers=UA, follow_redirects=True, timeout=10)
    out = []
    for href, title, snip in _A.findall(r.text):
        if "duckduckgo.com/y.js" in href:       # ads
            continue
        if "uddg=" in href:
            from urllib.parse import parse_qs, unquote
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        out.append({"href": _html.unescape(href), "title": _html.unescape(_TAG.sub("", title)).strip(),
                    "body": _html.unescape(_TAG.sub("", snip)).strip()})
    return out[:n]


def _one_search(q: str, n: int) -> list[dict]:
    try:
        res = _ddg_html(q, n)
        if res:
            return res
    except Exception:
        pass
    try:   # fallback: ddgs library (tries several engines, slower)
        with DDGS() as ddg:
            return ddg.text(q, region="us-en", max_results=n) or []
    except Exception:
        return []


def search(queries: list[str], game: str, max_per_query: int = 8) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(len(queries) or 1) as ex:
        all_results = list(ex.map(lambda q: _one_search(q, max_per_query), queries))
    seen, out = set(), []
    for results in all_results:
        for rank, r in enumerate(results):
            url = r.get("href", "")
            if not url or url in seen:
                continue
            seen.add(url)
            sc = score_result(url, r.get("title", ""), game) - rank * 0.3
            if sc > -50:
                out.append({"url": url, "title": r.get("title", ""),
                            "snippet": r.get("body", ""), "score": sc})
    out.sort(key=lambda x: -x["score"])
    return out


async def _fetch(client: httpx.AsyncClient, item: dict) -> dict | None:
    try:
        r = await client.get(item["url"], headers=UA, follow_redirects=True, timeout=12)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            return None
        text = await asyncio.to_thread(trafilatura.extract, r.text,
                                       include_links=False, include_tables=True)
        if not text or len(text) < 300:
            return None
        return {**item, "text": text}
    except Exception:
        return None


async def fetch_pages(items: list[dict], limit: int = 5) -> list[dict]:
    # Take the best results, but no more than 2 pages from the same site.
    picked, per_site = [], {}
    for it in items:
        d = _domain(it["url"])
        if per_site.get(d, 0) >= 2:
            continue
        per_site[d] = per_site.get(d, 0) + 1
        picked.append(it)
        if len(picked) >= limit + 3:   # a few spares for pages that fail/block
            break
    async with httpx.AsyncClient(http2=False) as client:
        pages = await asyncio.gather(*[_fetch(client, it) for it in picked])
    return [p for p in pages if p][:limit]


def _tokens(s: str) -> list[str]:
    return [w for w in _WORD.findall(s.lower()) if w not in STOP and len(w) > 1]


def _chunks(text: str, size: int = 900) -> list[str]:
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    out, cur = [], ""
    for p in paras:
        if len(cur) + len(p) > size and cur:
            out.append(cur)
            cur = ""
        cur += p + "\n"
    if cur:
        out.append(cur)
    return out


def select_passages(pages: list[dict], query: str, budget_chars: int = 11000) -> list[dict]:
    """BM25 over page chunks; returns sources with their best passages (kept in page order)."""
    q = set(_tokens(query))
    all_chunks = []
    for pi, p in enumerate(pages):
        for ci, c in enumerate(_chunks(p["text"])):
            all_chunks.append((pi, ci, c, _tokens(c)))
    if not all_chunks:
        return []
    n = len(all_chunks)
    avg = sum(len(t) for *_, t in all_chunks) / n
    df = {w: sum(1 for *_, t in all_chunks if w in t) for w in q}
    scored = []
    for pi, ci, c, toks in all_chunks:
        s = 0.0
        for w in q:
            tf = toks.count(w)
            if tf:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(toks) / avg))
        s += pages[pi]["score"] * 0.15
        scored.append((s, pi, ci, c))
    scored.sort(key=lambda x: -x[0])
    chosen, used = [], 0
    for s, pi, ci, c in scored:
        if s <= 0 or used + len(c) > budget_chars:
            continue
        chosen.append((pi, ci, c))
        used += len(c)
    by_page: dict[int, list] = {}
    for pi, ci, c in chosen:
        by_page.setdefault(pi, []).append((ci, c))
    sources = []
    for pi in sorted(by_page, key=lambda i: -pages[i]["score"]):
        parts = [c for _, c in sorted(by_page[pi])]
        sources.append({"url": pages[pi]["url"], "title": pages[pi]["title"],
                        "text": "\n...\n".join(parts)})
    return sources
