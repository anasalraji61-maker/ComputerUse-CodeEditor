"""تغطية حارس الإنفاق في الموجّه وفحص الاتصال: لا نداء مدفوع يُرسل إن لم يتّسع السقف (الحجز قبل الإرسال)، الحجز يُفك بعد كل نتيجة (نجاح/فشل)،
النداء الفاشل المفوتَر يُسجَّل في الكلفة، التقدير متحفظ لنموذج غير مسعَّر، والمحلي مجاني لا يُحجز."""
import datetime
import json
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.router import ModelRouter, estimate_max_cost
    from assistant.models.gemini_provider import GeminiProvider
    from assistant.models.openai_compat import OpenAICompatProvider
    from assistant.models.http_base import ProviderError
    from assistant.usage import UsageMeter
    from assistant.ui.settings import run_ping

    KEY = "AIza" + "S" * 35
    D = datetime.date(2026, 3, 10)

    class Net:
        def __init__(self, reply):
            self.reply, self.calls = reply, 0

        def __call__(self, url, headers, body, timeout):
            self.calls += 1
            return self.reply(self.calls)

    def gem_ok(text='{"ok": true}', tin=100, tout=20):
        return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}],
                                "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout}}).encode()

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()

        # 1. سقف صغير جداً: الحجز يُرفض والنداء المدفوع لا يخرج من الجهاز أبداً
        meter = UsageMeter(base / "u1.json", daily_cap_usd=0.0000001, today=lambda: D)
        net = Net(lambda n: gem_ok())
        prov = GeminiProvider(model="gemini-3.1-pro-preview", api_key=KEY, transport=net, sleep=lambda s: None)
        router = ModelRouter(prov, usage_meter=meter, cache_size=0)
        try:
            router.generate("اكتب خطة", "مخطط")
            raise AssertionError("كان يجب رفض الحجز")
        except ProviderError as exc:
            assert "سقف" in str(exc) or "الكلفة" in str(exc), str(exc)
        assert net.calls == 0 and meter.inflight_usd() == 0.0 and router.calls == []

        # 2. سقف كافٍ: ينجح، ويُفك الحجز، وتُسجَّل الكلفة
        meter2 = UsageMeter(base / "u2.json", daily_cap_usd=5.0, today=lambda: D)
        net = Net(lambda n: gem_ok('{"steps":[{"step_id":1,"action_type":"inspect","target":"."}]}'))
        router = ModelRouter(GeminiProvider(api_key=KEY, transport=net, sleep=lambda s: None), usage_meter=meter2, cache_size=2)
        out = router.generate("x", "مخطط")
        assert json.loads(out)["steps"] and meter2.inflight_usd() == 0.0
        assert router.generate("x", "مخطط") == out and router.cache_hits == 1 and net.calls == 1      # كاش: لا نداء ولا حجز
        for i in range(5):                                                                          # تجاوز حجم الكاش يطرد الأقدم
            router.generate(f"x{i}", "مخطط")
        assert len(router._cache) <= 2

        # 3. نداء فاشل لكنه مفوتَر (رد فارغ استهلك توكنات): يُسجَّل في الكلفة ويُفك الحجز ويُرفع الخطأ
        meter3 = UsageMeter(base / "u3.json", daily_cap_usd=5.0, today=lambda: D)
        prov3 = GeminiProvider(api_key=KEY, transport=Net(lambda n: gem_ok("", 500, 300)), sleep=lambda s: None)
        router3 = ModelRouter(prov3, usage_meter=meter3, cache_size=0)
        try:
            router3.generate("x", "")
            raise AssertionError("كان يجب أن يفشل الرد الفارغ")
        except ProviderError:
            pass
        assert meter3.inflight_usd() == 0.0
        failed = [c for c in router3.calls if c.get("failed")]
        assert failed and failed[0]["input"] == 500 and failed[0]["usd"] > 0, router3.calls

        # 4. تقدير الحد الأقصى: نموذج غير مسعَّر يُقدَّر بأعلى سعر معروف (لا صفر)، ومحلي مجاني صفر، ومزوّد بلا نموذج صفر، وعطل headroom لا يكسر
        unpriced = GeminiProvider(model="gemini-9-unknown-model", api_key=KEY, transport=Net(lambda n: gem_ok()))
        assert estimate_max_cost(unpriced, "p" * 1000, "s" * 1000) > 0
        local = OpenAICompatProvider(model="llama3", base_url="http://127.0.0.1:11434/v1", local=True, ollama=True, transport=Net(lambda n: gem_ok()))
        assert estimate_max_cost(local, "p", "s") == 0.0

        class NoModel:
            pass
        assert estimate_max_cost(NoModel(), "p", "s") == 0.0
        broken = GeminiProvider(api_key=KEY, transport=Net(lambda n: gem_ok()))
        broken.thinking_headroom = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        assert estimate_max_cost(broken, "p", "s") > 0

        # 5. فحص الاتصال: يحترم السقف (لا نداء)، ويُفك الحجز بعد الفشل، وينجح ويسجّل الكلفة، ولا يسرّب المفتاح في الرسالة
        meter5 = UsageMeter(base / "u5.json", daily_cap_usd=0.0000001, today=lambda: D)
        net5 = Net(lambda n: gem_ok())
        res = run_ping(GeminiProvider(model="gemini-3.1-pro-preview", api_key=KEY, transport=net5), meter5, None, [KEY])
        assert res["ok"] is False and net5.calls == 0 and KEY not in res["text"], res
        meter6 = UsageMeter(base / "u6.json", daily_cap_usd=5.0, today=lambda: D)
        net6 = Net(lambda n: (401, ("bad key " + KEY).encode()))
        res = run_ping(GeminiProvider(api_key=KEY, transport=net6, sleep=lambda s: None, max_retries=0), meter6, None, [KEY])
        assert res["ok"] is False and KEY not in res["text"] and meter6.inflight_usd() == 0.0, res
        net7 = Net(lambda n: gem_ok('{"ok": true}', 50, 10))
        res = run_ping(GeminiProvider(api_key=KEY, transport=net7), meter6, None, [KEY])
        assert res["ok"] is True and res["usd"] > 0 and meter6.spent_today() >= res["usd"] * 0.99, res
        assert run_ping(GeminiProvider(api_key="", transport=net7), meter6, None, [])["ok"] is False

    print("PASS: test_cov_router_spend_guard completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_router_spend_guard", file=sys.stderr)
    sys.exit(1)
