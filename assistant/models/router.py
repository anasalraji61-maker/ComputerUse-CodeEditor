"""الموجّه (Router): نموذج صغير للروتين وأكبر للتخطيط، مع ذاكرة مؤقتة وقصّ للسياق وحساب الكلفة بالنقاط."""
import hashlib
import json
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from .base import ModelProvider, CONTEXT_MARKER
from .http_base import ProviderError
from .pricing import cost_usd, usd_to_points, is_priced, PRICES_PER_MTOK

SMALL, LARGE = "small", "large"


def truncate_text(text: str, max_chars: int) -> str:
    """يبقي بداية النص ونهايته ويقتطع الوسط (توفير توكنات)."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    return text[:head] + "\n…[اقتُطع جزء من الوسط لتوفير الكلفة]…\n" + text[-tail:]


def estimate_max_cost(provider: ModelProvider, prompt: str, system_prompt: str) -> float:
    """أعلى كلفة محتملة لنداء واحد: توكن لكل محرف إدخال (حد أعلى متحفظ) + أقصى توكنات إخراج للمزوّد، بسعر نموذجه.
    مزوّد بلا اسم نموذج (تجريبي/وهمي مجاني) كلفته صفر."""
    model = getattr(provider, "model", None)
    if not isinstance(model, str) or not model:
        return 0.0
    attempts = getattr(provider, "max_attempts", 1)   # نداءات قد تُنفَّذ لطلب منطقي واحد (إعادات المزوّد)
    attempts = attempts if isinstance(attempts, int) and 1 <= attempts <= 5 else 1
    override = getattr(provider, "price_per_mtok", None)   # مزوّد محلي: سعر صفري صريح
    if override is not None and not any(override):
        return 0.0
    max_out = getattr(provider, "max_tokens", 2048)
    max_out = max_out if isinstance(max_out, int) and max_out > 0 else 2048
    headroom = getattr(provider, "thinking_headroom", None)   # توكنات تفكير قد تُفوتر كإخراج (Gemini)
    if callable(headroom):
        try:
            max_out += max(0, int(headroom()))
        except Exception:
            pass
    tin = len(prompt or "") + len(system_prompt or "")
    if override is not None:
        return attempts * cost_usd(model, tin, max_out, price=override)
    if not is_priced(model):
        # نموذج غير مسعَّر: الحساب الفعلي بأعلى سعر احتياطي، أما الحجز فبأعلى سعر معروف حتى لا يحجب النداء بلا داعٍ
        pin = max(p[0] for p in PRICES_PER_MTOK.values())
        pout = max(p[1] for p in PRICES_PER_MTOK.values())
        return attempts * (tin * pin + max_out * pout) / 1_000_000.0
    return attempts * cost_usd(model, tin, max_out)


class ModelRouter(ModelProvider):
    def __init__(self, small: ModelProvider, large: Optional[ModelProvider] = None,
                 cache_size: int = 64, max_prompt_chars: int = 8000, usage_meter=None, fallback_to_active_meter: bool = True):
        # عدّاد الاستهلاك لحجز الكلفة: يُضبط صراحةً (المتحكم يضبط router.usage_meter)؛ وإن لم يُضبط واستمر
        # fallback_to_active_meter=True يُستعمل آخر UsageMeter أُنشئ في العملية (احتياط). False يعطّل الحجز بلا عدّاد صريح.
        self.usage_meter = usage_meter
        self.fallback_to_active_meter = fallback_to_active_meter
        self.providers = {SMALL: small, LARGE: large or small}
        self.cache_size = cache_size
        self.max_prompt_chars = max_prompt_chars
        self._cache: "OrderedDict[str, str]" = OrderedDict()
        self.calls: List[Dict[str, Any]] = []
        self.cache_hits = 0

    def _meter(self):
        if self.usage_meter is not None:
            return self.usage_meter
        if not self.fallback_to_active_meter:
            return None
        from assistant.usage import active_meter
        return active_meter()

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
            data = json.loads(text)
        except (ValueError, TypeError, RecursionError, MemoryError):
            return False
        if not isinstance(data, dict):
            return False
        if "steps" in data:  # خطة غير صالحة لا تُخزَّن: إعادة الطلب يجب أن تسأل النموذج من جديد
            from assistant.planner import validate_step, PlanParseError
            steps = data["steps"]
            if not isinstance(steps, list) or not steps:
                return False
            try:
                for i, st in enumerate(steps, 1):
                    validate_step(st, i)
            except PlanParseError:
                return False
        return True

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
        meter, token = self._meter(), None
        if meter is not None:  # حجز الحد الأقصى المقدَّر قبل نداء مدفوع: يمنع تجاوز السقف بإيقاف/إعادة إرسال والنداء السابق طائر
            est = estimate_max_cost(provider, prompt, system_prompt)
            if est > 0:
                token = meter.reserve(est)
                if token is None:
                    raise ProviderError(meter.reserve_denied_message(est))
        try:
            text = provider.generate(prompt, system_prompt=system_prompt)
        except Exception:
            tin, tout = getattr(provider, "last_usage", (0, 0))
            if tin or tout:  # نداء فاشل لكنه مفوتَر (رد فارغ استهلك توكنات تفكير): يدخل في العدّاد وسقف الإنفاق
                self.calls.append({"tier": tier, "cached": False, "model": getattr(provider, "model", provider.name()),
                                   "input": tin, "output": tout, "usd": cost_usd(getattr(provider, "model", ""), tin, tout, getattr(provider, "price_per_mtok", None)),
                                   "failed": True})
            raise
        finally:
            if token is not None:
                meter.release(token)
        tin, tout = getattr(provider, "last_usage", (0, 0))
        usd = cost_usd(getattr(provider, "model", ""), tin, tout, getattr(provider, "price_per_mtok", None))
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
