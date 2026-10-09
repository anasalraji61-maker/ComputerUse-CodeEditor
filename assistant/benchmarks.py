"""المهام النموذجية الخمس (Phase 1 Benchmarks) وحالات الفشل المتعمدة."""
import json
from pathlib import Path
from typing import Tuple, Dict, Any, Optional

from assistant.planner import Planner, PlanStep
from assistant.executor import Executor
from assistant.verifier import Verifier
from assistant.reporter import ExecutionReport
from assistant.safety import SafetyGuard, KillSwitch
from assistant.budget import BudgetTracker
from assistant.ledger import SQLiteLedger
from assistant.tools.files import FileManager
from assistant.tools.shell import ShellTool
from assistant.tools.winget import WingetTool
from assistant.models.fake import FakeProvider


def run_benchmark_pipeline(
    task_id: str,
    objective: str,
    allowed_root: Path,
    provider: FakeProvider,
    ledger: SQLiteLedger,
    kill_switch: Optional[KillSwitch] = None,
    user_approved: bool = False,
    mock_winget_runner: Optional[Any] = None,
) -> ExecutionReport:
    """تشغيل دورة العمل الكاملة: التخطيط ← التنفيذ ← التدقيق المستقل ← التقرير وتحديث الدفتر."""
    ks = kill_switch or KillSwitch()
    safety = SafetyGuard(kill_switch=ks)
    budget = BudgetTracker(max_steps=12, max_tokens=4000, max_points=10)
    fm = FileManager(allowed_root=allowed_root)
    shell = ShellTool()
    winget = WingetTool(runner=mock_winget_runner) if mock_winget_runner else WingetTool()

    planner = Planner(provider=provider)
    executor = Executor(
        file_manager=fm,
        safety_guard=safety,
        budget_tracker=budget,
        kill_switch=ks,
        shell_tool=shell,
        winget_tool=winget,
        user_approved=user_approved,
    )
    verifier = Verifier(allowed_root=allowed_root)

    # 1. تقدير الكلفة وتسجيل بدء المهمة في الدفتر
    est_points = ledger.estimate_cost(objective, steps_count=4)
    ledger.record_task_start(task_id, objective, est_points)

    plan_steps = planner.plan(objective)
    executor_evidence = []
    execution_failed = False
    failure_msg = None

    # 2. التنفيذ خطوة بخطوة مع مراقبة الأمان والميزانية والإيقاف
    try:
        for step in plan_steps:
            ks.check()
            info = executor.execute_step(step)
            executor_evidence.append(info)
            if not info.get("success"):
                execution_failed = True
                failure_msg = f"فشل الخطوة {step.step_id}"
                break
    except Exception as exc:
        execution_failed = True
        failure_msg = str(exc)

    # 3. التدقيق المستقل على القرص
    if not execution_failed:
        is_verified, verified_evidence, verifier_errors = verifier.verify_plan(plan_steps, executor_evidence)
    else:
        is_verified = False
        verified_evidence = executor_evidence
        verifier_errors = [failure_msg or "انقطع التنفيذ قبل الاكتمال"]

    final_success = (not execution_failed) and is_verified

    # 4. تحديث الدفتر
    actual_charge = budget.current_points if final_success else 0
    if final_success:
        ledger.record_task_success(task_id, charged_points=actual_charge)
    else:
        ledger.record_task_failure(task_id, reason=str(verifier_errors), is_platform_failure=False)

    # 5. توليد التقرير العربي الشامل بالأدلة وطريقة التراجع
    report = ExecutionReport(
        task_id=task_id,
        objective=objective,
        success=final_success,
        executed_steps=executor.executed_log,
        evidence=verified_evidence,
        failures=verifier_errors if not final_success else [],
        points_charged=actual_charge,
        undo_available=True,
        undo_journal_path=str(fm.journal_path),
    )

    return report


# --- مولدات الاستجابات المبرمجة للمهام الخمس ---

def get_task_1_provider(root: Path) -> FakeProvider:
    """مهمة 1: ترتيب مجلد التنزيلات بحسب النوع."""
    steps_json = json.dumps({
        "steps": [
            {"step_id": 1, "action_type": "create_dir", "target": "documents", "params": {}, "acceptance_criteria": "وجود المجلد"},
            {"step_id": 2, "action_type": "create_dir", "target": "images", "params": {}, "acceptance_criteria": "وجود المجلد"},
            {"step_id": 3, "action_type": "move", "target": "file1.pdf", "params": {"dest": "documents/file1.pdf"}, "acceptance_criteria": "نقل سليم"},
            {"step_id": 4, "action_type": "move", "target": "photo.png", "params": {"dest": "images/photo.png"}, "acceptance_criteria": "نقل سليم"},
        ]
    })
    return FakeProvider(default_response=steps_json)


def get_task_2_provider(root: Path, file_hash: str) -> FakeProvider:
    """مهمة 2: نسخ مجلد المشروع مع تدقيق بصمة SHA256."""
    steps_json = json.dumps({
        "steps": [
            {"step_id": 1, "action_type": "create_dir", "target": "Backup", "params": {}, "acceptance_criteria": "وجود مجلد النسخ"},
            {
                "step_id": 2,
                "action_type": "copy",
                "target": "Work/project.txt",
                "params": {"dest": "Backup/project.txt", "expected_hash": file_hash},
                "acceptance_criteria": "مطابقة بصمة الهاش والحجم",
            },
        ]
    })
    return FakeProvider(default_response=steps_json)


def get_task_3_provider() -> FakeProvider:
    """مهمة 3: إعادة تسمية دفعة ملفات بنمط YYYY-MM-DD."""
    steps_json = json.dumps({
        "steps": [
            {"step_id": 1, "action_type": "rename", "target": "DCIM/img1.jpg", "params": {"new_name": "2026-10-09_img1.jpg"}, "acceptance_criteria": "تسمية مطابقة"},
            {"step_id": 2, "action_type": "rename", "target": "DCIM/img2.jpg", "params": {"new_name": "2026-10-09_img2.jpg"}, "acceptance_criteria": "تسمية مطابقة"},
        ]
    })
    return FakeProvider(default_response=steps_json)


def get_task_4_provider() -> FakeProvider:
    """مهمة 4: تحديث برنامج عبر winget وتأكيد رمز الخروج 0."""
    steps_json = json.dumps({
        "steps": [
            {"step_id": 1, "action_type": "winget_upgrade", "target": "Git.Git", "params": {}, "acceptance_criteria": "رمز الخروج 0"},
        ]
    })
    return FakeProvider(default_response=steps_json)


def get_task_5_provider() -> FakeProvider:
    """مهمة 5: إنشاء هيكل مجلدات لمشروع جديد."""
    steps_json = json.dumps({
        "steps": [
            {"step_id": 1, "action_type": "create_dir", "target": "my_project/src", "params": {}, "acceptance_criteria": "وجود المجلد"},
            {"step_id": 2, "action_type": "create_dir", "target": "my_project/docs", "params": {}, "acceptance_criteria": "وجود المجلد"},
            {"step_id": 3, "action_type": "create_dir", "target": "my_project/tests", "params": {}, "acceptance_criteria": "وجود المجلد"},
        ]
    })
    return FakeProvider(default_response=steps_json)
