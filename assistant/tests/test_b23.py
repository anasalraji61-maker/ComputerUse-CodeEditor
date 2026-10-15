"""اختبارات B23 (واجهة/متحكم): نتائج مراجعة الفريق الأحمر السابعة — تراجع ثانٍ، سقف الإنفاق للمجاني، الإيقاف أثناء التخطيط،
خطة بعد الإيقاف، حدود الطلب، فشل مغلق لعدّاد الاستهلاك، إعدادات تالفة، حدث أسئلة قديم."""
import datetime
import inspect
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.config import validate_workspace, load_config, WorkspaceError
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import ModelProvider
    from assistant.models.rules import RuleProvider
    from assistant.ui.controller import AssistantController, MAX_REQUEST_CHARS
    from assistant.usage import UsageMeter

    class Slow(ModelProvider):
        """مزوّد مدفوع بطيء: totals موجودة فيُعدّ مدفوعاً."""
        def __init__(self, delay=1.0, usd=0.0):
            self.delay, self.usd, self.calls = delay, usd, 0
        def name(self): return "slow"
        def totals(self): return {"usd": self.usd, "calls": self.calls, "points": 0}
        def generate(self, prompt, system_prompt=""):
            self.calls += 1
            time.sleep(self.delay)
            self.usd += 0.6
            if "CLARIFY" in system_prompt:
                return json.dumps({"questions": []})
            return json.dumps({"steps": [{"step_id": 1, "action_type": "create_dir", "target": "zz"}]})

    def mk(tmp, provider=None, meter=None):
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"; ws.mkdir(exist_ok=True)
        led = SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500)
        return base, ws, AssistantController(provider or RuleProvider(), ws, led, usage_meter=meter)

    today = datetime.date.today

    with tempfile.TemporaryDirectory() as tmp:
        base, ws, c = mk(tmp)

        # 1. تراجعان متتاليان يعودان إلى المهمتين السابقتين بالترتيب
        for name in ("Alpha", "Beta"):
            c.submit_request(f"أنشئ مجلد {name}")
            assert c.wait_for("plan"); c.start(); assert c.wait_for("report", 10)
        c.undo_last(); assert c.wait_for("undo") and not (ws / "Beta").exists() and (ws / "Alpha").exists()
        c.undo_last(); assert c.wait_for("undo") and not (ws / "Alpha").exists()

        # 2. سقف الإنفاق لا يحجب المزوّد المحلي المجاني
        meter = UsageMeter(base / "u.json", daily_cap_usd=1.0, today=today)
        meter.add(1.2)
        c2 = AssistantController(RuleProvider(), ws, SQLiteLedger(base / "l2.db", initial_balance=500, spending_limit=500), usage_meter=meter)
        c2.submit_request("أنشئ مجلد Z")
        assert c2.wait_for("plan"), "حُجب المجاني بسقف المدفوع"

        # 3. المدفوع يُحجب فعلاً عند السقف
        c3 = AssistantController(Slow(0.0), ws, SQLiteLedger(base / "l3.db", initial_balance=500, spending_limit=500), usage_meter=meter)
        c3.submit_request("أنشئ مجلد Q")
        ev = c3.wait_for("error"); assert ev and "سقف" in ev["text"], ev

        # 4. الإيقاف أثناء التخطيط يحرّر المتحكم فوراً ولا تظهر خطة متأخرة
        prov = Slow(1.0)
        _, ws4, c4 = mk(tmp, prov)
        c4.submit_request("أنشئ مجلد W")
        time.sleep(0.2)
        c4.stop()
        assert c4.state == "IDLE", c4.state
        c4.submit_request("أنشئ مجلد W2")        # طلب جديد مباشرة يُقبل (لا «مشغول»)
        errs = [e for e in c4.drain_events() if e["type"] == "error"]
        assert not errs, errs
        time.sleep(2.5)                           # يفرغ الخيط القديم
        plans = [e for e in c4.drain_events() if e["type"] == "plan"]
        assert len(plans) <= 1, plans             # خطة الطلب الجديد فقط إن اكتملت، لا خطة الطلب الملغى
        c4.stop()

        # 5. طول الطلب محدود
        base5, ws5, c5 = mk(tmp)
        c5.submit_request("ا" * (MAX_REQUEST_CHARS + 1))
        ev = c5.wait_for("error"); assert ev and "طويل" in ev["text"], ev
        assert c5.state == "IDLE"

        # 6. ردّ الموافقة وفحصه تحت القفل
        src = inspect.getsource(AssistantController.respond_approval)
        assert "with self._lock" in src

        # 7. عدّاد الاستهلاك: ملف تالف يُعزل ويبدأ من جديد (B35)
        m = UsageMeter(base / "u2.json", daily_cap_usd=1.0, today=today)
        assert not m.exceeded()
        m.add(0.1)
        (base / "u2.json").write_text("{ not json", encoding="utf-8")
        # B35: الملف التالف لا يقفل الإنفاق للأبد: يُعزل إلى .corrupt.<ts> ويبدأ عدّ جديد مع تنبيه ظاهر
        assert not m.exceeded() and m.reset_note and list(base.glob("u2.json.corrupt.*")), "الملف التالف يجب أن يُعزل لا أن يقفل للأبد"

        # 8. إعدادات تالفة
        os.environ["COS_DATA_DIR"] = str(base / "dd")
        (base / "dd").mkdir()
        (base / "dd" / "config.json").write_text("[]", encoding="utf-8")
        assert load_config() == {}
        try:
            validate_workspace("abc\x00def")
            raise AssertionError("قُبل مسار بحرف NUL")
        except WorkspaceError:
            pass

    # 9. حدث أسئلة قديم لا يبتلع الطلب التالي (واجهة حقيقية إن توفر tkinter وشاشة)
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:
        root = None
    if root is not None:
        try:
            from assistant.ui.widget import AssistantWidget
            with tempfile.TemporaryDirectory() as tmp:
                base, ws, c = mk(tmp)
                w = AssistantWidget(root, c)
                assert c.state == "IDLE"
                w.handle_event({"type": "questions", "questions": ["سؤال؟"]})
                assert w.awaiting_answers is False
                assert root.protocol("WM_DELETE_WINDOW"), "لا معالج لإغلاق النافذة"
        finally:
            root.destroy()

    print("PASS: test_b23 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b23", file=sys.stderr)
    sys.exit(1)
