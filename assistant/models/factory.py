"""اختيار المزوّد من البيئة: COS_PROVIDER = demo (افتراضي) | anthropic | gemini | auto."""
import os
from typing import Mapping, Optional, Tuple

from .base import ModelProvider
from .demo import DemoProvider
from .http_base import ProviderConfigError
from .anthropic_provider import AnthropicProvider
from .gemini_provider import GeminiProvider
from .router import ModelRouter
from .rules import RuleFirstProvider

DEFAULTS = {
    "anthropic": ("claude-haiku-5-5", "claude-sonnet-5-5"),
    "gemini": ("gemini-3.1-flash-lite", "gemini-3.1-pro-preview"),
}


def _has(env: Mapping[str, str], *names: str) -> bool:
    return any(env.get(n) for n in names)


def build_provider(env: Optional[Mapping[str, str]] = None, **provider_kw) -> Tuple[ModelProvider, str]:
    """يعيد (المزوّد، ملاحظة للمستخدم بالعربية). الغياب الصريح للمفتاح يعطي ProviderConfigError واضحاً."""
    env = os.environ if env is None else env
    kind = (env.get("COS_PROVIDER") or "demo").strip().lower()
    if kind == "auto":
        kind = "anthropic" if _has(env, "ANTHROPIC_API_KEY") else "gemini" if _has(env, "GEMINI_API_KEY", "GOOGLE_API_KEY") else "demo"

    if kind == "demo":
        return DemoProvider(), "الوضع المحلي (بلا مفاتيح وبلا كلفة): يفهم ترتيب الملفات (حسب النوع أو الشهر)، النقل، النسخ، الحذف للسلة، المكررات، إعادة التسمية، إنشاء المجلدات والمشاريع، الفحص، وتحديث winget."
    if kind not in DEFAULTS:
        raise ProviderConfigError(f"قيمة COS_PROVIDER غير معروفة: '{kind}'. القيم: demo, anthropic, gemini, auto.")

    small_m = env.get("COS_SMALL_MODEL") or DEFAULTS[kind][0]
    large_m = env.get("COS_LARGE_MODEL") or DEFAULTS[kind][1]
    cls = AnthropicProvider if kind == "anthropic" else GeminiProvider
    key = next((env[k] for k in cls.ENV_KEYS if env.get(k)), "")
    small = cls(model=small_m, api_key=key, **provider_kw)
    large = cls(model=large_m, api_key=key, **provider_kw) if large_m != small_m else small
    # يتحقق من وجود المفتاح الآن لا عند أول مهمة
    small._require_key()
    router = ModelRouter(small, large)
    return RuleFirstProvider(router), (
        f"نموذج حقيقي: {kind} (صغير: {small_m}، كبير: {large_m}). الطلبات المعروفة تُخطَّط محلياً بلا كلفة."
    )
