"""مزوّد Claude عبر واجهة Messages. المفتاح من ANTHROPIC_API_KEY فقط؛ لا يُكتب في أي ملف."""
from typing import Dict, Tuple

from .http_base import HttpProvider, ProviderError

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-5-5"


class AnthropicProvider(HttpProvider):
    PROVIDER_LABEL = "Claude"
    ENV_KEYS = ("ANTHROPIC_API_KEY",)

    def __init__(self, model: str = DEFAULT_MODEL, **kw):
        super().__init__(model=model, **kw)

    def _build(self, prompt: str, system_prompt: str) -> Tuple[str, Dict[str, str], dict]:
        headers = {
            "content-type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
        }
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            payload["system"] = system_prompt
        return API_URL, headers, payload

    def _usage(self, data: dict) -> Tuple[int, int]:
        u = data.get("usage") or {}
        return int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))

    def _parse(self, data: dict) -> Tuple[str, int, int]:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError("لم تُرجِع الخدمة أي رد. أعد المحاولة أو صُغ الطلب بشكل مختلف.")
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        if not text:
            raise ProviderError("جاء رد الخدمة فارغاً بلا أي نص. أعد المحاولة أو صُغ الطلب بشكل مختلف.")
        u = data.get("usage") or {}
        return text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
