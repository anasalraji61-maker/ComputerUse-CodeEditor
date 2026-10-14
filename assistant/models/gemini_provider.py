"""مزوّد Gemini عبر generateContent. المفتاح من GEMINI_API_KEY أو GOOGLE_API_KEY، ويُرسل في ترويسة لا في الرابط."""
from typing import Dict, Tuple

from .http_base import HttpProvider, ProviderError

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"
DEFAULT_MODEL = "gemini-3.1-flash-lite"


class GeminiProvider(HttpProvider):
    PROVIDER_LABEL = "Gemini"
    ENV_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(self, model: str = DEFAULT_MODEL, **kw):
        super().__init__(model=model, **kw)

    def _build(self, prompt: str, system_prompt: str) -> Tuple[str, Dict[str, str], dict]:
        url = f"{API_BASE}{self.model}:generateContent"
        headers = {"content-type": "application/json", "x-goog-api-key": self._api_key}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": self.max_tokens, "temperature": 0},
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        return url, headers, payload

    def _usage(self, data: dict) -> Tuple[int, int]:
        u = data.get("usageMetadata") or {}
        # توكنات «التفكير» تُفوتر كإخراج ولا تظهر في candidatesTokenCount
        return int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0)) + int(u.get("thoughtsTokenCount", 0))

    def _parse(self, data: dict) -> Tuple[str, int, int]:
        cands = data.get("candidates")
        if not cands:
            raise ProviderError("رد Gemini بلا مرشحين (قد يكون محجوباً).")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if not text:
            raise ProviderError("رد Gemini فارغ.")
        u = data.get("usageMetadata") or {}
        return text, int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0))
