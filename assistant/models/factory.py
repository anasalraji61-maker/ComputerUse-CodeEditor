"""اختيار المزوّد من البيئة: COS_PROVIDER = demo (افتراضي) | anthropic | gemini | openai | ollama | openai_compat | auto."""
import os
import urllib.parse
from typing import Mapping, Optional, Tuple

from .base import ModelProvider
from .demo import DemoProvider
from .http_base import ProviderConfigError
from .anthropic_provider import AnthropicProvider
from .gemini_provider import GeminiProvider
from .openai_compat import (OpenAICompatProvider, OLLAMA_BASE_URL, OPENAI_BASE_URL, is_local_url, normalize_base_url)
from .router import ModelRouter
from .rules import RuleFirstProvider

DEFAULTS = {
    "anthropic": ("claude-haiku-5-5", "claude-sonnet-5-5"),
    "gemini": ("gemini-3.1-flash-lite", "gemini-3.1-pro-preview"),
    "openai": ("gpt-4o-mini", "gpt-4o"),
    # أمثلة لا توصية: اسحب النموذج أولاً (ollama pull qwen2.5:7b) أو غيّره بـ COS_SMALL_MODEL / COS_LARGE_MODEL
    "ollama": ("qwen2.5:7b", "qwen2.5:7b"),
    "openai_compat": (None, None),   # لا افتراضي: الاسم مطلوب من COS_SMALL_MODEL
}
_ALIASES = {"openai-compat": "openai_compat", "openaicompat": "openai_compat", "local": "ollama"}


def _has(env: Mapping[str, str], *names: str) -> bool:
    return any(env.get(n) for n in names)


def build_provider(env: Optional[Mapping[str, str]] = None, **provider_kw) -> Tuple[ModelProvider, str]:
    """يعيد (المزوّد، ملاحظة للمستخدم بالعربية). الغياب الصريح للمفتاح يعطي ProviderConfigError واضحاً."""
    env = os.environ if env is None else env
    kind = (env.get("COS_PROVIDER") or "demo").strip().lower()
    kind = _ALIASES.get(kind, kind)
    if kind == "auto":
        kind = ("anthropic" if _has(env, "ANTHROPIC_API_KEY") else "gemini" if _has(env, "GEMINI_API_KEY", "GOOGLE_API_KEY")
                else "openai" if _has(env, "OPENAI_API_KEY") else "demo")

    if kind == "demo":
        return DemoProvider(), "الوضع المحلي (بلا مفاتيح وبلا كلفة): يفهم ترتيب الملفات (حسب النوع أو الشهر)، النقل، النسخ، الحذف للسلة، المكررات، إعادة التسمية، إنشاء المجلدات والمشاريع، الفحص، وتحديث winget."
    if kind not in DEFAULTS:
        raise ProviderConfigError(f"قيمة COS_PROVIDER غير معروفة: '{kind}'. القيم: demo, anthropic, gemini, openai, ollama, openai_compat, auto.")

    if kind in ("openai", "ollama", "openai_compat"):
        return _build_openai_family(kind, env, **provider_kw)

    small_m = env.get("COS_SMALL_MODEL") or DEFAULTS[kind][0]
    large_m = env.get("COS_LARGE_MODEL") or DEFAULTS[kind][1]
    cls = AnthropicProvider if kind == "anthropic" else GeminiProvider
    key = next((env[k] for k in cls.ENV_KEYS if env.get(k, "").strip()), "").strip()
    small = cls(model=small_m, api_key=key, **provider_kw)
    large = cls(model=large_m, api_key=key, **provider_kw) if large_m != small_m else small
    # يتحقق من وجود المفتاح الآن لا عند أول مهمة
    small._require_key()
    router = ModelRouter(small, large)
    return RuleFirstProvider(router), (
        f"نموذج حقيقي: {kind} (صغير: {small_m}، كبير: {large_m}). الطلبات المعروفة تُخطَّط محلياً بلا كلفة. "
        f"تنبيه خصوصية: في الطلبات غير المعروفة تُرسل أسماء الملفات (حتى 60 اسماً) وطلبك إلى {'Anthropic' if kind == 'anthropic' else 'Google'}."
    )


def _build_openai_family(kind: str, env: Mapping[str, str], **provider_kw) -> Tuple[ModelProvider, str]:
    """openai (سحابي، يتطلب مفتاحاً) | ollama (محلي بلا مفتاح) | openai_compat (أي خادم متوافق؛ العنوان والنموذج مطلوبان).
    سياسة المفتاح: OPENAI_API_KEY المحيط يُرسل إلى api.openai.com (https) فقط؛ لأي خادم آخر يلزم COS_OPENAI_API_KEY صريح."""
    base = (env.get("COS_OPENAI_BASE_URL") or "").strip() or (OLLAMA_BASE_URL if kind == "ollama" else OPENAI_BASE_URL if kind == "openai" else "")
    if not base:
        raise ProviderConfigError("openai_compat يتطلب COS_OPENAI_BASE_URL (مثل http://127.0.0.1:1234/v1) وCOS_SMALL_MODEL.")
    base = normalize_base_url(base)
    parsed = urllib.parse.urlparse(base)
    local = is_local_url(base)     # المحلية تُحكم دائماً من العنوان الفعلي لا من اسم المزوّد
    is_openai_host = parsed.scheme == "https" and parsed.hostname == "api.openai.com"
    if kind == "ollama" and not local and (env.get("COS_ALLOW_REMOTE_OLLAMA") or "").strip() != "1":
        raise ProviderConfigError(f"Ollama المحلي لا يُوجَّه إلى خادم غير محلي ({parsed.hostname}) لأن بياناتك ستغادر جهازك. "
                                  f"استعمل عنواناً محلياً، أو COS_ALLOW_REMOTE_OLLAMA=1 إن كنت تقصد خادماً بعيداً (ولن يُحسب مجانياً).")
    small_m = env.get("COS_SMALL_MODEL") or DEFAULTS[kind][0]
    large_m = env.get("COS_LARGE_MODEL") or DEFAULTS[kind][1] or small_m
    if not small_m:
        raise ProviderConfigError("حدّد اسم النموذج بمتغير COS_SMALL_MODEL (وCOS_LARGE_MODEL اختيارياً).")
    large_m = large_m or small_m
    explicit = (env.get("COS_OPENAI_API_KEY") or "").strip()
    ambient = (env.get("OPENAI_API_KEY") or "").strip()
    if explicit:
        key = explicit
    elif kind != "ollama" and is_openai_host:
        key = ambient          # المفتاح المحيط لـ OpenAI نفسه فقط
    else:
        key = ""
    if kind == "openai" and not key:
        if ambient and not is_openai_host:
            raise ProviderConfigError(f"لن يُرسل OPENAI_API_KEY إلى {parsed.hostname} (ليس api.openai.com). "
                                      f"لخادم آخر عيّن COS_OPENAI_API_KEY صراحةً أو استعمل COS_PROVIDER=openai_compat.")
        raise ProviderConfigError("مفتاح OpenAI غير موجود. عيّن متغير البيئة OPENAI_API_KEY ثم أعد التشغيل.")
    if key and parsed.scheme == "http" and not local:
        raise ProviderConfigError("لن يُرسل المفتاح عبر http غير مشفّر إلى خادم غير محلي؛ استعمل https.")
    kw = dict(base_url=base, local=local, ollama=(kind == "ollama"), key_required=(kind == "openai"), **provider_kw)
    small = OpenAICompatProvider(model=small_m, api_key=key, **kw)
    large = OpenAICompatProvider(model=large_m, api_key=key, **kw) if large_m != small_m else small
    small._require_key()
    router = ModelRouter(small, large)
    host = small.host_label
    if local:
        note = (f"نموذج محلي: {kind} على {host} (صغير: {small_m}، كبير: {large_m}). بلا كلفة، ولا تغادر أسماء ملفاتك وطلبك جهازك/شبكتك المحلية. "
                f"الطلبات المعروفة تُخطَّط بالقواعد المحلية أولاً."
                + (f" إن لم يكن النموذج منزّلاً: ollama pull {small_m}" if kind == "ollama" else ""))
    else:
        vendor = "OpenAI" if is_openai_host else host
        note = (f"نموذج حقيقي: {kind} (صغير: {small_m}، كبير: {large_m}). الطلبات المعروفة تُخطَّط محلياً بلا كلفة. "
                f"تنبيه خصوصية: في الطلبات غير المعروفة تُرسل أسماء الملفات (حتى 60 اسماً) وطلبك إلى {vendor}.")
    return RuleFirstProvider(router), note
