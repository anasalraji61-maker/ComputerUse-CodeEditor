"""وحدة المخطّط (Planner) — تحويل طلب المستخدم إلى خطوات عمل محققة الصحة بشروط قبول مقيسة."""
import json
from dataclasses import dataclass, field
from typing import List, Dict, Any
from assistant.models.base import ModelProvider
from assistant.safety import ACTION_LEVELS

MAX_PLAN_STEPS = 50


class PlanParseError(Exception):
    """رد النموذج لا يمكن تحويله إلى خطة صالحة — لا خطة افتراضية «ناجحة» أبداً (R20)."""
    pass


@dataclass
class PlanStep:
    """خطوة تنفيذية محددة ضمن الخطة."""
    step_id: int
    action_type: str
    target: str
    params: Dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: str = "تحقق وجود الملف ومطابقة البصمة"
    status: str = "PENDING"


def _extract_json(text: str) -> Any:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    try:
        return json.loads(cleaned.strip())
    except (ValueError, TypeError) as exc:
        raise PlanParseError(f"رد النموذج ليس JSON صالحاً: {exc}")


def _require_str(params: Dict[str, Any], key: str, action: str, step_id: int) -> None:
    v = params.get(key)
    if not isinstance(v, str) or not v.strip():
        raise PlanParseError(f"الخطوة {step_id} ({action}) تتطلب params.{key} نصاً غير فارغ.")


def validate_step(item: Any, index: int) -> PlanStep:
    if not isinstance(item, dict):
        raise PlanParseError(f"الخطوة رقم {index} ليست كائناً.")
    raw_id = item.get("step_id", index)
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        raise PlanParseError(f"step_id في الخطوة رقم {index} يجب أن يكون عدداً صحيحاً.")
    action = item.get("action_type")
    if not isinstance(action, str) or action.strip().lower() not in ACTION_LEVELS:
        raise PlanParseError(f"الخطوة {raw_id}: نوع الإجراء '{action}' غير معروف.")
    action = action.strip().lower()
    target = item.get("target")
    if not isinstance(target, str) or not target.strip():
        raise PlanParseError(f"الخطوة {raw_id}: الهدف (target) مطلوب.")
    params = item.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise PlanParseError(f"الخطوة {raw_id}: params يجب أن يكون كائناً.")
    if action in ("move", "copy"):
        _require_str(params, "dest", action, raw_id)
    elif action == "rename":
        _require_str(params, "new_name", action, raw_id)
    elif action == "run_command":
        cmd = params.get("cmd", [target])
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
            raise PlanParseError(f"الخطوة {raw_id}: params.cmd يجب أن تكون قائمة نصوص غير فارغة.")
    crit = item.get("acceptance_criteria", "تحقق الحالة الفعلية")
    return PlanStep(
        step_id=raw_id,
        action_type=action,
        target=target.strip(),
        params=dict(params),
        acceptance_criteria=str(crit),
    )


class Planner:
    """المخطط المسؤول عن صياغة خطوات العمل مع شروط القبول القابلة للقياس."""

    def __init__(self, provider: ModelProvider, max_steps: int = MAX_PLAN_STEPS):
        self.provider = provider
        self.max_steps = max_steps

    def plan(self, user_prompt: str) -> List[PlanStep]:
        """توليد الخطة؛ أي رد غير صالح يرفع PlanParseError ولا تُنشأ خطوات بديلة."""
        system_prompt = (
            "أنت مخطط مهام سطح المكتب. أعد خطة عمل بصيغة JSON تحتوي على قائمة steps، "
            "بحيث يكون لكل خطوة: step_id, action_type, target, params, acceptance_criteria. "
            "الإجراءات المسموحة: " + ", ".join(sorted(k for k in ACTION_LEVELS)) + "."
        )
        data = _extract_json(self.provider.generate(user_prompt, system_prompt=system_prompt))
        raw_steps = data.get("steps") if isinstance(data, dict) else data
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanParseError("الخطة فارغة أو لا تحتوي قائمة steps.")
        if len(raw_steps) > self.max_steps:
            raise PlanParseError(f"عدد الخطوات {len(raw_steps)} يتجاوز الحد الأقصى {self.max_steps}.")
        steps = [validate_step(item, i) for i, item in enumerate(raw_steps, 1)]
        ids = [s.step_id for s in steps]
        if len(set(ids)) != len(ids):
            raise PlanParseError("أرقام الخطوات (step_id) مكررة.")
        return steps
