"""تغطية المتحكم: التراجع ممنوع أثناء التنفيذ/وجود خطة، استثناء غير متوقع من المنسّق يُعاد الخمول بعده (ويُعاد رفع المقاطعة)، تغيير مجلد العمل لمجلد غير صالح
أو أثناء مهمة يُرفض بلا أثر، فشل تجهيز الإعدادات يتراجع عن المزوّد ولا يسرّب المفتاح، فشل قراءة السجل لا يكسر الواجهة."""
import json
import os
import pathlib
import sys
import tempfile
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    DATA = pathlib.Path(tempfile.mkdtemp(prefix="cov_ctl_data_")).resolve()
    os.environ["COS_DATA_DIR"] = str(DATA)
    for k in ("COS_PROVIDER", "COS_SMALL_MODEL", "COS_LARGE_MODEL", "COS_MAX_USD_PER_DAY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
              "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "COS_OPENAI_API_KEY", "COS_OPENAI_BASE_URL", "COS_ALLOW_REMOTE_OLLAMA"):
        os.environ.pop(k, None)

    from assistant.ui.controller import AssistantController, IDLE, RUNNING, PLAN_READY
    from assistant.ledger import SQLiteLedger
    from assistant.models.demo import DemoProvider
    from assistant.planner import PlanStep

    GKEY = "AI" + "za" + "T" * 35

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("a", encoding="utf-8")
        led = SQLiteLedger(base / "l.db", initial_balance=100, spending_limit=1000)
        c = AssistantController(DemoProvider(), ws, led)

        def evs(kind=None):
            out = c.drain_events()
            return [e for e in out if kind is None or e["type"] == kind]

        evs()
        # 1. التراجع ممنوع أثناء التنفيذ وأثناء وجود خطة، لكلٍّ من undo_last وundo_chain
        c._go(RUNNING)
        for fn in (c.undo_last, c.undo_chain):
            fn()
            errs = evs("error")
            assert errs and "لا يمكن التراجع الآن" in errs[0]["text"], errs
        c._go(PLAN_READY)
        for fn in (c.undo_last, c.undo_chain):
            fn()
            errs = evs("error")
            assert errs and "لا يمكن التراجع الآن" in errs[0]["text"], errs
        c._go(IDLE)

        # 2. خطأ عام من المنسّق أثناء التنفيذ: تعود الحالة IDLE ويصل حدث خطأ؛ ومقاطعة (BaseException) تُعاد بعد تحرير الحالة
        plan = [PlanStep(1, "inspect", ".")]
        c._go(RUNNING)
        with mock.patch.object(c.runner, "run", side_effect=RuntimeError("kaboom")):
            c._run("t-err", "x", plan)
        assert c.state == IDLE
        errs = evs("error")
        assert errs and "خلل غير متوقع" in errs[0]["text"] and "RuntimeError" not in errs[0]["text"], errs   # التفاصيل في crash.log
        c._go(RUNNING)
        with mock.patch.object(c.runner, "run", side_effect=KeyboardInterrupt()):
            try:
                c._run("t-int", "x", plan)
                raise AssertionError("كان يجب إعادة رفع المقاطعة")
            except KeyboardInterrupt:
                pass
        assert c.state == IDLE
        evs()

        # 3. تغيير مجلد العمل: غير موجود / ملف / أثناء مهمة -> يُرفض والمجلد الفعّال كما هو
        old_runner = c.runner
        for bad in (str(base / "nope"), str(ws / "a.txt"), "", "/"):
            assert c.set_workspace(bad) is False, bad
            assert evs("error") and c.runner is old_runner and c.workspace == ws.resolve()
        c._go(RUNNING)
        assert c.set_workspace(str(base)) is False and c.runner is old_runner
        errs = evs("error")
        assert errs and "لا يمكن تغيير المجلد الآن" in errs[0]["text"]
        c._go(IDLE)
        # فشل بناء المنسّق الجديد (مثلاً مجلد غير آمن) لا يُسقط التطبيق ولا يغيّر المنسّق
        ws2 = base / "ws2"
        ws2.mkdir()
        with mock.patch.object(c, "_make_runner", side_effect=RuntimeError("unsafe workspace")):
            assert c.set_workspace(str(ws2)) is False
        errs = evs("error")
        assert errs and "مجلد غير مقبول" in errs[0]["text"] and c.runner is old_runner

        # 4. apply_settings: فشل تجهيز المنسّق بعد بناء المزوّد -> رجوع كامل، ولا مفتاح في الرسالة أو الحالة
        p0, r0 = c.provider, c.runner
        with mock.patch.object(c, "_make_runner", side_effect=RuntimeError("boom")):
            ok = c.apply_settings({"provider": "gemini", "api_key": GKEY})
        assert ok is False and c.provider is p0 and c.runner is r0
        assert "تعذّر تطبيق الإعدادات" in c.last_settings_message and "boom" not in c.last_settings_message, c.last_settings_message
        assert c.current_settings()["has_session_key"] is False      # المفتاح الجديد لم يُخزَّن بعد الفشل
        evs()
        # استثناء غير متوقع أثناء التحقق نفسه
        with mock.patch("assistant.ui.settings.normalize", side_effect=ZeroDivisionError("x")):
            assert c.apply_settings({"provider": "demo"}) is False
        assert "تعذّر تطبيق الإعدادات" in c.last_settings_message and "ZeroDivisionError" not in c.last_settings_message and c.provider is p0
        evs()

        # 5. سجل الدفتر لا يُقرأ: خطأ عربي في الواجهة بدل انهيار
        with mock.patch.object(led, "get_history", side_effect=RuntimeError("db gone")):
            c.history()
        errs = evs("error")
        assert errs and "تعذّر قراءة السجل" in errs[0]["text"]

        # 6. أسئلة التوضيح بلا حالة توضيح
        c.answer_questions(["x"])
        errs = evs("error")
        assert errs and "لا توجد أسئلة" in errs[0]["text"]

        # 7. read_report بلا مجلد تقارير لا ينهار
        assert c.read_report("whatever") is None and c.list_reports() == []

    print("PASS: test_cov_controller_guards completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_controller_guards", file=sys.stderr)
    sys.exit(1)
