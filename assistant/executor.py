"""وحدة المنفّذ (Executor) — تنفيذ الخطوات حصراً عبر بوابات الأمان والموافقة والميزانية وزر الإيقاف."""
from pathlib import Path
from typing import Callable, Dict, Any, List, Optional

from assistant.planner import PlanStep
from assistant.safety import SafetyGuard, SafetyLevel, KillSwitch
from assistant.budget import BudgetTracker
from assistant.tools.files import FileManager
from assistant.tools.shell import ShellTool
from assistant.tools.winget import WingetTool

# مُوافِق لكل خطوة: يستلم الخطوة ويعيد True/False. الافتراضي: الرفض (R16).
Approver = Callable[[PlanStep], bool]


class ApprovalTimedOut(Exception):
    """يرفعها المُوافِق عند انتهاء مهلة الانتظار دون ردّ من المستخدم (تختلف عن الرفض الصريح)."""


class ExecutionInterruptedError(Exception):
    """استثناء عند انقطاع التنفيذ بسبب الإيقاف أو الميزانية."""
    pass


class Executor:
    """المنفّذ الصارم: لا تُستدعى أي أداة إلا بعد Safety ثم موافقة الخطوة (إن لزم) ثم Budget."""

    def __init__(
        self,
        file_manager: FileManager,
        safety_guard: SafetyGuard,
        budget_tracker: BudgetTracker,
        kill_switch: Optional[KillSwitch] = None,
        shell_tool: Optional[ShellTool] = None,
        winget_tool: Optional[WingetTool] = None,
        approver: Optional[Approver] = None,
    ):
        self.file_manager = file_manager
        self.safety_guard = safety_guard
        self.budget_tracker = budget_tracker
        self.kill_switch = kill_switch or safety_guard.kill_switch
        self.shell_tool = shell_tool
        self.winget_tool = winget_tool
        self.approver = approver  # None = رفض كل الإجراءات الخطرة

        self.executed_log: List[str] = []
        self.collected_evidence: List[Dict[str, Any]] = []
        self.approval_log: List[str] = []
        self.last_denial: Optional[str] = None  # "denied" | "timeout" | "stopped" لآخر رفض (لتفرقة الرسائل)

    def _decide_approval(self, step: PlanStep) -> bool:
        """طلب موافقة لهذه الخطوة تحديداً وتسجيل القرار."""
        outcome = "approved"
        if self.approver is None:
            decision, outcome = False, "denied"
        else:
            try:
                decision = bool(self.approver(step))
            except ApprovalTimedOut:
                decision, outcome = False, "timeout"
            if not decision and outcome != "timeout":
                outcome = "stopped" if self.kill_switch.is_triggered else "denied"
        self.last_denial = None if decision else outcome
        label = {"approved": "تمت الموافقة", "denied": "رُفضت",
                 "timeout": "انتهت المهلة دون ردّ فلم تُنفَّذ", "stopped": "أُوقفت المهمة قبل القرار"}[outcome]
        self.approval_log.append(f"الخطوة {step.step_id} ({step.action_type} → {step.target}): {label}")
        return decision

    def execute_step(self, step: PlanStep) -> Dict[str, Any]:
        """تنفيذ خطوة واحدة مع التحقق الإلزامي من الأمان والموافقة والميزانية وزر الإيقاف."""
        self.kill_switch.check()

        level = self.safety_guard.classify_action(step.action_type)
        approved = False
        if level == SafetyLevel.DANGEROUS:
            approved = self._decide_approval(step)
        # يرفع ActionForbiddenError / ApprovalRequiredError / EmergencyStopTriggered
        self.safety_guard.check_action(step.action_type, user_approved=approved)

        self.budget_tracker.charge(steps=1, points=1)
        self.kill_switch.check()

        action = step.action_type.strip().lower()
        params = step.params
        info: Dict[str, Any] = {"step_id": step.step_id, "action": action, "success": False}

        if action == "inspect":
            target = step.target.strip()
            p = self.file_manager.allowed_root if target in (".", "./") else self.file_manager.validate_path(target)
            info["exists"] = p.exists()
            if not p.exists():
                raise FileNotFoundError(f"العنصر المطلوب فحصه غير موجود: {p}")
            info["kind"] = "dir" if p.is_dir() else "file"
            info["size"] = p.stat().st_size if p.is_file() else None
            info["entries"] = len(list(p.iterdir())) if p.is_dir() else None
            info["path"] = str(p)
            info["success"] = True
            self.executed_log.append(f"فحص العنصر: {step.target}")

        elif action == "create_dir":
            created = self.file_manager.create_dir(step.target)
            info.update(success=True, path=str(created))
            self.executed_log.append(f"تم إنشاء المجلد: {created}")

        elif action == "move":
            moved = self.file_manager.move(step.target, params["dest"])
            info.update(success=True, dest=str(moved))
            self.executed_log.append(f"تم نقل {step.target} إلى {moved}")

        elif action == "copy":
            copied = self.file_manager.copy(step.target, params["dest"])
            info.update(success=True, dest=str(copied))
            self.executed_log.append(f"تم نسخ {step.target} إلى {copied}")

        elif action == "rename":
            renamed = self.file_manager.rename(step.target, params["new_name"])
            info.update(success=True, dest=str(renamed))
            self.executed_log.append(f"تمت إعادة تسمية {step.target} إلى {renamed.name}")

        elif action in ("delete", "trash"):
            deleted = self.file_manager.delete(step.target)
            info.update(success=True, trash_path=str(deleted))
            self.executed_log.append(f"تم نقل {step.target} إلى سلة المحذوفات")

        elif action == "winget_upgrade":
            if not self.winget_tool:
                raise RuntimeError("أداة winget غير مهيأة للمنفذ.")
            code, out, err = self.winget_tool.upgrade_package(step.target)
            info.update(success=(code == 0), exit_code=code, stdout=out, stderr=err, package=step.target)
            self.executed_log.append(f"ترقية حزمة winget {step.target} (رمز الخروج: {code})")

        elif action == "run_command":
            if not self.shell_tool:
                raise RuntimeError("أداة shell غير مهيأة للمنفذ.")
            cmd_list = params.get("cmd", [step.target])
            head = str(step.target).split()[0] if str(step.target).split() else ""
            if not isinstance(cmd_list, (list, tuple)) or not cmd_list or str(cmd_list[0]) != head:
                raise ExecutionInterruptedError("الأمر المعروض للمستخدم لا يطابق الأمر الفعلي؛ رُفضت الخطوة.")
            code, out, err = self.shell_tool.run(cmd_list)
            info.update(success=(code == 0), exit_code=code, stdout=out, stderr=err, cmd=list(cmd_list))
            self.executed_log.append(f"تشغيل الأمر {cmd_list} (رمز الخروج: {code})")

        else:  # لا يجب الوصول إليه: الجدول الصريح يمنع المجهول
            raise ExecutionInterruptedError(f"لا يوجد منفّذ للإجراء '{action}'.")

        self.collected_evidence.append(info)
        return info
