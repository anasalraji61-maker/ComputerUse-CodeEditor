"""يشغّل ملفات الاختبار test_*.py في المستودع ويكتب النتائج في docs/TEST_RESULTS.md.

يُستدعى من sync_repo.bat. يعيد التشغيل فقط عندما يتغيّر محتوى ملفات .py.
السبب: AI Studio يكتب الكود لكنه لا يستطيع تشغيل بايثون على ويندوز، فالنتائج الحقيقية تأتي من هنا.
"""
import hashlib
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "TEST_RESULTS.md"
STATE = ROOT / "tools" / ".last_tested"
SKIP = {".git", "venv", ".venv", "node_modules", "__pycache__", "CONSULTATION_PACK", "collab"}
TIMEOUT = 300


def py_files():
    for p in sorted(ROOT.rglob("*.py")):
        if SKIP.intersection(p.relative_to(ROOT).parts):
            continue
        yield p


def fingerprint():
    h = hashlib.sha1()
    for p in py_files():
        h.update(str(p.relative_to(ROOT)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def is_test(p):
    return p.name.startswith("test_") or p.name.endswith("_test.py")


def run(path):
    env = dict(os.environ, COS_TEST_MODE="1", PYTHONIOENCODING="utf-8")
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, str(path)], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TIMEOUT)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out, time.time() - t0
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {TIMEOUT}s", TIMEOUT


def main():
    fp = fingerprint()
    if STATE.exists() and STATE.read_text().strip() == fp:
        return 0
    tests = [p for p in py_files() if is_test(p)]
    rows, details, failed = [], [], 0
    for p in tests:
        ok, out, secs = run(p)
        rel = p.relative_to(ROOT).as_posix()
        rows.append(f"| `{rel}` | {'PASS' if ok else '**FAIL**'} | {secs:.1f}s |")
        tail = "\n".join(out.splitlines()[-(5 if ok else 40):])
        details.append(f"### {rel}\n```\n{tail}\n```")
        failed += 0 if ok else 1
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    body = [f"# نتائج الاختبارات الفعلية (آلية)\n",
            f"آخر تشغيل: {stamp} — {len(tests)} ملف، فشل: {failed}\n",
            "هذه النتائج تُنتَج على لابتوب المالك. لا تدّعِ نجاحاً غير مذكور هنا.\n"]
    if tests:
        body += ["| الملف | النتيجة | الزمن |", "|---|---|---|", *rows, "", *details]
    else:
        body.append("لا توجد ملفات اختبار test_*.py بعد.")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(body) + "\n", encoding="utf-8")
    STATE.write_text(fp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
