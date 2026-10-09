"""وحدة المخطّط (Planner) — تحويل طلب المستخدم إلى خطوات عمل محددة بشروط قبول مقيسة."""
import json
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from assistant.models.base import ModelProvider


@dataclass
class PlanStep:
    """خطوة تنفيذية محددة ضمن الخطة."""
    step_id: int
    action_type: str
    target: str
    params: Dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: str = "تحقق وجود الملف ومطابقة البصمة"
    status: str = "PENDING"


class Planner:
    """المخطط المسؤول عن صياغة خطوات العمل مع شروط القبول القابلة للقياس."""

    def __init__(self, provider: ModelProvider):
        self.provider = provider

    def plan(self, user_prompt: str) -> List[PlanStep]:
        """توليد الخطة بالاعتماد على مزود النموذج وتحليل الاستجابة إلى خطوات مقيسة."""
        system_prompt = (
            "أنت مخطط مهام سطح المكتب. أعد خطة عمل بصيغة JSON تحتوي على قائمة steps، "
            "بحيث يكون لكل خطوة: step_id, action_type, target, params, acceptance_criteria."
        )
        response_text = self.provider.generate(user_prompt, system_prompt=system_prompt)

        # محاولة قراءة JSON من رد النموذج
        steps: List[PlanStep] = []
        try:
            # تنظيف محتمل لكتل الكود
            cleaned = response_text.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            data = json.loads(cleaned.strip())
            raw_steps = data.get("steps", []) if isinstance(data, dict) else data
            for item in raw_steps:
                steps.append(
                    PlanStep(
                        step_id=int(item.get("step_id", len(steps) + 1)),
                        action_type=str(item.get("action_type", "read")),
                        target=str(item.get("target", "")),
                        params=dict(item.get("params", {})),
                        acceptance_criteria=str(item.get("acceptance_criteria", "تحقق الحالة الفعلية")),
                    )
                )
        except Exception:
            # في حال كان رد المزود نصياً (مثل FakeProvider القديم)، ننشئ خطوة افتراضية آمنة
            steps.append(
                PlanStep(
                    step_id=1,
                    action_type="inspect",
                    target=user_prompt,
                    params={},
                    acceptance_criteria="فحص المدخلات وتأكيد المسارات",
                )
            )

        return steps
