"""اختبارات التكامل بين المخطط والمنفّذ والمدقّق المستقل (لقطة قبل/بعد) وإعداد التقارير."""
import sys
import tempfile
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.planner import Planner, PlanStep
    from assistant.executor import Executor
    from assistant.verifier import Verifier
    from assistant.reporter import ExecutionReport
    from assistant.safety import SafetyGuard, KillSwitch, ApprovalRequiredError
    from assistant.budget import BudgetTracker
    from assistant.tools.files import FileManager
    from assistant.models.fake import FakeProvider

    def make(root):
        fm = FileManager(allowed_root=root)
        ks = KillSwitch()
        safety = SafetyGuard(kill_switch=ks)
        ex = Executor(fm, safety, BudgetTracker(max_steps=10), kill_switch=ks)
        return fm, ex, Verifier(allowed_root=root)

    with tempfile.TemporaryDirectory() as tmp_dir:
        root = pathlib.Path(tmp_dir).resolve()
        fm, ex, ver = make(root)

        # 1. تخطيط
        provider = FakeProvider(
            default_response='{"steps": [{"step_id": 1, "action_type": "create_dir", "target": "workspace", "params": {}, "acceptance_criteria": "وجود المجلد"}]}'
        )
        steps = Planner(provider=provider).plan("أنشئ مجلد العمل")
        assert len(steps) == 1 and steps[0].action_type == "create_dir"

        # 2. تنفيذ + تدقيق بلقطة
        snap = ver.snapshot(steps[0])
        res = ex.execute_step(steps[0])
        assert res["success"] is True
        ok, ev, err = ver.verify_step(steps[0], res, snap)
        assert ok is True, err
        jok, jev, jerr = ver.verify_journal(steps, {1: snap}, 0)
        assert jok, jerr

        # 3. R17: بلا دليل تنفيذ = فشل حتى لو الوجهة موجودة مسبقاً
        (root / "a.txt").write_text("hello", encoding="utf-8")
        (root / "dest").mkdir()
        (root / "dest" / "a.txt").write_text("hello", encoding="utf-8")
        mv = PlanStep(2, "move", "a.txt", {"dest": "dest/a.txt"})
        snap_mv = ver.snapshot(mv)
        ok, _, err = ver.verify_step(mv, None, snap_mv)
        assert ok is False and "دليل" in err
        ok, _, err = ver.verify_step(mv, {}, snap_mv)
        assert ok is False

        # 4. R17: ادعاء المنفّذ بوجهة خارج الجذر يُرفض
        ok, _, err = ver.verify_step(mv, {"step_id": 2, "success": True, "dest": str(root.parent / "evil.txt")}, snap_mv)
        assert ok is False and "خارج" in err

        # 5. R18: نقل حقيقي ثم إفساد المحتوى بعد النقل
        fm2_src = root / "b.txt"
        fm2_src.write_text("original-bytes", encoding="utf-8")
        mv2 = PlanStep(3, "move", "b.txt", {"dest": "dest/b.txt"})
        s2 = ver.snapshot(mv2)
        r2 = ex.execute_step(mv2)
        ok, _, err = ver.verify_step(mv2, r2, s2)
        assert ok is True, err
        (root / "dest" / "b.txt").write_text("CORRUPTED", encoding="utf-8")
        ok, _, err = ver.verify_step(mv2, r2, s2)
        assert ok is False and "لا يطابق" in err

        # 6. R18: النقل الذي ترك المصدر في مكانه يفشل (محاكاة منفّذ كاذب)
        (root / "c.txt").write_text("cc", encoding="utf-8")
        (root / "dest" / "c.txt").write_text("cc", encoding="utf-8")
        mv3 = PlanStep(4, "move", "c.txt", {"dest": "dest/c.txt"})
        s3 = ver.snapshot(mv3)
        ok, _, err = ver.verify_step(mv3, {"step_id": 4, "success": True, "dest": str(root / "dest" / "c.txt")}, s3)
        assert ok is False and "ما زال موجوداً" in err

        # 7. R19: inspect على عنصر غير موجود يفشل (لا «مكتمل» فارغ)
        ins = PlanStep(5, "inspect", "nope.txt")
        try:
            ex.execute_step(ins)
            raise AssertionError("كان يجب أن يفشل inspect لعنصر غير موجود")
        except FileNotFoundError:
            pass
        ok, _, err = ver.verify_step(ins, None, ver.snapshot(ins))
        assert ok is False
        ins_ok = PlanStep(6, "inspect", "dest")
        si = ver.snapshot(ins_ok)
        ri = ex.execute_step(ins_ok)
        ok, evi, _ = ver.verify_step(ins_ok, ri, si)
        assert ok is True and "مجلد" in evi["value"]

        # 8. R19: winget بلا فحص مستقل يفشل، ومع فحص يمرّ
        wg = PlanStep(7, "winget_upgrade", "Git.Git")
        sw = ver.snapshot(wg)
        info = {"step_id": 7, "success": True, "exit_code": 0}
        ok, _, err = ver.verify_step(wg, info, sw)
        assert ok is False
        ver2 = Verifier(allowed_root=root, version_probe=lambda pkg: (0, "Git.Git 2.0", ""))
        ok, _, err = ver2.verify_step(wg, info, sw)
        assert ok is True, err
        ver3 = Verifier(allowed_root=root, version_probe=lambda pkg: (1, "", "not found"))
        ok, _, err = ver3.verify_step(wg, info, sw)
        assert ok is False

        # 9. R16: خطوة خطرة بلا مُوافِق تُرفض؛ مع مُوافِق يُسأل عن كل خطوة
        asked = []
        ex_dang = Executor(fm, SafetyGuard(), BudgetTracker(), approver=lambda s: asked.append(s.step_id) or False)
        try:
            ex_dang.execute_step(PlanStep(8, "run_command", "whoami", {"cmd": ["whoami"]}))
            raise AssertionError("كان يجب رفض الخطوة الخطرة")
        except ApprovalRequiredError:
            pass
        assert asked == [8] and len(ex_dang.approval_log) == 1

        # 10. تقرير عربي
        report = ExecutionReport("t1", "إنشاء مجلد العمل", True, ex.executed_log, [ev], [], 1, True, str(fm.journal_path))
        rendered = report.render_arabic_text()
        for needle in ("تقرير المهمة: تم بنجاح", "المطلوب: إنشاء مجلد العمل", "الأدلة القاطعة", "الكلفة بالنقاط", "طريقة التراجع"):
            assert needle in rendered, needle

    print("PASS: test_planner_executor_verifier completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_planner_executor_verifier", file=sys.stderr)
    sys.exit(1)
