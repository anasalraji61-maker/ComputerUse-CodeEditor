"""أساس مشترك لمزودي النماذج الحقيقيين: نقل HTTP قابل للاستبدال، أخطاء عربية، عدّاد استهلاك، وحجب المفاتيح."""
import json
import os
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


def default_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # شبكة/مهلة
        raise ProviderError(f"تعذّر الاتصال بالخدمة: {type(exc).__name__}") from None


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
    ):
        self.model = model
        self._api_key = api_key or next((os.environ[k] for k in self.ENV_KEYS if os.environ.get(k)), "")
        self.transport = transport or default_transport
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self.usage = Usage()
        self.last_usage = (0, 0)

    def name(self) -> str:
        return f"{self.PROVIDER_LABEL}:{self.model}"

    def _require_key(self) -> str:
        if not self._api_key:
            raise ProviderConfigError(
                f"مفتاح {self.PROVIDER_LABEL} غير موجود. عيّن متغير البيئة {self.ENV_KEYS[0]} ثم أعد التشغيل."
            )
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

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        self._require_key()
        url, headers, payload = self._build(prompt, system_prompt)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, raw = 0, b""
        for attempt in range(self.max_retries + 1):
            status, raw = self.transport(url, headers, body, self.timeout)
            if status not in self.RETRY_STATUS:
                break
            if attempt < self.max_retries:
                self._sleep(1.5 * (attempt + 1))
        if status in (401, 403):
            raise ProviderError(f"رُفض المفتاح من خدمة {self.PROVIDER_LABEL} (رمز {status}). تحقق من المفتاح وصلاحياته.")
        if status != 200:
            snippet = self._redact(raw.decode("utf-8", "replace"))[:200]
            raise ProviderError(f"خطأ من خدمة {self.PROVIDER_LABEL} (رمز {status}): {snippet}")
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
