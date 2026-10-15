"""حجب الأسرار من نصوص تُكتب أو تُعرض (نتائج الاختبارات، رسائل الأخطاء): أنماط مفاتيح معروفة الشكل + قيم متغيرات بيئة سرية الاسم.

مصمَّم لتقليل الإيجابيات الكاذبة: الأنماط بحدود (لا تطابق داخل كلمة أخرى مثل disk-cleanup-plan) وبأطوال واقعية،
وقيم البيئة تُحجب فقط لأسماء سرية صريحة وبطول >= 12 وليست مسارات ولا كلمات شائعة، ولا تُمسّ داخل المسارات."""
import os
import re
from typing import List, Mapping, Optional, Pattern

_B = r"(?<![A-Za-z0-9_\-])"   # لا يسبقها محرف كلمة/شرطة: لا مطابقة داخل كلمة أخرى

_PATTERN_SOURCES = [
    _B + r"sk-ant-[A-Za-z0-9_\-]{10,}",              # Anthropic
    _B + r"sk-proj-[A-Za-z0-9_\-]{20,}",             # OpenAI (مشاريع)
    _B + r"sk-[A-Za-z0-9]{20,}",                     # OpenAI/عام
    _B + r"AIza[0-9A-Za-z_\-]{30,}",                 # Google
    _B + r"gh[pousr]_[A-Za-z0-9]{30,}",              # GitHub
    _B + r"github_pat_[A-Za-z0-9_]{20,}",
    _B + r"xox[abprs]-[A-Za-z0-9\-]{10,}",           # Slack
    _B + r"AKIA[0-9A-Z]{16}",                        # AWS
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{20,}",
]

SECRET_PATTERNS: List[Pattern] = [re.compile(src) for src in _PATTERN_SOURCES]

# أسماء متغيرات سرية: API_KEY / TOKEN / SECRET / PASSWORD / PASSWD / CREDENTIAL (بكلمات كاملة بين شرطات سفلية)، أو تنتهي بـ _KEY
_SECRET_NAME = re.compile(r"(^|_)(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)S?($|_)|(^|_)KEY$", re.I)
_MIN_ENV_SECRET_LEN = 12
_COMMON_WORDS = {"production", "development", "localhost", "undefined", "disabled", "enabled", "automatic", "interactive",
                 "anonymous", "default", "required", "optional", "password", "username", "not-set", "notavailable"}


def secret_patterns() -> List[Pattern]:
    """قائمة الأنماط المترجمة (للاستعمال في أي مكان يحجب نصاً: المتحكم، الاختبارات، التقارير)."""
    return list(SECRET_PATTERNS)


def is_secret_name(name: str) -> bool:
    return bool(_SECRET_NAME.search(str(name)))


def _looks_like_path(value: str) -> bool:
    return bool(value.startswith(("/", "~", "./", "../", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", value))


def secret_env_values(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """قيم المتغيرات السرية الاسم الصالحة للحجب (طول >= 12، ليست مسارات ولا كلمات شائعة)، الأطول أولاً."""
    env = os.environ if environ is None else environ
    out = set()
    for k, v in env.items():
        if not isinstance(v, str) or len(v) < _MIN_ENV_SECRET_LEN or not is_secret_name(k):
            continue
        if _looks_like_path(v) or v.strip().lower() in _COMMON_WORDS:
            continue
        out.add(v)
    return sorted(out, key=len, reverse=True)


def redact(text: str, environ: Optional[Mapping[str, str]] = None, replacement: str = "***") -> str:
    """يحجب قيم البيئة السرية (خارج المسارات) ثم الأنماط المعروفة. يعيد النص نفسه إن لم يوجد شيء."""
    for v in secret_env_values(environ):
        # لا نمس ما التصق بحرف كلمة أو فاصل مسار (مثل جزء من مسار ملف أو كلمة أطول)
        text = re.sub(r"(?<![A-Za-z0-9_/\\])" + re.escape(v) + r"(?![A-Za-z0-9_/\\])", replacement, text)
    for pat in SECRET_PATTERNS:
        text = pat.sub(replacement, text)
    return text
