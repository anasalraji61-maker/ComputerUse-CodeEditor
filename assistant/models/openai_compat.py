"""مزوّد متوافق مع OpenAI (POST {base_url}/chat/completions): OpenAI نفسه، وOllama المحلي، وأي خادم يتبع الواجهة نفسها (LM Studio، vLLM…).

- Ollama بلا مفتاح ومحلي: كلفته صفر (price_per_mtok=(0,0)) ولا تغادر بياناتك جهازك. المفتاح اختياري ويُرسل في Authorization فقط إن وُجد.
- وضع JSON: response_format={"type":"json_object"}؛ إن رفضه الخادم (400) نعيد بدونه ونفرض JSON بنص التعليمات. ردّ ليس JSON صالحاً يُعاد مرة واحدة بتذكير.
- أخطاء عربية واضحة: الخادم المحلي لا يعمل، النموذج غير منزّل (مع قائمة المتاح إن أمكن)، المفتاح مرفوض…
- شبكة بـ urllib فقط (بلا shell)."""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from .http_base import (HttpProvider, ProviderConfigError, ProviderError, is_local_url, is_loopback_url, is_blocked_host, opener_for,  # noqa: F401
                        default_get, _DOTTED_QUAD, _TLS, GET_TOTAL_TIMEOUT, MAX_RESPONSE_BYTES)
from .pricing import LOCAL_PRICE

OPENAI_BASE_URL = "https://api.openai.com/v1"
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"

_JSON_ENFORCE = ("\n\nأعد كائن JSON صالحاً فقط، بلا أي شرح قبله أو بعده وبلا أسوار markdown.")
_JSON_REMINDER = ("\n\n(تنبيه: ردّك السابق لم يكن JSON صالحاً. أعد كائن JSON صالحاً فقط، بلا شرح ولا أسوار markdown.)")
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?(.*?)\n?```\s*$", re.S)

GetTransport = Callable[[str, Dict[str, str], float], Tuple[int, bytes]]


_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)*\.?$")
_NUMERICISH = re.compile(r"^(?:0x[0-9a-f]+|\d+)$")
_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,79}$")
MAX_MODELS_LISTED = 12
MAX_MODELS_SCANNED = 200


def normalize_base_url(base_url: str) -> str:
    """يتحقق من العنوان ويعيده موحّداً (مخطط ومضيف بأحرف صغيرة، بلا شرطة أخيرة). يرفع ProviderConfigError دائماً (لا ValueError) برسالة عربية:
    http/https فقط؛ بلا بيانات اعتماد مضمّنة؛ بلا فراغات/محارف تحكم/شرطة مائلة عكسية؛ بلا استعلام (?) أو جزء (#)؛ منفذ صالح 1..65535؛
    مضيف ASCII سليم (لا تلاعب unicode ولا صيغ رقمية inet_aton)؛ وجهات link-local وغير المحدد (0.0.0.0) مرفوضة."""
    raw = str(base_url or "")
    url = raw.strip()
    bad_msg = "عنوان الخادم غير صالح؛ يجب أن يبدأ بـ http:// أو https:// ويكون سطراً واحداً بلا مسافات."
    if not url or _BAD_CHARS.search(url):
        raise ProviderConfigError(bad_msg)
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ProviderConfigError("عنوان الخادم غير صالح: المنفذ أو صيغة العنوان خاطئة (مثال صحيح: http://127.0.0.1:11434/v1).") from None
    if parsed.scheme.lower() not in ("http", "https") or not host:
        raise ProviderConfigError(bad_msg)
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise ProviderConfigError("لا تضع اسم مستخدم أو كلمة مرور داخل عنوان الخادم؛ استعمل مفتاح الخدمة.")
    if parsed.query or parsed.fragment or "?" in url or "#" in url:
        raise ProviderConfigError("عنوان الخادم لا يقبل علامة ? أو #؛ اكتب العنوان مباشرة مثل http://127.0.0.1:11434/v1")
    if port is not None and not (1 <= port <= 65535):
        raise ProviderConfigError("منفذ عنوان الخادم يجب أن يكون بين 1 و65535.")
    host = host.lower()
    if ":" in host:    # IPv6 حرفي
        try:
            import ipaddress
            ipaddress.IPv6Address(host.split("%", 1)[0])
        except ValueError:
            raise ProviderConfigError(bad_msg) from None
    else:
        labels = host.rstrip(".").split(".")
        if not _HOST_RE.match(host) or any("%" in x for x in labels):
            raise ProviderConfigError("اسم الخادم في العنوان غير صالح (حروف إنجليزية وأرقام وشرطات ونقاط فقط).")
        if all(_NUMERICISH.match(x) for x in labels) and not _DOTTED_QUAD.match(host):
            raise ProviderConfigError("اكتب عنوان IP بصيغته الكاملة (أربعة أجزاء مثل 127.0.0.1) لا صيغة مختصرة.")
    if is_blocked_host(host):
        raise ProviderConfigError("هذا العنوان غير مسموح كوجهة للخدمة الذكية (عنوان محلي خاص بالشبكة أو غير محدد).")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return f"{parsed.scheme.lower()}://{netloc}{parsed.path}".rstrip("/")


def _default_get(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, bytes]:
    """GET /models بآلية النقل نفسها (مهلة إجمالية + الإيقاف)؛ تُعاد في http_base.default_get."""
    return default_get(url, headers, timeout)


MAX_REPLY_CHARS = 1_000_000
_THINK_OPEN = re.compile(r"<think>", re.I)
_THINK_CLOSE = re.compile(r"</think>", re.I)


def _strip_think(text: str) -> str:
    """يحذف كتل <think>…</think> بمسح خطي (لا تعابير نمطية متراجعة)؛ كتلة بلا إغلاق تُحذف من وسمها إلى النهاية."""
    out, pos = [], 0
    while True:
        m = _THINK_OPEN.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        c = _THINK_CLOSE.search(text, m.end())
        if not c:
            break
        pos = c.end()
    return "".join(out)


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```") and t.endswith("```") and len(t) >= 6:
        inner = t[3:-3]
        nl = inner.find("\n")
        if nl != -1 and nl <= 20 and re.fullmatch(r"[A-Za-z0-9_-]*\s*", inner[:nl]):
            inner = inner[nl + 1:]
        return inner.strip()
    return t


def _strip_reply(text: str) -> str:
    """ينظّف رد نموذج محلي: يقتطع الطول المفرط، بلا كتل <think> ولا أسوار ```json (كله بمسح خطي)."""
    return _strip_fence(_strip_think((text or "")[:MAX_REPLY_CHARS]).strip())


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def _balanced_object(text: str, start: int) -> Optional[str]:
    """أول كائن {…} متوازن يبدأ عند start (مطابقة أقواس تراعي السلاسل النصية والهروب)، أو None."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json_object(text: str) -> Optional[str]:
    """JSON صالح من رد قد يحوي كلاماً حوله: الرد كله، أو أول كتلة ```json، أو أول كائن {…} متوازن صالح. غير ذلك None."""
    text = (text or "").strip()
    if not text:
        return None
    if _is_json(text):
        return text
    fence = re.search(r"```(?:json)?[ \t]*\n?", text, re.I)
    if fence:
        end = text.find("```", fence.end())
        if end != -1 and _is_json(text[fence.end():end].strip()):
            return text[fence.end():end].strip()
    pos = 0
    for _ in range(20):   # عدد محاولات محدود
        i = text.find("{", pos)
        if i == -1:
            break
        cand = _balanced_object(text, i)
        if cand and _is_json(cand):
            return cand
        pos = i + 1
    return None


class OpenAICompatProvider(HttpProvider):
    PROVIDER_LABEL = "OpenAI"
    ENV_KEYS = ("OPENAI_API_KEY",)

    def __init__(self, model: str, base_url: str = OPENAI_BASE_URL, local: Optional[bool] = None, ollama: bool = False,
                 key_required: Optional[bool] = None, json_mode: bool = True, get_transport: Optional[GetTransport] = None,
                 **kw):
        self.base_url = normalize_base_url(base_url)
        loopback = is_loopback_url(self.base_url)
        # «محلي» (سعر صفري، مفتاح اختياري، مهلة طويلة) يلزم أن يكون العنوان loopback فعلاً: لا يكفي ادعاء local=True لخادم LAN/بعيد
        self.local = loopback and (True if local is None else bool(local))
        self.ollama = bool(ollama)
        self.key_required = (not self.local) if key_required is None else bool(key_required)
        if ollama:
            self.PROVIDER_LABEL = "Ollama"
        elif self.local:
            self.PROVIDER_LABEL = "الخادم المحلي"
        elif urllib.parse.urlparse(self.base_url).hostname not in ("api.openai.com",):
            self.PROVIDER_LABEL = urllib.parse.urlparse(self.base_url).hostname or "OpenAI"
        kw.setdefault("timeout", 180.0 if self.local else 60.0)   # أول استدعاء محلي يحمّل النموذج في الذاكرة: أبطأ
        super().__init__(model=model, **kw)
        if self.local:
            self.price_per_mtok = LOCAL_PRICE
        self.json_mode = bool(json_mode)
        self._json_rejected = False
        self._completion_style = False   # رفض الخادم max_tokens/temperature (نماذج o-series وgpt-5): max_completion_tokens بلا temperature
        self.max_attempts = 3            # أقصى نداءات قد تُنفَّذ لطلب واحد (إعادة أسلوب المعاملات + إعادة JSON): الحجز يضربها
        self._get = get_transport or _default_get

    # ---------- المفتاح اختياري محلياً ----------
    def _require_key(self) -> str:
        if not self._api_key:
            if self.key_required:
                raise ProviderConfigError(f"مفتاح {self.PROVIDER_LABEL} غير موجود. افتح «الإعدادات» والصق مفتاح الخدمة (يُحفظ لهذه الجلسة فقط)، أو عيّن متغير البيئة {self.ENV_KEYS[0]} ثم أعد التشغيل.")
            return ""
        return super()._require_key()

    @property
    def host_label(self) -> str:
        p = urllib.parse.urlparse(self.base_url)
        return f"{p.hostname}:{p.port}" if p.port else (p.hostname or "")

    def _build(self, prompt: str, system_prompt: str) -> Tuple[str, Dict[str, str], dict]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        json_on = self.json_mode and not self._json_rejected
        sys_text = system_prompt or ""
        if self.json_mode and not json_on:
            sys_text += _JSON_ENFORCE      # الخادم لا يدعم response_format: نفرض JSON بالتعليمات
        messages: List[dict] = []
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "messages": messages}
        if self._completion_style:
            payload["max_completion_tokens"] = self.max_tokens
        else:
            payload["temperature"] = 0
            payload["max_tokens"] = self.max_tokens
        if json_on:
            payload["response_format"] = {"type": "json_object"}
        return f"{self.base_url}/chat/completions", headers, payload

    def _usage(self, data: dict) -> Tuple[int, int]:
        u = data.get("usage") or {}
        return int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0)

    def _parse(self, data: dict) -> Tuple[str, int, int]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("لم تُرجِع الخدمة أي رد. أعد المحاولة أو صُغ الطلب بشكل مختلف.")
        msg = (choices[0] or {}).get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):   # أجزاء نصية
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = _strip_reply(content if isinstance(content, str) else "")
        if not text:
            raise ProviderError("جاء رد الخدمة فارغاً بلا أي نص. أعد المحاولة أو صُغ الطلب بشكل مختلف.")
        pt, ct = self._usage(data)
        return text, pt, ct

    # ---------- أخطاء عربية خاصة ----------
    def _available_models(self) -> List[str]:
        """أسماء النماذج المتاحة من GET {base}/models (يعمل على Ollama وOpenAI)؛ فشل => قائمة فارغة. أفضل جهد وبمهلة قصيرة."""
        headers = {"authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        if self.cancel_check is not None and self.cancel_check():
            return []
        _TLS.ctx = (self.cancel_check, time.monotonic() + min(self.total_timeout, GET_TOTAL_TIMEOUT))
        try:
            status, raw = self._get(f"{self.base_url}/models", headers, 4.0)
            if status != 200:
                return []
            data = json.loads(raw.decode("utf-8", "replace"))
            items = data.get("data") if isinstance(data, dict) else None
            out: List[str] = []
            for m in (items or [])[:MAX_MODELS_SCANNED]:
                mid = m.get("id") if isinstance(m, dict) else None
                # بيانات خادم غير موثوقة: تُعرض فقط أسماء آمنة الشكل ولا تحوي مفتاحاً بأي صيغة (يُسقَط الاسم كاملاً لا يُحجب جزئياً)
                if not isinstance(mid, str) or not MODEL_ID_RE.match(mid) or self._redact(mid) != mid:
                    continue
                out.append(mid)
                if len(out) >= MAX_MODELS_LISTED:
                    break
            return out
        except Exception:
            return []
        finally:
            _TLS.ctx = None

    def _status_error_msg(self, status: int, raw: bytes) -> ProviderError:
        if status == 404 and (self.ollama or self.local):
            have = self._available_models()
            tail = f" المتاح لديك: {'، '.join(have)}." if have else ""
            model = self._redact(str(self.model))
            if self.ollama:
                return ProviderError(f"النموذج {model} غير منزَّل بعد. أنزِله من برنامج Ollama ثم أعد المحاولة.{tail}")
            return ProviderError(f"النموذج {model} غير موجود على الخادم المحلي ({self.host_label}). غيّر اسم النموذج من «الإعدادات».{tail}")
        return super()._status_error_msg(status, raw)

    def _refused_error(self) -> ProviderError:
        if self.ollama:
            err = ProviderError("برنامج Ollama غير مفتوح على جهازك. افتحه من قائمة ابدأ ثم أعد المحاولة، أو غيّر الخدمة من «الإعدادات».")
        elif self.local:
            err = ProviderError(f"الخادم المحلي لا يعمل على {self.host_label}. شغّله ثم أعد المحاولة، أو غيّر الخدمة من «الإعدادات».")
        else:
            err = ProviderError(f"رفض الخادم {self.host_label} الاتصال؛ قد يكون الوصول محجوباً من شبكتك.")
        err.refused = True
        return err

    @staticmethod
    def _param_rejected(body_lc: str) -> bool:
        return "max_completion_tokens" in body_lc or "temperature" in body_lc

    def _call_once(self, prompt: str, system_prompt: str) -> str:
        """نداء منطقي واحد مع: ترجمة رفض الاتصال، وإعادة بلا response_format إن رفضه الخادم، وإعادة بأسلوب max_completion_tokens
        بلا temperature إن رفضها (400). كل تعديل يُتذكَّر. هذه الإعادات على 400 لا تُفوتر."""
        localhost_retry = False
        for _ in range(4):
            try:
                text = super().generate(prompt, system_prompt)
            except ConnectionRefusedError:
                exc = ProviderError("refused")
                exc.refused = True
                text, err = None, exc
            except ProviderError as exc:
                text, err = None, exc
            else:
                self._estimate_if_unreported(prompt, system_prompt, text)
                return text
            if err.status == 400:
                if self.json_mode and not self._json_rejected and "response_format" in err.body_lc:
                    self._json_rejected = True
                    continue
                if not self._completion_style and self._param_rejected(err.body_lc):
                    self._completion_style = True
                    continue
            if err.refused:
                host = urllib.parse.urlparse(self.base_url).hostname
                if self.ollama and host == "localhost" and not localhost_retry:   # ::1 مقابل 127.0.0.1
                    localhost_retry = True
                    self.base_url = self.base_url.replace("//localhost", "//127.0.0.1", 1)
                    continue
                raise self._refused_error() from None
            raise err
        raise ProviderError(f"رفضت خدمة {self.PROVIDER_LABEL} إعدادات الطلب بعد عدة محاولات؛ جرّب نموذجاً آخر من «الإعدادات».")

    def _estimate_if_unreported(self, prompt: str, system_prompt: str, text: str) -> None:
        """خادم غير محلي لم يُرجع usage: نقدّر بتحفظ (توكن لكل محرف) كي لا تُحسب الكلفة صفراً."""
        if self.local or self.last_usage != (0, 0):
            return
        tin, tout = len(prompt or "") + len(system_prompt or ""), len(text or "")
        self.last_usage = (tin, tout)
        self.usage.input_tokens += tin
        self.usage.output_tokens += tout
        from .pricing import cost_usd
        self.usage.usd += cost_usd(self.model, tin, tout, self.price_per_mtok)

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        self._require_key()
        acc = [0, 0]

        def take():   # يجمع استعمال كل نداء حتى لو فشل اللاحق (الفوترة لا تضيع)
            acc[0] += self.last_usage[0]
            acc[1] += self.last_usage[1]
        try:
            try:
                text = self._call_once(prompt, system_prompt)
            finally:
                take()
            if not self.json_mode:
                return text
            found = extract_json_object(text)
            if found is not None:
                return found
            if self.cancel_check is not None and self.cancel_check():
                raise ProviderError("أُوقف الطلب بزر الإيقاف.")
            try:
                text2 = self._call_once(prompt + _JSON_REMINDER, system_prompt)   # مرة واحدة فقط
            finally:
                take()
            return extract_json_object(text2) or text2
        finally:
            self.last_usage = (acc[0], acc[1])
