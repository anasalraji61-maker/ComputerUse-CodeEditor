"""تغطية: مدقق النقل/النسخ/إعادة التسمية والأوامر يرفض النتائج الخاطئة (وجهة مختلفة/غائبة، مصدر تغيّر، رمز خروج، مخرجات متوقعة، لقطة غائبة، إجراء مجهول)."""
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
        root.mkdir()
        v = Verifier(root)

        def reject(step, info, before, fragment):
            ok, ev, err = v.verify_step(step, info, before)
            assert not ok and err and fragment in err, (fragment, ok, err)
            assert ev.get("type") == "فشل التدقيق"

        # ---- النقل ----
        (root / "s.txt").write_text("data", encoding="utf-8")
        mv = PlanStep(1, "move", "s.txt", {"dest": "d.txt"})
        before = v.snapshot(mv)
        # المصدر لم يكن موجوداً حسب اللقطة
        reject(mv, {"step_id": 1, "success": True, "dest": str(root / "d.txt")}, dict(before, exists=False), "لم يكن موجوداً قبل التنفيذ")
        # الوجهة الفعلية تختلف عن المخططة
        (root / "s.txt").replace(root / "zzz.txt")
        reject(mv, {"step_id": 1, "success": True, "dest": str(root / "zzz.txt")}, before, "لا تطابق الوجهة المخططة")
        # الوجهة المبلغ عنها مطابقة للمخطط لكنها غير موجودة على القرص
        reject(mv, {"step_id": 1, "success": True, "dest": str(root / "d.txt")}, before, "غير موجودة فعلياً")
        # المحتوى تغيّر بين اللقطة والتنفيذ
        (root / "zzz.txt").replace(root / "d.txt")
        (root / "d.txt").write_text("CHANGED", encoding="utf-8")
        reject(mv, {"step_id": 1, "success": True, "dest": str(root / "d.txt")}, before, "لا يطابق محتوى المصدر")
        # المصدر ما زال موجوداً بعد "نقل"
        (root / "d.txt").write_text("data", encoding="utf-8")
        (root / "s.txt").write_text("data", encoding="utf-8")
        reject(mv, {"step_id": 1, "success": True, "dest": str(root / "d.txt")}, before, "ما زال موجوداً بعد")

        # ---- النسخ: المصدر اختفى أو تغيّر أثناء النسخ ----
        (root / "c_src.txt").write_text("orig", encoding="utf-8")
        cp = PlanStep(2, "copy", "c_src.txt", {"dest": "c_dst.txt"})
        before_c = v.snapshot(cp)
        (root / "c_dst.txt").write_text("orig", encoding="utf-8")
        (root / "c_src.txt").unlink()
        reject(cp, {"step_id": 2, "success": True, "dest": str(root / "c_dst.txt")}, before_c, "تغيّر أو اختفى أثناء النسخ")
        (root / "c_src.txt").write_text("MODIFIED-DURING-COPY", encoding="utf-8")
        reject(cp, {"step_id": 2, "success": True, "dest": str(root / "c_dst.txt")}, before_c, "تغيّر أو اختفى أثناء النسخ")

        # ---- إعادة التسمية بوجهة خارج الجذر لا تُقبل ----
        rn = PlanStep(3, "rename", "d.txt", {"new_name": "new.txt"})
        before_r = v.snapshot(rn)
        ok, ev, err = v.verify_step(rn, {"step_id": 3, "success": True, "dest": str(root.parent / "escaped.txt")}, before_r)
        assert not ok and err

        # ---- run_command ----
        rc = PlanStep(4, "run_command", "whoami", {"cmd": ["whoami"]})
        reject(rc, {"step_id": 4, "success": True, "exit_code": 1, "stdout": "x"}, {"step_id": 4}, "رمز 1")
        rc2 = PlanStep(5, "run_command", "whoami", {"cmd": ["whoami"], "expected_output": "NEVER-PRESENT"})
        reject(rc2, {"step_id": 5, "success": True, "exit_code": 0, "stdout": "root"}, {"step_id": 5}, "النص المتوقع")
        ok, _, err = v.verify_step(rc2, {"step_id": 5, "success": True, "exit_code": 0, "stdout": "xx NEVER-PRESENT yy"}, {"step_id": 5})
        assert ok and err is None

        # ---- لقطة غائبة / دليل غير مطابق / المنفّذ لم ينجح / إجراء بلا مدقق ----
        ins = PlanStep(6, "inspect", ".")
        reject(ins, {"step_id": 6, "success": True}, None, "حالة الملف قبل التنفيذ غير معروفة")
        reject(ins, {"step_id": 99, "success": True}, {"step_id": 6}, "لم أجد ما يثبت تنفيذ")
        reject(ins, None, {"step_id": 6}, "لم أجد ما يثبت تنفيذ")
        reject(ins, {"step_id": 6, "success": False}, {"step_id": 6, "exists": True}, "عدم نجاح")
        odd = PlanStep(7, "some_unknown_action", "x")
        reject(odd, {"step_id": 7, "success": True}, {"step_id": 7, "exists": True}, "لا يوجد تدقيق معرّف")

    print("PASS: test_cov_verifier_transfer_cmd completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_verifier_transfer_cmd", file=sys.stderr)
    sys.exit(1)
