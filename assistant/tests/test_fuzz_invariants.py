"""fuzz الثوابت (سريع): 400 سيناريو (60 على ويندوز) عشوائي حتمي بأخطاء مُحقنة؛ يجب ألا يُفقد محتوى، ولا يخرج أثر من المجلد،
ويعيد التراجع الشجرة كما كانت، ولا يقبل المدقق أثراً خاطئاً، ولا يصير الرصيد سالباً. الأداة الكاملة: tools/fuzz_invariants.py"""
import os
import pathlib
import subprocess
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

try:
    win = os.name == "nt"   # على ويندوز: مجموعات أسماء لنظام ملفات غير حساس للحالة وعدد أقل (يبقى الاختبار سريعاً)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "fuzz_invariants.py"), "--n", "60" if win else "400", "--no-after-faults",
                        "--time-limit", "60" if win else "120"] + (["--fs-safe"] if win else []),
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
