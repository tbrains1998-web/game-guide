"""Map helpers for Game Guide: is a screenshot the game's map?

Open-world games (Phantom Blade Zero first) have a map screen with icons and a completion %, but no
quest log. When the player grabs a screenshot of that map (F11, or F4 with the map open) the guide
should treat it as a map: answer in "map" mode and remember it as the game's last map.

Detection is cheap: Windows OCR on the screenshot (runs in its own process, ~0.3 s) and a few words
that map screens show ("map completion", "fast travel", "legend", ...). It never raises: on any
error it says "not a map" so a bad screenshot can never break a question.
"""

import re

import screenread

# Words that show up on map screens (HUD labels, buttons, titles). Case-insensitive.
MAP_WORDS_RE = re.compile(r"\bmap\b|map completion|world map|fast travel|reset view|legend|view collectibles", re.I)
# Words only the real map screen shows. A legend or collectibles list may still say "map" / "legend" /
# "view collectibles" (weak words), but not these. Only a strong match may replace a saved last map.
STRONG_MAP_RE = re.compile(r"map completion|world map", re.I)

STRONG, WEAK, NONE = 2, 1, 0

# A saved last map older than this (seconds) is stale: a fresh map-like shot may replace it even when
# the fresh one only has weak words (one OCR miss of "MAP COMPLETION" must not keep an hours-old map).
MAX_LAST_MAP_AGE = 10 * 60


def may_replace_last_map(new_strength: int, saved_strength: int | None, saved_age: float | None) -> bool:
    """Should a fresh map-like screenshot (strength `new_strength`) become the game's last map?
    `saved_strength` = strength of the saved last map (None = nothing saved; unknown counts as WEAK),
    `saved_age` = its age in seconds (None = unknown).
    - A STRONG shot (real map screen) always replaces.
    - A WEAK shot (a legend / collectibles list, or a map whose title the OCR missed) replaces when nothing
      is saved, when the saved one was weak too, or when the saved one is older than MAX_LAST_MAP_AGE.
      It never pushes out a strong map taken moments ago (the legend screen the guide asks for)."""
    if new_strength <= NONE:
        return False
    if new_strength >= STRONG or saved_strength is None:
        return True
    if (saved_strength or WEAK) <= WEAK:
        return True
    return saved_age is None or saved_age > MAX_LAST_MAP_AGE


def map_text_strength(text: str | None) -> int:
    """How much OCR text reads like a map screen: STRONG (2), WEAK (1) or NONE (0).
    Separate from the OCR step so it can be unit-tested."""
    if not text:
        return NONE
    if STRONG_MAP_RE.search(text):
        return STRONG
    if MAP_WORDS_RE.search(text):
        return WEAK
    return NONE


def map_text_score(text: str | None) -> bool:
    """True when OCR text reads like a map screen (strong or weak words)."""
    return map_text_strength(text) > NONE


def map_strength(path) -> int:
    """STRONG / WEAK / NONE for the screenshot at `path`. Never raises (NONE on any error)."""
    try:
        if not path:
            return NONE
        text = screenread.ocr_windows(str(path))
        return map_text_strength(text)
    except Exception:
        # Missing file, OCR process failed, winrt missing... -> just "not a map".
        return NONE


def is_map_screenshot(path) -> bool:
    """True if the screenshot at `path` looks like the game's map screen. Never raises."""
    return map_strength(path) > NONE
