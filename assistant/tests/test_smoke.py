"""اختبار الدخان (Smoke Test) — يتحقق أن حزمة assistant تُستورد بنجاح وتعمل الثوابت الأساسية."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import assistant
    assert hasattr(assistant, "__version__"), "حزمة assistant تفتقد __version__"
    assert assistant.__version__ == "0.1.0", f"إصدار غير متوقع: {assistant.__version__}"
    print(f"PASS: assistant package imported successfully (v{assistant.__version__})")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: فشل استيراد assistant: {exc}", file=sys.stderr)
    sys.exit(1)
