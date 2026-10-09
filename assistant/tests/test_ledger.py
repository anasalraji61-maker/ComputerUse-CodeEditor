"""اختبارات دفتر النقاط المحلي SQLiteLedger — الرصيد وسقف الإنفاق والاسترجاع."""
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.ledger import (
        SQLiteLedger,
        SpendingLimitExceededError,
        InsufficientBalanceError,
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = pathlib.Path(tmp_dir) / "test_ledger.db"
        ledger = SQLiteLedger(db_path=db_path, initial_balance=20, spending_limit=10)

        # 1. التحقق من الرصيد وسقف الإنفاق الأولي
        assert ledger.get_balance() == 20
        assert ledger.get_total_spent() == 0

        # 2. تقدير الكلفة
        est = ledger.estimate_cost("تنظيم ملفات", steps_count=5)
        assert est == 1

        # 3. تسجيل بدء مهمة وخصم ناجح
        ledger.record_task_start("task_001", "تنظيم ملفات", estimated_points=1)
        ledger.record_task_success("task_001", charged_points=2)
        assert ledger.get_balance() == 18
        assert ledger.get_total_spent() == 2

        # 4. فشل بسبب خطأ في المنصة: لا يجب خصم أي نقاط إطلاقاً
        bal_before = ledger.get_balance()
        ledger.record_task_start("task_002", "نسخ ملفات", estimated_points=1)
        ledger.record_task_failure("task_002", reason="انقطاع اتصال النظام", is_platform_failure=True, charged_points=2)
        assert ledger.get_balance() == bal_before, "تم خصم نقاط بالخطأ رغم أن الفشل بسبب المنصة!"

        # 5. اختبار تجاوز سقف الإنفاق
        # الرصيد 18، المنفق 2، السقف 10، المتبقي للإنفاق 8
        ledger.record_task_start("task_003", "مهمة كبيرة", estimated_points=5)
        ledger.record_task_success("task_003", charged_points=5)
        # المنفق الآن 7 من أصل 10
        assert ledger.get_total_spent() == 7

        # محاولة بدء مهمة تقديرها يتجاوز السقف المتبقي (3 نقاط)
        try:
            ledger.record_task_start("task_004", "مهمة تتجاوز السقف", estimated_points=5)
            print("FAIL: كان يجب منع المهمة لتجاوز سقف الإنفاق!", file=sys.stderr)
            sys.exit(1)
        except SpendingLimitExceededError:
            pass  # صحيح ومطلوب

        # 6. اختبار استرجاع النقاط (Refund)
        ledger.refund("task_001", points=2, reason="تعويض خطأ تجريبي")
        assert ledger.get_balance() == 15  # 18 - 5 + 2 = 15

    print("PASS: test_ledger completed successfully")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
