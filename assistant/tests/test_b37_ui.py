"""B37: حدود التنقيح في التقارير، تأكيد التراجع غير الموثَّق (واجهة + متحكم)، إشعارات البدء، سلسلة التراجع الفارغة.
أجزاء tkinter تُتخطى بطباعة SKIP."""
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
    DATA = pathlib.Path(tempfile.mkdtemp(prefix="b37_data_")).resolve()
    os.environ["COS_DATA_DIR"] = str(DATA)

    from assistant.ui.controller import AssistantController, IDLE
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import ModelProvider
    from assistant.tools.files import UndoResult

    # ---------- 1) التنقيح بحدود كلمة ----------
    clean = AssistantController._clean_report_text
    for ok in ("risk-assessment-report-2024.docx", "disk-cleanup-plan", "task-manager-2024.pdf", "desk-" + "a" * 30,
               "notes sk- short", "mask-" + "b" * 25, "Bearer", "AKIA-short", "ghp_short"):
        assert clean(ok) == ok, (ok, clean(ok))
    secrets = ["sk" + "-ant-api03-" + "A" * 30, "sk" + "-proj-" + "B" * 30, "sk-" + "C" * 30, "AI" + "za" + "D" * 35,
               "gh" + "p_" + "e" * 36, "github" + "_pat_" + "f" * 40, "xox" + "b-" + "1234567890-abcdef", "AK" + "IA" + "G" * 16,
               "Bearer " + "h" * 30, "bearer " + "i" * 30 + "=="]
    for sec in secrets:
        out = clean(f"before {sec} after")
        assert out == "before [محجوب] after", (sec[:12], out)
    assert clean("risk-assessment.docx AND " + "sk-" + "Z" * 30) == "risk-assessment.docx AND [محجوب]"
    print("ok 1: redaction boundaries")

    NOQ = json.dumps({"questions": []})

    def st(i, a, t, **p):
        return {"step_id": i, "action_type": a, "target": t, "params": p}

    class Prov(ModelProvider):
        def __init__(self, fn):
            self.fn, self.n = fn, 0

        def name(self):
            return "b37"

        def generate(self, prompt, system_prompt=""):
            if "CLARIFY" in system_prompt:
                return NOQ
            self.n += 1
            return json.dumps({"steps": self.fn(self.n)})

    def mk(fn, files=("a.txt", "b.txt", "c.txt")):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="b37_")).resolve()
        ws = tmp / "ws"
        ws.mkdir()
        for f in files:
            (ws / f).write_text(f)
        led = SQLiteLedger(tmp / "l.db", initial_balance=100, spending_limit=1000)
        return AssistantController(Prov(fn), ws, led), ws

    def run(c, text="x"):
        c.submit_request(text)
        assert c.wait_for("plan", 5)
        c.start()
        assert c.wait_for("report", 10)
        end = time.time() + 5
        while c.state != IDLE and time.time() < end:
            time.sleep(0.01)

    ren = lambda n: [st(1, "rename", "abc"[n - 1] + ".txt", new_name="abc"[n - 1] + "2.txt")]

    # ---------- 2) تأكيد التراجع غير الموثَّق (مشغّل مزيّف) ----------
    c, ws = mk(ren)
    run(c)
    calls = []

    def fake_undo(tid, allow_unverified=False):
        calls.append(allow_unverified)
        if not allow_unverified:
            return UndoResult(0, remaining=1, reason="عملية غير موثَّقة", unverified=1)
        return UndoResult(1, remaining=0)
    c.runner.undo_task = fake_undo
    c.undo_last()
    ev = c.wait_for("undo_confirm", 3)
    assert ev and ev["unverified"] == 1 and ev["scope"] == "last" and "غير موثَّقة" in ev["reason"], ev
    assert calls == [False] and not [e for e in c.drain_events() if e["type"] in ("undo", "error")]
    c.undo_last(allow_unverified=True)
    u = c.wait_for("undo", 3)
    assert u and u["count"] == 1 and calls == [False, True], (u, calls)
    # سلسلة: نتوقف عند غير الموثَّق ثم نكمل بعد التأكيد
    c, ws = mk(ren)
    run(c, "نفس الطلب")
    run(c, "نفس الطلب")
    assert c.chain_size() == 2
    seen = []

    def fake_chain(tid, allow_unverified=False):
        seen.append(allow_unverified)
        fm = c.runner.file_manager
        if not allow_unverified:
            return UndoResult(0, remaining=1, reason="غير موثَّق", unverified=2)
        n = 0
        while fm.journal and fm.journal[-1].get("task") == tid:
            fm.undo_step()
            n += 1
        return UndoResult(n, remaining=0)
    c.runner.undo_task = fake_chain
    c.undo_chain()
    ev = c.wait_for("undo_confirm", 3)
    assert ev and ev["scope"] == "chain" and ev["unverified"] == 2 and c.chain_size() == 2, ev
    c.undo_chain(allow_unverified=True)
    u = c.wait_for("undo", 5)
    assert u and u["tasks"] == 2 and c.chain_size() == 0 and True in seen, (u, seen)
    print("ok 2: confirmation flow (fake runner)")

    # ---------- 2b) سجل مزروع حقيقي: لا تنفيذ صامت ويصل إشعار البدء ----------
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="b37_plant_")).resolve()
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "Report.docx").write_text("precious")
    (ws / ".undo_journal.json").write_text(json.dumps([{"action": "move", "src": "rel:Elsewhere.docx", "dest": "rel:Report.docx"}]))
    c = AssistantController(Prov(ren), ws, SQLiteLedger(tmp / "l.db", initial_balance=100, spending_limit=100))
    startup = [e for e in c.drain_events() if e["type"] == "info" and e.get("notice")]
    assert startup, "unverified/rejected journal entries must be announced at startup"
    c.undo_last()
    got = [e["type"] for e in c.drain_events() if e["type"] in ("undo", "undo_confirm", "error")]
    assert "undo" not in got, got   # لا تراجع صامت عن إدخال لا يمكن التحقق منه
    assert (ws / "Report.docx").read_text() == "precious" and not (ws / "Elsewhere.docx").exists() or "undo_confirm" in got, got
    c.undo_last(allow_unverified=True)   # لا استثناء
    print("ok 2b: planted journal not undone silently; startup notice shown")

    # ---------- 4) سلسلة غير فارغة وسجل فارغ ----------
    c, ws = mk(ren)
    c._chain = ["task_gone"]
    c.undo_chain()
    ev = c.wait_for("info", 3)
    assert ev and "لا يوجد ما يمكن التراجع" in ev["text"] and c.chain_size() == 0, ev
    c.undo_chain()
    evs = [x["type"] for x in c.drain_events()]
    assert "error" not in evs and "info" in evs, evs   # سلسلة منتهية: معلومة هادئة لا خطأ متكرر
    # نتيجة صفر بلا عائق تُنهي السلسلة (لا خطأ متكرر)
    c, ws = mk(ren)
    run(c, "نفس الطلب")
    c.runner.undo_task = lambda tid, allow_unverified=False: UndoResult(0, remaining=0)
    c.undo_chain()
    ev = c.wait_for("info", 3)
    assert ev and c.chain_size() == 0, ev
    print("ok 4: empty chain")

    # ---------- 3/2) الواجهة ----------
    try:
        import tkinter as tk
        from assistant.ui import widget as W
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: widget parts need tkinter ({type(exc).__name__}: {exc})")
        print("PASS: test_b37_ui completed successfully")
        sys.exit(0)
    try:
        probe = tk.Tk()
        probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: no display ({type(exc).__name__}: {exc})")
        print("PASS: test_b37_ui completed successfully")
        sys.exit(0)

    c, ws = mk(ren)
    rec = []
    c.undo_last = lambda allow_unverified=False: rec.append(("last", allow_unverified))
    c.undo_chain = lambda allow_unverified=False: rec.append(("chain", allow_unverified))
    root = tk.Tk()

    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            root.update()
            time.sleep(0.02)
    try:
        w = W.AssistantWidget(root, c)
        w.handle_event({"type": "undo_confirm", "unverified": 3, "reason": "سبب", "scope": "last"})
        win = w._undo_confirm_win
        pump(0.1)
        txt = w._undo_confirm_label.cget("text")
        assert "3 عملية سابقة لا يمكن التحقق من أصلها" in txt and "هل تريد التراجع عنها بعد فحص حالة الملفات الحالية؟" in txt, txt
        assert str(w._undo_confirm_yes.cget("state")) == "disabled", "Yes must start disabled"
        assert win.winfo_children()[-1].winfo_children(), "button frame must be the last child"
        pump(0.9)
        assert str(w._undo_confirm_yes.cget("state")) == "normal", "Yes enabled after ~700ms"
        w._undo_confirm_no.invoke()
        assert w._undo_confirm_win is None and rec == [], rec   # «لا» لا يتراجع
        w.handle_event({"type": "undo_confirm", "unverified": 1, "reason": "", "scope": "last"})
        pump(0.9)
        w._undo_confirm_yes.invoke()
        w.handle_event({"type": "undo_confirm", "unverified": 2, "reason": "", "scope": "chain"})
        pump(0.9)
        w._undo_confirm_yes.invoke()
        w.handle_event({"type": "undo_confirm", "unverified": 2, "reason": "", "scope": "chain"})
        w._close_undo_confirm()   # الإغلاق = لا
        assert rec == [("last", True), ("chain", True)], rec
        # إشعار البدء يظهر في المحادثة بوسم تنبيه
        w.handle_event({"type": "info", "text": "تُجوهل 2 إدخال في سجل التراجع غير صالح.", "notice": True})
        assert "تُجوهل 2 إدخال" in w.chat.get("1.0", "end")
    finally:
        root.destroy()
    print("ok 3: confirmation dialog")

    print("PASS: test_b37_ui completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b37_ui", file=sys.stderr)
    sys.exit(1)
