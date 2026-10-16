"""حجب الأسرار من نصوص تُكتب أو تُعرض (نتائج الاختبارات، رسائل الأخطاء): أنماط مفاتيح معروفة الشكل + قيم متغيرات بيئة سرية الاسم.

مصمَّم لتقليل الإيجابيات الكاذبة: الأنماط بحدود (لا تطابق داخل كلمة أخرى مثل disk-cleanup-plan) وبأطوال واقعية،
وقيم البيئة تُحجب فقط لأسماء سرية صريحة وبطول >= 12 وليست مسارات ولا كلمات شائعة، ولا تُمسّ داخل المسارات."""
import base64
import functools
import html
import json
import os
import re
import urllib.parse
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


# ---------- حجب مفتاح معروف بصيغه المحوّلة ----------
KEY_FRAGMENT_MIN_KEY = 16    # الأجزاء والصيغ المحوّلة (معكوس/ست عشري/مقطّع) لا تُطبَّق إلا على مفاتيح بطول >= 16 (أقصر منها إيجابيات كاذبة)
KEY_FRAGMENT_LEN = 12        # أي جزء متصل بهذا الطول فأكثر من المفتاح يُحجب
_SPLIT_SEP = r"[\s\-_.,:;|/\\]{0,3}"
_HEX_SEP = r"[\s:,\-]{0,2}"


def key_exact_forms(key: str) -> List[str]:
    """الصيغ التي يعكسها خادم عادةً: الخام، ترميز الرابط، هروب JSON (مفرد ومزدوج ومع \\/)، هروب HTML، base64؛ الأطول أولاً."""
    if not key or len(key) < 4:
        return []
    k = key
    out = {k, urllib.parse.quote(k, safe=""), urllib.parse.quote_plus(k), k.replace("\\", "\\\\")}
    j = json.dumps(k)[1:-1]
    out |= {j, j.replace("/", "\\/"), json.dumps(k, ensure_ascii=False)[1:-1], json.dumps(j)[1:-1]}
    h = html.escape(k)
    out |= {h, html.escape(k, quote=False), h.replace("&#x27;", "&#39;")}
    raw = k.encode("utf-8", "replace")
    b = base64.b64encode(raw).decode("ascii")
    out |= {b, b.rstrip("="), base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")}
    return sorted((v for v in out if v), key=len, reverse=True)


@functools.lru_cache(maxsize=64)
def _transformed_regexes(key: str):
    """(أنماط الصيغ المحوّلة الكاملة، نمط الأجزاء المتصلة) لمفتاح طوله >= KEY_FRAGMENT_MIN_KEY."""
    full: List[Pattern] = []
    esc = re.escape
    for src in (key, key[::-1]):
        full.append(re.compile(_SPLIT_SEP.join(esc(c) for c in src), re.I))          # مقطّع بشرطات/مسافات/نقاط ...
    hx = key.encode("utf-8", "replace").hex()
    pairs = [hx[i:i + 2] for i in range(0, len(hx), 2)]
    full.append(re.compile(_HEX_SEP.join(pairs), re.I))                               # ست عشري (بفواصل بين البايتات أو بدونها)
    full.append(re.compile(_HEX_SEP.join(pairs[::-1]), re.I))
    n = KEY_FRAGMENT_LEN
    wins = {key[i:i + n] for i in range(len(key) - n + 1)} | {key[::-1][i:i + n] for i in range(len(key) - n + 1)}
    hn = 2 * n
    wins |= {hx[i:i + hn] for i in range(len(hx) - hn + 1)}
    frag = re.compile("(?=(" + "|".join(esc(w) for w in sorted(wins, key=len, reverse=True)) + "))", re.I) if wins else None
    return tuple(full), frag


def redact_key(text, key: str, replacement: str = "***") -> str:
    """يحجب مفتاحاً معروفاً من نص: كل صيغه الدقيقة (حساسة لحالة الأحرف لا)، ثم للمفاتيح >= 16: المعكوس والست عشري والمقطّع بفواصل
    وأي جزء متصل >= 12 محرفاً منه (يُوسَّع إلى أطول مقطع متصل). يعيد النص نفسه إن لم يوجد شيء."""
    text = str(text)
    if not key:
        return text
    for v in key_exact_forms(key):
        text = re.sub(re.escape(v), replacement, text, flags=re.I)
    if len(key) < KEY_FRAGMENT_MIN_KEY:
        return text
    full, frag = _transformed_regexes(key)
    for pat in full:
        text = pat.sub(replacement, text)
    if frag is not None:
        spans = []
        for m in frag.finditer(text):
            a, b = m.start(), m.start() + len(m.group(1))
            if spans and a <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        for a, b in reversed(spans):
            text = text[:a] + replacement + text[b:]
    return text
