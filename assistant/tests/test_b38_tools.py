"""B38: مزوّد OpenAI-compatible / Ollama بنقل وهمي (بلا شبكة)."""
import json
import os
import pathlib
import sys
import threading
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from unittest import mock
    from assistant.models.openai_compat import OpenAICompatProvider, is_local_url, normalize_base_url, OLLAMA_BASE_URL, OPENAI_BASE_URL
    from assistant.models.http_base import ProviderError, ProviderConfigError
    from assistant.models.factory import build_provider
    from assistant.models.router import ModelRouter, estimate_max_cost
    from assistant.models.rules import RuleFirstProvider
    from assistant.models.pricing import cost_usd, LOCAL_PRICE
    from assistant.usage import UsageMeter
    import tempfile

    KEY = "sk" + "-test-NOT-REAL-1234567890abcdef"

    def chat(text, pt=11, ct=7, finish="stop"):
        return 200, json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}],
                                "usage": {"prompt_tokens": pt, "completion_tokens": ct}}).encode()

    class Net:
        def __init__(self, *responses):
            self.responses, self.requests = list(responses), []

        def __call__(self, url, headers, body, timeout):
            self.requests.append((url, dict(headers), json.loads(body.decode("utf-8")), timeout))
            r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
            if isinstance(r, BaseException):
                raise r
            return r

    def ollama(net, model="qwen2.5:7b", **kw):
        return OpenAICompatProvider(model=model, base_url=OLLAMA_BASE_URL, ollama=True, local=True, transport=net, sleep=lambda s: None, **kw)

    # ---- نجاح + شكل الطلب + محاسبة الاستعمال + كلفة صفر محلياً ----
    net = Net(chat('{"steps": []}', pt=100, ct=40))
    p = ollama(net)
    assert p.generate("سؤال", system_prompt="تعليمات") == '{"steps": []}'
    url, headers, body, timeout = net.requests[0]
    assert url == "http://127.0.0.1:11434/v1/chat/completions" and timeout >= 120
    assert body["model"] == "qwen2.5:7b" and body["temperature"] == 0 and body["max_tokens"] == p.max_tokens
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "system", "content": "تعليمات"}, {"role": "user", "content": "سؤال"}]
    assert p.last_usage == (100, 40) and p.usage.input_tokens == 100 and p.usage.output_tokens == 40
    assert p.usage.usd == 0.0 and p.price_per_mtok == LOCAL_PRICE
    assert cost_usd("qwen2.5:7b", 1_000_000, 1_000_000) > 0 and cost_usd("qwen2.5:7b", 10 ** 6, 10 ** 6, LOCAL_PRICE) == 0.0

    # ---- بلا مفتاح: لا ترويسة Authorization، ولا يُطلب مفتاح ----
    assert "authorization" not in headers and p._api_key == ""
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": KEY}):
        net = Net(chat("{}"))
        p = ollama(net, api_key="")
        p.generate("x")
        assert "authorization" not in net.requests[0][1]
        prov, note = build_provider({"COS_PROVIDER": "ollama"})                  # Ollama لا يستعمل مفتاح OpenAI المحيط
        assert prov.fallback.providers["small"]._api_key == ""

    # ---- مفتاح مُعطى: يُرسل في الترويسة فقط، ولا يظهر في الأخطاء ----
    net = Net(chat("{}"))
    p = OpenAICompatProvider(model="gpt-4o-mini", base_url=OPENAI_BASE_URL, api_key=KEY, transport=net, sleep=lambda s: None)
    p.generate("x")
    assert net.requests[0][1]["authorization"] == f"Bearer {KEY}" and KEY not in json.dumps(net.requests[0][2])
    assert net.requests[0][3] == 60.0
    for status in (401, 403, 404, 429, 500):
        pe = OpenAICompatProvider(model="gpt-4o-mini", base_url=OPENAI_BASE_URL, api_key=KEY, max_retries=0, sleep=lambda s: None,
                                  transport=Net((status, b"echo " + KEY.encode() + b" bad")))
        try:
            pe.generate("x")
            raise AssertionError
        except ProviderError as exc:
            assert KEY not in str(exc) and KEY not in exc.body_lc and exc.status == status, (status, str(exc))
    # السحابي يتطلب مفتاحاً
    try:
        OpenAICompatProvider(model="m", base_url=OPENAI_BASE_URL, api_key="").generate("x")
        raise AssertionError
    except ProviderConfigError as exc:
        assert "OPENAI_API_KEY" in str(exc)

    # ---- JSON مشوّه: إعادة مرة واحدة بتذكير، والاستعمال يُجمع ----
    net = Net(chat("هذا ليس json", pt=10, ct=5), chat('{"ok": 1}', pt=12, ct=6))
    p = ollama(net)
    assert p.generate("طلب") == '{"ok": 1}' and len(net.requests) == 2
    assert "JSON" in net.requests[1][2]["messages"][-1]["content"] and net.requests[1][2]["messages"][-1]["content"].startswith("طلب")
    assert p.last_usage == (22, 11) and p.usage.calls == 2
    net = Net(chat("ليس json أبداً"))
    p = ollama(net)
    assert p.generate("x") == "ليس json أبداً" and len(net.requests) == 2          # مرة واحدة فقط، ثم يترك المخطِّط يحكم
    # أسوار markdown وكتل التفكير تُنظَّف
    net = Net(chat('<think>hmm</think>\n```json\n{"a": 1}\n```'))
    assert ollama(net).generate("x") == '{"a": 1}' and len(net.requests) == 1

    # ---- الخادم لا يدعم response_format: إعادة بلا الحقل مع فرض JSON بالتعليمات ----
    net = Net((400, b'{"error":{"message":"Unsupported parameter: response_format"}}'), chat('{"a": 2}'))
    p = ollama(net)
    assert p.generate("x", system_prompt="sys") == '{"a": 2}' and len(net.requests) == 2
    assert "response_format" in net.requests[0][2] and "response_format" not in net.requests[1][2]
    assert "JSON" in net.requests[1][2]["messages"][0]["content"] and net.requests[1][2]["messages"][0]["content"].startswith("sys")
    p.generate("y")
    assert "response_format" not in net.requests[-1][2]                           # يُتذكَّر
    # 400 آخر لا يعطّل الوضع
    net = Net((400, b'{"error":"context length exceeded"}'))
    p = ollama(net)
    try:
        p.generate("x")
        raise AssertionError
    except ProviderError:
        assert not p._json_rejected and len(net.requests) == 1

    # ---- 404 نموذج غير منزّل (Ollama) مع قائمة المتاح ----
    gets = []

    def fake_get(url, headers, timeout):
        gets.append((url, dict(headers)))
        return 200, json.dumps({"data": [{"id": "llama3.2:3b"}, {"id": "qwen2.5:3b"}]}).encode()
    p = ollama(Net((404, b'{"error":{"message":"model \'qwen2.5:7b\' not found, try pulling it first"}}')), get_transport=fake_get, max_retries=0)
    try:
        p.generate("x")
        raise AssertionError
    except ProviderError as exc:
        m = str(exc)
        assert "qwen2.5:7b غير منزّل" in m and "ollama pull qwen2.5:7b" in m and "llama3.2:3b" in m, m
    assert gets[0][0] == "http://127.0.0.1:11434/v1/models"
    p = ollama(Net((404, b"{}")), get_transport=lambda *a: (0, b""), max_retries=0)       # القائمة غير متاحة: الرسالة الأساسية تكفي
    try:
        p.generate("x")
    except ProviderError as exc:
        assert "ollama pull qwen2.5:7b" in str(exc)

    # ---- رفض الاتصال ----
    for boom in (ConnectionRefusedError(111, "refused"),):
        p = ollama(Net(boom))
        try:
            p.generate("x")
            raise AssertionError
        except ProviderError as exc:
            assert "Ollama لا يعمل" in str(exc) and "COS_PROVIDER" in str(exc) and "127.0.0.1:11434" in str(exc)
    err = ProviderError("x")
    err.refused = True
    try:
        ollama(Net(err)).generate("x")
        raise AssertionError
    except ProviderError as exc:
        assert "Ollama لا يعمل" in str(exc)
    # عبر النقل الافتراضي الحقيقي (urllib) يُترجَم الرفض إلى الرسالة نفسها
    import urllib.error
    import urllib.request
    p = OpenAICompatProvider(model="m", base_url=OLLAMA_BASE_URL, ollama=True, local=True, sleep=lambda s: None)
    with mock.patch.object(urllib.request.OpenerDirector, "open", side_effect=urllib.error.URLError(ConnectionRefusedError(111, "refused"))):
        try:
            p.generate("x")
            raise AssertionError
        except ProviderError as exc:
            assert "Ollama لا يعمل" in str(exc)

    # ---- Retry-After والإيقاف ----
    sleeps = []
    net = Net((429, b"slow", {"Retry-After": "4"}), chat("{}"))
    p = OpenAICompatProvider(model="m", base_url=OLLAMA_BASE_URL, ollama=True, local=True, transport=net, sleep=sleeps.append)
    p.generate("x")
    assert sleeps == [4.0], sleeps
    stop = {"v": False}
    threading.Timer(0.4, lambda: stop.update(v=True)).start()
    p = OpenAICompatProvider(model="m", base_url=OLLAMA_BASE_URL, ollama=True, local=True, cancel_check=lambda: stop["v"],
                             transport=Net((503, b"busy", {"Retry-After": "30"})), max_retries=2)
    t0 = time.time()
    try:
        p.generate("x")
        raise AssertionError
    except ProviderError as exc:
        assert "الإيقاف" in str(exc)
    assert time.time() - t0 < 3

    # ---- ردود تالفة ----
    for bad in ((200, b"not json"), (200, b'{"choices": []}'), (200, b'{"choices":[{"message":{"content":""}}]}')):
        try:
            ollama(Net(bad)).generate("x")
            raise AssertionError(bad)
        except ProviderError:
            pass

    # ---- المصنع: المتغيرات والافتراضات والخصوصية ----
    prov, note = build_provider({"COS_PROVIDER": "ollama"})
    assert isinstance(prov, RuleFirstProvider) and isinstance(prov.fallback, ModelRouter)
    s = prov.fallback.providers["small"]
    assert s.model == "qwen2.5:7b" and s.base_url == "http://127.0.0.1:11434/v1" and s.price_per_mtok == LOCAL_PRICE
    assert "ollama pull" in note and "لا تغادر" in note and "تنبيه خصوصية" not in note
    prov, note = build_provider({"COS_PROVIDER": "ollama", "COS_SMALL_MODEL": "llama3.2:3b", "COS_LARGE_MODEL": "qwen2.5:14b",
                                 "COS_OPENAI_BASE_URL": "http://localhost:11500/v1/"})
    assert prov.fallback.providers["small"].model == "llama3.2:3b" and prov.fallback.providers["large"].model == "qwen2.5:14b"
    assert prov.fallback.providers["small"].base_url == "http://localhost:11500/v1"
    prov, note = build_provider({"COS_PROVIDER": "openai", "OPENAI_API_KEY": KEY})
    s = prov.fallback.providers["small"]
    assert s.base_url == OPENAI_BASE_URL and s.model == "gpt-4o-mini" and s.price_per_mtok is None
    assert "تنبيه خصوصية" in note and "OpenAI" in note and KEY not in note
    prov, note = build_provider({"COS_PROVIDER": "openai_compat", "COS_OPENAI_BASE_URL": "https://llm.example.com/v1", "COS_SMALL_MODEL": "m1",
                                 "OPENAI_API_KEY": KEY})
    s = prov.fallback.providers["small"]
    assert s.price_per_mtok is None and "تنبيه خصوصية" in note and "llm.example.com" in note   # بعيد: مسعَّر بتحفظ لا مجاني
    prov, note = build_provider({"COS_PROVIDER": "openai-compat", "COS_OPENAI_BASE_URL": "http://192.168.1.20:1234/v1", "COS_SMALL_MODEL": "m1"})
    assert prov.fallback.providers["small"].price_per_mtok == LOCAL_PRICE and "تنبيه خصوصية" not in note
    for env in ({"COS_PROVIDER": "openai"}, {"COS_PROVIDER": "openai_compat"}, {"COS_PROVIDER": "openai_compat", "COS_OPENAI_BASE_URL": "http://h/v1"},
                {"COS_PROVIDER": "ollama", "COS_OPENAI_BASE_URL": "ftp://x"}, {"COS_PROVIDER": "ollama", "COS_OPENAI_BASE_URL": "http://u:p@h/v1"},
                {"COS_PROVIDER": "openai", "OPENAI_API_KEY": KEY, "COS_OPENAI_BASE_URL": "http://evil.example.com/v1"}):
        try:
            build_provider(env)
            raise AssertionError(env)
        except ProviderConfigError:
            pass
    prov, _ = build_provider({"COS_PROVIDER": "auto", "OPENAI_API_KEY": KEY})
    assert prov.fallback.providers["small"].model == "gpt-4o-mini"
    assert is_local_url("http://127.0.0.1:1/v1") and is_local_url("http://localhost/v1") and is_local_url("http://172.20.0.5/v1")
    assert not is_local_url("http://172.32.0.5/v1") and not is_local_url("https://api.openai.com/v1") and not is_local_url("http://8.8.8.8/v1")

    # ---- الكلفة صفر عبر الموجّه والعدّاد ----
    with tempfile.TemporaryDirectory() as tmp:
        meter = UsageMeter(pathlib.Path(tmp) / "u.json", daily_cap_usd=0.01)      # سقف صغير جداً: المحلي لا يُحجب ولا يُستهلك
        net = Net(chat('{"x": 1}', pt=5000, ct=3000))
        router = ModelRouter(ollama(net), usage_meter=meter, cache_size=0)
        assert estimate_max_cost(router.providers["small"], "x" * 1000, "") == 0.0
        assert router.generate("x") == '{"x": 1}'
        assert router.totals()["usd"] == 0.0 and router.totals()["input_tokens"] == 5000 and not meter.exceeded()
        cloud = OpenAICompatProvider(model="gpt-4o", base_url=OPENAI_BASE_URL, api_key=KEY, transport=Net(chat("{}")), sleep=lambda s: None)
        assert estimate_max_cost(cloud, "x", "") > 0
        try:
            ModelRouter(cloud, usage_meter=meter, cache_size=0).generate("x")        # السحابي المدفوع يُحجب بالسقف
            raise AssertionError
        except ProviderError:
            pass

    # ---- القياس والمستندات ----
    import bench.run_bench as rb
    est = rb.estimate_cost("ollama", 20)
    assert est["total"] == 0.0 and rb.estimate_cost("openai", 5)["total"] > 0
    bat = (ROOT / "run_bench.bat").read_bytes()
    assert all(c < 128 for c in bat) and bat.count(b"\r\n") == bat.count(b"\n") and b"3 = Ollama" in bat and b"--provider ollama" in bat
    guide = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    assert "استعمال نموذج محلي (Ollama)" in guide and "ollama pull" in guide
    assert "غير منفّذ بعد" not in [l for l in (ROOT / "docs" / "SPEC_PHASE1.md").read_text(encoding="utf-8").splitlines() if "models/" in l][0].split("OpenAI")[1][:60]

    # ================= جولة المراجعة 7 =================
    import http.server
    import socket
    import socketserver
    import urllib.request
    import assistant.models.http_base as hb
    from assistant.models import openai_compat as oc
    from assistant.models.openai_compat import extract_json_object, _strip_reply

    def mk(model="gpt-4o-mini", base=OPENAI_BASE_URL, net=None, **kw):
        kw.setdefault("sleep", lambda s: None)
        return OpenAICompatProvider(model=model, base_url=base, api_key=kw.pop("api_key", KEY), transport=net, **kw)

    # (1) سياسة المفتاح
    AMB = {"OPENAI_API_KEY": KEY}
    p_ = build_provider({"COS_PROVIDER": "openai", **AMB})[0].fallback.providers["small"]
    assert p_._api_key == KEY
    for env in ({"COS_PROVIDER": "openai", "COS_OPENAI_BASE_URL": "https://llm.example.com/v1", **AMB},
                {"COS_PROVIDER": "openai", "COS_OPENAI_BASE_URL": "http://localhost:1234/v1", **AMB}):
        try:
            build_provider(env)
            raise AssertionError(env)
        except ProviderConfigError as exc:
            assert "COS_OPENAI_API_KEY" in str(exc) and KEY not in str(exc)
    k2 = "sk" + "-explicit-0987654321abcdef"
    p_ = build_provider({"COS_PROVIDER": "openai", "COS_OPENAI_BASE_URL": "https://llm.example.com/v1", "COS_OPENAI_API_KEY": k2, **AMB})[0].fallback.providers["small"]
    assert p_._api_key == k2
    p_ = build_provider({"COS_PROVIDER": "openai_compat", "COS_OPENAI_BASE_URL": "https://llm.example.com/v1", "COS_SMALL_MODEL": "m", **AMB})[0].fallback.providers["small"]
    assert p_._api_key == "", "المفتاح المحيط لا يذهب إلى مضيف غير OpenAI"
    p_ = build_provider({"COS_PROVIDER": "openai_compat", "COS_OPENAI_BASE_URL": "http://127.0.0.1:1234/v1", "COS_SMALL_MODEL": "m", **AMB})[0].fallback.providers["small"]
    assert p_._api_key == ""
    try:
        build_provider({"COS_PROVIDER": "openai_compat", "COS_OPENAI_BASE_URL": "http://llm.example.com/v1", "COS_SMALL_MODEL": "m", "COS_OPENAI_API_KEY": k2})
        raise AssertionError
    except ProviderConfigError:
        pass

    # (3) ollama: المحلية من العنوان الفعلي
    try:
        build_provider({"COS_PROVIDER": "ollama", "COS_OPENAI_BASE_URL": "https://api.openai.com/v1", "COS_SMALL_MODEL": "gpt-4o"})
        raise AssertionError
    except ProviderConfigError as exc:
        assert "COS_ALLOW_REMOTE_OLLAMA" in str(exc)
    prov, note = build_provider({"COS_PROVIDER": "ollama", "COS_OPENAI_BASE_URL": "https://gpu.example.com/v1", "COS_ALLOW_REMOTE_OLLAMA": "1"})
    rp = prov.fallback.providers["small"]
    assert rp.local is False and rp.price_per_mtok is None and "تنبيه خصوصية" in note and rp._api_key == ""
    assert estimate_max_cost(rp, "x", "") > 0

    # (8) is_local_url صارم
    for yes in ("http://localhost:11434/v1", "http://127.0.0.1/v1", "http://127.1.2.3/v1", "http://10.1.2.3/v1", "http://172.16.0.1/v1",
                "http://172.31.255.255/v1", "http://192.168.0.9/v1", "http://169.254.10.10/v1", "http://[::1]:11434/v1", "http://[fd12::1]/v1"):
        assert is_local_url(yes), yes
    for no in ("http://010.0.0.1/v1", "http://172.016.0.1/v1", "http://0x7f.1/v1", "http://2130706433/v1", "http://127.1/v1", "http://0177.0.0.1/v1",
               "http://printer.local/v1", "http://localhost./v1", "http://LOCALHOST.evil.com/v1", "http://172.32.0.1/v1", "http://172.15.0.1/v1",
               "http://8.8.8.8/v1", "http://[2001:db8::1]/v1", "https://api.openai.com/v1", "not a url", ""):
        assert not is_local_url(no), no

    # (2) البروكسي لا يُطبَّق على العناوين المحلية
    assert not [h for h in hb._LOCAL_OPENER.handlers if isinstance(h, urllib.request.ProxyHandler)]   # ProxyHandler({}) لا يسجّل شيئاً: اتصال مباشر
    assert hb.opener_for("http://127.0.0.1:11434/v1") is hb._LOCAL_OPENER and hb.opener_for("https://api.openai.com/v1") is hb._OPENER
    # localhost -> 127.0.0.1 (Ollama فقط)
    seen = []

    def tr_lh(url, h, b, t):
        seen.append(url)
        if "//localhost" in url:
            raise ConnectionRefusedError(111, "refused")
        return chat('{"a": 1}')
    p_ = OpenAICompatProvider(model="m", base_url="http://localhost:11434/v1", ollama=True, local=True, transport=tr_lh, sleep=lambda s: None)
    assert p_.generate("x") == '{"a": 1}' and seen[0].startswith("http://localhost") and seen[1].startswith("http://127.0.0.1") and p_.base_url.startswith("http://127.0.0.1")
    seen.clear()
    p_ = OpenAICompatProvider(model="m", base_url="http://localhost:11434/v1", local=True, transport=tr_lh, sleep=lambda s: None)   # ليس ollama: لا بديل
    try:
        p_.generate("x")
        raise AssertionError
    except ProviderError as exc:
        assert "لا يعمل" in str(exc) and len(seen) == 1

    # (4) الإيقاف قبل إعادة JSON
    flag = {"v": False}

    def tr_c(url, h, b, t):
        flag["v"] = True            # الإيقاف يُضغط أثناء النداء الأول
        return chat("ليس json", pt=9, ct=4)
    net_c = Net()
    net_c.responses = [chat("ليس json", pt=9, ct=4)]
    calls_c = []

    def tr_c2(*a):
        calls_c.append(1)
        flag["v"] = True
        return chat("ليس json", pt=9, ct=4)
    p_ = mk("m", OLLAMA_BASE_URL, tr_c2, api_key="", ollama=True, local=True, cancel_check=lambda: flag["v"])
    try:
        p_.generate("x")
        raise AssertionError
    except ProviderError as exc:
        assert "الإيقاف" in str(exc) and len(calls_c) == 1
    assert p_.last_usage == (9, 4)

    # (5) الاستعمال لا يضيع عند فشل إعادة JSON، ويصل إلى الموجّه
    net5 = Net(chat("ليس json", pt=100, ct=50), (500, b"boom"))
    p5 = mk("gpt-4o-mini", OPENAI_BASE_URL, net5, max_retries=0)
    router5 = ModelRouter(p5, cache_size=0)
    try:
        router5.generate("x")
        raise AssertionError
    except ProviderError:
        pass
    assert p5.last_usage == (100, 50), p5.last_usage
    assert router5.totals()["input_tokens"] == 100 and router5.totals()["usd"] > 0, router5.totals()

    # (6) الحجز يغطي أقصى النداءات
    cloud = mk("gpt-4o", OPENAI_BASE_URL, Net(chat("{}")))
    one = cost_usd("gpt-4o", len("x"), cloud.max_tokens)
    assert abs(estimate_max_cost(cloud, "x", "") - cloud.max_attempts * one) < 1e-9 and cloud.max_attempts == 3

    # (7) غياب usage عن خادم غير محلي: تقدير متحفظ لا صفر
    nu = Net((200, json.dumps({"choices": [{"message": {"content": '{"a": 1}'}}]}).encode()))
    p7 = mk("gpt-4o", OPENAI_BASE_URL, nu)
    p7.generate("س" * 1000, "y" * 500)
    assert p7.last_usage == (1500, 8) and p7.usage.usd > 0 and p7.usage.input_tokens == 1500
    p7l = OpenAICompatProvider(model="m", base_url=OLLAMA_BASE_URL, ollama=True, local=True, transport=Net((200, json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode())))
    p7l.generate("x")
    assert p7l.usage.usd == 0.0

    # (9) كتل التفكير: خطي وغير متراجع
    t0 = time.time()
    _strip_reply("<think>" * 200000)
    _strip_reply("```\n" + "``` \n" * 200000 + "x")
    _strip_reply(("<think>a</think>" * 50000))
    assert time.time() - t0 < 2.0, time.time() - t0
    assert _strip_reply('<think>تفكير {"x": 1}') == "" and _strip_reply('{"a":1}<think>غير مغلق') == '{"a":1}'
    assert _strip_reply('<THINK>q</Think>```json\n{"a": 1}\n```') == '{"a": 1}'
    assert len(_strip_reply("a" * 3_000_000)) == 1_000_000

    # (10) استخراج JSON
    assert extract_json_object('تفضل: {"a": "}", "b": [1, {"c": 2}]} شكراً') == '{"a": "}", "b": [1, {"c": 2}]}'
    assert extract_json_object('قبل ```json\n{"x": 1}\n``` بعد') == '{"x": 1}'
    assert extract_json_object('{ليس json} ثم {"ok": true}') == '{"ok": true}'
    assert extract_json_object("لا شيء هنا") is None and extract_json_object("") is None and extract_json_object('{"a": ') is None
    netj = Net(chat('إليك الخطة: {"steps": [{"id": 1}]} أتمنى أن تعجبك'))
    pj = ollama(netj)
    assert pj.generate("x") == '{"steps": [{"id": 1}]}' and len(netj.requests) == 1       # لا إعادة طلب مكلفة

    # (new) o-series: max_completion_tokens بلا temperature
    net_o = Net((400, b'{"error":{"message":"Unsupported parameter: \'max_tokens\' is not supported with this model. Use \'max_completion_tokens\' instead."}}'),
                chat('{"a": 1}'))
    po = mk("o3-mini", OPENAI_BASE_URL, net_o)
    assert po.generate("x") == '{"a": 1}' and len(net_o.requests) == 2
    r1, r2 = net_o.requests[0][2], net_o.requests[1][2]
    assert "max_tokens" in r1 and "temperature" in r1 and "max_completion_tokens" not in r1
    assert "max_completion_tokens" in r2 and "max_tokens" not in r2 and "temperature" not in r2
    po.generate("y")
    assert "temperature" not in net_o.requests[-1][2] and len(net_o.requests) == 3        # يُتذكَّر
    net_t = Net((400, b'{"error":{"message":"Unsupported value: \'temperature\' does not support 0 with this model."}}'), chat("{}"))
    mk("gpt-5", OPENAI_BASE_URL, net_t).generate("x")
    assert "temperature" not in net_t.requests[1][2] and "max_completion_tokens" in net_t.requests[1][2]

    # (11) سقف الحجم والمهلة الإجمالية والإيقاف على خادم حقيقي محلي
    class Slow(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a):
            pass
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            try:
                if self.path.startswith("/big/"):
                    body = b"x" * (hb.MAX_RESPONSE_BYTES + 1000)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path.startswith("/chunked-big/"):
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for _ in range(40):
                        c = b"y" * 4096
                        self.wfile.write(b"%x\r\n" % len(c) + c + b"\r\n")
                elif self.path.startswith("/trickle/"):
                    self.send_response(200)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    for _ in range(100):
                        time.sleep(0.25)
                        self.wfile.write(b" ")
                        self.wfile.flush()
                elif self.path.startswith("/silent/"):
                    time.sleep(6)
                else:
                    body = json.dumps({"choices": [{"message": {"content": '{"ok": 1}'}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    class TS(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
    srv = TS(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def real(path, **kw):
        return OpenAICompatProvider(model="m", base_url=f"http://127.0.0.1:{port}{path}", ollama=True, local=True, max_retries=0, **kw)
    with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}):
        assert real("/ok").generate("x") == '{"ok": 1}'                    # يصل رغم بروكسي ميت (محلي لا يمر به)
    with mock.patch.object(hb, "MAX_RESPONSE_BYTES", 100_000):
        for path in ("/big", "/chunked-big"):
            try:
                real(path).generate("x")
                raise AssertionError(path)
            except ProviderError as exc:
                assert "الحد المسموح" in str(exc), (path, str(exc))
    t0 = time.time()
    try:
        real("/trickle", total_timeout=1.2).generate("x")
        raise AssertionError
    except ProviderError as exc:
        assert "المهلة الإجمالية" in str(exc), str(exc)
    assert time.time() - t0 < 4, time.time() - t0
    for path in ("/trickle", "/silent"):
        stopf = {"v": False}
        threading.Timer(0.6, lambda f=stopf: f.update(v=True)).start()
        t0 = time.time()
        try:
            real(path, cancel_check=lambda f=stopf: f["v"]).generate("x")
            raise AssertionError
        except ProviderError as exc:
            assert "الإيقاف" in str(exc), str(exc)
        assert time.time() - t0 < 2.5, (path, time.time() - t0)
    srv.shutdown()

    print("PASS: test_b38_tools completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b38_tools", file=sys.stderr)
    sys.exit(1)
