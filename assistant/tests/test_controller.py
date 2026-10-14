"""اختبارات المتحكم بلا نافذة: الأسئلة التوضيحية، الخطة، الموافقة لكل خطوة، الإيقاف، التراجع."""
import json
import sys
import tempfile
import threading
import time
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.ui.controller import AssistantController, IDLE, PLAN_READY, CLARIFYING
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import ModelProvider

    class ScriptedProvider(ModelProvider):
        def __init__(self, steps, questions=None):
            self.steps, self.questions, self.prompts = steps, questions or [], []

        def generate(self, prompt, system_prompt=""):
            self.prompts.append(prompt)
            if "CLARIFY" in system_prompt:
                return json.dumps({"questions": self.questions})
            return json.dumps({"steps": self.steps})

        def name(self):
            return "scripted"

    def st(i, action, target, **params):
        return {"step_id": i, "action_type": action, "target": target, "params": params}

    def mk(tmp, steps, questions=None, timeout=5.0):
        root = pathlib.Path(tmp).resolve() / "ws"
        led = SQLiteLedger(pathlib.Path(tmp).resolve() / "l.db", initial_balance=100, spending_limit=100)
        prov = ScriptedProvider(steps, questions)
        return AssistantController(prov, root, led, approval_timeout=timeout), prov, root

    def wait_state(c, state, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if c.state == state:
                return True
            time.sleep(0.01)
        return False

    with tempfile.TemporaryDirectory() as tmp:
        # 1. تدفق عادي: طلب ← خطة (بمستويات) ← بدء ← تقرير نجاح مع أحداث التقدم
        c, prov, root = mk(tmp, [st(1, "create_dir", "docs"), st(2, "inspect", "docs")])
        c.submit_request("أنشئ مجلد docs")
        plan = c.wait_for("plan")
        assert plan and [s["level"] for s in plan["steps"]] == ["REVERSIBLE", "READ"]
        assert c.state == PLAN_READY
        c.start()
        evs = []
        while True:
            e = c.next_event(10)
            assert e is not None, "لم يصل التقرير"
            evs.append(e["type"])
            if e["type"] == "report":
                rep = e
                break
        assert rep["success"], rep
        assert (root / "docs").is_dir() and "تقرير المهمة: تم بنجاح" in rep["text"]
        assert wait_state(c, IDLE)
        assert evs.count("step_start") == 2 and evs.count("step_done") == 2

        # 2. أسئلة توضيحية: حد أقصى 3، والإجابات تدخل في الطلب
        c, prov, root = mk(tmp, [st(1, "create_dir", "q")], questions=["س1", "س2", "س3", "س4", "س5"])
        c.submit_request("رتّب ملفاتي")
        q = c.wait_for("questions")
        assert q and len(q["questions"]) == 3 and c.state == CLARIFYING
        c.answer_questions(["مجلد التنزيلات"])
        assert c.wait_for("plan")
        assert "مجلد التنزيلات" in prov.prompts[-1]

        # 3. رفض طلب جديد أثناء وجود خطة، ورفض طلب فارغ
        c.submit_request("آخر")
        e = c.wait_for("error")
        assert e and "مشغول" in e["text"]
        c.cancel_plan()
        assert c.state == IDLE
        c.submit_request("   ")
        assert c.wait_for("error")

        # 4. خطة تالفة: خطأ بلا تنفيذ والعودة لـ IDLE
        c, prov, root = mk(tmp, [{"step_id": 1, "action_type": "weird", "target": "x"}])
        c.submit_request("شيء")
        e = c.wait_for("error")
        assert e and "الخطة" in e["text"] and wait_state(c, IDLE)

        # 5. موافقة: نعم تنجح، لا تفشل، والمهلة = رفض
        danger = [st(1, "create_dir", "d1"), st(2, "run_command", "whoami", cmd=["whoami"])]
        c, prov, root = mk(tmp, danger)
        c.submit_request("هوية"); c.wait_for("plan"); c.start()
        req = c.wait_for("approval_request")
        assert req and req["step"]["action"] == "run_command"
        c.respond_approval(True, c._pending_approval)
        rep = c.wait_for("report", 10)
        assert rep["success"], rep["text"]

        c, prov, root = mk(tmp, danger)
        c.submit_request("هوية"); c.wait_for("plan"); c.start()
        assert c.wait_for("approval_request")
        c.respond_approval(False, c._pending_approval)
        rep = c.wait_for("report", 10)
        assert not rep["success"] and "رُفضت" in rep["text"]

        c, prov, root = mk(tmp, danger, timeout=0.3)
        c.submit_request("هوية"); c.wait_for("plan"); c.start()
        assert c.wait_for("approval_request")
        rep = c.wait_for("report", 10)  # لا رد من المستخدم → رفض بالمهلة
        assert not rep["success"]

        # 6. الإيقاف أثناء نافذة موافقة معلّقة يقطع المهمة فوراً
        c, prov, root = mk(tmp, danger, timeout=60)
        c.submit_request("هوية"); c.wait_for("plan"); c.start()
        assert c.wait_for("approval_request")
        t0 = time.time()
        c.stop()
        rep = c.wait_for("report", 5)
        assert rep and not rep["success"] and time.time() - t0 < 3
        assert "الإيقاف" in rep["text"] or "رُفضت" in rep["text"]
        assert wait_state(c, IDLE)
        # بعد الإيقاف يمكن بدء طلب جديد (يُعاد ضبط الزر)
        c.submit_request("جديد")
        assert c.wait_for("plan")

        # 7. التراجع عن آخر مهمة
        c, prov, root = mk(tmp, [st(1, "create_dir", "undo_me")])
        c.submit_request("x"); c.wait_for("plan"); c.start(); c.wait_for("report", 10)
        wait_state(c, IDLE)
        assert (root / "undo_me").is_dir()
        c.undo_last()
        u = c.wait_for("undo")
        assert u and u["count"] == 1 and not (root / "undo_me").exists()

    print("PASS: test_controller completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_controller", file=sys.stderr)
    sys.exit(1)
