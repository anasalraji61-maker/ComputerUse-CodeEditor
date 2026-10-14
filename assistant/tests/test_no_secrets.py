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
    import subprocess
    skip_dirs = {".git", "__pycache__", "venv", ".venv"}

    def candidates():
        """الملفات المتتبَّعة أو غير المتجاهَلة بـ git (ملف .env محلي متجاهَل لا يُفشل الفحص)؛ بدون git: كل الملفات."""
        try:
            r = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                names = [n for n in r.stdout.decode("utf-8", "replace").split("\0") if n]
                if names:
                    return [ROOT / n for n in names]
        except Exception:
            pass
        return [p for p in ROOT.rglob("*") if p.is_file() and not skip_dirs.intersection(p.relative_to(ROOT).parts)]

    hits = []
    for p in candidates():
        if not p.is_file():
            continue
        n = p.name.lower()
        if (n == ".env" or n.endswith(".env") or (n.startswith(".env.") and n != ".env.example")) and n != "example.env":
            hits.append(f"{p.relative_to(ROOT)}: ملف بيئة داخل المستودع")
            continue
        if p.resolve() == pathlib.Path(__file__).resolve():
            continue
        try:
            if p.stat().st_size > 2_000_000:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue  # ثنائي
        text = raw.decode("utf-8", errors="ignore")
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
