"""اختبارات حالات الفشل المتعمدة للتأكد من أن التقرير يقول 'فشل' ولا يقول 'تم' مطلقاً."""
import json
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.ledger import SQLiteLedger
    from assistant.safety import KillSwitch
    from assistant.models.fake import FakeProvider
    from assistant.benchmarks import run_benchmark_pipeline

    with tempfile.TemporaryDirectory() as tmp_dir:
        root = pathlib.Path(tmp_dir).resolve()
        ledger = SQLiteLedger(db_path=root / "fail_ledger.db", initial_balance=100)

        # ---------------------------------------------------------
        # حالة 1: عدم تطابق الـ Hash (ملف تالف أو محتوى مغشوش)
        # ---------------------------------------------------------
        work_file = root / "important.txt"
        work_file.write_text("Original Clean Content", encoding="utf-8")
        corrupted_hash = "0000000000000000000000000000000000000000000000000000000000000000"
        p_corrupt = FakeProvider(
            default_response=json.dumps({
                "steps": [
                    {
                        "step_id": 1,
                        "action_type": "copy",
                        "target": "important.txt",
                        "params": {"dest": "backup_important.txt", "expected_hash": corrupted_hash},
                        "acceptance_criteria": "مطابقة الهاش",
                    }
                ]
            })
        )
        rep1 = run_benchmark_pipeline("fail_hash", "نسخ وتدقيق الهاش", root, p_corrupt, ledger)
        text1 = rep1.render_arabic_text()
        assert rep1.success is False, "المهمة عُدّت ناجحة رغم عدم تطابق الـ Hash!"
        assert "تقرير المهمة: فشل" in text1, "التقرير لم يذكر كلمة 'فشل'!"
        assert "تقرير المهمة: تم بنجاح" not in text1, "التقرير ذكر 'تم بنجاح' بالخطأ!"
        assert any("عدم تطابق البصمة" in f for f in rep1.failures)

        # ---------------------------------------------------------
        # حالة 2: مجلد أو ملف مفقود (طلب عملية على مسار غير موجود)
        # ---------------------------------------------------------
        p_missing = FakeProvider(
            default_response=json.dumps({
                "steps": [
                    {
                        "step_id": 1,
                        "action_type": "move",
                        "target": "non_existent_file.pdf",
                        "params": {"dest": "dest.pdf"},
                        "acceptance_criteria": "نقل الملف",
                    }
                ]
            })
        )
        rep2 = run_benchmark_pipeline("fail_missing", "نقل ملف مفقود", root, p_missing, ledger)
        text2 = rep2.render_arabic_text()
        assert rep2.success is False, "المهمة عُدّت ناجحة رغم أن الملف غير موجود!"
        assert "تقرير المهمة: فشل" in text2
        assert "تقرير المهمة: تم بنجاح" not in text2

        # ---------------------------------------------------------
        # حالة 3: تجاوز سقف الميزانية (أكثر من 12 خطوة)
        # ---------------------------------------------------------
        too_many_steps = [
            {"step_id": i, "action_type": "create_dir", "target": f"folder_{i}", "params": {}, "acceptance_criteria": "إنشاء"}
            for i in range(1, 20)
        ]
        p_budget = FakeProvider(default_response=json.dumps({"steps": too_many_steps}))
        rep3 = run_benchmark_pipeline("fail_budget", "مهمة تتجاوز الميزانية", root, p_budget, ledger)
        text3 = rep3.render_arabic_text()
        assert rep3.success is False, "المهمة عُدّت ناجحة رغم تجاوز الميزانية!"
        assert "تقرير المهمة: فشل" in text3
        assert "تقرير المهمة: تم بنجاح" not in text3

        # ---------------------------------------------------------
        # حالة 4: ضغط زر الإيقاف (Kill Switch) أثناء التنفيذ
        # ---------------------------------------------------------
        ks = KillSwitch()
        ks.trigger("ضغط المستخدم على زر الإيقاف الأحمر في منتصف العملية")
        p_stopped = FakeProvider(
            default_response=json.dumps({
                "steps": [
                    {"step_id": 1, "action_type": "create_dir", "target": "should_not_run", "params": {}, "acceptance_criteria": "إنشاء"}
                ]
            })
        )
        rep4 = run_benchmark_pipeline("fail_stop", "مهمة مقطوعة بالإيقاف", root, p_stopped, ledger, kill_switch=ks)
        text4 = rep4.render_arabic_text()
        assert rep4.success is False, "المهمة عُدّت ناجحة رغم تفعيل زر الإيقاف!"
        assert "تقرير المهمة: فشل" in text4
        assert "تقرير المهمة: تم بنجاح" not in text4
        assert "طريقة التراجع" in text4  # تأكيد وجود إرشادات التراجع في التقرير

        # ---------------------------------------------------------
        # حالة 5: إجراء خطر دون موافقة صريحة
        # ---------------------------------------------------------
        p_danger = FakeProvider(
            default_response=json.dumps({
                "steps": [
                    {"step_id": 1, "action_type": "winget_upgrade", "target": "SomeApp", "params": {}, "acceptance_criteria": "ترقية"}
                ]
            })
        )
        rep5 = run_benchmark_pipeline("fail_consent", "ترقية دون موافقة", root, p_danger, ledger, user_approved=False)
        text5 = rep5.render_arabic_text()
        assert rep5.success is False, "المهمة عُدّت ناجحة رغم غياب الموافقة للإجراء الخطر!"
        assert "تقرير المهمة: فشل" in text5
        assert "تقرير المهمة: تم بنجاح" not in text5

    print("PASS: test_deliberate_failures completed successfully (all deliberate failures correctly reported as 'فشل')")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
