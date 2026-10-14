"""اختبارات B11: إصلاحات المراجعة الثانية (كلفة المزوّدات، الدفتر، عدّاد الاستهلاك، الموافقة المعلّقة، تحقق الخطة)."""
import datetime
import json
import sys
import tempfile
import threading
import time
import pathlib
import traceback
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.gemini_provider import GeminiProvider
    from assistant.models.anthropic_provider import AnthropicProvider
    from assistant.models.http_base import ProviderError, default_transport, _OPENER
    from assistant.models.pricing import cost_usd, is_priced
    from assistant.models.base import ModelProvider
    from assistant.ledger import SQLiteLedger, InsufficientBalanceError, SpendingLimitExceededError
    from assistant.usage import UsageMeter
    from assistant.planner import Planner, PlanParseError
    from assistant.ui.controller import AssistantController, IDLE
    from assistant.runner import TaskRunner
    from assistant.models.rules import RuleProvider
    import bench.run_bench as rb

    def raises(exc, fn, *a, **kw):
        try:
            fn(*a, **kw)
        except exc:
            return True
        return False

    # 1. Gemini: توكنات التفكير تُفوتر كإخراج، والرد الفارغ المفوتَر يُسجَّل قبل الخطأ
    def gem(resp):
        return lambda url, h, b, t: (200, json.dumps(resp).encode())
    g = GeminiProvider("gemini-3.1-pro-preview", api_key="K", transport=gem({
        "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 10, "thoughtsTokenCount": 990}}))
    g.generate("x")
    assert g.usage.output_tokens == 1000 and abs(g.usage.usd - (1000 * 2 + 1000 * 12) / 1e6) < 1e-12
    g2 = GeminiProvider("gemini-3.1-pro-preview", api_key="K", transport=gem({
        "candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
        "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 0, "thoughtsTokenCount": 2000}}))
    assert raises(ProviderError, g2.generate, "x") and g2.usage.output_tokens == 2000 and g2.usage.usd > 0
    a = AnthropicProvider("claude-haiku-5-5", api_key="K", transport=lambda u, h, b, t: (200, json.dumps(
        {"content": [], "usage": {"input_tokens": 7, "output_tokens": 9}}).encode()))
    assert raises(ProviderError, a.generate, "x") and a.usage.input_tokens == 7 and a.usage.calls == 1

    # 2. نموذج غير مسعَّر لا يُحتسب مجاناً
    assert not is_priced("mystery-model") and cost_usd("mystery-model", 1_000_000, 0) >= 2.0

    # 3. لا اتباع للتحويلات (المفتاح لا يُنسخ إلى مضيف آخر)، وإخفاء المفتاح مشفّراً بالرابط
    handler = next(h for h in _OPENER.handlers if h.__class__.__name__ == "_NoRedirect")
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None
    p = AnthropicProvider("claude-haiku-5-5", api_key="a b&c/d", transport=lambda *x: (500, b"echo a%20b%26c%2Fd and a b&c/d"), max_retries=0, sleep=lambda s: None)
    try:
        p.generate("x")
    except ProviderError as e:
        assert "a%20b" not in str(e) and "a b&c/d" not in str(e)

    # 4. الدفتر: لا قيم سالبة، لا معرّف مكرر، لا استرجاع خارج المخصوم، لا خصم لمهمة غير بادئة، سباق آمن
    with tempfile.TemporaryDirectory() as tmp:
        led = SQLiteLedger(pathlib.Path(tmp) / "l.db", initial_balance=100, spending_limit=100)
        assert raises(ValueError, led.record_task_start, "t0", "x", -5)
        led.record_task_start("t1", "x", 1)
        assert raises(ValueError, led.record_task_start, "t1", "x", 1)  # معرّف مكرر
        assert raises(ValueError, led.record_task_success, "t1", -50)
        led.record_task_success("t1", 10)
        assert led.get_balance() == 90 and led.get_total_spent() == 10
        led.record_task_failure("t1", "late", charged_points=5)  # ليست STARTED: لا خصم
        assert led.get_balance() == 90
        assert raises(ValueError, led.refund, "never-charged", 50)
        led.refund("t1", 10)
        assert raises(ValueError, led.refund, "t1", 1) and led.get_balance() == 100
        assert led.estimate_cost("Screenshots of files") != 5 and led.estimate_cost("screen capture") == 5
        led.top_up(50)
        assert led.get_balance() == 150 and led.spending_limit == 150
        assert all(r["status"] not in ("TOPUP", "REFUNDED") for r in led.get_history(10))

    with tempfile.TemporaryDirectory() as tmp:
        led = SQLiteLedger(pathlib.Path(tmp) / "l.db", initial_balance=10, spending_limit=10)
        for i in range(2):
            led.record_task_start(f"c{i}", "x", 1)
        results = []
        def charge(tid):
            try:
                led.record_task_success(tid, 8)
                results.append("ok")
            except (SpendingLimitExceededError, InsufficientBalanceError):
                results.append("rejected")
        ts = [threading.Thread(target=charge, args=(f"c{i}",)) for i in range(2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert sorted(results) == ["ok", "rejected"] and led.get_balance() == 2 and led.get_total_spent() == 8

    # 5. عدّاد الاستهلاك اليومي وسقفه وتسجيل كلفة التخطيط حتى عند الفشل
    with tempfile.TemporaryDirectory() as tmp:
        day = [datetime.date(2026, 10, 14)]
        m = UsageMeter(pathlib.Path(tmp) / "u.json", daily_cap_usd=0.5, today=lambda: day[0])
        assert not m.exceeded()
        m.add(0.3); m.add(0.3)
        assert m.exceeded() and abs(m.spent_today() - 0.6) < 1e-9
        day[0] = datetime.date(2026, 10, 15)
        assert not m.exceeded() and m.spent_today() == 0.0
        m.add(float("nan")); m.add(-1)
        assert m.spent_today() == 0.0
        assert UsageMeter(pathlib.Path(tmp) / "u2.json", daily_cap_usd=float("inf")).cap == 1.0

        class Paid(ModelProvider):
            def __init__(self):
                self.usd = 0.0
            def generate(self, prompt, system_prompt=""):
                self.usd += 0.2
                return "ليس json"  # خطة تالفة: ومع ذلك دُفع ثمنها
            def totals(self):
                return {"usd": self.usd, "calls": 1}
            def name(self):
                return "paid"
        root = pathlib.Path(tmp) / "ws"
        led = SQLiteLedger(pathlib.Path(tmp) / "l2.db", initial_balance=100, spending_limit=100)
        prov = Paid()
        meter = UsageMeter(pathlib.Path(tmp) / "u3.json", daily_cap_usd=0.5)
        c = AssistantController(prov, root, led, usage_meter=meter)
        for _ in range(3):
            c.submit_request("افعل شيئاً")
            end = time.time() + 5
            while c.state != IDLE and time.time() < end:
                time.sleep(0.02)
        assert meter.spent_today() >= 0.4  # سُجّلت كلفة استدعاءات فاشلة
        evs = c.drain_events()
        assert any(e["type"] == "error" and "سقف" in e["text"] for e in evs)
        calls_before = prov.usd
        c.submit_request("مرة أخرى")
        time.sleep(0.2)
        assert prov.usd == calls_before  # السقف منع استدعاءً مدفوعاً جديداً

    # 6. الموافقة: ردّ متأخر بعد المهلة لا يُحتسب، ويصدر حدث إغلاق للنافذة
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "ws"
        led = SQLiteLedger(pathlib.Path(tmp) / "l.db", initial_balance=100, spending_limit=100)
        c = AssistantController(RuleProvider(), root, led, approval_timeout=0.3)
        from assistant.planner import PlanStep
        step = PlanStep(1, "run_command", "whoami", {"cmd": ["whoami"]})
        assert c._approve(step) is False  # انتهت المهلة
        evs = c.drain_events()
        req = [e for e in evs if e["type"] == "approval_request"][0]["step"]
        assert any(e["type"] == "approval_closed" and e["approval_id"] == req["approval_id"] for e in evs)
        c.respond_approval(True, req["approval_id"])  # متأخر: يُتجاهل
        assert not c._approval_event.is_set()
        # الموافقة الصحيحة تعمل ولا تنتظر المهلة
        t0 = time.time()
        res = []
        th = threading.Thread(target=lambda: res.append(AssistantController._approve(c, step)))
        c._approval_timeout = 5
        th.start()
        end = time.time() + 3
        while c._pending_approval is None and time.time() < end:
            time.sleep(0.01)
        c.respond_approval(True, c._pending_approval)
        th.join(3)
        assert res == [True] and time.time() - t0 < 3
        # الإيقاف قبل الطلب لا ينتظر المهلة
        c.kill_switch.trigger("x")
        t0 = time.time()
        c._approval_timeout = 5
        assert c._approve(step) is False and time.time() - t0 < 1

    # 7. المخطط: بارامترات مسموحة فقط، ورفض محارف الاتجاه/التحكم
    class P(ModelProvider):
        def __init__(self, steps): self.steps = steps
        def generate(self, prompt, system_prompt=""): return json.dumps({"steps": self.steps})
        def name(self): return "p"
    pl = Planner(P([{"step_id": 1, "action_type": "inspect", "target": ".", "params": {"cmd": 5, "evil": [1]}}]))
    assert pl.plan("x")[0].params == {}
    pl = Planner(P([{"step_id": 1, "action_type": "move", "target": "a", "params": {"dest": "b/a", "junk": 1}}]))
    assert pl.plan("x")[0].params == {"dest": "b/a"}
    for bad in ({"action_type": "delete", "target": "invoice‮gpj.exe"}, {"action_type": "delete", "target": "a\nb"},
                {"action_type": "move", "target": "a", "params": {"dest": "x‮y"}},
                {"action_type": "move", "target": "a", "params": {"dest": 5}}):
        assert raises(PlanParseError, Planner(P([{"step_id": 1, **bad}])).plan, "x"), bad

    # 8. القياس: سقف غير محدود مرفوض، والمسارات المحلية تُحجب من التقرير
    for badcap in (float("nan"), float("inf"), 0, -1):
        assert raises(ValueError, rb.run_bench, RuleProvider(), 1, None, badcap)
    assert rb._scrub("فشل في " + str(pathlib.Path.home()) + "/x", "/nonexistent-tmp").startswith("فشل في ~")

    # 9. فشل تسجيل الدفتر بعد التنفيذ لا يترك المهمة بلا تراجع
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "ws"
        led = SQLiteLedger(pathlib.Path(tmp) / "l.db", initial_balance=100, spending_limit=100)
        orig = led.record_task_success
        def boom(*a, **k): raise sqlite3.OperationalError("database is locked")
        led.record_task_success = boom
        r = TaskRunner(root, RuleProvider(), led)
        rep = r.run("z", "أنشئ مجلد باسم Z")
        assert not rep.success and rep.undo_available and (root / "Z").is_dir()
        assert r.undo_task("z") == 1 and not (root / "Z").exists()

    print("PASS: test_b11 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b11", file=sys.stderr)
    sys.exit(1)
