"""B39: الإعدادات (مزوّد/مفتاح جلسة/نماذج/مجلد/سقف) وفحص الاتصال وإشعار البدء بالمزوّد النشط. محكم بلا شبكة حقيقية.
أجزاء tkinter تُتخطى بطباعة SKIP."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    DATA = pathlib.Path(tempfile.mkdtemp(prefix="b39_data_")).resolve()
    os.environ["COS_DATA_DIR"] = str(DATA)
    for k in ("COS_PROVIDER", "COS_SMALL_MODEL", "COS_LARGE_MODEL", "COS_MAX_USD_PER_DAY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
              "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "COS_OPENAI_API_KEY", "COS_OPENAI_BASE_URL", "COS_ALLOW_REMOTE_OLLAMA"):
        os.environ.pop(k, None)

    from assistant.ui import settings as S
    from assistant.ui.controller import AssistantController, IDLE
    from assistant.ledger import SQLiteLedger
    from assistant.usage import UsageMeter
    from assistant.models.demo import DemoProvider
    from assistant.models.gemini_provider import GeminiProvider
    from assistant.models.anthropic_provider import AnthropicProvider
    from assistant.models.openai_compat import OpenAICompatProvider
    from assistant.models.router import ModelRouter
    from assistant.models.rules import RuleFirstProvider
    from assistant.models.http_base import ProviderError
    from assistant.config import load_config

    # مفاتيح اختبار تُركَّب من أجزاء كي لا يعلّمها test_no_secrets
    GKEY = "AI" + "za" + "T" * 35
    AKEY = "sk" + "-ant-" + "U" * 30
    OKEY = "sk" + "-" + "V" * 30

    # ---------- 1) منطق خالص ----------
    assert S.infer_key_kind(AKEY) == "anthropic" and S.infer_key_kind(GKEY) == "gemini" and S.infer_key_kind(OKEY) == "openai"
    assert S.infer_key_kind("whatever") is None
    for bad in ("abc def", "abcاdef", "x" * 500):
        try:
            S.validate_key(bad)
            raise AssertionError("bad key accepted")
        except S.SettingsError:
            pass
    assert S.validate_key("  " + GKEY + "\n") == GKEY and S.validate_key("") == ""
    assert S.validate_model(" qwen2.5:7b ") == "qwen2.5:7b" and S.validate_model("") == ""
    for bad in ("a b", "x;rm", "نموذج", "-lead"):
        try:
            S.validate_model(bad)
            raise AssertionError("bad model accepted")
        except S.SettingsError:
            pass
    assert S.parse_cap("") is None and S.parse_cap(None) is None and S.parse_cap("0.5") == 0.5 and S.parse_cap("٢٫٥") == 2.5
    assert S.parse_cap("1,5") == 1.5 and S.parse_cap(0) == 0.0 and S.parse_cap("$3") == 3.0
    for bad in ("abc", "-1", "nan", "inf", "5000", True):
        try:
            S.parse_cap(bad)
            raise AssertionError(("bad cap accepted", bad))
        except S.SettingsError:
            pass
    # normalize: مفتاح مع تلقائي يُستنتج نوعه؛ نوع مخالف يُرفض؛ مفتاح مع Ollama يُرفض
    n = S.normalize({"provider": "auto", "api_key": GKEY})
    assert n["key_kind"] == "gemini" and n["provider"] == "auto"
    for vals in ({"provider": "auto", "api_key": "plainkey"}, {"provider": "gemini", "api_key": AKEY},
                 {"provider": "ollama", "api_key": GKEY}, {"provider": "nope"}, {"provider": "demo", "api_key": GKEY}):
        try:
            S.normalize(vals)
            raise AssertionError(("accepted", vals["provider"]))
        except S.SettingsError as exc:
            assert GKEY not in str(exc) and AKEY not in str(exc)
    env = S.build_env({"PATH": "p", "GEMINI_API_KEY": "ambient"}, "gemini", {"GEMINI_API_KEY": GKEY}, "m1", "")
    assert env["GEMINI_API_KEY"] == GKEY and env["COS_PROVIDER"] == "gemini" and env["COS_SMALL_MODEL"] == "m1" and "COS_LARGE_MODEL" not in env
    assert os.environ.get("GEMINI_API_KEY") is None   # البيئة الحقيقية لا تُلمس
    assert S.key_available({"GOOGLE_API_KEY": "x"}, "gemini") and not S.key_available({}, "anthropic") and S.key_available({}, "ollama")
    # الإعدادات المحفوظة: غير سرية فقط، وأي تالف يُتجاهل، والبيئة الصريحة تتقدم
    assert S.load_saved({"provider": "x", "small_model": "bad name", "daily_cap_usd": "zzz"}) == {}
    assert S.load_saved({"provider": "ollama", "small_model": "qwen2.5:7b", "daily_cap_usd": 2}) == \
        {"provider": "ollama", "small_model": "qwen2.5:7b", "daily_cap_usd": 2.0}
    assert S.startup_env({"COS_PROVIDER": "demo"}, {"provider": "gemini"})["COS_PROVIDER"] == "demo"
    assert S.startup_env({}, {"provider": "gemini", "small_model": "m"})["COS_PROVIDER"] == "gemini"
    assert S.saved_cap({}, {"daily_cap_usd": 3}) == 3.0 and S.saved_cap({"COS_MAX_USD_PER_DAY": "9"}, {"daily_cap_usd": 3}) is None
    upd = S.safe_config_update({"workspace": "w", "api_key": "LEAK", "gemini_token": "LEAK2", "x": 1},
                               {"provider": "gemini", "small_model": "", "large_model": "L", "daily_cap": 2.0, "api_key": GKEY})
    assert upd == {"workspace": "w", "x": 1, "provider": "gemini", "large_model": "L", "daily_cap_usd": 2.0}, upd
    assert GKEY not in json.dumps(upd)
    assert S.redact(f"err {GKEY} and {GKEY}", [GKEY]).count("***") == 2
    # الاستيراد لا يتطلب tkinter (يعمل تحت py3 بلا tk)
    r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); import assistant.ui.settings, assistant.ui.controller; "
                        "assert 'tkinter' not in sys.modules; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); print('نظيف')" % str(ROOT)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    assert r.returncode == 0 and "نظيف" in r.stdout, (r.stdout[-200:], r.stderr[-300:])
    print("ok 1: pure settings logic")

    # ---------- أدوات ----------
    def gem_reply(text='{"ok": true}', tin=12, tout=4):
        return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}],
                           "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout}}).encode()

    def ant_reply(text='{"ok": true}'):
        return json.dumps({"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 10, "output_tokens": 3}}).encode()

    def oai_reply(text='{"ok": true}'):
        return json.dumps({"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}).encode()

    def real(cls, model, transport, **kw):
        p = cls(model, api_key=kw.pop("api_key", "K" * 20), transport=transport, sleep=lambda s: None, max_retries=0, **kw)
        return RuleFirstProvider(ModelRouter(p, p, usage_meter=None, fallback_to_active_meter=False))

    def mk(provider=None, cap=None):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="b39_")).resolve()
        ws = tmp / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("a", encoding="utf-8")
        led = SQLiteLedger(tmp / "l.db", initial_balance=100, spending_limit=1000)
        meter = UsageMeter(tmp / "usage.json", daily_cap_usd=cap if cap is not None else 5.0)
        c = AssistantController(provider or DemoProvider(), ws, led, usage_meter=meter)
        return c, ws, meter, tmp

    def events(c, kinds=None):
        evs = c.drain_events()
        return [e for e in evs if kinds is None or e["type"] in kinds]

    # ---------- 2) وصف المزوّد (مجاني/مدفوع) ----------
    d = S.describe_provider(DemoProvider())
    assert d["kind"] == "rules" and "محلي (بلا إنترنت)" in d["line"] and "مجاني" in d["line"]
    d = S.describe_provider(real(GeminiProvider, "gemini-3.1-flash-lite", lambda *a: (200, gem_reply())), 2.0)
    assert d["kind"] == "gemini" and "مدفوع" in d["line"] and "Gemini" in d["line"] and "2 دولار" in d["line"] and d["cost"] == "paid"
    d = S.describe_provider(real(AnthropicProvider, "claude-haiku-5-5", lambda *a: (200, ant_reply())))
    assert d["kind"] == "anthropic" and "مدفوع" in d["line"] and "claude-haiku-5-5" in d["line"]
    oll = RuleFirstProvider(ModelRouter(OpenAICompatProvider("qwen2.5:7b", base_url="http://127.0.0.1:11434/v1", ollama=True, local=True,
                                                             key_required=False, transport=lambda *a: (200, oai_reply())),
                                        fallback_to_active_meter=False))
    d = S.describe_provider(oll)
    assert d["kind"] == "ollama" and d["cost"] == "free" and "مجاني" in d["line"] and "Ollama" in d["line"]
    assert S.choice_of(oll) == "ollama" and S.choice_of(DemoProvider()) == "demo"
    print("ok 2: describe_provider")

    # ---------- 3) تطبيق الإعدادات (ذرّي، مفتاح الجلسة في الذاكرة فقط) ----------
    c, ws, meter, tmp = mk()
    p0 = c.provider
    cs = c.current_settings()
    assert cs["provider"] == "demo" and cs["has_session_key"] is False and cs["workspace"] == str(ws)
    # بلا مفتاح: خطأ عربي واضح، ولا تغيير
    assert c.apply_settings({"provider": "gemini"}) is False
    assert c.provider is p0 and "لا يوجد مفتاح لـ Gemini" in c.last_settings_message
    assert [e["type"] for e in events(c)] == ["error"]
    # مفتاح بنوع خاطئ
    assert c.apply_settings({"provider": "anthropic", "api_key": GKEY}) is False and c.provider is p0
    events(c)
    # ليس خاملاً: يرفض ويبقى كل شيء
    c.state = "RUNNING"
    assert c.apply_settings({"provider": "demo"}) is False and "لا يمكن تغيير الإعدادات الآن" in c.last_settings_message
    c.state = IDLE
    events(c)
    # نجاح: Gemini بمفتاح جلسة + نموذج + سقف
    assert c.apply_settings({"provider": "gemini", "api_key": "  " + GKEY + " ", "small_model": "gemini-3.1-flash-lite",
                             "daily_cap": "0.75"}) is True
    evs = events(c)
    assert [e["type"] for e in evs] == ["settings_applied"] and "Gemini" in evs[0]["text"] and "مدفوع" in evs[0]["text"]
    assert c.provider is not p0 and c.runner.provider is c.provider and S.describe_provider(c.provider)["kind"] == "gemini"
    assert meter.cap == 0.75
    assert c.current_settings()["has_session_key"] is True and c.current_settings()["daily_cap"] == 0.75
    assert GKEY not in json.dumps(c.current_settings()) and GKEY not in json.dumps(evs)
    cfg_text = (DATA / "config.json").read_text(encoding="utf-8")
    cfg = json.loads(cfg_text)
    assert cfg["provider"] == "gemini" and cfg["small_model"] == "gemini-3.1-flash-lite" and cfg["daily_cap_usd"] == 0.75
    assert GKEY not in cfg_text and not any("key" in k.lower() for k in cfg), cfg
    # لا أثر للمفتاح على القرص إطلاقاً (config/crash/تقارير/سجلات في مجلد البيانات والمجلد المؤقت)
    for base in (DATA, tmp):
        for f in base.rglob("*"):
            if f.is_file():
                assert GKEY.encode() not in f.read_bytes(), f
    assert os.environ.get("GEMINI_API_KEY") is None and os.environ.get("COS_PROVIDER") is None   # لا تلويث للبيئة
    # الإبقاء على مفتاح الجلسة عند تغيير النموذج فقط، ثم النسيان
    assert c.apply_settings({"provider": "gemini", "large_model": "gemini-3.1-pro-preview"}) is True
    events(c)
    assert c.current_settings()["has_session_key"] is True
    assert c.apply_settings({"provider": "gemini", "clear_key": True}) is False and "لا يوجد مفتاح" in c.last_settings_message
    assert c.current_settings()["has_session_key"] is True   # الفشل لم يمسح شيئاً (ذرّية)
    events(c)
    # مجلد غير صالح: لا يتغير المزوّد (ذرّية)
    before = c.provider
    assert c.apply_settings({"provider": "demo", "workspace": str(tmp / "no_such_workspace_dir")}) is False and c.provider is before
    assert str(c.workspace) == str(ws)
    events(c)
    # مجلد جديد + قواعد فقط
    ws2 = tmp / "ws2"
    ws2.mkdir()
    assert c.apply_settings({"provider": "demo", "workspace": str(ws2)}) is True
    kinds = [e["type"] for e in events(c)]
    assert "workspace" in kinds and "settings_applied" in kinds, kinds
    assert str(c.workspace) == str(ws2.resolve()) and S.describe_provider(c.provider)["kind"] == "rules"
    assert load_config()["workspace"] == str(ws2.resolve()) and load_config()["provider"] == "demo"
    # سقف غير صالح يُرفض بلا تغيير
    assert c.apply_settings({"provider": "demo", "daily_cap": "abc"}) is False and meter.cap == 0.75
    events(c)
    # Ollama: بلا مفتاح ومجاني
    assert c.apply_settings({"provider": "ollama", "small_model": "qwen2.5:7b"}) is True
    evs = events(c, ("settings_applied",))
    assert "مجاني" in evs[0]["text"] and S.describe_provider(c.provider)["kind"] == "ollama"
    # تلقائي بمفتاح مُلصق: النوع يُستنتج من المفتاح
    assert c.apply_settings({"provider": "auto", "api_key": AKEY}) is True
    assert S.describe_provider(c.provider)["kind"] == "anthropic"
    events(c)
    # المزوّد الجديد موصول بزر الإيقاف وبالعدّاد
    inner = c.provider.fallback.providers["small"]
    assert inner.cancel_check is not None and c.provider.fallback.usage_meter is meter
    print("ok 3: apply_settings atomic + session-only key")

    # ---------- 4) فحص الاتصال ----------
    def wait_ping(c, timeout=10):
        ev = c.wait_for("ping", timeout)
        assert ev is not None, "no ping event"
        return ev

    sent = []

    def good(url, headers, body, timeout):
        sent.append((url, dict(headers), json.loads(body.decode("utf-8"))))
        return 200, gem_reply()
    prov = real(GeminiProvider, "gemini-3.1-flash-lite", good, api_key=GKEY, max_tokens=2048)
    c, ws, meter, tmp = mk(prov)
    live = prov.fallback.providers["small"]
    assert c.check_connection() is True
    ev = wait_ping(c)
    assert ev["ok"] is True and ev["kind"] == "gemini" and "الاتصال يعمل" in ev["text"] and "gemini-3.1-flash-lite" in ev["text"] and "ثانية" in ev["text"], ev
    assert sent[-1][2]["generationConfig"]["maxOutputTokens"] == S.PING_MAX_TOKENS     # طلب صغير جداً
    assert live.max_tokens == 2048 and live.usage.usd == 0 and live.cancel_check is not None   # المزوّد الحي لم يُلمس
    assert meter.spent_today() > 0 and ev["usd"] > 0, "ping cost must be accounted"
    assert GKEY not in ev["text"] and GKEY not in json.dumps(sent[-1][2])
    time.sleep(0.1)
    assert c._pinging is False
    # ردّ بنص فارغ مع استهلاك: الاتصال سليم
    prov = real(GeminiProvider, "gemini-3.1-flash-lite", lambda *a: (200, gem_reply("", 10, 24)))
    c, ws, meter, tmp = mk(prov)
    c.check_connection()
    ev = wait_ping(c)
    assert ev["ok"] is True, ev
    # أخطاء الخدمة: رسائل المزوّد العربية الدقيقة تصل كما هي، ولا يتسرب المفتاح حتى لو أعاده الخادم
    for status, needle in ((401, "401"), (403, "403"), (404, "404"), (429, "429"), (503, "503")):
        body = (b'{"error": "echo %s"}' % GKEY.encode())
        prov = real(GeminiProvider, "gemini-3.1-flash-lite", lambda *a, st=status, b=body: (st, b), api_key=GKEY)
        c, ws, meter, tmp = mk(prov)
        c.check_connection()
        ev = wait_ping(c)
        assert ev["ok"] is False and ev["text"].startswith("تعذّر الاتصال") and needle in ev["text"], (status, ev["text"])
        assert GKEY not in ev["text"]
        assert meter.spent_today() == 0
    # الشبكة
    def netfail(*a):
        raise ProviderError("تعذّر تحليل اسم الخادم (DNS): تحقق من اتصال الإنترنت أو إعدادات DNS/البروكسي.")
    c, ws, meter, tmp = mk(real(AnthropicProvider, "claude-haiku-5-5", netfail))
    c.check_connection()
    ev = wait_ping(c)
    assert ev["ok"] is False and "DNS" in ev["text"], ev
    # Anthropic نجاح
    c, ws, meter, tmp = mk(real(AnthropicProvider, "claude-haiku-5-5", lambda u, h, b, t: (200, ant_reply())))
    c.check_connection()
    ev = wait_ping(c)
    assert ev["ok"] and ev["kind"] == "anthropic" and "claude-haiku-5-5" in ev["text"]

    # Ollama: غير شغّال / النموذج غير منزّل / ناجح ومجاني (لا يمس العدّاد)
    def mk_oll(transport, getter=None):
        p = OpenAICompatProvider("qwen2.5:7b", base_url="http://127.0.0.1:11434/v1", ollama=True, local=True, key_required=False,
                                 transport=transport, get_transport=getter or (lambda u, h, t: (200, json.dumps({"data": [{"id": "llama3"}]}).encode())),
                                 sleep=lambda s: None, max_retries=0)
        return RuleFirstProvider(ModelRouter(p, p, fallback_to_active_meter=False))

    def refused(*a):
        raise ConnectionRefusedError()
    c, ws, meter, tmp = mk(mk_oll(refused))
    c.check_connection()
    ev = wait_ping(c)
    assert ev["ok"] is False and "Ollama غير مفتوح" in ev["text"], ev["text"]
    c, ws, meter, tmp = mk(mk_oll(lambda *a: (404, b'{"error":"model not found"}')))
    c.check_connection()
    ev = wait_ping(c)
    assert ev["ok"] is False and "qwen2.5:7b غير منزَّل" in ev["text"] and "llama3" in ev["text"], ev["text"]
    c, ws, meter, tmp = mk(mk_oll(lambda *a: (200, oai_reply())), cap=0.0)   # سقف صفر: المحلي المجاني لا يُحجب
    c.check_connection()
    ev = wait_ping(c)
    assert ev["ok"] is True and ev["usd"] == 0.0 and "بلا كلفة" in ev["text"] and meter.spent_today() == 0, ev
    # سقف الإنفاق يُحترم للمدفوع: لا نداء أصلاً
    calls = []
    c, ws, meter, tmp = mk(real(GeminiProvider, "gemini-3.1-flash-lite", lambda *a: calls.append(1) or (200, gem_reply())), cap=0.0)
    c.check_connection()
    ev = wait_ping(c)
    assert ev["ok"] is False and "سقف" in ev["text"] and calls == [], (ev, calls)
    # قواعد محلية فقط
    c, ws, meter, tmp = mk()
    c.check_connection()
    ev = wait_ping(c)
    assert ev["ok"] is True and ev["text"] == "لا يوجد نموذج — أعمل بالقواعد المحلية فقط" and ev["kind"] == "rules"
    # عدم الحجب وعدم التكرار: النداء يجري في خيط عامل، والفحص الثاني أثناء الأول يُتجاهل
    gate = threading.Event()

    def slow(*a):
        gate.wait(10)
        return 200, gem_reply()
    c, ws, meter, tmp = mk(real(GeminiProvider, "gemini-3.1-flash-lite", slow))
    t0 = time.time()
    assert c.check_connection() is True and time.time() - t0 < 1.0
    assert c.check_connection() is False
    gate.set()
    assert wait_ping(c)["ok"] is True
    # بعد مفتاح الجلسة: لا يظهر المفتاح في نص أي حدث
    c, ws, meter, tmp = mk()
    c.apply_settings({"provider": "gemini", "api_key": GKEY})
    c.provider.fallback.providers["small"].transport = lambda *a: (401, ("bad key " + GKEY).encode())
    c.provider.fallback.providers["large"].transport = c.provider.fallback.providers["small"].transport
    c.check_connection()
    ev = wait_ping(c)
    assert ev["ok"] is False and "401" in ev["text"] and GKEY not in json.dumps(ev, ensure_ascii=False)
    print("ok 4: connection check")

    # ---------- 5) إشعار البدء بالمزوّد النشط ----------
    from assistant import app
    tmp5 = pathlib.Path(tempfile.mkdtemp(prefix="b39_app_")).resolve()
    saved_env = {k: os.environ.get(k) for k in ("COS_DATA_DIR", "COS_WORKSPACE", "COS_PROVIDER", "GEMINI_API_KEY", "COS_OPENAI_BASE_URL")}
    try:
        os.environ["COS_DATA_DIR"] = str(tmp5 / "d")
        os.environ["COS_WORKSPACE"] = str(tmp5 / "w")
        c5, notice = app.build_controller()
        assert "الوضع الحالي: محلي (بلا إنترنت)" in notice and "مجاني" in notice, notice
        os.environ["COS_PROVIDER"] = "gemini"
        os.environ["GEMINI_API_KEY"] = GKEY
        c5, notice = app.build_controller()
        assert notice.startswith("الخدمة الذكية الحالية: Gemini") and "مدفوع" in notice, notice[:200]
        assert S.describe_provider(c5.provider)["kind"] == "gemini"
        os.environ["COS_PROVIDER"] = "ollama"
        c5, notice = app.build_controller()
        assert "Ollama" in notice and "مجاني" in notice, notice[:200]
        # ما اختير في الإعدادات يُحفظ (بلا مفتاح) ويعود بلا مفتاح فيرجع للقواعد مع تلميح الإعدادات، ونيّة المستخدم تبقى ظاهرة
        os.environ.pop("GEMINI_API_KEY"); os.environ.pop("COS_PROVIDER")
        (tmp5 / "d" / "config.json").write_text(json.dumps({"provider": "gemini", "daily_cap_usd": 0.3}), encoding="utf-8")
        c5, notice = app.build_controller()
        assert S.describe_provider(c5.provider)["kind"] == "rules" and "الإعدادات" in notice and "مفتاح" in notice, notice
        assert c5.current_settings()["provider"] == "gemini" and c5.usage_meter.cap == 0.3
        # إلصاق المفتاح للجلسة يكمل ما اختير سابقاً
        assert c5.apply_settings({"provider": "gemini", "api_key": GKEY}) is True
        assert S.describe_provider(c5.provider)["kind"] == "gemini"
        assert GKEY not in (tmp5 / "d" / "config.json").read_text(encoding="utf-8")
        # البيئة الصريحة تتقدم على config.json
        os.environ["COS_PROVIDER"] = "demo"
        c5, notice = app.build_controller()
        assert S.describe_provider(c5.provider)["kind"] == "rules"
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok 5: startup notice names the active provider and cost")

    # ---------- 6) الواجهة ----------
    try:
        import tkinter as tk
        probe = tk.Tk()
        probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: no tkinter/display ({type(exc).__name__}: {exc})")
        print("PASS: test_b39_ui completed successfully")
        sys.exit(0)
    import assistant.ui.widget as W

    c, ws, meter, tmp = mk()
    root = tk.Tk()
    try:
        w = W.AssistantWidget(root, c)
        root.update()
        assert w.settings_btn.cget("text") == "الإعدادات" and w.ping_btn.cget("text") == "فحص اتصال الخدمة"

        def pump(cond, timeout=8.0):
            end = time.time() + timeout
            while time.time() < end:
                root.update()
                if cond():
                    return True
                time.sleep(0.02)
            return False
        # النافذة: آخر ابن إطار الأزرار؛ تنبيه «لا يُحفظ على القرص» ظاهر؛ حقل المفتاح مخفي
        w._on_settings()
        dlg = w._settings_dlg
        assert dlg is not None and w._on_settings() is None and w._settings_dlg is dlg   # لا نافذة مكررة
        kids = dlg.win.winfo_children()
        assert kids[-1].winfo_children() and dlg.apply_btn.master is kids[-1]
        texts = []

        def walk(x):
            try:
                texts.append(str(x.cget("text")))
            except Exception:
                pass
            for ch in x.winfo_children():
                walk(ch)
        walk(dlg.win)
        assert any("لا يُحفظ على القرص" in t for t in texts), texts
        assert dlg.key_entry.cget("show") != ""
        # خطأ: بلا مفتاح → رسالة داخل النافذة (لا خلفها) وتبقى مفتوحة
        dlg.v_provider.set("Gemini (Google)")
        assert dlg.apply() is False and "لا يوجد مفتاح" in dlg.msg_lbl.cget("text") and w._settings_dlg is dlg
        # نجاح: المفتاح يُمسح من الحقل والنافذة تُغلق ورسالة في المحادثة
        dlg.v_key.set(GKEY)
        dlg.v_cap.set("0.4")
        assert dlg.collect()["api_key"] == GKEY and dlg.collect()["provider"] == "gemini"
        assert dlg.apply() is True
        assert w._settings_dlg is None
        assert pump(lambda: "تم تطبيق الإعدادات" in w.chat.get("1.0", "end"))
        assert "Gemini" in w.chat.get("1.0", "end") and "مدفوع" in w.chat.get("1.0", "end")
        assert GKEY not in w.chat.get("1.0", "end") and dlg.v_key.get() == ""
        assert meter.cap == 0.4
        # فتح من جديد: يعرض الحالي بلا المفتاح ويقترح الإبقاء
        w._on_settings()
        dlg2 = w._settings_dlg
        assert dlg2.v_provider.get() == "Gemini (Google)" and dlg2.v_key.get() == "" and dlg2.v_cap.get() == "0.4"
        assert hasattr(dlg2, "clear_chk")
        dlg2.close()
        # فحص الاتصال من الواجهة: الزر يُعطَّل أثناء الفحص ثم يعود، والنتيجة في المحادثة، ولا حجب
        gate = threading.Event()

        def slow_ok(*a):
            gate.wait(10)
            return 200, gem_reply()
        for p in c.provider.fallback.providers.values():
            p.transport = slow_ok
        t0 = time.time()
        w._on_ping()
        assert time.time() - t0 < 1.0 and str(w.ping_btn.cget("state")) == "disabled"
        root.update()
        gate.set()
        assert pump(lambda: "الاتصال يعمل" in w.chat.get("1.0", "end")), w.chat.get("1.0", "end")
        assert str(w.ping_btn.cget("state")) == "normal"
        # فشل الفحص يظهر بالعربية داخل نافذة الإعدادات أيضاً إن كانت مفتوحة
        for p in c.provider.fallback.providers.values():
            p.transport = lambda *a: (401, b"{}")
        w._on_settings()
        w._on_ping()
        assert pump(lambda: "401" in w._settings_dlg.msg_lbl.cget("text")), w._settings_dlg.msg_lbl.cget("text")
        w._settings_dlg.close()
        # تعطّل التطبيق لا يحدث بعد الإغلاق: لا أمر after معلق
    finally:
        root.destroy()
    assert not w._alive
    print("ok 6: settings dialog + ping button")

    print("PASS: test_b39_ui completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b39_ui", file=sys.stderr)
    sys.exit(1)
