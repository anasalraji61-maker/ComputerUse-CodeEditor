"""B41 (security): عنوان الخادم، loopback وحده «محلي مجاني»، حجب المفتاح بكل صيغه، أسماء النماذج من خادم غير موثوق،
ومهلة/إيقاف جلب /models. مفاتيح وهمية فقط وخوادم 127.0.0.1."""
import json
import os
import pathlib
import socketserver
import http.server
import sys
import tempfile
import threading
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from unittest import mock
    import html
    import base64
    import urllib.parse
    from assistant.models import http_base as hb
    from assistant.models import openai_compat as oc
    from assistant.models.http_base import ProviderConfigError, ProviderError, is_loopback_url, is_local_url, is_blocked_host
    from assistant.models.openai_compat import OpenAICompatProvider, normalize_base_url, MODEL_ID_RE
    from assistant.models.factory import build_provider
    from assistant.models.pricing import LOCAL_PRICE

    KEY = "sk" + "-test-FAKE-" + "Zq8" * 10          # مفتاح وهمي
    ODD = 'sk' + '-a"b\\c<d&e\'' + "X" * 20               # مفتاح فيه محارف خاصة بـ JSON/HTML

    class Srv:
        """خادم محلي صغير: handler(h, body) يقرر الرد."""

        def __init__(self, fn):
            self.hits = []
            outer = self

            class H(http.server.BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def _go(self):
                    n = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(n) if n else b""
                    outer.hits.append((self.command, self.path))
                    fn(self, body)
                do_GET = do_POST = _go

            self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
            self.httpd.daemon_threads = True
            self.port = self.httpd.server_address[1]
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        def close(self):
            self.httpd.shutdown()
            self.httpd.server_close()

    def reply(h, code, body=b""):
        h.send_response(code)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    # ================= 1) normalize_base_url: ProviderConfigError دائماً، وتوحيد الشكل =================
    bad = ["", "   ", "file:///etc/passwd", "ftp://x/", "http://", "http:///v1", "//evil.com/v1", "http://[::1/v1", "http://127.0.0.1:99999/v1",
           "http://127.0.0.1:abc/v1", "http://127.0.0.1:0/v1", "http://127.0.0.1:1234/v1?x=1", "http://127.0.0.1:1234/v1#f", "http://127.0.0.1/v1?",
           "http://127.0.0.1 /v1", "http://127.0.0.1\r\nX-Evil: 1/v1", "http://127.0.0.1\t/v1", "http://127.0.0.1\\@evil.com/v1",
           "http://u:p@127.0.0.1/v1", "http://127.0.0.1@evil.com/v1", "http://169.254.169.254/latest", "http://[fe80::1]/v1", "http://0.0.0.0:11434/v1",
           "http://[::]/v1", "http://224.0.0.1/v1", "http://2852039166/v1", "http://0xA9FEA9FE/v1", "http://127.1/v1", "http://0x7f.0.0.1/v1",
           "http://127.0.0.1%00.evil.com/v1", "http://examıple.com/v1", "http://localhost。evil.com/v1", "http://a..b/v1", "http://-a.com/v1",
           "http://[::ffff:169.254.1.1]/v1", None]
    for u in bad:
        try:
            normalize_base_url(u)
            raise AssertionError(("accepted", u))
        except ProviderConfigError as exc:
            assert str(exc) and not any(ch.isascii() and ch.isalpha() and False for ch in str(exc))
        # لا استثناء آخر (ValueError...) يتسرب من أي مدخل: التأكد أن النوع ProviderConfigError لا غير
    assert normalize_base_url("HTTP://LocalHost:11434/v1/") == "http://localhost:11434/v1"
    assert normalize_base_url("  https://API.OpenAI.com/v1  ") == "https://api.openai.com/v1"
    assert normalize_base_url("http://[::1]:8080/v1") == "http://[::1]:8080/v1"
    assert normalize_base_url("http://192.168.1.5:8000/v1") == "http://192.168.1.5:8000/v1"
    assert normalize_base_url("http://127.0.0.1") == "http://127.0.0.1"

    # التشغيل لا يدور على COS_OPENAI_BASE_URL سيئ: الوضع المحلي مع رسالة عربية بدل استثناء
    from assistant import app as app_mod
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        env = {"COS_DATA_DIR": str(base / "data"), "COS_PROVIDER": "openai_compat", "COS_SMALL_MODEL": "m"}
        for badurl in ("http://127.0.0.1:99999/v1", "http://127.0.0.1:1234/v1?x=1", "http://[::1/v1", "http://127.0.0.1 /v1", "ftp://x"):
            env["COS_OPENAI_BASE_URL"] = badurl
            with mock.patch.dict(os.environ, env), mock.patch.object(app_mod, "default_paths", lambda: (base / "ws", base / "data")):
                ctl, notice = app_mod.build_controller()
            assert type(ctl.provider).__name__ == "RuleProvider" and "تنبيه" in notice and "عنوان الخادم" in notice, (badurl, notice)
            assert "Traceback" not in notice and "ValueError" not in notice
    for badurl in ("http://127.0.0.1:99999/v1", "http://x y/"):
        try:
            build_provider({"COS_PROVIDER": "ollama", "COS_OPENAI_BASE_URL": badurl})
            raise AssertionError
        except ProviderConfigError:
            pass

    # ================= 2) loopback وحده محلي مجاني؛ LAN وبعيد مسعَّر؛ المفتاح عبر http للـ loopback فقط =================
    for yes in ("http://127.0.0.1:1/v1", "http://localhost/v1", "http://[::1]:5/v1", "http://127.8.8.8/v1", "http://[::ffff:127.0.0.1]/v1"):
        assert is_loopback_url(yes), yes
    for no in ("http://192.168.1.5/v1", "http://10.0.0.1/v1", "http://169.254.1.1/v1", "http://localhost.evil.com/v1", "http://[fd00::1]/v1", "https://api.openai.com/v1"):
        assert not is_loopback_url(no), no
    assert is_local_url("http://192.168.1.5/v1") and is_blocked_host("169.254.169.254") and not is_blocked_host("127.0.0.1")
    p = OpenAICompatProvider(model="m", base_url="http://192.168.1.5:8000/v1", local=True, api_key="")      # ادعاء local=True لا يكفي
    assert p.local is False and p.price_per_mtok is None and p.key_required is True
    p = OpenAICompatProvider(model="m", base_url="http://127.0.0.1:8000/v1", api_key="")
    assert p.local is True and p.price_per_mtok == LOCAL_PRICE
    p = OpenAICompatProvider(model="m", base_url="http://127.0.0.1:8000/v1", local=False, api_key="")
    assert p.local is False
    # المصنع: مفتاح عبر http لغير loopback مرفوض (حتى LAN)، وعبر https أو loopback مقبول
    base_env = {"COS_PROVIDER": "openai_compat", "COS_SMALL_MODEL": "m", "COS_OPENAI_API_KEY": KEY}
    for u in ("http://192.168.1.5:8000/v1", "http://example.com/v1", "http://10.0.0.5/v1"):
        try:
            build_provider({**base_env, "COS_OPENAI_BASE_URL": u})
            raise AssertionError(u)
        except ProviderConfigError as exc:
            assert "https" in str(exc), exc
    prov, note = build_provider({**base_env, "COS_OPENAI_BASE_URL": "https://192.168.1.5:8000/v1"})
    sp = prov.fallback.providers["small"]
    assert sp._api_key == KEY and sp.local is False and sp.price_per_mtok is None and "تنبيه خصوصية" in note
    prov, note = build_provider({**base_env, "COS_OPENAI_BASE_URL": "http://127.0.0.1:8000/v1"})
    sp = prov.fallback.providers["small"]
    assert sp._api_key == KEY and sp.local is True and sp.price_per_mtok == LOCAL_PRICE
    # بلا مفتاح عبر http إلى LAN: مسموح لكنه مسعَّر (ليس مجانياً)
    prov, note = build_provider({"COS_PROVIDER": "openai_compat", "COS_SMALL_MODEL": "m", "COS_OPENAI_BASE_URL": "http://192.168.1.5:8000/v1"})
    assert prov.fallback.providers["small"].price_per_mtok is None
    # Ollama: على جهازك فقط افتراضياً؛ LAN يحتاج السماح الصريح ويصبح مسعَّراً
    try:
        build_provider({"COS_PROVIDER": "ollama", "COS_OPENAI_BASE_URL": "http://192.168.1.5:11434/v1"})
        raise AssertionError
    except ProviderConfigError as exc:
        assert "COS_ALLOW_REMOTE_OLLAMA" in str(exc)
    prov, _ = build_provider({"COS_PROVIDER": "ollama", "COS_OPENAI_BASE_URL": "http://192.168.1.5:11434/v1", "COS_ALLOW_REMOTE_OLLAMA": "1"})
    assert prov.fallback.providers["small"].local is False and prov.fallback.providers["small"].price_per_mtok is None
    # مفتاح COS_OPENAI_API_KEY لا يُعاد استعماله مع Ollama (محلي أو بعيد مسموح)
    for env in ({"COS_PROVIDER": "ollama", "COS_OPENAI_API_KEY": KEY},
                {"COS_PROVIDER": "ollama", "COS_OPENAI_API_KEY": KEY, "COS_OPENAI_BASE_URL": "https://gpu.example.com/v1", "COS_ALLOW_REMOTE_OLLAMA": "1"}):
        prov, _ = build_provider(env)
        assert prov.fallback.providers["small"]._api_key == "", env
    # OpenAI المحيط: المنفذ يُقارَن أيضاً (api.openai.com:444 ليس OpenAI)
    for u in ("https://api.openai.com:444/v1", "https://api.openai.com:8443/v1", "http://api.openai.com/v1"):
        try:
            build_provider({"COS_PROVIDER": "openai", "OPENAI_API_KEY": KEY, "COS_OPENAI_BASE_URL": u})
            raise AssertionError(u)
        except ProviderConfigError as exc:
            assert KEY not in str(exc)
    prov, _ = build_provider({"COS_PROVIDER": "openai", "OPENAI_API_KEY": KEY, "COS_OPENAI_BASE_URL": "https://api.openai.com:443/v1"})
    assert prov.fallback.providers["small"]._api_key == KEY

    # ================= 3) حجب المفتاح بكل صيغه + نمط المفاتيح على نصوص الخادم =================
    for key in (KEY, ODD):
        p = OpenAICompatProvider(model="m", base_url="http://127.0.0.1:1/v1", api_key=key)
        forms = {"raw": key, "upper": key.upper(), "lower": key.lower(), "json": json.dumps(key)[1:-1], "json-upper": json.dumps(key)[1:-1].upper(),
                 "html": html.escape(key), "html-nq": html.escape(key, quote=False), "html39": html.escape(key).replace("&#x27;", "&#39;"),
                 "url": urllib.parse.quote(key, safe=""), "urlplus": urllib.parse.quote_plus(key), "b64": base64.b64encode(key.encode()).decode(),
                 "json2": json.dumps(json.dumps(key)[1:-1])[1:-1]}
        for name, form in forms.items():
            out = p._redact(f"error: bad header [{form}] end")
            assert form not in out and "***" in out, (key, name, out)
        assert p._redact("نص عادي بلا مفتاح") == "نص عادي بلا مفتاح"
    # مفتاح آخر غير مضبوط لكنه بشكل مفتاح معروف يُحجب بالنمط (خادم يعكس مفتاحاً قديماً)
    other = "sk" + "-proj-" + "Q" * 40
    p = OpenAICompatProvider(model="m", base_url="http://127.0.0.1:1/v1", api_key=KEY)
    assert other not in p._redact(f"saw {other} here") and "AIza" + "Y" * 35 not in p._redact("k AIza" + "Y" * 35)
    p0 = OpenAICompatProvider(model="m", base_url="http://127.0.0.1:1/v1", api_key="")
    assert other not in p0._redact(f"saw {other}")

    # إنهاء إلى النهاية: خادم يعكس المفتاح بصيغ مختلفة في جسم الخطأ فلا يظهر في الاستثناء ولا body_lc ولا traceback
    def echo_server(variant):
        return Srv(lambda h, b: reply(h, 418, json.dumps({"error": f"bad auth {variant}"}).encode()))
    for key in (KEY, ODD):
        for variant in (key, key.upper(), json.dumps(key)[1:-1], html.escape(key), urllib.parse.quote(key, safe="")):
            srv = echo_server(variant)
            try:
                prov = OpenAICompatProvider(model="m", base_url=f"http://127.0.0.1:{srv.port}/v1", api_key=key, max_retries=0, sleep=lambda s: None)
                try:
                    prov.generate("x", "")
                    raise AssertionError
                except ProviderError as exc:
                    blob = str(exc) + "|" + exc.body_lc + "|" + repr(exc) + "|" + "".join(traceback.format_exception(exc))
                    for form in (key, key.upper(), key.lower(), json.dumps(key)[1:-1], html.escape(key), urllib.parse.quote(key, safe="")):
                        assert form.lower() not in blob.lower(), (key, variant, form)
            finally:
                srv.close()

    # ================= 4) أسماء النماذج من /models: آمنة الشكل، محجوبة، محدودة =================
    huge = [{"id": f"model-{i}"} for i in range(500)]
    ids = ([{"id": "llama3.2:3b"}, {"id": "Bearer " + KEY}, {"id": KEY}, {"id": "x" * 500}, {"id": "bad name"}, {"id": "a\nb"}, {"id": "q/ok-1@v2+x"},
            {"id": None}, {"id": 5}, "str", {"nope": 1}, {"id": "<script>"}, {"id": KEY.upper()}, {"id": "qwen2.5:7b"}, {"id": "‮evil"},
            {"id": "m-" + html.escape(ODD)}, {"id": "sk" + "-" + "A" * 30}] + huge)

    def models_handler(h, b):
        if h.path.endswith("/models"):
            reply(h, 200, json.dumps({"data": ids}).encode())
        else:
            reply(h, 404, b'{"error":"model not found"}')
    srv = Srv(models_handler)
    try:
        for key in (KEY, ""):
            prov = OpenAICompatProvider(model="nomodel", base_url=f"http://127.0.0.1:{srv.port}/v1", ollama=True, api_key=key, max_retries=0,
                                        sleep=lambda s: None)
            have = prov._available_models()
            assert 0 < len(have) <= oc.MAX_MODELS_LISTED, have
            assert have[0] == "llama3.2:3b" and "q/ok-1@v2+x" in have, have
            assert all(MODEL_ID_RE.match(m) and len(m) <= 80 for m in have), have
            joined = "|".join(have).lower()
            assert "sk" + "-" + "a" * 30 not in joined and "bearer" not in joined and "<" not in joined and "‮" not in joined
            if key:   # المزوّد يعرف مفتاحه فيُسقط كل صيغه؛ بلا مفتاح يعمل كاشف الأنماط فقط
                assert have[:2] == ["llama3.2:3b", "q/ok-1@v2+x"] and KEY.lower() not in joined, have
            try:
                prov.generate("x", "")
                raise AssertionError
            except ProviderError as exc:
                msg = str(exc)
                assert "غير منزَّل" in msg and (not key or KEY.lower() not in msg.lower()) and "Bearer" not in msg and "x" * 100 not in msg, msg
        # النموذج المطلوب نفسه يُحجب في الرسالة إن حوى المفتاح
        prov = OpenAICompatProvider(model=KEY, base_url=f"http://127.0.0.1:{srv.port}/v1", ollama=True, api_key=KEY, max_retries=0, sleep=lambda s: None)
        try:
            prov.generate("x", "")
            raise AssertionError
        except ProviderError as exc:
            assert KEY not in str(exc) and KEY not in exc.body_lc
    finally:
        srv.close()

    # ================= 5) جلب /models: مهلة إجمالية والإيقاف بآلية POST نفسها =================
    stop = threading.Event()

    def drip(h, b):
        if h.path.endswith("/models"):
            h.send_response(200)
            h.send_header("Content-Length", "100000")
            h.end_headers()
            try:
                for _ in range(2000):
                    if stop.is_set():
                        return
                    h.wfile.write(b" ")
                    h.wfile.flush()
                    time.sleep(0.1)          # أقل بكثير من مهلة المقبس: لا تُطلقها أبداً
            except Exception:
                pass
        else:
            reply(h, 404, b"{}")
    srv = Srv(drip)
    try:
        # مهلة إجمالية
        with mock.patch.object(oc, "GET_TOTAL_TIMEOUT", 1.2):
            prov = OpenAICompatProvider(model="m", base_url=f"http://127.0.0.1:{srv.port}/v1", ollama=True, max_retries=0, sleep=lambda s: None)
            t0 = time.monotonic()
            try:
                prov.generate("x", "")
                raise AssertionError
            except ProviderError as exc:
                assert "غير منزَّل" in str(exc)         # الرسالة الأساسية تكفي دون قائمة
            took = time.monotonic() - t0
            assert took < 6, took
        # الإيقاف أثناء التقطير
        cancelled = threading.Event()
        prov = OpenAICompatProvider(model="m", base_url=f"http://127.0.0.1:{srv.port}/v1", ollama=True, max_retries=0, sleep=lambda s: None,
                                    cancel_check=cancelled.is_set)
        threading.Timer(0.8, cancelled.set).start()
        t0 = time.monotonic()
        try:
            prov.generate("x", "")
        except ProviderError:
            pass
        assert time.monotonic() - t0 < 4, "الإيقاف لم يقطع جلب /models"
        # إيقاف سابق للبدء: لا طلب GET أصلاً
        n_before = len([h for h in srv.hits if h[0] == "GET"])
        assert prov._available_models() == []
        assert len([h for h in srv.hits if h[0] == "GET"]) == n_before
        # default_get وحدها: الإيقاف يرفع ProviderError، والمهلة تعيد (0, b"")
        ev = threading.Event()
        hb._TLS.ctx = (ev.is_set, time.monotonic() + 30)
        threading.Timer(0.5, ev.set).start()
        t0 = time.monotonic()
        try:
            hb.default_get(f"http://127.0.0.1:{srv.port}/v1/models", {}, 4.0)
            raise AssertionError
        except ProviderError as exc:
            assert "الإيقاف" in str(exc)
        assert time.monotonic() - t0 < 3
        hb._TLS.ctx = (None, time.monotonic() + 0.8)
        t0 = time.monotonic()
        assert hb.default_get(f"http://127.0.0.1:{srv.port}/v1/models", {}, 4.0) == (0, b"")
        assert time.monotonic() - t0 < 3
        hb._TLS.ctx = None
    finally:
        stop.set()
        srv.close()
    # جلب سليم عبر الخادم الحقيقي بلا ترويسة مفتاح عند غيابه
    seen_auth = []

    def okmodels(h, b):
        seen_auth.append(h.headers.get("authorization"))
        reply(h, 200, json.dumps({"data": [{"id": "llama3"}]}).encode())
    srv = Srv(okmodels)
    try:
        assert hb.default_get(f"http://127.0.0.1:{srv.port}/v1/models", {}, 4.0) == (200, json.dumps({"data": [{"id": "llama3"}]}).encode())
        assert seen_auth == [None]
    finally:
        srv.close()

    print("PASS: test_b41_sec completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b41_sec", file=sys.stderr)
    sys.exit(1)
