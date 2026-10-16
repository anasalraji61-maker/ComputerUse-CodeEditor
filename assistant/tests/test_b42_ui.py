"""B42: طلب تراجع مكتوب في الدردشة = زر «تراجع» نفسه (بما فيه تأكيد غير الموثَّق)، ولا ينفّذ شيئاً آخر. بلا tkinter."""
import json
import os
import pathlib
import sys
import tempfile
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    DATA = pathlib.Path(tempfile.mkdtemp(prefix="b42_data_")).resolve()
    os.environ["COS_DATA_DIR"] = str(DATA)

    from assistant.ui.controller import AssistantController, IDLE, PLAN_READY, wants_undo
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import ModelProvider
    from assistant.tools.files import UndoResult

    NOQ = json.dumps({"questions": []})

    def st(i, a, t, **p):
        return {"step_id": i, "action_type": a, "target": t, "params": p}

    class Prov(ModelProvider):
        def __init__(self, fn):
            self.fn, self.n = fn, 0

        def name(self):
            return "b42"

        def generate(self, prompt, system_prompt=""):
            if "CLARIFY" in system_prompt:
                return NOQ
            self.n += 1
            return json.dumps({"steps": self.fn(self.n)})

    def mk(fn, files=("a.txt", "b.txt", "temp.tmp")):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="b42_")).resolve()
        ws = tmp / "ws"
        ws.mkdir()
        for f in files:
            (ws / f).write_text(f, encoding="utf-8")
        led = SQLiteLedger(tmp / "l.db", initial_balance=100, spending_limit=1000)
        return AssistantController(Prov(fn), ws, led), ws

    def settle(c):
        end = time.time() + 5
        while c.state != IDLE and time.time() < end:
            time.sleep(0.01)
        assert c.state == IDLE

    def run(c, text="x"):
        c.submit_request(text)
        assert c.wait_for("plan", 5)
        c.start()
        seen, end = [], time.time() + 10
        while time.time() < end and "report" not in seen:   # الحذف يطلب موافقة: نوافق عليها
            for e in c.drain_events():
                seen.append(e["type"])
                if e["type"] == "approval_request":
                    c.respond_approval(True, e["step"]["approval_id"])
            time.sleep(0.02)
        assert "report" in seen, seen
        settle(c)

    ren = lambda n: [st(1, "rename", "a.txt", new_name="a2.txt")]

    # ---------- 1) الكاشف ----------
    for yes in ("تراجع", "تراجع عن آخر عملية", "ارجع الملفات اللي حذفتها", "undo", "استرجع الملفات المحذوفة"):
        assert wants_undo(yes), yes
    for no in ("", "احذف temp.tmp", "افتح سلة المحذوفات", "اعرض سلة المحذوفات", "open the recycle bin",
               "نظّم الملفات ثم تراجع عن الباقي وارجع ما حذفته من المجلد القديم وأعد تسمية كل الصور في المجلد",
               "غيّر اسم a.txt إلى b.txt"):
        assert not wants_undo(no), no
    print("ok 1: wants_undo detector")

    # ---------- 2) تراجع حقيقي مكتوب ----------
    c, ws = mk(ren)
    run(c, "غيّر اسم a.txt إلى a2.txt")
    assert (ws / "a2.txt").exists() and not (ws / "a.txt").exists()
    c.drain_events()
    c.submit_request("تراجع")
    u = c.wait_for("undo", 5)
    assert u and u["count"] >= 1, u
    assert (ws / "a.txt").exists() and not (ws / "a2.txt").exists()
    evs = c.drain_events()
    assert not [e for e in evs if e["type"] in ("plan", "status", "error")], evs   # لا تخطيط ولا طلب للنموذج
    assert c.state == IDLE and c.provider.n == 1, c.provider.n
    print("ok 2: typed undo restores, no plan")

    # ---------- 3) صياغة بالعامية ----------
    c, ws = mk(lambda n: [st(1, "delete", "temp.tmp")])
    run(c, "احذف temp.tmp")
    assert not (ws / "temp.tmp").exists()
    c.drain_events()
    c.submit_request("ارجع الملفات اللي حذفتها")
    u = c.wait_for("undo", 5)
    assert u and (ws / "temp.tmp").exists(), u
    assert c.provider.n == 1 and not [e for e in c.drain_events() if e["type"] == "plan"]
    print("ok 3: colloquial undo")

    # ---------- 4) تأكيد غير الموثَّق عبر الدردشة ----------
    c, ws = mk(lambda n: [st(1, "rename", "a.txt", new_name="a2.txt")])
    run(c)
    calls = []

    def fake_undo(tid, allow_unverified=False):
        calls.append(allow_unverified)
        if not allow_unverified:
            return UndoResult(0, remaining=1, reason="عملية غير موثَّقة", unverified=1)
        return UndoResult(1, remaining=0)
    c.runner.undo_task = fake_undo
    c.drain_events()
    c.submit_request("تراجع")
    ev = c.wait_for("undo_confirm", 3)
    assert ev and ev["unverified"] == 1 and ev["scope"] == "last", ev
    assert calls == [False], calls   # لا تنفيذ بلا تأكيد
    assert not [e for e in c.drain_events() if e["type"] in ("undo", "plan")]
    print("ok 4: unverified confirmation via chat")

    # ---------- 5) عمل آخر لا يتحول إلى تراجع، والانشغال يعطي رسالة التراجع ----------
    c, ws = mk(lambda n: [st(1, "delete", "temp.tmp")])
    c.submit_request("احذف temp.tmp")
    assert c.wait_for("plan", 5) and c.state == PLAN_READY
    c.drain_events()
    c.submit_request("تراجع")   # خطة جاهزة: ليس IDLE
    e = c.wait_for("error", 3)
    assert e and "لا يمكن التراجع الآن" in e["text"], e
    assert c.state == PLAN_READY and (ws / "temp.tmp").exists()
    c.cancel_plan() if hasattr(c, "cancel_plan") else None
    print("ok 5: busy state gives undo-busy message, plan untouched")

    c, ws = mk(lambda n: [st(1, "delete", "temp.tmp")])
    c.submit_request("افتح سلة المحذوفات")
    c.wait_for("plan", 3)
    settle_ev = c.drain_events()
    assert not [e for e in settle_ev if e["type"] in ("undo", "undo_confirm")], settle_ev   # لم يُعامل كتراجع
    print("ok 6: open-trash request is not an undo")
    print("PASS test_b42_ui")
except Exception:
    traceback.print_exc()
    print("FAIL test_b42_ui")
    sys.exit(1)
