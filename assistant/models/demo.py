"""مزوّد تجريبي محلي (DemoProvider) لتجربة الواجهة قبل ربط نموذج حقيقي (B3). بلا شبكة ولا مفاتيح."""
import json
from .base import ModelProvider


class DemoProvider(ModelProvider):
    """يعيد خططاً بسيطة وآمنة: إنشاء مجلد وفحصه. كلمة «هوية» أو whoami تضيف خطوة خطرة لتجربة نافذة الموافقة."""

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        if "CLARIFY" in system_prompt:
            questions = []
            if len(prompt.strip()) < 8:
                questions.append("ما الذي تريد فعله بالضبط؟ اكتب طلباً أوضح قليلاً.")
            return json.dumps({"questions": questions}, ensure_ascii=False)
        steps = [
            {"step_id": 1, "action_type": "create_dir", "target": "مجلد_تجريبي", "params": {},
             "acceptance_criteria": "وجود المجلد"},
            {"step_id": 2, "action_type": "inspect", "target": "مجلد_تجريبي", "params": {},
             "acceptance_criteria": "المجلد موجود فعلاً"},
        ]
        low = prompt.lower()
        if "whoami" in low or "هوية" in prompt:
            steps.append({"step_id": 3, "action_type": "run_command", "target": "whoami",
                          "params": {"cmd": ["whoami"]}, "acceptance_criteria": "رمز الخروج 0"})
        return json.dumps({"steps": steps}, ensure_ascii=False)

    def name(self) -> str:
        return "DemoProvider(Local)"
