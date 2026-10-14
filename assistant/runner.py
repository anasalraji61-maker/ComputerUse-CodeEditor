"""المنسّق (TaskRunner) — دورة العمل الكاملة: دفتر ← تخطيط ← فحص مسبق ← تنفيذ وتدقيق لكل خطوة ← تقرير."""
import sqlite3
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
from assistant.tools.files import FileManager, _is_link
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
        max_steps: int = 60,
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
        stop_check = lambda: self.kill_switch.is_triggered  # noqa: E731
        self.winget = WingetTool(runner=winget_runner, stop_check=stop_check) if winget_runner else WingetTool(stop_check=stop_check)
        # فحص winget المستقل الافتراضي: winget list -q <id>؛ مع نفس المشغّل القابل للاستبدال
        self.version_probe = version_probe or (lambda pkg: self.winget.list_packages(pkg))
        bind = getattr(provider, "bind_workspace", None)
        if callable(bind):
            bind(self.allowed_root)  # يسمح لمخطط القواعد بقراءة أحجام/تواريخ الملفات (قراءة فقط)
        self.planner = Planner(provider=provider)
        self.verifier = Verifier(allowed_root=self.allowed_root, version_probe=self.version_probe, kill_switch=self.kill_switch)
        self._journal_starts: Dict[str, int] = {}
        self._journal_ends: Dict[str, int] = {}

    def workspace_listing(self, max_entries: int = 60) -> str:
        """قائمة مختصرة بمحتويات مجلد العمل (مستويان) لتزويد المخطط بالسياق. لا تدخل الروابط/التقاطعات."""
        out: List[str] = []
        truncated = False

        def visible(p: Path, rel: Path) -> Optional[str]:
            if rel.parts[0] == ".trash" or rel.parts[0].startswith(".undo_journal") or _is_link(p):
                return None
            real = rel.as_posix()
            name = "".join(ch for ch in real if ch.isprintable())
            if name != real or len(name) > 120:
                return None  # اسم فيه محارف غير مرئية/طويل جداً: لا يمكن الإشارة إليه بأمان فلا يُعرض للمخطط
            return name + ("/" if p.is_dir() else "")

        try:
            for p in sorted(self.allowed_root.iterdir()):
                name = visible(p, p.relative_to(self.allowed_root))
                if name is None:
                    continue
                out.append(name)
                if len(out) >= max_entries:
                    truncated = True
                    break
                if p.is_dir():
                    for c in sorted(p.iterdir()):
                        cname = visible(c, c.relative_to(self.allowed_root))
                        if cname is None:
                            continue
                        out.append(cname)
                        if len(out) >= max_entries:
                            truncated = True
                            break
                if truncated:
                    break
        except OSError:
            pass
        if truncated:
            out.append("… (القائمة مقتطعة)")
        return "\n".join(out) if out else "(فارغ)"

    def make_plan(self, objective: str) -> List[PlanStep]:
        return self.planner.plan(objective, context=self.workspace_listing())

    # ---------- التراجع الخاص بالمهمة (R22) ----------
    def undo_task(self, task_id: str) -> int:
        """يتراجع فقط عن عمليات هذه المهمة (من نهاية السجل حتى بدايتها)."""
        journal = self.file_manager.journal
        start = self._journal_starts.get(task_id)
        if start is None:  # بعد إعادة التشغيل/تبديل المجلد: نستعيد حدود المهمة من وسوم السجل المحفوظ على القرص
            i = len(journal)
            while i > 0 and journal[i - 1].get("task") == task_id:
                i -= 1
            if i == len(journal):
                return 0
            start = i
        end = self._journal_ends.get(task_id, len(journal))
        if len(journal) > end:
            return 0  # توجد مهام أحدث بعدها: يجب التراجع عنها أولاً حتى لا تُلغى عملياتها بالخطأ
        count = 0
        while len(self.file_manager.journal) > start:
            r = self.file_manager.undo_step()
            if r == "ok":
                count += 1
            elif r != "missing":
                break
        return count

    def last_journaled_task(self) -> Optional[str]:
        """معرّف آخر مهمة غيّرت الملفات (من وسم آخر إدخال في السجل المحفوظ)، أو None."""
        journal = self.file_manager.journal
        return journal[-1].get("task") if journal and isinstance(journal[-1].get("task"), str) else None

    def _preflight_paths(self, step: PlanStep) -> None:
        """يرفع استثناءً إن كان أي مسار في الخطوة مرفوضاً (خارج الجذر/محجوز/محمي/رابط) دون أي تنفيذ."""
        fm = self.file_manager
        a = step.action_type
        if a in ("create_dir", "move", "copy", "rename", "delete", "trash") or (a == "inspect" and step.target not in (".", "./")):
            src = fm.validate_path(step.target)
            if a in ("move", "copy"):
                fm.validate_path(step.params["dest"])
            elif a == "rename":
                fm.validate_path(src.parent / str(step.params["new_name"]))

    # ---------- التنفيذ ----------
    def run(
        self,
        task_id: str,
        objective: str,
        plan: Optional[List[PlanStep]] = None,
        progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> ExecutionReport:
        """plan: خطة جاهزة (معروضة سابقاً على المستخدم) فلا يُعاد التخطيط؛ progress: ردّ نداء للواجهة."""
        def emit(**ev):
            if progress is not None:
                try:
                    progress(ev)
                except Exception:
                    pass  # عطب الواجهة لا يجب أن يكسر التنفيذ

        fm = self.file_manager
        journal_start = len(fm.journal)
        self._journal_starts[task_id] = journal_start
        fm.current_task = task_id
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
        charge = 0
        started = False
        platform_failure = False

        skipped: List[str] = []
        skipped_ids: set = set()

        def finish(success: bool) -> ExecutionReport:
            fm.current_task = None
            self._journal_ends[task_id] = len(fm.journal)
            if started:
                if success:
                    try:
                        self.ledger.record_task_success(task_id, charged_points=charge)
                    except (SpendingLimitExceededError, InsufficientBalanceError, ValueError, sqlite3.Error) as exc:
                        failures.append(f"تعذّر تسجيل الكلفة في الدفتر: {exc}")
                        success = False
                        try:
                            self.ledger.record_task_failure(task_id, reason=str(exc), is_platform_failure=True)
                        except Exception:
                            pass  # الملفات تغيّرت فعلاً والتقرير يوضح ذلك؛ التراجع متاح
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
                skipped=list(skipped),
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

        # 2. التخطيط (يُتخطى إن وُفّرت خطة جاهزة)
        try:
            if plan is None:
                plan = self.make_plan(objective)
            else:
                plan = list(plan)
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
        for s in plan:  # كل المسارات تُفحص قبل البدء: لا تنفيذ جزئي ثم رفض في منتصف الخطة
            try:
                self._preflight_paths(s)
            except Exception as exc:
                failures.append(f"مسار مرفوض قبل التنفيذ في الخطوة {s.step_id}: {exc} — لم يُنفَّذ شيء.")
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
            total = len(plan)
            for idx, step in enumerate(plan, 1):
                self.kill_switch.check()
                emit(event="step_start", index=idx, total=total, step_id=step.step_id,
                     action=step.action_type, target=step.target)
                before = self.verifier.snapshot(step)
                befores[step.step_id] = before
                try:
                    info = executor.execute_step(step)
                except ApprovalRequiredError as exc:
                    if step.action_type.strip().lower() in ("delete", "trash") and executor.last_denial == "denied":
                        # رفض صريح لحذف واحد: نتخطاه ونعرض الباقي بدل إلغاء المهمة كلها
                        skipped.append(f"{step.target} (رفضتَ حذفه فلم يُنفَّذ)")
                        skipped_ids.add(step.step_id)
                        evidence.append({"item": step.target, "type": "تخطّي بقرارك", "value": "لم يُحذف"})
                        emit(event="step_done", index=idx, total=total, step_id=step.step_id, ok=True, detail="تخطّي بقرارك")
                        continue
                    raise
                ok, ev, err = self.verifier.verify_step(step, info, before)
                evidence.append(ev)
                emit(event="step_done", index=idx, total=total, step_id=step.step_id, ok=ok, detail=ev.get("value", ""))
                if not ok:
                    failures.append(err or f"فشل تدقيق الخطوة {step.step_id}")
                    return finish(False)
            self.kill_switch.check()  # الإيقاف أثناء الخطوة الأخيرة لا يُعلَن نجاحاً صامتاً
            if skipped_ids and len(skipped_ids) == len(plan):
                failures.append("لم تُنفَّذ أي خطوة لأنك رفضت كل الخطوات الخطرة.")
                return finish(False)
            if skipped_ids:  # الكلفة على ما نُفّذ فعلاً فقط
                charge = self.ledger.estimate_cost(objective, steps_count=len(plan) - len(skipped_ids))
            j_ok, j_ev, j_err = self.verifier.verify_journal(
                [s for s in plan if s.step_id not in skipped_ids], befores, journal_start)
            evidence.append(j_ev)
            if not j_ok:
                failures.append(j_err or "فشل تدقيق سجل التراجع")
                return finish(False)
        except EmergencyStopTriggered as exc:
            failures.append(f"تم الإيقاف أثناء التنفيذ: {exc}")
            return finish(False)
        except ApprovalRequiredError as exc:
            why = {"timeout": "انتهت مهلة الموافقة دون ردّ منك فتوقفت المهمة (لم تُنفَّذ الخطوة).",
                   "stopped": "تم الإيقاف أثناء انتظار الموافقة."}.get(executor.last_denial or "", f"رُفضت الموافقة: {exc}")
            failures.append(why)
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
