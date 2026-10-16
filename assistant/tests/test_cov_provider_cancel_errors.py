"""تغطية مزوّد HTTP: زر الإيقاف أثناء إعادة المحاولة لا يعيد الإرسال ولا ينام، حدّ الإنفاق للنداءات المعاد إرسالها، رسائل أخطاء الشبكة لا تسرّب المفتاح،
Retry-After يُقيَّد، وردّ غير مفهوم يُبلَّغ دون انهيار ودون تسجيل كلفة خاطئة."""
import json
import pathlib
import socket
import ssl
import sys
import traceback
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.gemini_provider import GeminiProvider
    from assistant.models.anthropic_provider import AnthropicProvider
    from assistant.models.http_base import (ProviderError, describe_network_error, parse_retry_after, is_local_url, MAX_RETRY_AFTER_SEC)

    KEY = "AIza" + "SyFAKE-NOT-REAL-KEY-0123456789abcdef"

    class Net:
        def __init__(self, *responses):
            self.responses = list(responses)
            self.calls = 0

        def __call__(self, url, headers, body, timeout):
            self.calls += 1
            return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def raises(fn, fragment=None):
        try:
            fn()
        except ProviderError as exc:
            if fragment:
                assert fragment in str(exc), (fragment, str(exc))
            assert KEY not in str(exc)
            return exc
        raise AssertionError("كان يجب رفع ProviderError")

    ok = (200, json.dumps({"candidates": [{"content": {"parts": [{"text": "hi"}]}}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}}).encode())

    # 1. الإيقاف قبل إعادة المحاولة بعد 429/503: لا إعادة إرسال (لا نداء مدفوع ثانٍ) ولا نوم
    for status in (429, 503):
        net = Net((status, b"busy"), ok)
        slept = []
        p = GeminiProvider(api_key=KEY, transport=net, sleep=lambda s: slept.append(s), cancel_check=lambda: True, max_retries=3)
        raises(lambda: p.generate("x"), "الإيقاف")
        assert net.calls == 1 and slept == [] and p.usage.calls == 0, (net.calls, slept)

    # 2. الإيقاف أثناء الانتظار (بعد النوم) يمنع الإرسال التالي
    state = {"n": 0}

    def cancel_after_sleep():
        return state["n"] >= 1
    net = Net((503, b"busy"), ok)
    p = GeminiProvider(api_key=KEY, transport=net, sleep=lambda s: state.update(n=state["n"] + 1), cancel_check=cancel_after_sleep, max_retries=3)
    raises(lambda: p.generate("x"), "الإيقاف")
    assert net.calls == 1

    # 3. بدون إيقاف: يعيد المحاولة بنجاح، ويحترم Retry-After مقيّداً بالسقف
    delays = []
    net = Net((429, b"slow", {"Retry-After": "9999"}), ok)
    p = GeminiProvider(api_key=KEY, transport=net, sleep=lambda s: delays.append(s), max_retries=2)
    assert p.generate("x") == "hi" and net.calls == 2 and delays == [MAX_RETRY_AFTER_SEC], delays
    assert p.usage.calls == 1

    # 4. أخطاء دائمة تُترجم لرسائل واضحة بلا مفتاح
    for st, frag in ((401, "401"), (403, "403"), (404, "404"), (429, "429"), (500, "500"), (418, "418")):
        p = GeminiProvider(api_key=KEY, transport=Net((st, ("body " + KEY).encode())), sleep=lambda s: None, max_retries=0)
        e = raises(lambda: p.generate("x"), frag)
        assert getattr(e, "status", None) == st
        assert KEY not in getattr(e, "body_lc", "")

    # 5. ردّ 200 غير مفهوم: خطأ واضح، وردّ بلا مرشحين/فارغ لا ينهار
    for raw in (b"not json", b"[]", json.dumps({"candidates": []}).encode(), json.dumps({"candidates": [{"content": {"parts": []}}]}).encode()):
        p = GeminiProvider(api_key=KEY, transport=Net((200, raw)), sleep=lambda s: None, max_retries=0)
        raises(lambda: p.generate("x"))
    p = AnthropicProvider(api_key=KEY, transport=Net((200, b"\xff\xfe")), sleep=lambda s: None, max_retries=0)
    raises(lambda: p.generate("x"), "غير مفهوم")

    # 6. وصف أخطاء الشبكة حسب النوع (لا نص الاستثناء الذي قد يحوي الرابط/المفتاح)
    secret_url = "https://x/?key=" + KEY
    cases = [
        (urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED " + secret_url)), "شهادة"),
        (urllib.error.URLError(ssl.SSLError("handshake " + secret_url)), "الاتصال الآمن"),
        (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), "الإنترنت"),
        (urllib.error.URLError(socket.timeout("timed out")), "مهلة"),
        (TimeoutError("t"), "مهلة"),
        (urllib.error.URLError(ConnectionRefusedError("refused")), "رفض"),
        (ConnectionResetError("reset " + secret_url), "قُطع"),
        (BrokenPipeError("pipe"), "قُطع"),
        (urllib.error.URLError(ConnectionAbortedError("abort")), "قُطع"),
        (RuntimeError("weird " + secret_url), "تعذّر الاتصال بالخدمة"),
    ]
    for exc, frag in cases:
        msg = describe_network_error(exc)
        assert frag in msg and KEY not in msg and "http" not in msg, (frag, msg)

    # 7. Retry-After: فارغ/غير صالح/سالب/تاريخ ماضٍ
    assert parse_retry_after(None) is None and parse_retry_after("") is None and parse_retry_after("   ") is None
    assert parse_retry_after("abc") is None and parse_retry_after("nan") is None
    assert parse_retry_after("-5") == 0.0 and parse_retry_after("5") == 5.0 and parse_retry_after("1e9") == MAX_RETRY_AFTER_SEC
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0

    # 8. العنوان "المحلي" الصارم: لا صيغ ملتبسة تتجاوز البروكسي
    for u in ("http://localhost:11434", "http://127.0.0.1:1", "http://192.168.1.5/x", "http://[::1]:8080/", "http://[fe80::1%25eth0]/"):
        assert is_local_url(u), u
    for u in ("http://127.1/", "http://0x7f.0.0.1/", "http://2130706433/", "http://010.0.0.1/", "http://example.local/", "http://[::ffff:8.8.8.8]/",
              "http://8.8.8.8/", "http://[bad]/", "not a url", "", "http://localhost.evil.com/"):
        assert not is_local_url(u), u

    print("PASS: test_cov_provider_cancel_errors completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_provider_cancel_errors", file=sys.stderr)
    sys.exit(1)
