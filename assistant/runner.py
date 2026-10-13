"""المنسّق (TaskRunner) — دورة العمل الكاملة: دفتر ← تخطيط ← فحص مسبق ← تنفيذ وتدقيق لكل خطوة ← تقرير."""
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from assistant.models.base import ModelProvider
from assistant.planner import Planner, PlanStep, PlanParseError
from assistant.executor import Executor, Approver
from assistant.verifier import Verifier, VersionProbe
from assistant.reporter import ExecutionReport
from assistant.safety import (
    SafetyGuard, SafetyLevel, KillSwitch,
    EmergencyStopTriggered, ActionForbiddenError, ApprovalRequiredError,
)
from assistant.budget import BudgetTracker, BudgetExceededError
from assistant.ledger import SQLiteLedger, InsufficientBalanceError, SpendingLimitExceededError
from assistant.tools.files import FileManager
from assistant.tools.shell import ShellTool
from assistant.tools.winget import WingetTool


class TaskRunner:
    """ينسّق مهمة واحدة أو أكثر على جذر مسموح. كل الاعتماديات قابلة للحقن (R21)."""

    def __init__(
        self,
        allowed_root: Path,
        provider: ModelProvider,
        ledger: SQLiteLedger,
        kill_switch: Optional[KillSwitch] = None,
        approver: Optional[Approver] = None,
        winget_runner: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
        version_probe: Optional[VersionProbe] = None,
        shell_tool: Optional[ShellTool] = None,
        max_steps: int = 12,
        max_tokens: int = 4000,
    ):
        self.allowed_root = Path(allowed_root).resolve()
        self.provider = provider
        self.ledger = ledger
        self.kill_switch = kill_switch or KillSwitch()
        self.approver = approver
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.safety = SafetyGuard(kill_switch=self.kill_switch)
        self.file_manager = FileManager(allowed_root=self.allowed_root, kill_switch=self.kill_switch)
        self.shell = shell_tool or ShellTool()
        self.winget = WingetTool(runner=winget_runner) if winget_runner else WingetTool()
        # فحص winget المستقل الافتراضي: winget list -q <id>؛ مع نفس المشغّل القابل للاستبدال
        self.version_probe = version_probe or (lambda pkg: self.winget.list_packages(pkg))
        self.planner = Planner(provider=provider)
        self.verifier = Verifier(allowed_root=self.allowed_root, version_probe=self.version_probe)
        self._journal_starts: Dict[str, int] = {}

    # ---------- التراجع الخاص بالمهمة (R22) ----------
    def undo_task(self, task_id: str) -> int:
        """يتراجع فقط عن عمليات هذه المهمة (من نهاية السجل حتى بدايتها)."""
        start = self._journal_starts.get(task_id)
        if start is None:
            return 0
        count = 0
        while len(self.file_manager.journal) > start:
            if not self.file_manager.undo_last():
                break
            count += 1
        return count

    # ---------- التنفيذ ----------
    def run(self, task_id: str, objective: str) -> ExecutionReport:
        fm = self.file_manager
        journal_start = len(fm.journal)
        self._journal_starts[task_id] = journal_start
        executor = Executor(
            file_manager=fm,
            safety_guard=self.safety,
            budget_tracker=BudgetTracker(
                max_steps=self.max_steps, max_tokens=self.max_tokens, max_points=self.max_steps
            ),
            kill_switch=self.kill_switch,
            shell_tool=self.shell,
            winget_tool=self.winget,
            approver=self.approver,
        )
        evidence: List[Dict[str, Any]] = []
        failures: List[str] = []
        plan: List[PlanStep] = []
        charge = 0
        started = False
        platform_failure = False

        def finish(success: bool) -> ExecutionReport:
            if started:
                if success:
                    try:
                        self.ledger.record_task_success(task_id, charged_points=charge)
                    except (SpendingLimitExceededError, InsufficientBalanceError, ValueError) as exc:
                        failures.append(f"تعذّر تسجيل الكلفة في الدفتر: {exc}")
                        success = False
                        self.ledger.record_task_failure(task_id, reason=str(exc), is_platform_failure=True)
                else:
                    self.ledger.record_task_failure(
                        task_id, reason="; ".join(failures) or "فشل", is_platform_failure=platform_failure
                    )
            return ExecutionReport(
                task_id=task_id,
                objective=objective,
                success=success,
                executed_steps=list(executor.executed_log),
                evidence=evidence,
                failures=failures if not success else [],
                points_charged=charge if success else 0,
                undo_available=len(fm.journal) > journal_start,
                undo_journal_path=str(fm.journal_path),
                approvals=list(executor.approval_log),
            )

        # 1. فحص زر الإيقاف والقدرة المالية قبل أي شيء
        try:
            self.kill_switch.check()
            est0 = self.ledger.estimate_cost(objective, steps_count=1)
            self.ledger.record_task_start(task_id, objective, est0)
            started = True
        except EmergencyStopTriggered as exc:
            failures.append(f"تم الإيقاف: {exc}")
            return finish(False)
        except (InsufficientBalanceError, SpendingLimitExceededError) as exc:
            failures.append(f"لم تبدأ المهمة: {exc}")
            return finish(False)

        # 2. التخطيط
        try:
            plan = self.planner.plan(objective)
        except PlanParseError as exc:
            failures.append(f"خطة غير صالحة: {exc}")
            platform_failure = True  # خطأ نموذج لا ذنب للمستخدم فيه: لا خصم
            return finish(False)
        except Exception as exc:
            failures.append(f"تعذّر التخطيط: {exc}")
            platform_failure = True
            return finish(False)

        # 3. فحص مسبق للخطة كاملة قبل تنفيذ أي خطوة (محظور / ميزانية / كلفة)
        for s in plan:
            if self.safety.classify_action(s.action_type) == SafetyLevel.FORBIDDEN:
                failures.append(f"الخطة تحوي إجراءً محظوراً '{s.action_type}' (الخطوة {s.step_id}) — لم يُنفَّذ شيء.")
                return finish(False)
        if len(plan) > self.max_steps:
            failures.append(f"عدد خطوات الخطة {len(plan)} يتجاوز سقف الميزانية {self.max_steps} — لم يُنفَّذ شيء.")
            return finish(False)
        charge = self.ledger.estimate_cost(objective, steps_count=len(plan))
        if not self.ledger.can_afford(charge):
            failures.append(f"الرصيد أو سقف الإنفاق لا يكفي لكلفة المهمة ({charge} نقطة) — لم يُنفَّذ شيء.")
            charge = 0
            return finish(False)

        # 4. تنفيذ وتدقيق مستقل لكل خطوة فور تنفيذها
        befores: Dict[int, Dict[str, Any]] = {}
        try:
            for step in plan:
                self.kill_switch.check()
                before = self.verifier.snapshot(step)
                befores[step.step_id] = before
                info = executor.execute_step(step)
                ok, ev, err = self.verifier.verify_step(step, info, before)
                evidence.append(ev)
                if not ok:
                    failures.append(err or f"فشل تدقيق الخطوة {step.step_id}")
                    return finish(False)
            j_ok, j_ev, j_err = self.verifier.verify_journal(plan, befores, journal_start)
            evidence.append(j_ev)
            if not j_ok:
                failures.append(j_err or "فشل تدقيق سجل التراجع")
                return finish(False)
        except EmergencyStopTriggered as exc:
            failures.append(f"تم الإيقاف أثناء التنفيذ: {exc}")
            return finish(False)
        except ApprovalRequiredError as exc:
            failures.append(f"رُفضت الموافقة: {exc}")
            return finish(False)
        except ActionForbiddenError as exc:
            failures.append(f"إجراء محظور: {exc}")
            return finish(False)
        except BudgetExceededError as exc:
            failures.append(f"تجاوز الميزانية: {exc}")
            return finish(False)
        except Exception as exc:
            failures.append(f"فشل التنفيذ: {type(exc).__name__}: {exc}")
            return finish(False)

        return finish(True)
