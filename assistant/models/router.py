"""الموجّه (Router): نموذج صغير للروتين وأكبر للتخطيط، مع ذاكرة مؤقتة وقصّ للسياق وحساب الكلفة بالنقاط."""
import hashlib
import json
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from .base import ModelProvider, CONTEXT_MARKER
from .pricing import cost_usd, usd_to_points

SMALL, LARGE = "small", "large"


def truncate_text(text: str, max_chars: int) -> str:
    """يبقي بداية النص ونهايته ويقتطع الوسط (توفير توكنات)."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    return text[:head] + "\n…[اقتُطع جزء من الوسط لتوفير الكلفة]…\n" + text[-tail:]


class ModelRouter(ModelProvider):
    def __init__(self, small: ModelProvider, large: Optional[ModelProvider] = None,
                 cache_size: int = 64, max_prompt_chars: int = 8000):
        self.providers = {SMALL: small, LARGE: large or small}
        self.cache_size = cache_size
        self.max_prompt_chars = max_prompt_chars
        self._cache: "OrderedDict[str, str]" = OrderedDict()
        self.calls: List[Dict[str, Any]] = []
        self.cache_hits = 0

    def name(self) -> str:
        return f"Router({self.providers[SMALL].name()} / {self.providers[LARGE].name()})"

    @staticmethod
    def choose_tier(prompt: str, system_prompt: str) -> str:
        """الأسئلة التوضيحية والروتين → صغير؛ التخطيط والمراجعة → كبير."""
        header = (system_prompt or "").split(CONTEXT_MARKER, 1)[0]  # قائمة الملفات (بيانات غير موثوقة) لا تؤثر في الاختيار
        if "CLARIFY" in header:
            return SMALL
        if "مخطط" in header or "VERIFY" in header:
            return LARGE
        return SMALL

    @staticmethod
    def _is_cacheable(text: str) -> bool:
        """لا نخزّن إلا ردوداً صالحة (JSON قاموس)؛ الرد التالف يجب أن يُعاد طلبه لا أن يتكرر."""
        try:
            return isinstance(json.loads(text), dict)
        except (ValueError, TypeError):
            return False

    def generate(self, prompt: str, system_prompt: str = "", tier: Optional[str] = None) -> str:
        tier = tier or self.choose_tier(prompt, system_prompt)
        key = hashlib.sha256(f"{tier}\x00{system_prompt}\x00{prompt}".encode("utf-8")).hexdigest()  # قبل القصّ: لا تصادم
        prompt = truncate_text(prompt, self.max_prompt_chars)
        system_prompt = truncate_text(system_prompt, self.max_prompt_chars)
        if self.cache_size and key in self._cache:
            self._cache.move_to_end(key)
            self.cache_hits += 1
            self.calls.append({"tier": tier, "cached": True, "input": 0, "output": 0, "usd": 0.0})
            return self._cache[key]
        provider = self.providers[tier]
        text = provider.generate(prompt, system_prompt=system_prompt)
        tin, tout = getattr(provider, "last_usage", (0, 0))
        usd = cost_usd(getattr(provider, "model", ""), tin, tout)
        self.calls.append({"tier": tier, "cached": False, "model": getattr(provider, "model", provider.name()),
                           "input": tin, "output": tout, "usd": usd})
        if self.cache_size and self._is_cacheable(text):
            self._cache[key] = text
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return text

    # ---------- حساب الكلفة ----------
    def totals(self) -> Dict[str, Any]:
        real = [c for c in self.calls if not c["cached"]]
        usd = sum(c["usd"] for c in real)
        return {
            "calls": len(real),
            "cache_hits": self.cache_hits,
            "input_tokens": sum(c["input"] for c in real),
            "output_tokens": sum(c["output"] for c in real),
            "usd": usd,
            "points": usd_to_points(usd),
        }
