"""The files git would add in this repo (tracked + new, not ignored). Shared by the tests that scan the repo,
so a .venv (or a venv under any other name), your own settings or other ignored folders are never read."""
import fnmatch
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ignore_rules():
    p = ROOT / ".gitignore"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def ignored(parts, rules, is_dir=False):
    """Would .gitignore skip this path (a tuple of folder names + file name; is_dir = the last part is a folder)?
    shortcut: no "!" rules and no nested .gitignore files (this repo has neither); git itself is used when there."""
    for i, name in enumerate(parts):
        folder = is_dir or i < len(parts) - 1
        sub = "/".join(parts[:i + 1])
        for r in rules:
            if r.endswith("/") and not folder:
                continue
            pat = r.strip("/")
            if fnmatch.fnmatch(sub if "/" in pat or r.startswith("/") else name, pat):
                return True
    return False


def git_files():
    """Relative paths from git, or None when this is not a git folder (or git is missing)."""
    try:
        p = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return [Path(n) for n in p.stdout.decode("utf-8", "replace").split(chr(0)) if n]


def walk_files():
    """Same list without git: walk the folder, never going into .git or an ignored folder."""
    rules, out = ignore_rules(), []
    for d, dirs, names in os.walk(ROOT):
        rel = Path(d).relative_to(ROOT).parts
        dirs[:] = [x for x in dirs if x != ".git" and not ignored(rel + (x,), rules, is_dir=True)
                   and not (Path(d) / x / "pyvenv.cfg").is_file()]
        out += [Path(*rel, n) for n in names if not ignored(rel + (n,), rules)]
    return out


def repo_files(suffix=None):
    """(absolute path, relative path) of every file git would add, optionally only one suffix (".py")."""
    listed = git_files()
    if listed is None:
        listed = walk_files()
    venv = {}                                  # folder -> is it a Python venv (any name, e.g. env\)?

    def in_venv(rel):
        for i in range(1, len(rel.parts)):
            d = Path(*rel.parts[:i])
            if d not in venv:
                venv[d] = (ROOT / d / "pyvenv.cfg").is_file()
            if venv[d]:
                return True
        return False
    for rel in sorted(listed):
        p = ROOT / rel
        if p.is_file() and (suffix is None or p.suffix.lower() == suffix) and not in_venv(rel):
            yield p, rel
