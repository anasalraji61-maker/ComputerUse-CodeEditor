"""أساس مشترك لمزودي النماذج الحقيقيين: نقل HTTP قابل للاستبدال، أخطاء عربية، عدّاد استهلاك، وحجب المفاتيح."""
import email.utils
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from .base import ModelProvider
from .pricing import cost_usd

Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, bytes]]


class ProviderError(Exception):
    """فشل في استدعاء نموذج (شبكة / مصادقة / رد غير مفهوم)."""


class ProviderConfigError(ProviderError):
    """إعداد ناقص (مثل غياب المفتاح)."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0

    def add(self, model: str, tin: int, tout: int):
        self.calls += 1
        self.input_tokens += tin
        self.output_tokens += tout
        self.usd += cost_usd(model, tin, tout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """لا نتبع التحويلات أبداً: urllib ينسخ ترويسة المفتاح إلى المضيف الجديد."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


MAX_RETRY_AFTER_SEC = 60.0
_TLS = threading.local()   # ترويسة Retry-After لآخر رد من default_transport (النقل المخصص قد يعيد ثلاثية (حالة، جسم، ترويسات))


def parse_retry_after(value) -> Optional[float]:
    """Retry-After: ثوانٍ أو تاريخ HTTP؛ يُقيَّد بـ 60 ثانية. غير صالح => None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        secs = float(text)
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(text)
            secs = dt.timestamp() - time.time()
        except Exception:
            return None
    if secs != secs or secs < 0:   # NaN أو سالب
        return 0.0 if secs == secs else None
    return min(secs, MAX_RETRY_AFTER_SEC)


def describe_network_error(exc: BaseException) -> str:
    """وصف عربي لسبب فشل الاتصال حسب نوع الاستثناء فقط (لا نص الاستثناء: قد يحوي الرابط)."""
    reason = getattr(exc, "reason", exc) if isinstance(exc, urllib.error.URLError) else exc
    chain = [reason, exc]
    for e in chain:
        if isinstance(e, ssl.SSLCertVerificationError) or (isinstance(e, ssl.SSLError) and "CERTIFICATE" in str(e).upper()):
            return "فشل التحقق من شهادة الخادم (شهادة غير موثوقة؛ قد يعترض برنامج حماية أو وسيط/بروكسي على الاتصال)."
        if isinstance(e, ssl.SSLError):
            return "فشل إنشاء اتصال مشفّر (TLS) بالخدمة؛ تحقق من الإنترنت أو من برنامج الحماية."
        if isinstance(e, socket.gaierror):
            return "تعذّر تحليل اسم الخادم (DNS): تحقق من اتصال الإنترنت أو إعدادات DNS/البروكسي."
        if isinstance(e, (socket.timeout, TimeoutError)):
            return "انتهت مهلة الاتصال بالخدمة؛ الشبكة بطيئة أو محجوبة. أعد المحاولة."
        if isinstance(e, ConnectionRefusedError):
            return "رفض الخادم الاتصال؛ قد يكون الوصول محجوباً من شبكتك."
        if isinstance(e, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return "قُطع الاتصال بالخدمة أثناء الطلب؛ أعد المحاولة."
    return f"تعذّر الاتصال بالخدمة (نوع الخطأ: {type(exc).__name__}). تحقق من الإنترنت."


def default_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    _TLS.retry_after = None
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            _TLS.retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
        except Exception:
            _TLS.retry_after = None
        return exc.code, exc.read()
    except Exception as exc:  # شبكة/مهلة
        raise ProviderError(describe_network_error(exc)) from None


_REAL_SLEEP = time.sleep
_AUTO = object()
_OFF_WORDS = ("off", "none", "omit", "default", "null")


class HttpProvider(ModelProvider):
    """يطبّق إعادة المحاولة المحدودة وإخفاء المفتاح؛ الصنف الفرعي يبني الطلب ويحلل الرد."""

    PROVIDER_LABEL = "النموذج"
    ENV_KEYS: Tuple[str, ...] = ()
    RETRY_STATUS = (429, 500, 502, 503, 529)

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        transport: Optional[Transport] = None,
        max_tokens: int = 2048,
        timeout: float = 60.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        cancel_check: Optional[Callable[[], bool]] = None,
        thinking_budget=_AUTO,
    ):
        self.model = model
        # api_key=None => من البيئة؛ أي قيمة صريحة (حتى "") تُستعمل كما هي ولا تسقط إلى مفتاح البيئة المحيط (يمنع استعمال مفتاح غير مقصود)
        if api_key is None:
            api_key = next((os.environ[k] for k in self.ENV_KEYS if os.environ.get(k)), "")
        self._api_key = str(api_key).strip()
        self.transport = transport or default_transport
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self.cancel_check = cancel_check  # يُضبط من المتحكم: عند الإيقاف لا نعيد المحاولة ولا ننام
        self.usage = Usage()
        self.last_usage = (0, 0)
        # ميزانية التفكير (Gemini فقط): _AUTO = من COS_THINKING_BUDGET أو الافتراضي حسب النموذج؛ None = لا يُرسل الحقل؛ رقم = يُقيَّد بمدى النموذج
        self._thinking_arg = thinking_budget
        self._thinking_rejected = False  # ثبت أن الخدمة ترفض الحقل (400 يذكر thinking، أو نجح الطلب بدونه بعد 400): لا نرسله بعدها

    def name(self) -> str:
        return f"{self.PROVIDER_LABEL}:{self.model}"

    def _backoff(self, seconds: float) -> None:
        """نوم الانتظار قبل إعادة المحاولة. مع النوم الحقيقي نقطّعه إلى شرائح 0.2ث ونفحص الإيقاف بينها؛
        ومع نوم مُحقَن (اختبارات) يُستدعى مرة واحدة بالمدة كاملة."""
        if self._sleep is not _REAL_SLEEP:
            self._sleep(seconds)
            return
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if self.cancel_check is not None and self.cancel_check():
                raise ProviderError("أُوقف الطلب بزر الإيقاف.")
            left = end - time.monotonic()
            if left <= 0:
                return
            _REAL_SLEEP(min(0.2, left))

    def _status_error(self, status: int, raw: bytes) -> ProviderError:
        """رسالة عربية مميّزة لكل فئة خطأ HTTP؛ لا تحوي المفتاح ولا الرابط."""
        label = self.PROVIDER_LABEL
        if status == 401:
            return ProviderError(f"رُفض المفتاح من خدمة {label} (رمز 401): المفتاح غير صالح أو منتهٍ أو منسوخ ناقصاً. أنشئ مفتاحاً جديداً وأعد المحاولة.")
        if status == 403:
            return ProviderError(f"ممنوع من خدمة {label} (رمز 403): صلاحيات المفتاح لا تسمح بهذا الطلب، أو قد يكون البلد أو الفوترة غير مدعوم من الخدمة "
                                 f"(فعّل الفوترة وتحقق من أن بلدك مدعوم).")
        if status == 404:
            return ProviderError(f"النموذج '{self._redact(str(self.model))}' غير موجود أو غير متاح لحسابك لدى {label} (رمز 404). "
                                 f"غيّره بمتغير البيئة COS_SMALL_MODEL أو COS_LARGE_MODEL.")
        if status == 429:
            return ProviderError(f"تجاوزتَ حد الطلبات أو الحصة لدى {label} (رمز 429) حتى بعد إعادة المحاولة. انتظر قليلاً أو راجع حصتك وفوترتك.")
        snippet = self._redact(raw.decode("utf-8", "replace"))[:200]
        if status >= 500:
            return ProviderError(f"خدمة {label} مشغولة أو معطّلة مؤقتاً (رمز {status}) بعد إعادة المحاولة؛ جرّب لاحقاً. {snippet}".strip())
        return ProviderError(f"خطأ من خدمة {label} (رمز {status}): {snippet}")

    def _require_key(self) -> str:
        if not self._api_key:
            raise ProviderConfigError(
                f"مفتاح {self.PROVIDER_LABEL} غير موجود. عيّن متغير البيئة {self.ENV_KEYS[0]} ثم أعد التشغيل."
            )
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) > 126 for ch in self._api_key):
            raise ProviderConfigError(f"مفتاح {self.PROVIDER_LABEL} يحوي فراغات أو محارف غير صالحة؛ انسخه من جديد بلا مسافات.")
        return self._api_key

    def _redact(self, text: str) -> str:
        if not self._api_key:
            return text
        for variant in {self._api_key, urllib.parse.quote(self._api_key, safe=""), urllib.parse.quote_plus(self._api_key)}:
            text = text.replace(variant, "***")
        return text

    # يوفّرها الصنف الفرعي
    def _build(self, prompt: str, system_prompt: str) -> Tuple[str, Dict[str, str], dict]:
        raise NotImplementedError

    def _parse(self, data: dict) -> Tuple[str, int, int]:
        raise NotImplementedError

    def _usage(self, data: dict) -> Tuple[int, int]:
        """(توكنات الإدخال، توكنات الإخراج المفوترة) من رد صالح؛ تُسجَّل حتى لو كان النص فارغاً (الخدمة تفوتر)."""
        raise NotImplementedError

    THINKING_DYNAMIC_MAX = 32768   # أقصى تفكير ديناميكي محتمل عند حذف الحقل (حد أعلى متحفظ للحجز)

    def thinking_headroom(self) -> int:
        """توكنات إخراج إضافية قد تُفوتر كـ«تفكير» فوق max_tokens (Gemini فقط): الميزانية المضبوطة، أو الحد الديناميكي إن حُذف الحقل.
        يستعملها الموجّه لحجز الكلفة بتحفظ."""
        if "gemini" not in self.model.lower():
            return 0
        b = self._thinking_budget()
        return self.THINKING_DYNAMIC_MAX if b is None else b

    def _thinking_budget(self) -> Optional[int]:
        """ميزانية «التفكير» لنماذج Gemini (توكنات التفكير تُفوتر كإخراج وتأكل maxOutputTokens). None = احذف الحقل.
        الإعداد: وسيط thinking_budget ثم متغير COS_THINKING_BUDGET (رقم، أو off/none لحذف الحقل). القيمة تُقيَّد بمدى النموذج:
        فئة pro لا تقبل أقل من 128 ولا -1 (ديناميكي) فنرفعها؛ غيرها 0..24576. الافتراضي: 0 لغير pro و256 لـ pro."""
        if self._thinking_rejected:
            return None
        is_pro = "pro" in self.model.lower()
        default = 256 if is_pro else 0
        arg = self._thinking_arg
        if arg is _AUTO:
            raw = os.environ.get("COS_THINKING_BUDGET", "").strip().lower()
            if raw in _OFF_WORDS:
                return None
            try:
                arg = int(raw) if raw else default
            except ValueError:
                arg = default
        if arg is None:
            return None
        if not isinstance(arg, int) or isinstance(arg, bool) or arg < 0:   # -1 (ديناميكي) وغير الصالح: الافتراضي
            arg = default
        return max(128, min(32768, arg)) if is_pro else min(24576, arg)

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        self._require_key()
        self.last_usage = (0, 0)
        url, headers, payload = self._build(prompt, system_prompt)
        gen_cfg = payload.get("generationConfig") if isinstance(payload, dict) else None
        added_thinking = False
        if isinstance(gen_cfg, dict) and "thinkingConfig" not in gen_cfg:  # شكل طلب Gemini
            budget = self._thinking_budget()
            if budget is not None:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}
                added_thinking = True
        status, raw = 0, b""
        retried_unknown_400 = False   # أُعيد الطلب بلا الحقل بعد 400 لا يذكر thinking: نحكم بنتيجة الإعادة
        for _round in range(2):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            for attempt in range(self.max_retries + 1):
                res = self.transport(url, headers, body, self.timeout)
                status, raw = res[0], res[1]
                retry_after = parse_retry_after((res[2] or {}).get("Retry-After")) if len(res) > 2 and isinstance(res[2], dict) \
                    else getattr(_TLS, "retry_after", None) if self.transport is default_transport else None
                if status not in self.RETRY_STATUS:
                    break
                if attempt < self.max_retries:
                    if self.cancel_check is not None and self.cancel_check():
                        raise ProviderError("أُوقف الطلب بزر الإيقاف.")
                    delay = retry_after if retry_after is not None else 1.5 * (attempt + 1)   # يحترم Retry-After (سقف 60ث)
                    self._backoff(delay)
                    if self.cancel_check is not None and self.cancel_check():
                        raise ProviderError("أُوقف الطلب بزر الإيقاف.")
            if status == 400 and added_thinking:  # النموذج لا يقبل حقل التفكير: نعيد مرة واحدة بدونه ونتذكر ذلك
                del gen_cfg["thinkingConfig"]
                added_thinking = False
                if b"thinking" in raw.lower():
                    self._thinking_rejected = True   # الخطأ يذكر الحقل صراحةً
                else:
                    retried_unknown_400 = True       # 400 لسبب آخر (مثل طول المطالبة): لا نعطّل الحقل إلا إن نجحت الإعادة بدونه
                if self.cancel_check is not None and self.cancel_check():
                    raise ProviderError("أُوقف الطلب بزر الإيقاف.")
                continue
            break
        if retried_unknown_400 and 200 <= status < 300:
            self._thinking_rejected = True
        if status != 200:
            raise self._status_error(status, raw)
        try:
            data = json.loads(raw.decode("utf-8"))
            tin, tout = self._usage(data)
        except Exception:
            raise ProviderError(f"رد غير مفهوم من خدمة {self.PROVIDER_LABEL}.") from None
        self.last_usage = (tin, tout)
        self.usage.add(self.model, tin, tout)  # الفوترة تحدث حتى لو فشل تحليل النص لاحقاً
        try:
            text, _, _ = self._parse(data)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(f"رد غير مفهوم من خدمة {self.PROVIDER_LABEL}.") from None
        return text
