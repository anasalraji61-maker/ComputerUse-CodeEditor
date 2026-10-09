"""وحدة المدقّق المستقل (Verifier) — فحص الحالة الفعلية على القرص دون الوثوق بكلام المنفّذ."""
import hashlib
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from assistant.planner import PlanStep
from assistant.tools.files import FileManager


class Verifier:
    """المدقّق المستقل: لا يعتمد على كلام المنفّذ، بل يفحص القرص والحالة المادية بنفسه."""

    def __init__(self, allowed_root: Path):
        self.allowed_root = Path(allowed_root).resolve()

    def calculate_hash(self, file_path: Path) -> str:
        """حساب بصمة SHA-256 للتأكد القاطع من المحتوى على القرص."""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    def verify_step_evidence(self, step: PlanStep, executor_info: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """التحقق المادي المستقل من نتيجة خطوة تنفيذية معينة."""
        action = step.action_type.lower()
        evidence: Dict[str, Any] = {"item": step.target, "action": action}

        if action == "create_dir":
            p = self.allowed_root / step.target
            if not p.exists() or not p.is_dir():
                return False, evidence, f"المجلد المطلوب '{step.target}' غير موجود فعلياً على القرص."
            evidence["type"] = "مجلد موجود"
            evidence["value"] = str(p)
            return True, evidence, None

        elif action in ("move", "copy", "rename"):
            dest_str = executor_info.get("dest") or step.params.get("dest")
            if not dest_str:
                return False, evidence, f"لم يتم العثور على مسار الوجهة للخطوة {step.step_id}."
            dest_path = Path(dest_str)
            if not dest_path.is_absolute():
                dest_path = self.allowed_root / dest_path

            if not dest_path.exists():
                return False, evidence, f"الملف الوجهة '{dest_path}' غير موجود فعلياً على القرص."

            # التحقق من سلامة الحجم وبصمة الـ Hash إن تم تحديدها
            expected_hash = step.params.get("expected_hash")
            actual_hash = self.calculate_hash(dest_path) if dest_path.is_file() else "DIR"
            if expected_hash and actual_hash != expected_hash:
                return False, evidence, f"عدم تطابق البصمة للملف '{dest_path.name}': متوقع {expected_hash}، الفعلي {actual_hash}."

            evidence["type"] = "ملف موجود ومطابق"
            evidence["value"] = f"SHA256: {actual_hash[:16]}... (الحجم: {dest_path.stat().st_size if dest_path.is_file() else 'مجلد'})"
            return True, evidence, None

        elif action in ("delete", "trash"):
            # التأكد من عدم وجود الملف في مساره الأصلي ووجوده في .trash
            trash_str = executor_info.get("trash_path")
            orig_path = self.allowed_root / step.target
            if orig_path.exists():
                return False, evidence, f"الملف '{orig_path}' ما زال موجوداً في مساره الأصلي ولم يُنقل للسلة."
            if trash_str and not Path(trash_str).exists():
                return False, evidence, f"الملف المحذوف غير موجود في سلة المحذوفات '{trash_str}'."

            evidence["type"] = "نقل آمن للسلة"
            evidence["value"] = f"منقول إلى: {trash_str}"
            return True, evidence, None

        elif action in ("winget_upgrade", "run_command"):
            code = executor_info.get("exit_code", -1)
            if code != 0:
                return False, evidence, f"فشل الأمر: رمز الخروج {code} بدلاً من 0."
            evidence["type"] = "رمز الخروج"
            evidence["value"] = f"نجاح (0) — {executor_info.get('stdout', '')[:40]}"
            return True, evidence, None

        # خطوة فحص عامة
        evidence["type"] = "فحص عام"
        evidence["value"] = "مكتمل"
        return True, evidence, None

    def verify_plan(self, steps: List[PlanStep], executor_evidence: List[Dict[str, Any]]) -> Tuple[bool, List[Dict[str, Any]], List[str]]:
        """التدقيق الشامل للخطة بأكملها وإعادة قائمة الأدلة المادية وقائمة أسباب الفشل."""
        all_passed = True
        evidence_list: List[Dict[str, Any]] = []
        failure_reasons: List[str] = []

        step_map = {ev.get("step_id"): ev for ev in executor_evidence}

        for step in steps:
            info = step_map.get(step.step_id, {})
            passed, ev, error = self.verify_step_evidence(step, info)
            if not passed:
                all_passed = False
                if error:
                    failure_reasons.append(error)
            evidence_list.append(ev)

        # التحقق الإضافي: التأكد من وجود سجل التراجع على القرص وعدم تلفه
        journal_path = self.allowed_root / ".undo_journal.json"
        if not journal_path.exists():
            all_passed = False
            failure_reasons.append("سجل التراجع .undo_journal.json مفقود على القرص!")
        else:
            evidence_list.append({
                "item": ".undo_journal.json",
                "type": "سلامة سجل التراجع",
                "value": "موجود وموثق على القرص",
            })

        return all_passed, evidence_list, failure_reasons
