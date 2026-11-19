"""Checks every text file in the repo for things that must not be published: absolute Windows paths,
user folders, e-mail addresses, things that look like API keys, private IP addresses, leftover
working notes and backup files. Only general patterns, no personal words.
Only the files git would add are checked (your own settings and data are ignored, see .gitignore), so it
also passes in a folder where the app has been used.
Run: python tests/test_privacy.py   (exit 0 = all PASS)"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _repo_files import ROOT, ignore_rules, ignored, repo_files  # noqa: E402

TEXT = {".py", ".lua", ".json", ".md", ".txt", ".html", ".js", ".ps1", ".bat", ".gitignore", ""}
SELF = Path(__file__).resolve()

IP_OK = ("127.0.0.1", "0.0.0.0")
IP_TEST_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")

# Words are joined at run time so this file does not flag itself.
JOB = "|".join([
    "PL" + r"AN\d?\.md", r"\b" + "re" + r"con\b", r"\bfix" + r" round", r"\bround[- ]\d\b",
    r"\bpart [A-H]\b", r"(?-i:\b[" + "BRUTCLIM" + r"][1-9]\d?\b)", r"\bspec point", r"\bhard rule",
    r"\bfinding #?\d", r"\bfix" + r"er\b", r"\bre-?" + "test" + r"er\b",
    r"\bre-?" + "review" + r"er\b", r"\buser[,:]? (?:choice |confirmed |request )?\d{4}-\d\d-\d\d",
    r"\(" + "user" + r"\)", r"\buser'?s " + "request", r"\buser " + "choice", "CONTRACT" + "-ISSUE",
    r"\buser'?s [\w-]+\.(?:lua|py|js|dll)\b",               # someone's own files that are not in the repo
    r"\bfix ?#?\d+\b", r"\bbuild " + r"plan\b",                # work labels ("fix2:", "Fix #7")
])
STD_DIRS = re.compile(r"[A-Za-z]:[\\/]+(?:Program Files|Windows)\b", re.I)   # standard Windows folders are fine
PATTERNS = {
    "absolute Windows path": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?:\\\\?|/)(?:[A-Za-z]|\.\.\.)"),
    "user folder": re.compile(r"[\\/]Users[\\/][^\\/\s\"']+", re.I),
    "e-mail": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}"),
    "api key": re.compile(r"AIza[0-9A-Za-z_\-]{30,}|\bsk-[A-Za-z0-9_\-]{20,}|\bgh[pousr]_[A-Za-z0-9]{30,}"
                          r"|\bxox[abp]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY"),
    "ipv4": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "job note": re.compile(JOB, re.I),
}


def allowed(kind, hit, line, pos=0):
    if kind == "absolute Windows path":
        return any(m.start() == pos for m in STD_DIRS.finditer(line))
    if kind == "e-mail":
        dom = hit.split("@", 1)[1].lower()
        return ("noreply" in hit.lower() or dom.startswith("example.") or dom.endswith(".example")
                or re.search(r"@\d", hit) is not None)        # "name@1.2.3" = a version pin, not a mail
    if kind == "ipv4":
        parts = [int(p) for p in hit.split(".")]
        if any(p > 255 for p in parts):
            return True                                          # not an address (a version number)
        return (hit in IP_OK or hit.startswith(IP_TEST_NETS) or "version" in line.lower()
                or "==" + hit in line)                           # a pinned package version
    return False


fails = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        fails.append(name)


hits = []
backups = []
publish = list(repo_files())
for p, rel in publish:
    if re.search(r"\.bak|\.orig$|~$", p.name, re.I):
        backups.append(rel.as_posix())
    if p == SELF or (p.suffix.lower() not in TEXT and p.name != ".gitignore"):
        continue
    minified = p.name.endswith(".min.js")
    for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        for kind, rx in PATTERNS.items():
            if minified and kind in ("job note", "ipv4"):
                continue                                         # third-party minified code: names, not notes
            for m in rx.finditer(line):
                if not allowed(kind, m.group(0), line, m.start()):
                    hits.append(f"{rel.as_posix()}:{n}: {kind}: {m.group(0)[:60]}")

for h in hits[:80]:
    print("   ", h)
check("no private paths, mails, keys, addresses or working notes", not hits, f"{len(hits)} hits")
check("no backup files", not backups, ", ".join(backups[:10]))
check("no personal config.json in the files git would add",
      not any(rel.as_posix() == "config.json" for _p, rel in publish))
check("games.json is empty", (ROOT / "games.json").read_text(encoding="utf-8").strip() == "{}")

# the checker itself must catch the patterns it is meant to catch
samples = {"absolute Windows path": "X:" + "\\" + "Stuff", "user folder": "C:" + "\\Users\\" + "someone",
           "e-mail": "a.person" + "@" + "mail.test.org", "api key": "AI" + "za" + "x" * 35,
           "ipv4": "10.1" + ".2.3", "job note": "see " + "PL" + "AN.md"}
for kind, s in samples.items():
    check(f"self-test: {kind} is caught",
          any(not allowed(kind, m.group(0), s, m.start()) for m in PATTERNS[kind].finditer(s)))
for s in ("X:/" + "Stuff", "fixed in " + "B" + "12", "(" + "user" + ") asked", '"@X:' + '\\...\\Mods"',
          "from the user" + "'s SomeMod.lua", "-- " + "fix" + "2: x", "the door (" + "fix" + "1)", "Fix " + "#7.",
          "see the build" + " plan"):
    check(f"self-test: {s!r} is caught", any(rx.search(s) for rx in PATTERNS.values()))
check("self-test: a standard Windows folder is fine",
      all(allowed("absolute Windows path", m.group(0), t, m.start())
          for t in ["C:" + "\\Program Files\\x"] for m in PATTERNS["absolute Windows path"].finditer(t)))

rules = ignore_rules()
for parts, want in ((("config.json",), True), (("games", "some-game", "chat.md"), True), (("games",), False),
                    (("last_game.txt",), True), (("tests", "out2", "a.txt"), True), ((".venv", "Lib", "x.py"), True),
                    (("sense", "backup", "2026", "mods.txt"), True), (("web", "index.html.bak1"), True),
                    (("guide.log",), True), (("guide.py",), False), (("games.json",), False),
                    (("sense", "helper", "x.lua"), False), (("tests", "test_privacy.py"), False)):
    check(f"self-test: {'/'.join(parts)} is {'ignored' if want else 'checked'}", ignored(parts, rules) == want)

print("RESULT: " + ("ALL PASS" if not fails else f"{len(fails)} FAIL"))
sys.exit(1 if fails else 0)
