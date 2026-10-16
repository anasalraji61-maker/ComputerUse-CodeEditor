"""تغطية: مدقق الحذف يرفض (فشل مغلق) كل حالة تُظهر أن العنصر لم يُنقل فعلاً لسلة .trash (verifier.verify_step فرع delete/trash)."""
import sys
import tempfile
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.verifier import Verifier
    from assistant.planner import PlanStep

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve() / "ws"
        trash = root / ".trash"
        trash.mkdir(parents=True)
        v = Verifier(root)
        step = PlanStep(1, "delete", "a.txt")

        def info(tp):
            return {"step_id": 1, "success": True, "action": "delete", "trash_path": str(tp)}

        def check(src_exists, trash_path, trash_content, expect_fragment, before_exists=True, mutate=None):
            # حالة جديدة لكل سيناريو
            for p in list(root.iterdir()):
                if p.name != ".trash":
                    p.unlink()
            for p in list(trash.iterdir()):
                p.unlink()
            f = root / "a.txt"
            f.write_text("original", encoding="utf-8")
            before = v.snapshot(step)
            before["exists"] = before_exists
            f.unlink()
            if src_exists:
                f.write_text("original", encoding="utf-8")
            if trash_content is not None:
                (trash / trash_content[0]).write_text(trash_content[1], encoding="utf-8")
            ok, ev, err = v.verify_step(step, info(trash_path(trash) if callable(trash_path) else trash_path), before)
            assert not ok and err and expect_fragment in err, (expect_fragment, ok, err)
            assert ev.get("type") == "فشل التدقيق"

        # 1. العنصر لم يكن موجوداً قبل الحذف (لقطة تقول غير موجود)
        check(False, lambda t: t / "a.txt", ("a.txt", "original"), "لم يكن موجوداً قبل الحذف", before_exists=False)
        # 2. المصدر ما زال في مكانه ولم يُنقل
        check(True, lambda t: t / "a.txt", ("a.txt", "original"), "ما زال موجوداً في مساره الأصلي")
        # 3. مسار السلة المبلغ عنه ليس داخل .trash
        check(False, lambda t: t.parent / "elsewhere.txt", None, "ليس داخل .trash")
        # 4. المسار المبلغ عنه داخل .trash لكنه غير موجود
        check(False, lambda t: t / "a.txt", None, "غير موجود في السلة")
        # 5. اسم العنصر في السلة لا يطابق اسم الأصل
        check(False, lambda t: t / "other.txt", ("other.txt", "original"), "لا يطابق اسم الأصل")
        # 6. المحتوى في السلة لا يطابق المحتوى قبل الحذف
        check(False, lambda t: t / "a.txt", ("a.txt", "TAMPERED"), "محتوى العنصر في السلة")

        # 7. مسار فارغ في دليل التنفيذ يُرفض ولا يرفع استثناءً غير معالج
        (root / "a.txt").write_text("x", encoding="utf-8")
        before = v.snapshot(step)
        (root / "a.txt").unlink()
        ok, ev, err = v.verify_step(step, {"step_id": 1, "success": True, "trash_path": ""}, before)
        assert not ok and err

        # 8. التحقق الإيجابي المرجعي: النقل الصحيح يُقبل (لا رفض زائف)
        (root / "a.txt").write_text("good", encoding="utf-8")
        before = v.snapshot(step)
        (root / "a.txt").replace(trash / "a.txt")
        ok, ev, err = v.verify_step(step, info(trash / "a.txt"), before)
        assert ok and err is None, err

    print("PASS: test_cov_verifier_delete completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_verifier_delete", file=sys.stderr)
    sys.exit(1)
