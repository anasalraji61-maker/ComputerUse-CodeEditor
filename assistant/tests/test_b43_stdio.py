"""B43: أدوات سطر الأوامر لا تنهار عند الطباعة العربية إلى أنبوب/كونسول غير UTF-8 (cp1252، PYTHONUTF8=0، بلا -X utf8). بلا tkinter."""
import os
import pathlib
import subprocess
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

try:
    def legacy_env(**extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONIOENCODING", "PYTHONUTF8", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "COS_OPENAI_API_KEY")}
        env.update(PYTHONIOENCODING="cp1252", PYTHONUTF8="0", COS_NO_DIALOG="1", COS_PROVIDER="", **extra)
        return env

    def run(args, cwd=ROOT, **extra):
        r = subprocess.run([sys.executable] + args, cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace",
                           env=legacy_env(**extra), timeout=200)
        return r, (r.stdout or "") + (r.stderr or "")

    def no_crash(out, who):
        assert "UnicodeEncodeError" not in out and "Traceback" not in out, (who, out[-600:])

    # 1) bench: رسالة خطأ عربية على stderr (لا مفتاح)
    r, out = run([str(ROOT / "bench" / "run_bench.py"), "--provider", "anthropic"])
    no_crash(out, "bench")
    assert r.returncode != 0 and "خطأ" in out, (r.returncode, out[-400:])
    print("ok 1: bench CLI survives cp1252")

    # 2) launch.py و__main__.py: فشل الاستيراد يكتب رسالة عربية على stderr
    for entry in ("launch.py", "assistant/__main__.py"):
        T = pathlib.Path(tempfile.mkdtemp(prefix="b43_shim_")).resolve()
        (T / "assistant").mkdir()
        (T / "assistant" / "__init__.py").write_text("", encoding="utf-8")
        (T / "assistant" / "app.py").write_text("raise ImportError('boot failed خطأ')\n", encoding="utf-8")
        (T / "entry.py").write_text((ROOT / entry).read_text(encoding="utf-8"), encoding="utf-8")
        r, out = run([str(T / "entry.py")], cwd=T, COS_DATA_DIR=str(T / "data"), PYTHONPATH=str(T))
        assert "UnicodeEncodeError" not in out, (entry, out[-600:])   # التتبّع المقصود لفشل الاستيراد مسموح؛ انهيار الترميز لا
        assert r.returncode == 1 and "تعذّر تشغيل المساعد" in out, (entry, r.returncode, out[-400:])
    print("ok 2: launch/__main__ stderr survives cp1252")

    # 3) fuzz: تقرير عربي على stdout
    r, out = run([str(ROOT / "tools" / "fuzz_invariants.py"), "--n", "12", "--no-after-faults", "--time-limit", "60"])
    no_crash(out, "fuzz")
    assert r.returncode == 0 and "total violations: 0" in out, (r.returncode, out[-400:])
    print("ok 3: fuzz tool survives cp1252")
    print("PASS test_b43_stdio")
except Exception:
    traceback.print_exc()
    print("FAIL test_b43_stdio")
    sys.exit(1)
