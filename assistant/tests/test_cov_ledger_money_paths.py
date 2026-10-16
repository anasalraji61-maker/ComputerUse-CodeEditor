"""تغطية الدفتر: خصم الفشل (غير المنصة) فعلاً، منع الخصم الزائد عن الرصيد، الاسترجاع المحدود، مهمة STARTED المعلّقة، وعزل ملف دفتر تالف حتى عند فشل النقل."""
import os
import pathlib
import sqlite3
import sys
import tempfile
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.ledger import SQLiteLedger, InsufficientBalanceError, SpendingLimitExceededError

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()

        # 1. record_task_success: الرصيد أقل من الخصم الفعلي (والسقف يكفي) -> رفض بلا تغيير
        led = SQLiteLedger(base / "a.db", initial_balance=5, spending_limit=100)
        led.record_task_start("t1", "x", 1)
        try:
            led.record_task_success("t1", charged_points=9)
            raise AssertionError("كان يجب رفض الخصم")
        except InsufficientBalanceError:
            pass
        assert led.get_balance() == 5 and led.get_total_spent() == 0
        assert led.get_history(1)[0]["status"] == "STARTED"   # لم تُغلق المهمة بنجاح مزيف
        # فوق السقف
        try:
            led.record_task_success("t1", charged_points=101)
            raise AssertionError("كان يجب رفض الخصم")
        except SpendingLimitExceededError:
            pass
        assert led.get_balance() == 5

        # 2. record_task_failure: فشل المستخدم يُخصم، فشل المنصة لا يُخصم، ومعرّف مجهول لا يُخصم شيئاً، ولا يُخصم مرتين
        led.record_task_failure("t1", reason="boom", charged_points=2)
        assert led.get_balance() == 3 and led.get_total_spent() == 2
        led.record_task_failure("t1", reason="again", charged_points=2)          # مغلقة أصلاً: لا خصم ثانٍ
        assert led.get_balance() == 3 and led.get_total_spent() == 2
        led.record_task_failure("never_started", reason="x", charged_points=2)   # مجهولة
        assert led.get_balance() == 3
        led.record_task_start("t2", "x", 1)
        led.record_task_failure("t2", reason="platform", is_platform_failure=True, charged_points=3)
        assert led.get_balance() == 3 and led.get_total_spent() == 2

        # 3. الاسترجاع لا يتجاوز المخصوم ولا يتكرر
        led.record_task_start("t3", "x", 1)
        led.record_task_success("t3", charged_points=2)
        assert led.get_balance() == 1
        try:
            led.refund("t3", 3)
            raise AssertionError("كان يجب رفض استرجاع زائد")
        except ValueError:
            pass
        led.refund("t3", 2)
        assert led.get_balance() == 3
        try:
            led.refund("t3", 1)
            raise AssertionError("كان يجب رفض استرجاع مكرر")
        except ValueError:
            pass
        assert led.get_balance() == 3

        # 4. مهمة STARTED قديمة (انهيار) تُغلق FAILED بلا خصم عند الفتح التالي؛ والحديثة لا تُمَس
        with led._get_connection(immediate=True) as c:
            c.execute("INSERT INTO transactions (task_id, task_type, estimated_points, charged_points, status, reason, created_at) "
                      "VALUES ('old', 'x', 1, 5, 'STARTED', 'r', datetime('now', '-3 hours'))")
        led.record_task_start("fresh", "x", 1)
        led2 = SQLiteLedger(base / "a.db")
        assert led2.reconciled_stuck == 1
        with led2._get_connection() as c:
            rows = {r["task_id"]: dict(r) for r in c.execute("SELECT task_id, status, charged_points FROM transactions")}
        assert rows["old"]["status"] == "FAILED" and rows["old"]["charged_points"] == 0 and rows["fresh"]["status"] == "STARTED"
        # عطب قاعدة البيانات أثناء المصالحة لا يمنع الفتح
        led3 = SQLiteLedger(base / "a.db")
        with mock.patch.object(SQLiteLedger, "_get_connection", side_effect=sqlite3.OperationalError("locked")):
            led3._reconcile_stuck()

        # 5. ملف دفتر تالف: يُعزل (مع جانبيه) ويبدأ دفتر جديد برصيده الافتراضي مع تنبيه
        bad = base / "b.db"
        bad.write_bytes(b"this is not a sqlite database" * 50)
        (base / "b.db-wal").write_bytes(b"w")
        old = os.utime(str(bad), (1, 1))
        led4 = SQLiteLedger(bad, initial_balance=42, spending_limit=50)
        assert led4.recovered_from_corruption and led4.get_balance() == 42
        assert led4.quarantined_path is not None and led4.quarantined_path.exists()
        assert led4.quarantined_path.read_bytes().startswith(b"this is not")
        assert not (base / "b.db-wal").exists()

        # 6. تعذّر نقل الملف التالف: يُحذف ويبدأ دفتر جديد (لا توقف دائم)
        bad2 = base / "c.db"
        bad2.write_bytes(b"garbage" * 100)
        os.utime(str(bad2), (1, 1))
        real_replace = os.replace

        def replace_fails_for_db(src, dst):
            if str(src) == str(bad2.resolve()):
                raise PermissionError("locked by antivirus")
            return real_replace(src, dst)
        with mock.patch("os.replace", replace_fails_for_db):
            led5 = SQLiteLedger(bad2, initial_balance=7, spending_limit=50)
        assert led5.recovered_from_corruption and led5.quarantined_path is None and led5.get_balance() == 7

        # 7. تعذّر النقل والحذف معاً: يُرفع الخطأ الأصلي (لا إخفاء ولا دفتر وهمي)
        bad3 = base / "d.db"
        bad3.write_bytes(b"garbage" * 100)
        os.utime(str(bad3), (1, 1))

        def replace_always_fails(src, dst):
            raise PermissionError("locked")
        with mock.patch("os.replace", replace_always_fails), mock.patch.object(pathlib.Path, "unlink", side_effect=PermissionError("locked")):
            try:
                SQLiteLedger(bad3)
                raise AssertionError("كان يجب أن يُرفع الخطأ")
            except sqlite3.DatabaseError:
                pass
        assert bad3.exists()

    print("PASS: test_cov_ledger_money_paths completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_ledger_money_paths", file=sys.stderr)
    sys.exit(1)
