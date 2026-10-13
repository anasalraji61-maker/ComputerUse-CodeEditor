"""المهام النموذجية الخمس (Phase 1 Benchmarks) وحالات الفشل المتعمدة."""
import json
from pathlib import Path
from typing import Tuple, Dict, Any, Optional

from assistant.runner import TaskRunner
from assistant.reporter import ExecutionReport
from assistant.safety import KillSwitch
from assistant.ledger import SQLiteLedger
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
    """غلاف رفيع حول TaskRunner (R21): المنسّق الحقيقي في assistant/runner.py.

    user_approved هنا يمثّل مُوافِقاً آلياً للاختبارات فقط؛ في التطبيق الفعلي تُسأل الواجهة لكل خطوة.
    """
    approver = (lambda step: True) if user_approved else None
    runner = TaskRunner(
        allowed_root=allowed_root,
        provider=provider,
        ledger=ledger,
        kill_switch=kill_switch,
        approver=approver,
        winget_runner=mock_winget_runner,
        max_steps=12,
    )
    return runner.run(task_id, objective)


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
