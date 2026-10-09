"""اختبارات التكامل بين المخطط والمنفّذ والمدقّق المستقل وإعداد التقارير."""
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.planner import Planner, PlanStep
    from assistant.executor import Executor
    from assistant.verifier import Verifier
    from assistant.reporter import ExecutionReport
    from assistant.safety import SafetyGuard, KillSwitch
    from assistant.budget import BudgetTracker
    from assistant.tools.files import FileManager
    from assistant.models.fake import FakeProvider

    with tempfile.TemporaryDirectory() as tmp_dir:
        root = pathlib.Path(tmp_dir).resolve()
        fm = FileManager(allowed_root=root)
        ks = KillSwitch()
        safety = SafetyGuard(kill_switch=ks)
        budget = BudgetTracker(max_steps=5)

        # 1. اختبار التخطيط
        provider = FakeProvider(
            default_response='{"steps": [{"step_id": 1, "action_type": "create_dir", "target": "workspace", "params": {}, "acceptance_criteria": "وجود المجلد"}]}'
        )
        planner = Planner(provider=provider)
        steps = planner.plan("أنشئ مجلد العمل")
        assert len(steps) == 1
        assert steps[0].action_type == "create_dir"
        assert steps[0].acceptance_criteria == "وجود المجلد"

        # 2. اختبار التنفيذ الإلزامي عبر بوابات الأمان والميزانية
        executor = Executor(
            file_manager=fm,
            safety_guard=safety,
            budget_tracker=budget,
            kill_switch=ks,
        )
        res = executor.execute_step(steps[0])
        assert res["success"] is True
        assert budget.current_steps == 1  # تم الخصم بنجاح عبر الميزانية

        # 3. اختبار التدقيق المستقل على القرص
        verifier = Verifier(allowed_root=root)
        ok, evidence, errors = verifier.verify_plan(steps, [res])
        assert ok is True
        assert len(errors) == 0
        assert (root / "workspace").exists()

        # 4. اختبار صياغة التقرير العربي
        report = ExecutionReport(
            task_id="task_int_01",
            objective="إنشاء مجلد العمل",
            success=True,
            executed_steps=executor.executed_log,
            evidence=evidence,
            failures=errors,
            points_charged=1,
            undo_available=True,
            undo_journal_path=str(fm.journal_path),
        )
        rendered = report.render_arabic_text()
        assert "تقرير المهمة: تم بنجاح" in rendered
        assert "المطلوب: إنشاء مجلد العمل" in rendered
        assert "الأدلة القاطعة" in rendered
        assert "الكلفة بالنقاط" in rendered
        assert "طريقة التراجع" in rendered

    print("PASS: test_planner_executor_verifier completed successfully")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
