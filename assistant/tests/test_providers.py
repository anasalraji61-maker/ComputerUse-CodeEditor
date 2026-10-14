"""اختبارات مزودي Claude وGemini بمحاكاة الشبكة بالكامل (لا اتصال ولا مفاتيح حقيقية)."""
import json
import os
import sys
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.anthropic_provider import AnthropicProvider
    from assistant.models.gemini_provider import GeminiProvider
    from assistant.models.http_base import ProviderError, ProviderConfigError

    FAKE_KEY = "test-key-NOT-REAL-123456"

    class Net:
        """نقل وهمي يسجّل الطلبات ويعيد ردوداً مبرمجة بالتتابع."""
        def __init__(self, *responses):
            self.responses = list(responses)
            self.requests = []

        def __call__(self, url, headers, body, timeout):
            self.requests.append((url, dict(headers), json.loads(body.decode("utf-8")), timeout))
            return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def ok_claude(text="مرحبا", tin=1000, tout=500):
        return 200, json.dumps({"content": [{"type": "text", "text": text}], "usage": {"input_tokens": tin, "output_tokens": tout}}).encode()

    def ok_gemini(text="مرحبا", tin=800, tout=200):
        return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}],
                                "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout}}).encode()

    # ---- Claude: شكل الطلب والرد ----
    net = Net(ok_claude("نص الرد"))
    p = AnthropicProvider(model="claude-haiku-5-5", api_key=FAKE_KEY, transport=net)
    assert p.generate("سؤال", system_prompt="تعليمات") == "نص الرد"
    url, headers, body, _ = net.requests[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == FAKE_KEY and headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-haiku-5-5" and body["system"] == "تعليمات"
    assert body["messages"] == [{"role": "user", "content": "سؤال"}]
    assert p.last_usage == (1000, 500) and p.usage.calls == 1
    assert abs(p.usage.usd - (1000 * 0.10 + 500 * 0.50) / 1e6) < 1e-12

    # ---- Gemini: المفتاح في الترويسة لا في الرابط ----
    net = Net(ok_gemini("رد جيميني"))
    g = GeminiProvider(model="gemini-3.1-flash-lite", api_key=FAKE_KEY, transport=net)
    assert g.generate("سؤال", system_prompt="تعليمات") == "رد جيميني"
    url, headers, body, _ = net.requests[0]
    assert FAKE_KEY not in url and headers["x-goog-api-key"] == FAKE_KEY
    assert url.endswith("gemini-3.1-flash-lite:generateContent")
    assert body["systemInstruction"]["parts"][0]["text"] == "تعليمات"
    assert g.last_usage == (800, 200)

    # ---- غياب المفتاح: رسالة عربية واضحة بلا انهيار ولا شبكة ----
    saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")}
    try:
        net = Net(ok_claude())
        for prov, var in ((AnthropicProvider(transport=net), "ANTHROPIC_API_KEY"), (GeminiProvider(transport=net), "GEMINI_API_KEY")):
            try:
                prov.generate("x")
                raise AssertionError("كان يجب رفع ProviderConfigError")
            except ProviderConfigError as exc:
                assert var in str(exc) and "مفتاح" in str(exc)
        assert net.requests == []
        # المفتاح من البيئة
        os.environ["GOOGLE_API_KEY"] = FAKE_KEY
        net = Net(ok_gemini())
        GeminiProvider(transport=net).generate("x")
        assert net.requests[0][1]["x-goog-api-key"] == FAKE_KEY
    finally:
        for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    # ---- إعادة المحاولة عند 429 ثم نجاح، مع مهل نوم وهمية ----
    sleeps = []
    net = Net((429, b"slow down"), (529, b"overloaded"), ok_claude("بعد المحاولة"))
    p = AnthropicProvider(api_key=FAKE_KEY, transport=net, sleep=sleeps.append)
    assert p.generate("x") == "بعد المحاولة" and len(net.requests) == 3 and len(sleeps) == 2

    # ---- فشل دائم: يتوقف بعد المحاولات ولا يسرّب المفتاح ----
    net = Net((500, f"internal error key={FAKE_KEY}".encode()))
    p = AnthropicProvider(api_key=FAKE_KEY, transport=net, sleep=lambda s: None, max_retries=2)
    try:
        p.generate("x")
        raise AssertionError("كان يجب الفشل")
    except ProviderError as exc:
        assert "500" in str(exc) and FAKE_KEY not in str(exc)
    assert len(net.requests) == 3

    # ---- مفتاح مرفوض ----
    p = AnthropicProvider(api_key=FAKE_KEY, transport=Net((401, b"invalid x-api-key " + FAKE_KEY.encode())))
    try:
        p.generate("x")
        raise AssertionError
    except ProviderError as exc:
        assert "401" in str(exc) and FAKE_KEY not in str(exc)

    # ---- ردود تالفة أو محجوبة ----
    for prov in (AnthropicProvider(api_key=FAKE_KEY, transport=Net((200, b"not json"))),
                 AnthropicProvider(api_key=FAKE_KEY, transport=Net((200, b'{"content": []}'))),
                 GeminiProvider(api_key=FAKE_KEY, transport=Net((200, b'{"candidates": []}'))),
                 GeminiProvider(api_key=FAKE_KEY, transport=Net((200, b'{"candidates": [{"content": {"parts": []}}]}')))):
        try:
            prov.generate("x")
            raise AssertionError("كان يجب رفض الرد التالف")
        except ProviderError:
            pass

    print("PASS: test_providers completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_providers", file=sys.stderr)
    sys.exit(1)
