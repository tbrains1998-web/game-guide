"""Run every tests/test_*.py once (each in its own process) and print pass/fail per file.
Pass = exit code 0 and no 'FAIL' verdict line in the output. A Traceback that a test logs on
purpose (exit 0, no FAIL line) does not count as a failure; an uncaught one exits nonzero.
Exit code 0 only when every file passes. Never touches the running app."""
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
rows = []
for f in files:
    name = os.path.basename(f)
    try:
        p = subprocess.run([PY, f], cwd=os.path.dirname(HERE), capture_output=True, text=True,
                           timeout=300, encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) + "\nTIMEOUT"
        code = "timeout"
    verdict = [l for l in out.splitlines() if re.search(r"RESULT|ALL PASS|\bFAIL\b|Traceback|SKIP", l)]
    fail_lines = [l for l in out.splitlines() if re.match(r"\s*FAIL\b", l)]
    ok = code == 0 and not fail_lines
    rows.append((name, ok, code, verdict[-3:] if verdict else out.strip().splitlines()[-2:]))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  (exit {code})")
    for v in (verdict[-3:] if verdict else out.strip().splitlines()[-2:]):
        print("       ", v[:160])
    sys.stdout.flush()
print()
print(f"{sum(1 for r in rows if r[1])}/{len(rows)} files pass")
sys.exit(0 if rows and all(r[1] for r in rows) else 1)
