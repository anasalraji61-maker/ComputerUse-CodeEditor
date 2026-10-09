"""وحدة المنفّذ (Executor) — تنفيذ الخطوات حصراً عبر بوابات الأمان والميزانية وزر الإيقاف."""
from typing import Dict, Any, List, Optional
from pathlib import Path
from assistant.planner import PlanStep
from assistant.safety import SafetyGuard, KillSwitch
from assistant.budget import BudgetTracker
from assistant.tools.files import FileManager
from assistant.tools.shell import ShellTool
from assistant.tools.winget import WingetTool


class ExecutionInterruptedError(Exception):
    """استثناء عند انقطاع التنفيذ بسبب الإيقاف أو الميزانية."""
    pass


class Executor:
    """المنفّذ الصارم الذي لا يستدعي أي أداة إلا بالمرور الإلزامي عبر Safety ثم Budget."""

    def __init__(
        self,
        file_manager: FileManager,
        safety_guard: SafetyGuard,
        budget_tracker: BudgetTracker,
        kill_switch: Optional[KillSwitch] = None,
        shell_tool: Optional[ShellTool] = None,
        winget_tool: Optional[WingetTool] = None,
        user_approved: bool = False,
    ):
        self.file_manager = file_manager
        self.safety_guard = safety_guard
        self.budget_tracker = budget_tracker
        self.kill_switch = kill_switch or safety_guard.kill_switch
        self.shell_tool = shell_tool
        self.winget_tool = winget_tool
        self.user_approved = user_approved

        self.executed_log: List[str] = []
        self.collected_evidence: List[Dict[str, Any]] = []

    def execute_step(self, step: PlanStep) -> Dict[str, Any]:
        """تنفيذ خطوة واحدة مع التحقق الإلزامي من الأمان والميزانية وزر الإيقاف."""
        # 1. فحص زر الإيقاف المشترك أولاً
        self.kill_switch.check()

        # 2. فحص الأمان الصارم (يرفع ActionForbiddenError أو ApprovalRequiredError)
        self.safety_guard.check_action(step.action_type, user_approved=self.user_approved)

        # 3. فحص وخصم الميزانية (يرفع BudgetExceededError)
        self.budget_tracker.charge(steps=1, points=1)

        # 4. فحص زر الإيقاف مجدداً قبل استدعاء الأداة
        self.kill_switch.check()

        action = step.action_type.lower()
        params = step.params
        result_info: Dict[str, Any] = {"step_id": step.step_id, "action": action, "success": False}

        # تنفيذ الأداة المناسبة
        if action == "create_dir":
            created = self.file_manager.create_dir(step.target)
            result_info["success"] = True
            result_info["path"] = str(created)
            self.executed_log.append(f"تم إنشاء المجلد: {created}")

        elif action == "move":
            dest = params.get("dest", "")
            moved = self.file_manager.move(step.target, dest)
            result_info["success"] = True
            result_info["dest"] = str(moved)
            self.executed_log.append(f"تم نقل {step.target} إلى {moved}")

        elif action == "copy":
            dest = params.get("dest", "")
            copied = self.file_manager.copy(step.target, dest)
            result_info["success"] = True
            result_info["dest"] = str(copied)
            self.executed_log.append(f"تم نسخ {step.target} إلى {copied}")

        elif action == "rename":
            new_name = params.get("new_name", "")
            renamed = self.file_manager.rename(step.target, new_name)
            result_info["success"] = True
            result_info["dest"] = str(renamed)
            self.executed_log.append(f"تمت إعادة تسمية {step.target} إلى {renamed.name}")

        elif action in ("delete", "trash"):
            deleted = self.file_manager.delete(step.target)
            result_info["success"] = True
            result_info["trash_path"] = str(deleted)
            self.executed_log.append(f"تم نقل {step.target} إلى سلة المحذوفات")

        elif action == "winget_upgrade":
            if not self.winget_tool:
                raise RuntimeError("أداة winget غير مهيأة للمنفذ.")
            code, out, err = self.winget_tool.upgrade_package(step.target)
            result_info["success"] = (code == 0)
            result_info["exit_code"] = code
            result_info["stdout"] = out
            result_info["stderr"] = err
            self.executed_log.append(f"ترقية حزمة winget {step.target} (رمز الخروج: {code})")

        elif action == "run_command":
            if not self.shell_tool:
                raise RuntimeError("أداة shell غير مهيأة للمنفذ.")
            cmd_list = params.get("cmd", [step.target])
            code, out, err = self.shell_tool.run(cmd_list)
            result_info["success"] = (code == 0)
            result_info["exit_code"] = code
            result_info["stdout"] = out
            result_info["stderr"] = err
            self.executed_log.append(f"تشغيل الأمر {cmd_list} (رمز الخروج: {code})")

        else:
            # عملية فحص عامة
            result_info["success"] = True
            self.executed_log.append(f"فحص العنصر: {step.target}")

        self.collected_evidence.append(result_info)
        return result_info
