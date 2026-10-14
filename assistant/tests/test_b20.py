"""اختبارات B20: سقف زمن المهمة، وزر الإيقاف يقطع إعادة محاولات HTTP."""
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.ledger import SQLiteLedger
    from assistant.models.http_base import HttpProvider, ProviderError
    from assistant.models.router import ModelRouter
    from assistant.models.rules import RuleProvider
    from assistant.planner import PlanStep
    from assistant.runner import TaskRunner
    from assistant.ui.controller import AssistantController

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"; ws.mkdir()
        led = SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500)

        # 1. سقف الزمن: ساعة مزيّفة تقفز؛ الخطوة الأولى تُنفَّذ والثانية تُرفض
        ticks = iter([0, 0, 5000, 5000, 5000, 5000])
        r = TaskRunner(ws, RuleProvider(), led, max_seconds=100, clock=lambda: next(ticks))
        plan = [PlanStep(1, "create_dir", "d1"), PlanStep(2, "create_dir", "d2")]
        rep = r.run("t1", "انشئ مجلدين", plan=plan)
        assert not rep.success and any("الحد الزمني" in f for f in rep.failures), rep.failures
        assert (ws / "d1").is_dir() and not (ws / "d2").exists()
        assert rep.undo_available   # ما نُفّذ قابل للتراجع
        # الافتراضي لا يعيق مهمة عادية
        r2 = TaskRunner(ws, RuleProvider(), led)
        rep = r2.run("t2", "انشئ مجلد", plan=[PlanStep(1, "create_dir", "d3")])
        assert rep.success, rep.failures

        # 2. الإيقاف يقطع إعادة محاولات HTTP
        calls, sleeps = [], []
        class P(HttpProvider):
            ENV_KEYS = ("X",)
            def _build(self, prompt, system_prompt):
                return "http://x", {}, {}
        stop = {"v": False}
        def transport(url, h, b, t):
            calls.append(1)
            stop["v"] = True       # الإيقاف يُضغط أثناء أول محاولة
            return 503, b"busy"
        p = P("m", api_key="k", transport=transport, sleep=sleeps.append, cancel_check=lambda: stop["v"])
        try:
            p.generate("a")
            raise AssertionError("كان يجب أن يُقطع")
        except ProviderError as e:
            assert "الإيقاف" in str(e), e
        assert len(calls) == 1 and not sleeps, (calls, sleeps)

        # 3. المتحكم يربط الإيقاف بالمزوّد وبمزوّدي الموجّه
        small, large = P("s", api_key="k"), P("l", api_key="k")
        router = ModelRouter(small, large)
        c = AssistantController(router, ws, led)
        assert small.cancel_check is not None and large.cancel_check is not None
        c.kill_switch.trigger("t")
        assert small.cancel_check() is True

    print("PASS: test_b20 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b20", file=sys.stderr)
    sys.exit(1)
