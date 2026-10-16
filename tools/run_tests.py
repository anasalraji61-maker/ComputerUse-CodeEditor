"""يشغّل ملفات الاختبار test_*.py في المستودع ويكتب النتائج في docs/TEST_RESULTS.md.

يُستدعى من sync_repo.bat. يعيد التشغيل فقط عندما يتغيّر محتوى ملفات .py. رمز الخروج 1 عند وجود فشل (فيمنع sync_repo.bat رفع الكود).
السبب: AI Studio يكتب الكود لكنه لا يستطيع تشغيل بايثون على ويندوز، فالنتائج الحقيقية تأتي من هنا.
"""
import hashlib
import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "TEST_RESULTS.md"
STATE = ROOT / "tools" / ".last_tested"
SKIP = {".git", "venv", ".venv", "node_modules", "__pycache__", "CONSULTATION_PACK", "collab"}
TIMEOUT = 300


def _walk_files():
    """كل الملفات تحت الجذر مرتبة، مع تقليم مجلدات SKIP (.git وغيره) قبل النزول إليها: rglob كانت تمشي .git كله ثم تُرشِّح."""
    for cur, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for f in sorted(files):
            yield pathlib.Path(cur) / f


def py_files():
    for p in sorted(q for q in _walk_files() if q.suffix == ".py"):
        yield p


def _fp_files():
    """كل ملفات المستودع (لا .py فقط): تغيير وثيقة يحوي مفتاحاً يجب أن يعيد تشغيل فحص الأسرار."""
    skip_names = {OUT.name, STATE.name, "sync.log"}
    for p in sorted(_walk_files()):
        rel = p.relative_to(ROOT)
        if SKIP.intersection(rel.parts) or p.name in skip_names:
            continue
        if p.suffix.lower() in {".pyc", ".db", ".exe", ".log"}:
            continue
        try:
            if not p.is_file() or p.stat().st_size > 5_000_000:
                continue
        except OSError:
            continue
        yield p


def fingerprint():
    h = hashlib.sha1()
    for p in _fp_files():
        h.update(str(p.relative_to(ROOT)).encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"?")
    return h.hexdigest()


# ---- أمان: لا مفاتيح في بيئة الاختبارات ولا في docs/TEST_RESULTS.md (يُرفع إلى GitHub آلياً) ----
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)     # أسماء تُستبعد من بيئة الأبناء
_EXTRA_ENV = ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONUTF8",
              "TZ", "TCL_LIBRARY", "TK_LIBRARY", "LANG", "GIT_EXEC_PATH", "SSL_CERT_FILE")

try:   # القائمة المشتركة (assistant/redact.py)؛ احتياط مبسّط إن تعذّر استيراد الحزمة (مثلاً أثناء سحب غير مكتمل)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from assistant.redact import redact, secret_env_values as secret_values  # noqa: F401
except Exception:  # pragma: no cover
    _FALLBACK = [re.compile(p) for p in (
        r"(?<![A-Za-z0-9_\-])sk-ant-[A-Za-z0-9_\-]{10,}", r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9]{20,}",
        r"(?<![A-Za-z0-9_\-])AIza[0-9A-Za-z_\-]{30,}", r"(?<![A-Za-z0-9_\-])gh[pousr]_[A-Za-z0-9]{30,}",
        r"(?<![A-Za-z0-9_\-])github_pat_[A-Za-z0-9_]{20,}", r"(?<![A-Za-z0-9_\-])AKIA[0-9A-Z]{16}",
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{20,}")]

    def secret_values(environ=None):
        env = os.environ if environ is None else environ
        return sorted({v for k, v in env.items() if isinstance(v, str) and len(v) >= 12 and "/" not in v and "\\" not in v
                       and re.search(r"(^|_)(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)S?($|_)|_KEY$", k, re.I)}, key=len, reverse=True)

    def redact(text, environ=None):
        for v in secret_values(environ):
            text = text.replace(v, "***")
        for pat in _FALLBACK:
            text = pat.sub("***", text)
        return text


def child_env(data_dir, environ=None):
    """بيئة الاختبارات: قائمة بيضاء (minimal_env + متغيرات COS_* غير السرية + ما يلزم للعرض)، بلا أي مفتاح/توكن/سر."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from assistant.tools.shell import minimal_env
    src = os.environ if environ is None else environ
    env = minimal_env(src)
    for k, v in src.items():
        if not isinstance(v, str):
            continue
        if k in _EXTRA_ENV or k.startswith("LC_"):
            env[k] = v
        elif k.startswith("COS_") and not _SECRET_NAME.search(k):
            env[k] = v
    env.update(COS_TEST_MODE="1", PYTHONIOENCODING="utf-8", COS_DATA_DIR=data_dir)
    return env


def is_test(p):
    return p.name.startswith("test_") or p.name.endswith("_test.py")


def _kill_tree(proc):
    """يقتل العملية وأبناءها (اختبارات تُشغّل عمليات فرعية): taskkill /T /F على ويندوز، وkillpg لمجموعة العملية على posix."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def run(path):
    import tempfile
    t0 = time.time()
    # بيانات التطبيق في مجلد مؤقت: الاختبارات لا تمس إعدادات المالك الحقيقية (~/.cos_assistant)
    # ignore_cleanup_errors: ملف ما زال مفتوحاً في ويندوز (قاعدة بيانات/قفل) لا يُسقط التشغيل كله عند التنظيف
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data:
        env = child_env(data)
        # posix: جلسة جديدة ليقتل killpg الشجرة كلها. ويندوز: بلا creationflags (taskkill /T يكفي ويتتبّع الأبناء بالأب)؛ مجموعة عمليات جديدة
        # تعطّل Ctrl+C وتغيّر سلوك الأبناء ذوي الكونسول. stdin=DEVNULL: لا اختبار ولا أداة يرثان إدخال الكونسول فينتظران سؤالاً إلى الأبد.
        kw = {} if os.name == "nt" else {"start_new_session": True}
        proc = subprocess.Popen([sys.executable, str(path)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kw)
        try:
            stdout, stderr = proc.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=30)
            except Exception:
                pass
            return False, f"TIMEOUT after {TIMEOUT}s", TIMEOUT
    out = redact(((stdout or "") + (stderr or "")).strip())
    return proc.returncode == 0, out, time.time() - t0


def main():
    for _s in (sys.stdout, sys.stderr):   # أنابيب/كونسول غير UTF-8 (cp1252) أو بلا كونسول: الطباعة العربية لا تُسقط الأداة
        if _s is not None and hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    fp = fingerprint()
    if STATE.exists():
        saved = STATE.read_text(encoding="utf-8").strip()
        if saved == fp:
            return 0
        if saved == fp + ":FAIL":
            return 1  # الكود لم يتغير منذ آخر فشل: يبقى الفشل قائماً ولا يُرفع كود فاشل
    tests = [p for p in py_files() if is_test(p)]
    rows, details, failed = [], [], 0
    for p in tests:
        ok, out, secs = run(p)
        rel = p.relative_to(ROOT).as_posix()
        skipped = ok and out.splitlines() and out.splitlines()[-1].startswith("SKIP")
        rows.append(f"| `{rel}` | {'SKIP (لم يُختبر فعلياً)' if skipped else ('PASS' if ok else '**FAIL**')} | {secs:.1f}s |")
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
    STATE.write_text(fp if failed == 0 else fp + ":FAIL", encoding="utf-8")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
