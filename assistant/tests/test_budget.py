"""اختبارات وحدة حارس الميزانية (Budget Guard) — السقوف والاستهلاك."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.budget import BudgetTracker, BudgetExceededError

    # 1. اختبار استهلاك الخطوات الطبيعي
    bt = BudgetTracker(max_steps=5, max_tokens=100, max_retries=2, max_points=10)
    for _ in range(5):
        bt.record_step()
    assert bt.current_steps == 5

    # تجاوز سقف الخطوات
    try:
        bt.record_step()
        print("FAIL: كان يجب رفع BudgetExceededError عند تجاوز سقف الخطوات!", file=sys.stderr)
        sys.exit(1)
    except BudgetExceededError:
        pass  # صحيح ومطلوب

    # 2. اختبار تجاوز سقف التوكنات
    bt2 = BudgetTracker(max_steps=10, max_tokens=50)
    bt2.record_tokens(30)
    assert bt2.current_tokens == 30
    try:
        bt2.record_tokens(25)  # 30 + 25 = 55 > 50
        print("FAIL: كان يجب رفع BudgetExceededError عند تجاوز سقف التوكنات!", file=sys.stderr)
        sys.exit(1)
    except BudgetExceededError:
        pass

    # 3. اختبار تجاوز سقف المحاولات
    bt3 = BudgetTracker(max_retries=2)
    bt3.record_retry()
    bt3.record_retry()
    try:
        bt3.record_retry()
        print("FAIL: كان يجب رفع BudgetExceededError عند تجاوز سقف المحاولات!", file=sys.stderr)
        sys.exit(1)
    except BudgetExceededError:
        pass

    # 4. اختبار ملخص الاستهلاك
    summary = bt2.get_summary()
    assert "tokens" in summary
    assert summary["tokens_count"] == 30

    print("PASS: test_budget completed successfully")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
