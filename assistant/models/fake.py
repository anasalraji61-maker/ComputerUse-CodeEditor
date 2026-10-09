"""المزوّد الوهمي الحتمي (FakeProvider) للاختبارات الحتمية بلا شبكة ولا مفاتيح."""
from typing import Dict, Optional, List, Any
from .base import ModelProvider


class FakeProvider(ModelProvider):
    """مزود حتمي يعيد ردوداً مبرمجة مسبقاً، مخصص للاختبارات دون الاتصال بالشبكة."""

    def __init__(self, responses: Optional[Dict[str, str]] = None, default_response: str = "OK"):
        self.responses: Dict[str, str] = responses or {}
        self.default_response: str = default_response
        self.call_history: List[Dict[str, Any]] = []

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        self.call_history.append({"prompt": prompt, "system_prompt": system_prompt})
        for key, reply in self.responses.items():
            if key in prompt:
                return reply
        return self.default_response

    def name(self) -> str:
        return "FakeProvider(Deterministic)"
