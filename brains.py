"""The 'brains' that answer questions. Each one is an async generator of events:
   {"type": "status", "text": ...}   what it's doing right now (searching, reading...)
   {"type": "token",  "text": ...}   answer text, streamed
   {"type": "sources", "items": [{"url", "title"}]}
   {"type": "error",  "text": ...}
"""

import asyncio
import base64
import json
import re
import os
import shutil
import subprocess
from pathlib import Path

import httpx

import research

LEVELS = {
    "nudge": "NUDGE: ONE short sentence pointing the right way (the area or the kind of thing to look for). "
             "Never give item locations, codes, numbers, combinations or puzzle answers.",
    "hint": "HINT: 2-4 short bullets saying where to go and what KIND of clue or item to look for, so the "
            "player solves it themselves. NEVER reveal a code, combination, password, number, dial setting, "
            "symbol order or puzzle answer - not the full answer and not its parts (e.g. don't list which "
            "number each poster shows). Say where the clue is ('the posters around the pharmacy each hide one "
            "digit') instead. This applies even if the player asks 'what's the code' - then end with: "
            "'Want the code? Press Just tell me.'",
    "solution": "SOLUTION: numbered steps with the exact route, item locations, codes and puzzle answers - "
                "as many steps as needed, but each step is ONE short sentence doing ONE thing.",
}

# Shared by the "where" and "map" modes (Feature 1b): never send the player back to things they already did.
PROGRESS_RULE = (
    " ALREADY DONE: BEFORE answering, work out what the player has already done - from the map (which "
    "bells/checkpoints are already on it, the map completion %, which markers are gone or ticked) and from the "
    "progress notes. Start from the player's CURRENT spot and only give what is still ahead; never list a boss, "
    "area or task that is already done. If you cannot tell between two possible spots, then step 1 is ONE short "
    "yes/no question (e.g. 'Have you beaten the horse rider boss?') and nothing else - no other steps. In the "
    "<notes> block write what the map proves is done, as lines 'Bells on map: ...' and 'Done: ...'.")

MODE_RULES = {
    "where": ("\n- WHERE AM I: the player wants to know where they are. Answer in exactly 3 short numbered steps "
              "(unless the ALREADY DONE rule below makes step 1 a yes/no question): 1. 'You're in: <chapter/part> - "
              "<area/room>'  2. 'Objective now: <what the game wants "
              "right now>'  3. 'Next: <the next thing to do, one sentence>'. Work it out from the screenshot (HUD, "
              "objective text, surroundings), the progress notes, the game overview and walkthroughs. Never give "
              "codes or puzzle answers here. If you can't tell, say what to screenshot (the map or objective screen)."
              + PROGRESS_RULE),
    "boss": ("\n- BOSS CARD: the player is facing a boss. Answer as a compact card of at most 7 numbered steps, one "
             "sentence each, in this order: 1. 'Boss: <name> - <where>'  2. 'Phases: ...'  3. 'Dodge: <its dangerous "
             "attacks and the tell before each>'  4. 'Use: <best weapon/item/ability and why>'  5. 'Don't: <the "
             "mistake most players make>'  6. 'Plan: <the safe strategy in one sentence>'  7. (optional) 'After: "
             "<what the win unlocks>'. Only this boss - no spoilers beyond it. If it's unclear which boss, ask which "
             "one (or for a screenshot of its health bar) and stop."),
}

# Shared by the two map modes: how the model tells guide.py what the map icons mean (guide.py saves the
# <legend> block into the dossier under "## Map icons"). It goes after the <notes> block so the stream filter
# hides it from the player like the notes.
LEGEND_RULE = (
    " MAP ICONS: work out what the icons mean from the 'map icons' list given with the question (if any), the "
    "game overview and walkthroughs. If you do NOT know this game's map icons, add this to the END of step 1 (after "
    "the place/area/completion part, as one sentence): \" - I don't know this game's map icons yet - press F11 on "
    "the map legend or the collectibles list and ask again\", e.g. \"You're at: Mirrorwood (Lóng Pillar, map "
    "completion 40%) - I don't know this game's map icons yet - press F11 on the map legend or the collectibles "
    "list and ask again\", and at the very end of your reply (after the <notes> block) output "
    "<legend>unknown</legend>. If a screenshot shows the "
    "map legend or the collectibles list, or a walkthrough explains the icons, then at the very end (after the "
    "<notes> block) output <legend> with one line per icon: '- <icon description>: <meaning>' </legend> - plain "
    "words, e.g. '- red square: story objective'. If the question says the attached map is the player's LAST map "
    "(taken earlier), still circle the target on it and say 'The nearest checkpoint to open the map is <Y>'.")

MODE_RULES["map"] = (
    "\n- MAP: the player is looking at (or asking about) the game map and wants to know where to go. Main story "
    "first. Answer in exactly these numbered steps, one short sentence each, and no other steps (the only "
    "exceptions: the ALREADY DONE rule may make step 1 a yes/no question, and the MAP ICONS rule may add a "
    "sentence to step 1):  "
    "1. 'You're at: <place> (<area>, map completion N%)'  "
    "2. 'Go to: <place or icon>' - mark THIS step with a POINT on the map screenshot (x/y of the target)  "
    "3. 'How: from <this checkpoint / the nearest checkpoint>, <the route in words>' - mark THIS step with a PATH "
    "on the map (\"path\": [[x, y], ...] from the player's position to the target, following walkable ground)  "
    "4. (ONLY if the player asked for side stuff) 'Also nearby: <side things, one sentence>'. "
    "Read the place, area and completion % from the map screenshot; if a value is not visible, leave that part "
    "out instead of guessing. If the progress notes have open '- Clue' lines (tasks, places, names from notes the "
    "player read), treat them as likely next goals. Never give codes or puzzle answers here."
    + PROGRESS_RULE + LEGEND_RULE)

MODE_RULES["whatsleft"] = (
    "\n- WHAT'S LEFT HERE: the player wants to know what is still left to do in the area shown on the map. Answer "
    "as a short numbered list, one item per step, each in exactly this form and nothing else: "
    "'<thing> - <done / probably not done / unknown> - <where>'. Main story items first, then side things "
    "(chests, collectibles, optional fights, hidden paths). At Hint level '<where>' is the kind of thing and a "
    "rough place ('a chest near the north bridge'); at Solution level it is the exact spot. Decide 'done' from the "
    "map icons (ticked / greyed icons, completion %), the progress notes and the game overview; say 'unknown' "
    "when you can't tell. Mark every item that is NOT done with a POINT on the map screenshot. Never give codes "
    "or puzzle answers here." + LEGEND_RULE)

DOSSIER_PROMPT = (
    'Research the video game "{game}" on the web (WebSearch and WebFetch, several sources) and write a compact '
    "dossier for a walkthrough assistant. Markdown, at most 400 words, these sections with one line per item:\n"
    "## Structure - the chapters/parts/areas in order\n"
    "## Bosses - in order, where they are, a one-line gist\n"
    "## Points of no return - and what is locked after each\n"
    "## Missables - items/quests that are easy to miss, and where\n"
    "## Saving - how and where the game saves\n"
    "## Known bugs - common stuck points that are bugs, and the fix\n"
    "## Difficulty & accessibility - options that make it easier\n"
    "Facts only from sources; mark anything uncertain with (?). Output ONLY the markdown: no preamble, no links.")

LEVEL_REMINDER = {
    "nudge": "Spoiler level for THIS answer: NUDGE - one vague sentence, no items/codes/answers.",
    "hint": "Spoiler level for THIS answer: HINT - where to look and what kind of clue, but NO codes, numbers, "
            "combinations or puzzle answers (not even partial), even if earlier answers in this chat gave some.",
    "solution": "Spoiler level for THIS answer: SOLUTION - give the exact steps and answers, "
                "one short sentence per step.",
}


PUZZLE_RULE = (
    "\n- SOLVE FROM THE SCREEN: if the screenshots show a puzzle's clue/target (a pattern, code, diagram, note, "
    "camera feed) and/or its current state (switches, valves, dials, tiles, wires, a keypad), work the solution out "
    "from what is visible for THIS playthrough instead of quoting a walkthrough's answer. Step 1 must state what "
    "you read ('I read the target as ... and your current state as ...') so the player can confirm it; then give "
    "ONE move per step (which switch/valve/tile, which direction or position), each marked on the screenshot that "
    "shows the puzzle. Use walkthroughs only for the RULES of the puzzle (how it works, what the symbols mean, in "
    "which order to read). If the clue or the current state is not visible in the screenshots, say exactly what to "
    "press F11 on (the clue, then the puzzle) and stop there.")

NOTES_RULE = ("\n- After the answer, on a new line, write a <notes>...</notes> block: the complete, up-to-date progress "
              "notes (max 8 short bullets: current chapter/area, what they've done, key items, codes confirmed from the "
              "player's screen, attempts that were rejected), keeping still-true "
              "facts from the known progress and replacing outdated ones. This block is hidden from the player and "
              "replaces the old notes. Only include facts the player stated or that are certain "
              "from the conversation - no guesses. Keep every line starting with '- Clue' (clues read from notes, "
              "signs or dialogue in the game) word for word until the player has clearly done it; only then drop it.")


def system_prompt(game: str, level: str, notes: bool = True, image_size=None, image_sizes=None, mode=None) -> str:
    extra = (MODE_RULES.get(mode) or "") + (NOTES_RULE if notes else "")
    sizes = [sz for sz in (image_sizes or ([image_size] if image_size else [])) if sz]
    if sizes:
        from marks import MARKS_RULE
        desc = ", ".join(f"{i + 1} = {sz[0]}x{sz[1]}" for i, sz in enumerate(sizes))
        extra += MARKS_RULE.format(sizes=desc, n=len(sizes)) + PUZZLE_RULE
    return f"""You are Game Guide, a walkthrough helper for a player who is stuck while playing {game or "a video game"}.
The player is mid-game and reads your answer at a glance (or hears it read aloud), so be short and concrete.

Rules:
- Answer ONLY from the provided sources / search results. If they don't cover the player's exact spot, say so in one line, give the best lead, and ask ONE short question to pin it down. Never invent items, rooms, codes or bosses.
- Puzzle answers can change with difficulty or be randomized. If sources show that, say in one line how to work out the answer from the in-game clue.
- For pattern or sequence puzzles (switches, valves, dials, tiles, wires, a code from a note), the sure route is to solve from the player's own screen: tell them to press F11 on the clue or target, F11 again on the puzzle itself, and then ask "solve this".
- CODES, IDs, COMBINATIONS: numbers from walkthroughs (locker codes, device IDs, dial or valve settings) often differ per playthrough. Never present a walkthrough number as the player's own: say "guides give X" and tell them exactly where in the game to read theirs (which camera, note, poster, panel) and to press F11 on it so you can read it. A value read from the player's own screenshot is CONFIRMED: record it in the notes as "confirmed from screen" and use it from then on.
- GUIDES DISAGREE: if sources give different values or steps, say so inside the step in this form: "⚠ guides disagree (103 or 130) - read it off camera 000". Never silently pick one.
- FAILED ATTEMPTS: the player finds panels with many buttons confusing and may have executed a correct answer wrongly. When something "didn't work", first assume execution: re-give the same steps more precisely (symbol AND row/column of every button, clear the panel first), ask for a screenshot of the panel after entering so you can check what is lit, and only then consider a different answer. Record attempts in the notes as "Tried: ... - rejected (execution unverified)" or "(verified by screenshot)", and never repeat an answer that was verified wrong.
- KNOWN BUGS: if the steps are right but the game does not respond (a trigger didn't fire, a door stays locked, an item is missing), check Steam discussions and Reddit for the same report and give the known workaround (reload the save, leave and re-enter the area, restart the game) as a final step.
- Don't spoil anything beyond the player's current point.
- Never narrate what you are about to do ("I'll look this up", "Let me search") - search silently, then reply with only the answer.
- SHORT but DETAILED: your reply's very first characters are the first step (for Hint/Solution: "- " or "1."), never a comment about the search or the sources. No intro, no recap of the question, no filler, no closing offers.
- ONE SENTENCE PER STEP: the player is shown one step at a time, so every bullet/step is exactly ONE short sentence (about 8-18 words) with ONE action. Split anything longer into more steps. Keep the specifics: exact place names, directions (left/right/upstairs/behind the counter), item names, what to interact with.
- Do NOT cite or mention sources: no [1]-style references, no links, no "Sources" list, no site names.
- Spoiler level: {LEVELS.get(level, LEVELS["hint"])}""" + extra


def req_images(req: dict) -> list:
    """The screenshots attached to a question, in the order guide.py listed them. Never raises."""
    try:
        return list(req.get("images") or ([req["image"]] if req.get("image") else []))
    except Exception:
        return []


def last_map_index(req: dict) -> int | None:
    """1-based place of the saved last map among the attached screenshots (guide.py sets
    req["last_map_index"], 1 = first screenshot, same numbering as the model sees), or None when it is
    missing, not a number, or out of range. Never raises."""
    try:
        idx = int(req.get("last_map_index"))
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(req.get("last_map_index"), bool) or idx < 1:
        return None
    n = len(req_images(req))
    if n and idx > n:
        return None
    return idx


def last_map_sentence(req: dict) -> str | None:
    """What to tell the model about the saved last map among the screenshots, or None when there is none."""
    idx = last_map_index(req)
    old = bool(req.get("last_map_old"))
    if idx is None and not old:
        return None
    several = len(req_images(req)) > 1
    if idx is not None:
        which = f"Screenshot {idx} (of the numbered screenshots below)"
        on_it = (f' Put the point and the path for the route on screenshot {idx} ("img": {idx} in the <marks> block)'
                 ", not on the other screenshots." if several
                 else f" Put the point and the path for the route on it.")
    else:
        which, on_it = "The attached map screenshot", ""
    if old:
        return (f"{which} is the player's LAST map, taken earlier at a checkpoint"
                + (", BEFORE the other screenshots" if several and idx is not None else "")
                + "; the player is now somewhere else and cannot open the map until the next checkpoint." + on_it)
    return (f"{which} is the player's saved map: the last map the guide saved, taken a few minutes ago at the "
            "checkpoint the player is at now, so it still shows where the player is." + on_it)


def user_block(req: dict, extra: str = "") -> str:
    parts = []
    if req.get("dossier"):
        parts.append("Game overview from earlier research (for structure and order; it may contain spoilers - "
                     f"respect the spoiler level; verify specifics):\n{req['dossier'][:2500]}")
    if req.get("notes"):
        parts.append(f"What we know about the player's progress so far:\n{req['notes']}")
    # Map modes (guide.py fills these): icon meanings learned earlier, and which attached screenshot is the
    # saved last map (req["last_map_index"], 1-based) and whether it is stale (req["last_map_old"]).
    if req.get("map_icons"):
        parts.append(f"This game's map icons (learned earlier):\n{req['map_icons']}")
    lm_sentence = last_map_sentence(req)
    if lm_sentence:
        parts.append(lm_sentence)
    if extra:
        parts.append(extra)
    parts.append(f"Player's question: {req['question']}")
    # Repeated every turn: in a long (resumed) chat the model otherwise drifts from the chosen level.
    parts.append(LEVEL_REMINDER.get(req.get("level", "hint"), LEVEL_REMINDER["hint"]))
    return "\n\n".join(parts)


def history_msgs(req: dict, n: int = 8) -> list[dict]:
    return [{"role": m["role"], "content": m["content"]} for m in req.get("history", [])[-n:]]


# --------------------------------------------------------------------------- local (free)
class LocalBrain:
    """Ollama model + DuckDuckGo search + page reading. 100% free, no account."""

    name = "local"

    def __init__(self, cfg: dict):
        self.url = cfg.get("ollama_url", "http://127.0.0.1:11434")
        self.model = cfg.get("local_model", "qwen3:4b-instruct")
        self.vision_model = cfg.get("vision_model", "qwen3-vl:4b-instruct")
        self.num_ctx = int(cfg.get("num_ctx", 12288))
        self.keep_alive = cfg.get("keep_alive", "10m")   # unload after idle -> VRAM back to the game

    async def _chat(self, client, model, messages, stream=False, fmt=None, images=None, num_ctx=None):
        body = {"model": model, "messages": messages, "stream": stream, "think": False,
                "keep_alive": self.keep_alive,
                "options": {"num_ctx": num_ctx or self.num_ctx, "temperature": 0.3}}
        if fmt:
            body["format"] = fmt
        if images:
            body["messages"][-1]["images"] = images
        return body

    async def describe_image(self, client, path: str, game: str) -> str | None:
        b64 = base64.b64encode(Path(path).read_bytes()).decode()
        body = await self._chat(client, self.vision_model, [{
            "role": "user",
            "content": f"This is a screenshot from the video game {game}. Describe precisely what is "
                       "visible that could help identify where the player is and what they're stuck "
                       "on: location, UI text, objective text, puzzle elements, symbols, numbers, "
                       "items, doors. Transcribe any readable text exactly. Be factual, no guessing."}],
            images=[b64], num_ctx=8192)
        r = await client.post(f"{self.url}/api/chat", json=body, timeout=180)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()["message"]["content"].strip()

    async def plan_queries(self, client, req: dict) -> tuple[list[str], list[str]]:
        """Returns (search queries, rewritten progress notes)."""
        game = req.get("game") or ""
        recent = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in req.get("history", [])[-4:])
        prompt = (f"Game: {game}\nKnown progress:\n{req.get('notes') or '-'}\n"
                  f"Recent conversation:\n{recent or '-'}\n\nPlayer's question: {req['question']}\n\n"
                  "Write 3 web search queries that would find the walkthrough section answering the player's "
                  "CURRENT question (not earlier ones - if they say they already did something, search for what "
                  "comes AFTER it). "
                  f"Each query MUST start with the game name \"{game}\". Use the specific names of places, "
                  "puzzles, items, bosses or chapters mentioned. Make one query broader "
                  "(e.g. '<game> <chapter/area> walkthrough'). "
                  "Also rewrite the player's progress notes as a complete, up-to-date list of at most 6 short "
                  "bullets: current chapter/area, what they've done, key items. Keep facts from 'Known progress' "
                  "that are still true, replace outdated locations, add what the player just said. Only facts the "
                  "player stated or that follow directly from it - no guesses from walkthroughs, and don't list "
                  "things that haven't happened (no 'no items yet' lines). "
                  'Reply as JSON: {"queries": ["...", "...", "..."], "progress": ["...", "..."]}')
        body = await self._chat(client, self.model, [{"role": "user", "content": prompt}], fmt="json",
                                num_ctx=4096)
        r = await client.post(f"{self.url}/api/chat", json=body, timeout=120)
        r.raise_for_status()
        try:
            d = json.loads(r.json()["message"]["content"])
            qs = [q.strip() for q in d.get("queries", []) if isinstance(q, str) and q.strip()][:3]
            prog = [p.strip() for p in d.get("progress", []) if isinstance(p, str) and p.strip()][:6]
        except Exception:
            qs, prog = [], []
        if not qs:
            qs = [f"{game} {req['question']}"[:200]]
        return qs, prog

    async def ask(self, req: dict):
        game = req.get("game") or ""
        async with httpx.AsyncClient() as client:
            try:
                await client.get(f"{self.url}/api/version", timeout=3)
            except Exception:
                yield {"type": "error", "text": "Ollama isn't running (the local AI). Start it or pick another brain."}
                return

            if req.get("image"):
                yield {"type": "status", "text": "Looking at your screenshot..."}
                try:
                    desc = await self.describe_image(client, (req.get("images") or ([req["image"]] if req.get("image") else []))[-1], game)
                except Exception as e:
                    desc = None
                    yield {"type": "status", "text": f"Screenshot reading failed ({e})"}
                if desc:
                    req = {**req, "question": req["question"] +
                           f"\n\n[What the player's screen shows: {desc}]"}
                else:
                    yield {"type": "status", "text": f"Vision model '{self.vision_model}' not installed - ignoring screenshot"}

            yield {"type": "status", "text": "Working out what to search..."}
            try:
                queries, progress = await self.plan_queries(client, req)
            except Exception as e:
                # e.g. Ollama busy updating/loading models: carry on with plain search terms.
                detail = e.response.text[:200] if isinstance(e, httpx.HTTPStatusError) else str(e)
                yield {"type": "status", "text": f"Search planning failed ({detail}) - using simple search"}
                q = req["question"].split("\n\n[What the player's screen shows")[0][:150]
                queries, progress = [f"{game} {q}", f"{game} walkthrough"], []
            if progress:
                yield {"type": "progress", "items": progress}
            yield {"type": "status", "text": "Searching: " + " | ".join(queries)}
            results = await asyncio.to_thread(research.search, queries, game)
            if not results:
                yield {"type": "status", "text": "No search results (offline?) - answering from memory"}
            yield {"type": "status", "text": f"Reading {min(5, len(results))} guide pages..."}
            pages = await research.fetch_pages(results, limit=5)
            sources = research.select_passages(pages, req["question"] + " " + req["question"] + " " + " ".join(queries))
            if not sources and results:   # pages blocked us: fall back to search snippets
                sources = [{"url": r["url"], "title": r["title"], "text": r["snippet"]} for r in results[:8]]
            yield {"type": "sources", "items": [{"url": s["url"], "title": s["title"]} for s in sources]}

            src_txt = "\n\n".join(f"[{i + 1}] {s['title']} ({s['url']})\n{s['text']}"
                                  for i, s in enumerate(sources)) or "(no sources found)"
            # Small models copy earlier answers when given chat turns, so pass a short recap instead.
            recap = "\n".join(f"- {'Player' if m['role'] == 'user' else 'You answered'}: {m['content'][:220]}"
                              for m in req.get("history", [])[-4:])
            extra = f"Sources:\n{src_txt}"
            if recap:
                extra = (f"Earlier in this conversation (already dealt with - do NOT repeat these answers):\n"
                         f"{recap}\n\n{extra}")
            msgs = [{"role": "system", "content": system_prompt(game, req.get("level", "hint"), notes=False,
                                                              mode=req.get("mode"))},
                    {"role": "user", "content": user_block(req, extra) +
                     "\n\nAnswer the player's NEW question above, using the sources."}]

            yield {"type": "status", "text": f"Writing answer ({self.model})..."}
            body = await self._chat(client, self.model, msgs, stream=True)
            async with client.stream("POST", f"{self.url}/api/chat", json=body, timeout=300) as r:
                if r.status_code != 200:
                    txt = (await r.aread()).decode(errors="replace")
                    yield {"type": "error", "text": f"Ollama error {r.status_code}: {txt[:300]}"}
                    return
                in_think, pending = False, ""
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    d = json.loads(line)
                    t = d.get("message", {}).get("content", "")
                    if not t:
                        continue
                    # Some models emit <think>...</think> even with think=false: hide it.
                    pending += t
                    if not in_think and pending.lstrip().startswith("<think>"):
                        in_think = True
                    if in_think:
                        if "</think>" in pending:
                            pending = pending.split("</think>", 1)[1].lstrip()
                            in_think = False
                        else:
                            continue
                    if pending and not "<think>".startswith(pending.lstrip()[:7] or "x"):
                        yield {"type": "token", "text": pending}
                        pending = ""
                if pending and not in_think:
                    yield {"type": "token", "text": pending}


# --------------------------------------------------------------------------- Gemini (free key)
class GeminiBrain:
    """Google Gemini with built-in Google Search. Free tier with a free API key
    from https://aistudio.google.com/apikey (put it in config.json 'gemini_api_key')."""

    name = "gemini"

    def __init__(self, cfg: dict):
        self.key = cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")
        self.model = cfg.get("gemini_model", "gemini-2.5-flash")

    async def ask(self, req: dict):
        if not self.key:
            yield {"type": "error", "text": "No Gemini API key set (config.json -> gemini_api_key)."}
            return
        contents = []
        for m in history_msgs(req):
            contents.append({"role": "model" if m["role"] == "assistant" else "user",
                             "parts": [{"text": m["content"]}]})
        parts = [{"text": user_block(req, "Search the web for walkthroughs before answering.")}]
        for ip in (req.get("images") or ([req["image"]] if req.get("image") else [])):
            parts.append({"inline_data": {"mime_type": "image/jpeg",
                                          "data": base64.b64encode(Path(ip).read_bytes()).decode()}})
        contents.append({"role": "user", "parts": parts})
        body = {"system_instruction": {"parts": [{"text": system_prompt(req.get("game"), req.get("level", "hint"), image_sizes=req.get("image_sizes"), image_size=req.get("image_size"), mode=req.get("mode"))}]},
                "contents": contents, "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0.3}}
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}"
               ":streamGenerateContent?alt=sse")
        yield {"type": "status", "text": f"Searching Google ({self.model})..."}
        sources = {}
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", url, json=body, timeout=180,
                                     headers={"x-goog-api-key": self.key}) as r:
                if r.status_code != 200:
                    txt = (await r.aread()).decode(errors="replace")
                    yield {"type": "error", "text": f"Gemini error {r.status_code}: {txt[:400]}"}
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    d = json.loads(line[5:])
                    for c in d.get("candidates", []):
                        for p in c.get("content", {}).get("parts", []):
                            if p.get("text") and not p.get("thought"):
                                yield {"type": "token", "text": p["text"]}
                        for g in c.get("groundingMetadata", {}).get("groundingChunks", []):
                            w = g.get("web") or {}
                            if w.get("uri"):
                                sources[w["uri"]] = w.get("title", w["uri"])
        if sources:
            yield {"type": "sources", "items": [{"url": u, "title": t} for u, t in sources.items()]}


# --------------------------------------------------------------------------- Claude Code CLI
# Game Guide's hidden claude runs must not load the user's Claude Code plugins: a plugin can speak or
# rewrite the raw answers (hidden notes included), and every plugin costs time and usage.
def no_plugins_settings(config_dir=None) -> str:
    """--settings JSON that switches off every plugin found in the user's Claude Code config folder
    (settings.json "enabledPlugins" + plugins\\installed_plugins.json). Read-only, never raises."""
    d = Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    names = []
    for f, pick in ((d / "settings.json", lambda j: j.get("enabledPlugins")),
                    (d / "plugins" / "installed_plugins.json", lambda j: j.get("plugins"))):
        try:
            found = pick(json.loads(f.read_text(encoding="utf-8-sig")))
            if isinstance(found, dict):
                names += [k for k in found if isinstance(k, str)]
        except (OSError, ValueError, AttributeError):
            pass
    return json.dumps({"enabledPlugins": {k: False for k in dict.fromkeys(names)}})


def find_claude_exe() -> str | None:
    """Path of the Claude Code CLI, or None. Never raises."""
    try:
        # Standalone install (since the desktop app stopped shipping the CLI, 2026-10-28): wins when present.
        for cand in (Path.home() / ".local" / "bin" / "claude.exe",
                     Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "claude.exe"):
            if cand.is_file():
                return str(cand)
        which = shutil.which("claude")
        if which and Path(which).is_file():
            return which
        # Older layout: the CLI bundled with the desktop app (a packaged MSIX app: outside the package its files
        # live under Packages\Claude_*). App updates changed the layout (claude-code\<ver>\claude.exe, later
        # claude-code\<ver>\<hash>\claude.exe), so search below each claude-code folder and sort by <ver>.
        roots = [Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code"]
        roots += list((Path(os.environ.get("LOCALAPPDATA", "")) / "Packages").glob(
            "Claude_*/LocalCache/Roaming/Claude/claude-code"))
        found = []
        for root in roots:
            for exe in root.glob("**/claude.exe"):
                ver = exe.relative_to(root).parts[0]
                if re.fullmatch(r"\d+(\.\d+)*", ver):
                    found.append((tuple(int(x) for x in ver.split(".")), exe.stat().st_mtime, exe))
        return str(max(found)[2]) if found else None
    except Exception:
        return None


class ClaudeCliBrain:
    """Uses the Claude Code CLI bundled with the desktop app (your Claude subscription).
    Needs a one-time login: run the claude.exe yourself and type /login."""

    name = "claude"

    def __init__(self, cfg: dict):
        self.model = cfg.get("claude_model", "haiku")
        # Screenshot questions (puzzles, panels) and retries get the careful model: it reads the
        # image properly instead of guessing which symbol is which.
        self.careful_model = cfg.get("claude_model_careful", "opus")
        self.exe = find_claude_exe()

    async def ask(self, req: dict):
        if not self.exe:
            yield {"type": "error", "text": "Claude Code CLI not found."}
            return
        prompt = user_block(req, "Use WebSearch/WebFetch to find walkthroughs before answering.")
        images = req_images(req)
        if images:
            # guide.py may append the SAVED last map after the fresh shots, so the list is not simply "in the
            # order taken": label that one, so marks land on the right image.
            lm_idx = last_map_index(req)
            listing = "\n".join(
                f"  {i + 1}: {ip}" + (" (the saved last map, taken earlier)" if i + 1 == lm_idx else "")
                for i, ip in enumerate(images))
            order = ("numbered as above; the fresh screenshots are in the order taken, the saved last map was "
                     "taken earlier" if lm_idx is not None else "in the order taken")
            prompt += (f"\n\nScreenshots of the player's screen, {order} (use Read to view each):\n{listing}\n"
                       "If the question is about something shown in them, describe the steps in terms of what is "
                       "visible (which button/symbol/valve) and add the <marks> block (with \"img\" = the number "
                       "above, when there is more than one screenshot).")
        model = self.careful_model if (images or req.get("careful")) else self.model
        cmd = [self.exe, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--model", model,
               "--allowedTools", "WebSearch,WebFetch,Read",
               "--disallowedTools", "Bash,Edit,Write,NotebookEdit,Agent",
               "--strict-mcp-config", "--settings", no_plugins_settings(),
               "--append-system-prompt", system_prompt(req.get("game"), req.get("level", "hint"),
                                                       image_sizes=req.get("image_sizes"), image_size=req.get("image_size"),
                                                       mode=req.get("mode"))]
        if req.get("claude_session"):
            cmd += ["--resume", req["claude_session"]]
        yield {"type": "status", "text": f"Asking Claude ({model}{' - careful mode' if model != self.model else ''})..."}
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=req.get("cwd"),
            creationflags=subprocess.CREATE_NO_WINDOW, limit=2 ** 24)
        async for raw in proc.stdout:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("type") == "stream_event":
                ev = d.get("event", {})
                if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    yield {"type": "token", "text": ev["delta"]["text"]}
                elif ev.get("type") == "content_block_start" and ev["content_block"].get("type") in ("server_tool_use", "tool_use"):
                    # Anything written before a search is narration ("I'll look that up...") - drop it.
                    yield {"type": "reset"}
                    name = ev["content_block"].get("name", "")
                    label = {"WebSearch": "Searching walkthroughs...", "web_search": "Searching walkthroughs...",
                             "WebFetch": "Reading a guide page...", "web_fetch": "Reading a guide page...",
                             "Read": "Looking at your screenshot..."}.get(name, f"Using {name}...")
                    yield {"type": "status", "text": label}
            elif d.get("type") == "result":
                if d.get("is_error"):
                    # Don't keep a failed session (e.g. not logged in) - resuming it later replays junk.
                    yield {"type": "claude_session", "id": None}
                    yield {"type": "error", "text": f"Claude: {d.get('result')}"}
                else:
                    yield {"type": "claude_session", "id": d.get("session_id")}
        await proc.wait()


def build_dossier(game: str, model: str = "sonnet", timeout: int = 300) -> str:
    """One-time research of a game (chapters, bosses, points of no return, missables...). Blocking; run in a thread."""
    exe = find_claude_exe()
    if not exe:
        raise RuntimeError("Claude Code CLI not found")
    cmd = [exe, "-p", DOSSIER_PROMPT.format(game=game), "--output-format", "json", "--model", model,
           "--allowedTools", "WebSearch,WebFetch",
           "--disallowedTools", "Bash,Edit,Write,Read,NotebookEdit,Agent", "--strict-mcp-config",
           "--settings", no_plugins_settings()]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    d = json.loads(r.stdout)
    if d.get("is_error"):
        raise RuntimeError(d.get("result"))
    text = (d.get("result") or "").strip()
    text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", text)
    # The model sometimes leaks a stray line like "Good enough; compose." before the markdown: drop it.
    m = re.search(r"(?m)^#", text)
    if m and m.start() > 0:
        text = text[m.start():]
    if "##" not in text:
        raise RuntimeError("no dossier content")
    return text.strip()


CLUES_PROMPT = (
    'The text below was read from the screen of the video game "{game}" (a note, a sign, dialogue or a menu). '
    "List the clues it gives the player: tasks to do, places to go, names of people or things that matter, codes or "
    "numbers. Each clue is ONE short plain sentence (at most 15 words), e.g. \"go to the burial grounds\" or "
    "\"the key is under the third stone\". Skip HUD text, menu labels, button prompts and flavour text that gives no "
    "task or fact. Reply with ONLY a JSON list of 0-5 strings, like [\"...\", \"...\"], and reply [] if there is "
    "nothing useful. No other text.\n\nText:\n{text}")

# Every tool the CLI offers: extract_clues must only read the prompt and answer, nothing else.
NO_TOOLS = "Bash,Edit,Write,Read,NotebookEdit,Agent,Task,WebSearch,WebFetch,Glob,Grep,TodoWrite,BashOutput,KillShell"


def extract_clues(text: str, game: str, model: str = "haiku", timeout: int = 60) -> list[str]:
    """Clues (tasks, places, names, codes) from text read off the screen (F8). Blocking; run in a thread.
    Never raises: [] on any failure, bad JSON or useless text."""
    try:
        text = " ".join((text or "").split())
        if len(text) < 12:   # a word or two can't hold a clue
            return []
        exe = find_claude_exe()
        if not exe:
            return []
        cmd = [exe, "-p", CLUES_PROMPT.format(game=game or "a video game", text=text[:4000]),
               "--output-format", "json", "--model", model,
               "--disallowedTools", NO_TOOLS, "--strict-mcp-config", "--settings", no_plugins_settings()]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        d = json.loads(r.stdout)
        if d.get("is_error"):
            return []
        out = (d.get("result") or "").strip()
        out = re.sub(r"^```(?:json)?\s*|\s*```$", "", out)
        m = re.search(r"\[.*\]", out, re.S)   # tolerate a stray sentence around the list
        if not m:
            return []
        raw = json.loads(m.group(0))
        if not isinstance(raw, list):
            return []
        clues = []
        for c in raw:
            if not isinstance(c, str):
                continue
            c = " ".join(c.split()).strip(" -*•\"'")
            if 3 <= len(c) <= 160 and c.lower() not in (x.lower() for x in clues):
                clues.append(c)
        return clues[:5]
    except Exception:
        return []


BRAINS = {"local": LocalBrain, "gemini": GeminiBrain, "claude": ClaudeCliBrain}
