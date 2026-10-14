"""fuzz الثوابت (سريع): 400 سيناريو عشوائي حتمي بأخطاء مُحقنة؛ يجب ألا يُفقد محتوى، ولا يخرج أثر من المجلد،
ويعيد التراجع الشجرة كما كانت، ولا يقبل المدقق أثراً خاطئاً، ولا يصير الرصيد سالباً. الأداة الكاملة: tools/fuzz_invariants.py"""
import os
import pathlib
import subprocess
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

try:
    if os.name == "nt":
        # سيناريوهات الأداة تفترض نظام ملفات حساساً لحالة الأحرف (مجلدا docs وDOCS معاً) ولم تُجرَّب على ويندوز بعد
        print("SKIP: fuzz الثوابت يعمل على لينكس فقط حالياً (python tools/fuzz_invariants.py يدوياً على ويندوز للتجربة)")
        sys.exit(0)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "fuzz_invariants.py"), "--n", "400", "--no-after-faults",
                        "--time-limit", "120"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT, env=env, timeout=280)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, out[-3000:]
    assert "total violations: 0" in out, out[-3000:]
    print("PASS: test_fuzz_invariants completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_fuzz_invariants", file=sys.stderr)
    sys.exit(1)
