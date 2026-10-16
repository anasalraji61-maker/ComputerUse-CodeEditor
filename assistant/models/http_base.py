"""أساس مشترك لمزودي النماذج الحقيقيين: نقل HTTP قابل للاستبدال، أخطاء عربية، عدّاد استهلاك، وحجب المفاتيح."""
import base64
import email.utils
import html
import ipaddress
import json
import os
import re
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
    """فشل في استدعاء نموذج (شبكة / مصادقة / رد غير مفهوم). status: رمز HTTP إن وُجد؛ refused: رُفض الاتصال؛ body_lc: جسم الرد صغيراً (للتشخيص الداخلي فقط)."""
    status: Optional[int] = None
    refused: bool = False
    body_lc: str = ""


class ProviderConfigError(ProviderError):
    """إعداد ناقص (مثل غياب المفتاح)."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0

    def add(self, model: str, tin: int, tout: int, price=None):
        self.calls += 1
        self.input_tokens += tin
        self.output_tokens += tout
        self.usd += cost_usd(model, tin, tout, price)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """لا نتبع التحويلات أبداً: urllib ينسخ ترويسة المفتاح إلى المضيف الجديد."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)   # العناوين المحلية لا تمر عبر HTTP(S)_PROXY

MAX_RESPONSE_BYTES = 8 * 1024 * 1024   # سقف حجم رد واحد


_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_DOTTED_QUAD = re.compile(rf"^{_OCTET}(?:\.{_OCTET}){{3}}$")
_LOCAL_V4 = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")]
_LOCAL_V6 = [ipaddress.ip_network(n) for n in ("::1/128", "fc00::/7", "fe80::/10")]


def _host_of(base_url: str) -> str:
    try:
        return (urllib.parse.urlparse(str(base_url)).hostname or "")
    except ValueError:
        return ""


def _ip_of(host: str):
    """IPv4/IPv6 حرفي صارم أو None (IPv6 المُعيَّن من IPv4 ::ffff:a.b.c.d يُحلَّل كعنوان IPv4 نفسه)."""
    try:
        if _DOTTED_QUAD.match(host):
            return ipaddress.IPv4Address(host)
        if ":" in host:
            ip6 = ipaddress.IPv6Address(host.split("%", 1)[0])
            return ip6.ipv4_mapped or ip6
    except ValueError:
        return None
    return None


def is_loopback_url(base_url: str) -> bool:
    """جهازك نفسه فقط: الاسم الحرفي localhost أو IP حرفي صارم ضمن 127.0.0.0/8 أو ::1. وحدها تستحق السعر المجاني وسماحية «بلا مفتاح عبر http»."""
    host = _host_of(base_url)
    if host == "localhost":
        return True
    ip = _ip_of(host)
    return bool(ip is not None and ip.is_loopback)


def is_blocked_host(host: str) -> bool:
    """عناوين لا تصلح وجهةً للمزوّد أبداً: link-local (169.254.x.x / fe80::/10 — بيانات وصف السحابة)، غير المحدّد (0.0.0.0 / ::)، والبثّ المتعدد."""
    ip = _ip_of(host)
    return bool(ip is not None and (ip.is_link_local or ip.is_unspecified or ip.is_multicast))


def is_local_url(base_url: str) -> bool:
    """شبكة داخلية (لتجاوز البروكسي فقط، وليست «مجانية»): localhost أو IP حرفي صارم (رباعي منقّط بلا أصفار بادئة ولا صيغ inet_aton)
    ضمن loopback/RFC1918/link-local، أو IPv6 (::1، fc00::/7، fe80::/10). أسماء .local وغيرها ليست محلية.
    link-local مرفوض كوجهة للمزوّد (is_blocked_host). السعر المجاني وسماحية المفتاح عبر http للـ loopback فقط (is_loopback_url)."""
    try:
        host = (urllib.parse.urlparse(str(base_url)).hostname or "")
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        if _DOTTED_QUAD.match(host):
            ip = ipaddress.IPv4Address(host)
            return any(ip in n for n in _LOCAL_V4)
        if ":" in host:
            ip6 = ipaddress.IPv6Address(host.split("%", 1)[0])
            return any(ip6 in n for n in _LOCAL_V6)
    except ValueError:
        return False
    return False


def opener_for(url: str):
    return _LOCAL_OPENER if is_local_url(url) else _OPENER



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
            return "تعذّر الاتصال الآمن بالخدمة: شهادة الخادم غير موثوقة. قد يعترضه برنامج الحماية أو الشبكة؛ جرّب شبكة أخرى."
        if isinstance(e, ssl.SSLError):
            return "تعذّر الاتصال الآمن بالخدمة. قد يعترضه برنامج الحماية أو الشبكة؛ جرّب شبكة أخرى."
        if isinstance(e, socket.gaierror):
            return "تعذّر الوصول إلى الخدمة. تحقق من اتصال الإنترنت ثم أعد المحاولة."
        if isinstance(e, (socket.timeout, TimeoutError)):
            return "انتهت مهلة الاتصال بالخدمة؛ الشبكة بطيئة أو محجوبة. أعد المحاولة."
        if isinstance(e, ConnectionRefusedError):
            return "رفض الخادم الاتصال؛ قد يكون الوصول محجوباً من شبكتك."
        if isinstance(e, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return "قُطع الاتصال بالخدمة أثناء الطلب؛ أعد المحاولة."
    return "تعذّر الاتصال بالخدمة. تحقق من اتصال الإنترنت ثم أعد المحاولة."


def _stopped_error() -> ProviderError:
    return ProviderError("أُوقف الطلب بزر الإيقاف.")


def _do_request(url: str, headers: Dict[str, str], body: bytes, timeout: float, abort: threading.Event):
    """الطلب الفعلي (يعمل في خيط عامل): قراءة بأجزاء مع سقف حجم، وتوقف سريع عند abort. يعيد (حالة، جسم، Retry-After)."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    def read_all(resp) -> bytes:
        total = []
        size = 0
        try:
            declared = int(resp.headers.get("Content-Length", "0") or 0)
        except (ValueError, AttributeError):
            declared = 0
        if declared > MAX_RESPONSE_BYTES:
            raise ProviderError(f"رد الخدمة أكبر من الحد المسموح ({MAX_RESPONSE_BYTES // (1024 * 1024)}MB).")
        while True:
            if abort.is_set():
                raise _stopped_error()
            chunk = resp.read1(65536)
            if not chunk:
                return b"".join(total)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ProviderError(f"رد الخدمة أكبر من الحد المسموح ({MAX_RESPONSE_BYTES // (1024 * 1024)}MB).")
            total.append(chunk)

    try:
        with opener_for(url).open(req, timeout=timeout) as resp:
            return resp.status, read_all(resp), None
    except urllib.error.HTTPError as exc:
        try:
            ra = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
        except Exception:
            ra = None
        return exc.code, read_all(exc), ra


def default_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float) -> Tuple[int, bytes]:
    """POST عبر urllib في خيط عامل: الخيط الرئيسي يفحص زر الإيقاف والمهلة الإجمالية كل 0.2ث فيقطع حتى لو الخادم صامت أو يقطّر بايتاً بايتاً.
    العناوين المحلية لا تمر عبر البروكسي. سياق الإيقاف/المهلة يضعه HttpProvider.generate في _TLS.ctx."""
    cancel_check, deadline = getattr(_TLS, "ctx", None) or (None, None)
    _TLS.retry_after = None
    abort = threading.Event()
    box: Dict[str, object] = {}

    def work():
        try:
            box["res"] = _do_request(url, headers, body, timeout, abort)
        except BaseException as exc:   # noqa: BLE001
            box["exc"] = exc

    th = threading.Thread(target=work, daemon=True)
    th.start()
    while True:
        th.join(0.2)
        if not th.is_alive():
            break
        if cancel_check is not None and cancel_check():
            abort.set()
            raise _stopped_error()
        if deadline is not None and time.monotonic() > deadline:
            abort.set()
            raise ProviderError("انتهت المهلة الإجمالية للطلب؛ الخدمة بطيئة جداً أو تقطّر الرد. أعد المحاولة لاحقاً.")
    exc = box.get("exc")
    if exc is not None:
        if isinstance(exc, ProviderError):
            raise exc
        err = ProviderError(describe_network_error(exc))   # شبكة/مهلة
        err.refused = isinstance(exc, ConnectionRefusedError) or isinstance(getattr(exc, "reason", None), ConnectionRefusedError)
        raise err from None
    status, raw, ra = box["res"]
    _TLS.retry_after = ra
    return status, raw


MAX_GET_BYTES = 1_000_000
GET_TOTAL_TIMEOUT = 6.0


def _do_get(url: str, headers: Dict[str, str], timeout: float, abort: threading.Event):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener_for(url).open(req, timeout=timeout) as resp:
            total, size = [], 0
            while True:
                if abort.is_set():
                    raise _stopped_error()
                chunk = resp.read1(65536)
                if not chunk:
                    return resp.status, b"".join(total)
                size += len(chunk)
                total.append(chunk[: max(0, MAX_GET_BYTES - (size - len(chunk)))])
                if size >= MAX_GET_BYTES:
                    return resp.status, b"".join(total)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def default_get(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, bytes]:
    """GET بالآلية نفسها لـ POST: خيط عامل، مهلة إجمالية (تشمل القراءة) وفحص زر الإيقاف كل 0.2ث، وحجم محدود، بلا تحويلات.
    السياق (الإيقاف، الموعد النهائي) من _TLS.ctx. أي فشل => (0, b"")؛ الإيقاف يرفع ProviderError."""
    cancel_check, deadline = getattr(_TLS, "ctx", None) or (None, None)
    if deadline is None:
        deadline = time.monotonic() + GET_TOTAL_TIMEOUT
    abort = threading.Event()
    box: Dict[str, object] = {}

    def work():
        try:
            box["res"] = _do_get(url, headers, timeout, abort)
        except BaseException as exc:   # noqa: BLE001
            box["exc"] = exc

    th = threading.Thread(target=work, daemon=True)
    th.start()
    while True:
        th.join(0.2)
        if not th.is_alive():
            break
        if cancel_check is not None and cancel_check():
            abort.set()
            raise _stopped_error()
        if time.monotonic() > deadline:
            abort.set()
            return 0, b""
    exc = box.get("exc")
    if isinstance(exc, ProviderError):
        raise exc
    if exc is not None:
        return 0, b""
    return box["res"]


_REAL_SLEEP = time.sleep
_AUTO = object()
_OFF_WORDS = ("off", "none", "omit", "default", "null")


class HttpProvider(ModelProvider):
    """يطبّق إعادة المحاولة المحدودة وإخفاء المفتاح؛ الصنف الفرعي يبني الطلب ويحلل الرد."""

    PROVIDER_LABEL = "النموذج"
    price_per_mtok = None   # (إدخال، إخراج) لكل مليون توكن يتجاوز جدول الأسعار؛ المزوّد المحلي (0, 0)
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
        total_timeout: Optional[float] = None,
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
        # المهلة الإجمالية لنداء نقل واحد (تشمل قراءة الرد): الافتراضي 3 أضعاف مهلة المقبس؛ يمنع خادماً يقطّر الرد من تعليقنا
        self.total_timeout = float(total_timeout) if total_timeout else 3.0 * float(timeout)
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
        err = self._status_error_msg(status, raw)
        err.status = status
        err.body_lc = self._redact(raw.decode("utf-8", "replace")).lower()[:2000]
        return err

    def _status_error_msg(self, status: int, raw: bytes) -> ProviderError:
        """رسالة عربية مميّزة لكل فئة خطأ HTTP؛ لا تحوي المفتاح ولا الرابط."""
        label = self.PROVIDER_LABEL
        if status == 401:
            return ProviderError(f"لم تقبل خدمة {label} المفتاح (رمز 401): قد يكون خاطئاً أو منتهياً أو منسوخاً ناقصاً. أنشئ مفتاحاً جديداً والصقه في «الإعدادات».")
        if status == 403:
            return ProviderError(f"رفضت خدمة {label} الطلب (رمز 403): صلاحيات المفتاح لا تسمح به، أو الفوترة غير مفعّلة، أو الخدمة غير متاحة في بلدك. "
                                 f"إن كانت غير متاحة في بلدك فاختر «Ollama (محلي)» من «الإعدادات».")
        if status == 404:
            return ProviderError(f"النموذج '{self._redact(str(self.model))}' غير موجود أو غير متاح لحسابك لدى {label} (رمز 404). "
                                 f"غيّر اسم النموذج من «الإعدادات».")
        if status == 429:
            return ProviderError(f"وصلت إلى حد الاستعمال المسموح لدى {label} (رمز 429) حتى بعد إعادة المحاولة. انتظر دقائق أو راجع حصة حسابك.")
        snippet = self._redact(raw.decode("utf-8", "replace"))[:200]
        if status >= 500:
            return ProviderError(f"خدمة {label} مشغولة أو معطّلة مؤقتاً (رمز {status}) بعد إعادة المحاولة؛ جرّب لاحقاً.")
        return ProviderError(f"تعذّر إتمام الطلب لدى خدمة {label} (رمز {status}). {snippet}".strip())

    def _require_key(self) -> str:
        if not self._api_key:
            raise ProviderConfigError(
                f"مفتاح {self.PROVIDER_LABEL} غير موجود. افتح «الإعدادات» والصق مفتاح الخدمة (يُحفظ لهذه الجلسة فقط)، أو عيّن متغير البيئة {self.ENV_KEYS[0]} ثم أعد التشغيل."
            )
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) > 126 for ch in self._api_key):
            raise ProviderConfigError(f"مفتاح {self.PROVIDER_LABEL} فيه مسافات أو رموز غير مقبولة. انسخه من جديد كما هو.")
        return self._api_key

    def _key_variants(self) -> list:
        """كل صيغ المفتاح المتوقع أن يعكسها خادم في رد: الخام، ترميز الرابط، هروب JSON (مفرد ومزدوج)، هروب HTML، base64."""
        from assistant.redact import key_exact_forms
        return key_exact_forms(self._api_key)

    def _redact(self, text: str) -> str:
        """يحجب المفتاح بكل صيغه (حساس لحالة الأحرف لا) وبصيغ محوّلة للمفاتيح الطويلة (معكوس/ست عشري/مقطّع/أجزاء >= 12 محرفاً)، ثم أي نمط
        مفتاح معروف الشكل: النص قد يكون من خادم يعكس الترويسة."""
        text = str(text)
        try:
            from assistant.redact import redact_key, redact
            if self._api_key:
                text = redact_key(text, self._api_key)
            text = redact(text)
        except Exception:
            pass
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
                _TLS.ctx = (self.cancel_check, time.monotonic() + self.total_timeout)
                try:
                    res = self.transport(url, headers, body, self.timeout)
                finally:
                    _TLS.ctx = None
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
        self.usage.add(self.model, tin, tout, self.price_per_mtok)  # الفوترة تحدث حتى لو فشل تحليل النص لاحقاً
        try:
            text, _, _ = self._parse(data)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(f"رد غير مفهوم من خدمة {self.PROVIDER_LABEL}.") from None
        return text
