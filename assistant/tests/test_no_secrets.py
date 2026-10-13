"""فحص أن المستودع العام لا يحوي أي مفتاح حقيقي الشكل ولا ملف .env."""
import re
import sys
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

try:
    patterns = [
        re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
        re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
        re.compile(r"sk-[A-Za-z0-9]{32,}"),
        re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{24,}['\"]"),
    ]
    skip_dirs = {".git", "__pycache__", "venv", ".venv"}
    hits = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or skip_dirs.intersection(p.relative_to(ROOT).parts):
            continue
        if p.name == ".env":
            hits.append(f"{p.relative_to(ROOT)}: ملف .env داخل المستودع")
            continue
        if p.suffix.lower() not in {".py", ".md", ".txt", ".bat", ".json", ".toml", ".cfg", ".ini", ".example", ""}:
            continue
        if p.resolve() == pathlib.Path(__file__).resolve():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in patterns:
            m = pat.search(text)
            if m:
                hits.append(f"{p.relative_to(ROOT)}: يشبه مفتاحاً ({m.group(0)[:8]}…)")
    assert not hits, "\n".join(hits)
    print("PASS: test_no_secrets completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_no_secrets", file=sys.stderr)
    sys.exit(1)
