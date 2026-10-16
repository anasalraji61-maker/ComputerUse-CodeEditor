"""B36: أول تشغيل/حدود الواجهة: شحن تجريبي قبل الفحص، رسالة خطأ المزوّد بلا بادئة مكررة، حفظ التقارير، تراجع عن السلسلة،
مجلد فارغ، الوثائق. أجزاء tkinter تُتخطى بطباعة SKIP."""
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
    DATA = pathlib.Path(tempfile.mkdtemp(prefix="b36_data_")).resolve()
    os.environ["COS_DATA_DIR"] = str(DATA)

    import assistant.app as app
    from assistant.ui.controller import AssistantController, IDLE
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import ModelProvider
    from assistant.models.http_base import ProviderError

    NOQ = json.dumps({"questions": []})

    def st(i, a, t, **p):
        return {"step_id": i, "action_type": a, "target": t, "params": p}

    class Prov(ModelProvider):
        def __init__(self, plan_fn):
            self.plan_fn = plan_fn
            self.n = 0

        def name(self):
            return "b36"

        def generate(self, prompt, system_prompt=""):
            if "CLARIFY" in system_prompt:
                return NOQ
            self.n += 1
            return self.plan_fn(self.n, prompt)

    def mk(plan_fn, files=("a.txt", "b.txt", "c.txt"), balance=100, reports=True):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="b36_")).resolve()
        ws = tmp / "ws"
        ws.mkdir()
        for f in files:
            (ws / f).write_text(f, encoding="utf-8")
        led = SQLiteLedger(tmp / "l.db", initial_balance=balance, spending_limit=1000)
        c = AssistantController(Prov(plan_fn), ws, led, reports_dir=(tmp / "reports") if reports else None)
        return c, ws, tmp

    def settle(c):
        end = time.time() + 5
        while c.state != IDLE and time.time() < end:
            time.sleep(0.01)

    def run(c, text):
        c.submit_request(text)
        assert c.wait_for("plan", 5)
        c.start()
        rep = c.wait_for("report", 10)
        assert rep
        settle(c)
        return rep

    # ---------- 3) الشحن التجريبي قبل الفحص ----------
    plan1 = lambda n, p: json.dumps({"steps": [st(1, "inspect", "a.txt")]})
    c, ws, tmp = mk(plan1)
    calls = []
    real = getattr(c.ledger, "ensure_trial_balance", None)
    c.ledger.ensure_trial_balance = lambda *a, **k: calls.append(1) or False   # مزيّف
    c.submit_request("x")
    assert c.wait_for("plan", 5) and len(calls) >= 1, calls
    c.start()
    assert c.wait_for("report", 10) and len(calls) >= 2, calls
    # غياب الدالة لا يكسر المتحكم
    c, ws, tmp = mk(plan1)
    c.ledger = type("L", (), {"estimate_cost": lambda self, *a, **k: 1, "get_balance": lambda self: 5,
                              "can_afford": lambda self, p: True})()
    assert c._ensure_trial() is None
    # دفتر حقيقي برصيد منخفض يُشحن قبل التقدير
    if real is not None:
        c, ws, tmp = mk(plan1, balance=5)
        c.submit_request("x")
        ev = c.wait_for("plan", 5)
        assert ev and ev["estimate"]["balance"] >= 100, ev["estimate"]
    print("ok 3: trial balance wired")

    # ---------- 4) رسالة الخطأ من المزوّد كما هي ----------
    msg403 = "ممنوع من خدمة Gemini (رمز 403): صلاحيات المفتاح لا تسمح بهذا الطلب، أو قد يكون البلد غير مدعوم."

    def boom(exc):
        def fn(n, p):
            raise exc
        return fn
    c, ws, tmp = mk(boom(ProviderError(msg403)))
    c.submit_request("x")
    e = c.wait_for("error", 5)
    assert e and e["text"] == msg403, e
    c, ws, tmp = mk(boom(RuntimeError("انهار شيء")))
    c.submit_request("x")
    e = c.wait_for("error", 5)
    assert e and e["text"].startswith("تعذّر التخطيط: ") and e["text"].count("تعذّر") == 1, e
    from assistant.planner import PlanParseError
    c, ws, tmp = mk(boom(PlanParseError("تعذّر قراءة رد النموذج.")))
    c.submit_request("x")
    e = c.wait_for("error", 5)
    assert e and e["text"] == "تعذّر قراءة رد النموذج.", e
    print("ok 4: provider error text")

    # ---------- 6) حفظ التقارير ----------
    c, ws, tmp = mk(plan1)
    rep = run(c, "x")
    saved = tmp / "reports" / f"{rep['task_id']}.txt"
    assert saved.is_file() and saved.read_text(encoding="utf-8") == rep["text"]
    rows = c.list_reports()
    assert rows and rows[0]["task_id"] == rep["task_id"] and c.read_report(rep["task_id"]) == rep["text"]
    c._save_report("../evil/../x", "a ⁨b.txt⁩ \x00 " + "sk" + "-ant-api03-" + "ABCDEFGHIJKLMNOPQRSTUV" + " AI" + "zaSy" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 z")
    names = [p.name for p in (tmp / "reports").iterdir()]
    assert not any("/" in n or ".." in n for n in names) and all((tmp / "reports" / n).parent == tmp / "reports" for n in names), names
    txt = c.read_report("../evil/../x")
    assert txt and "⁨" not in txt and "\x00" not in txt and "sk-ant" not in txt and "AIza" not in txt and "[محجوب]" in txt, txt
    assert c.read_report("nope") is None
    c, ws, tmp = mk(plan1, reports=False)   # الاختبارات بلا reports_dir: لا كتابة
    run(c, "x")
    assert c.list_reports() == [] and not (tmp / "reports").exists()
    # build_controller يمرّر مجلد التقارير
    base = pathlib.Path(tempfile.mkdtemp(prefix="b36_app_")).resolve()
    os.environ["COS_DATA_DIR"], os.environ["COS_WORKSPACE"] = str(base / "d"), str(base / "w")
    try:
        from assistant.models.demo import DemoProvider
        ctl, _ = app.build_controller(DemoProvider())
        assert pathlib.Path(ctl.reports_dir) == base / "d" / "reports"
    finally:
        os.environ["COS_DATA_DIR"] = str(DATA)
        os.environ.pop("COS_WORKSPACE", None)
    print("ok 6: reports persisted")

    # ---------- 2) تراجع عن السلسلة ----------
    plans = {1: [st(1, "rename", "a.txt", new_name="a2.txt")], 2: [st(1, "rename", "b.txt", new_name="b2.txt")],
             3: [st(1, "rename", "c.txt", new_name="c2.txt")]}
    c, ws, tmp = mk(lambda n, p: json.dumps({"steps": plans[n]}))
    run(c, "رتّب  الملفات")
    run(c, "رتّب الملفات")   # الطلب نفسه (مسافات مختلفة) = إكمال
    assert c.chain_size() == 2
    run(c, "طلب آخر")        # طلب مختلف يبدأ سلسلة جديدة
    assert c.chain_size() == 1
    c.undo_chain()
    u = c.wait_for("undo", 5)
    assert u and u["tasks"] == 1 and u["count"] == 1 and c.chain_size() == 0, u
    assert (ws / "c.txt").exists() and (ws / "a2.txt").exists()   # السلسلة السابقة لم تُمس
    c.undo_chain()
    assert c.wait_for("info", 3)
    c.undo_last()
    assert c.wait_for("undo", 3)
    c.undo_last()
    assert c.wait_for("undo", 3)
    assert sorted(p.name for p in ws.iterdir() if not p.name.startswith(".")) == ["a.txt", "b.txt", "c.txt"]
    c, ws, tmp = mk(lambda n, p: json.dumps({"steps": plans[n]}))
    run(c, "نفس الطلب")
    run(c, "نفس الطلب")
    c.undo_chain()
    u = c.wait_for("undo", 5)
    assert u and u["tasks"] == 2 and u["count"] == 2 and not u["partial"], u
    assert sorted(p.name for p in ws.iterdir() if not p.name.startswith(".")) == ["a.txt", "b.txt", "c.txt"]
    print("ok 2: undo chain")

    # ---------- 5) الوثائق ----------
    ug = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    for needle in ("Python 3.10", "tkinter", ".trash", "~/.cos_assistant", "COS_WORKSPACE", "COS_DATA_DIR", "crash.log",
                   "2 جيجابايت", "5 جيجابايت", "SmartScreen", "GEMINI_API_KEY", "صور", "تراجع عن الكل", "reports"):
        assert needle in ug, needle
    assert "لآخر مهمة فقط" not in ug
    rd = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("Python 3.10", "run_assistant.bat", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "USER_GUIDE", "BENCH_HOWTO", "SPEC_PHASE1", "TASKS"):
        assert needle in rd, needle
    assert "ensure_trial_balance" in (ROOT / "docs" / "SPEC_PHASE1.md").read_text(encoding="utf-8")
    ai = (ROOT / "docs" / "AI_STUDIO_PROMPT.md").read_text(encoding="utf-8")
    assert "ComputerUse-CodeEditor" in ai and "WindowsComputerUse" in ai and "ComputerUse-CodeEditor" in rd
    print("ok 5: docs")

    # ---------- أجوبة التوضيح: طلب جديد كامل، خانة path، وأسئلة متسلسلة ----------
    from assistant.models.rules import RuleProvider

    def mkr(files):
        t = pathlib.Path(tempfile.mkdtemp(prefix="b36_r_")).resolve()
        w_ = t / "ws"
        w_.mkdir()
        for f in files:
            (w_ / f).parent.mkdir(parents=True, exist_ok=True)
            (w_ / f).write_text("x", encoding="utf-8")
        return AssistantController(RuleProvider(), w_, SQLiteLedger(t / "l.db", initial_balance=100, spending_limit=100)), w_

    def first(c, kinds, t=8):
        end = time.time() + t
        while time.time() < end:
            e = c.next_event(0.3)
            if e and e["type"] in kinds:
                return e
        return None

    steps_of = lambda e: [(x["action"], x["target"]) for x in e["steps"]]
    # جواب هو طلب جديد كامل يحلّ محل الطلب (حتى لو الخانة معروفة)
    c, w_ = mkr(["a.txt"])
    c.submit_request("انقل a.txt")
    assert first(c, ("questions",)) and c._slots == ["dest"]
    c.answer_questions(["احذف a.txt"])
    e = first(c, ("plan", "questions", "error"))
    assert e and e["type"] == "plan" and steps_of(e) == [("delete", "a.txt")], e
    assert c._request == "احذف a.txt"
    c, w_ = mkr(["a.txt", "b.jpg"])
    c.submit_request("انقل a.txt")
    first(c, ("questions",))
    c.answer_questions(["بل رتّب الملفات حسب النوع"])
    e = first(c, ("plan", "questions", "error"))
    assert e and e["type"] == "plan" and ("move", "b.jpg") in steps_of(e), e
    # خانة path: المسار الكامل يحلّ محل الاسم الملتبس
    c, w_ = mkr(["a.txt", "Docs/a.txt", "Pics/a.txt"])
    c.submit_request("احذف a.txt")
    q = first(c, ("questions",))
    assert q and c._slots == ["path"], (q, c._slots)
    c.answer_questions(["Docs/a.txt"])
    e = first(c, ("plan", "questions", "error"))
    assert e and e["type"] == "plan" and steps_of(e) == [("delete", "Docs/a.txt")], e
    # أسئلة متسلسلة: جواب يولّد سؤالاً ثانياً عادياً (لا «تعذّر فهم الخطة») ثم يكتمل
    c, w_ = mkr(["a.txt", "b.txt"])
    c.submit_request("انقل a.txt")
    first(c, ("questions",))
    c.answer_questions(["a.txt"])   # «a.txt» اسم ملف لا مجلد: سؤال ثانٍ
    q2 = first(c, ("questions", "error", "plan"))
    assert q2 and q2["type"] == "questions" and "مجلد" in q2["questions"][0] and len(q2["questions"]) <= 3, q2
    assert c.state == "CLARIFYING" and c._slots == ["dest"] and c._request == "انقل a.txt إلى a.txt", (c.state, c._slots, c._request)
    c.answer_questions(["Docs"])
    e = first(c, ("plan", "questions", "error"))
    assert e and e["type"] == "plan" and ("move", "a.txt") in steps_of(e), e
    # حد الجولات: عند آخر جولة يُخطَّط مباشرة ولا يُسأل سؤال جديد
    import assistant.ui.controller as _C
    c, w_ = mkr(["a.txt", "b.txt"])
    c.submit_request("انقل")
    first(c, ("questions",))
    c._clarify_round = _C.MAX_CLARIFY_ROUNDS - 1
    c.answer_questions(["a.txt"])
    e = first(c, ("questions", "plan", "error", "info"))
    assert e and e["type"] != "questions", e
    # سؤال نموذج (بلا خانات) بعد جواب لا يُدخل المحادثة في حلقة
    class AlwaysAsk(ModelProvider):
        def name(self):
            return "ask"

        def generate(self, prompt, system_prompt=""):
            return json.dumps({"questions": ["؟"]}) if "CLARIFY" in system_prompt else json.dumps({"steps": [st(1, "inspect", "a.txt")]})
    t = pathlib.Path(tempfile.mkdtemp(prefix="b36_q_")).resolve()
    (t / "ws").mkdir()
    (t / "ws" / "a.txt").write_text("x", encoding="utf-8")
    c = AssistantController(AlwaysAsk(), t / "ws", SQLiteLedger(t / "l.db", initial_balance=100, spending_limit=100))
    c.submit_request("x")
    assert first(c, ("questions",))
    c.answer_questions(["y"])
    e = first(c, ("plan", "questions", "error"))
    assert e and e["type"] == "plan", e
    print("ok 8: clarification merge/chain")

    # ---------- واجهة ----------
    try:
        import tkinter as tk
        from assistant.ui import widget as W
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: widget parts need tkinter ({type(exc).__name__}: {exc})")
        print("PASS: test_b36_ui completed successfully")
        sys.exit(0)
    try:
        probe = tk.Tk()
        probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: no display ({type(exc).__name__}: {exc})")
        print("PASS: test_b36_ui completed successfully")
        sys.exit(0)

    emp = pathlib.Path(tempfile.mkdtemp(prefix="b36_empty_"))
    assert W.workspace_is_empty(emp) is True
    (emp / ".trash").mkdir()
    (emp / ".undo_journal.json").write_text("[]", encoding="utf-8")
    assert W.workspace_is_empty(emp) is True   # المخفي لا يُحسب
    (emp / "f.txt").write_text("x", encoding="utf-8")
    assert W.workspace_is_empty(emp) is False

    c, ws, tmp = mk(plan1, files=())
    root = tk.Tk()
    try:
        w = W.AssistantWidget(root, c)
        chat = w.chat.get("1.0", "end")
        assert W.EMPTY_WORKSPACE_HINT in chat and str(ws)[-25:] in W.strip_isolates(chat), chat
        assert w.ws_open_btn.cget("text") == "فتح المجلد" and w.undo_all_btn.cget("text") == "تراجع عن الكل"
        assert w.reports_btn.cget("text") == "التقارير"
        # فتح المجلد: لا استثناء حتى لو لا توجد أداة فتح
        import subprocess
        real_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(OSError("no xdg-open"))
        try:
            w._open_workspace()
        finally:
            subprocess.Popen = real_popen
        # حدث مجلد جديد فارغ يعرض التلميح
        w.handle_event({"type": "workspace", "path": str(emp / "nonexistent_zzz")})
        # التقارير: بلا تقارير ثم مع تقرير
        w._on_reports()
        assert "لا توجد تقارير" in w.chat.get("1.0", "end")
        run(c, "x")
        w._on_reports()
        win = w._reports_win
        lb = [x for x in win.winfo_children() if isinstance(x, tk.Listbox)][0]
        assert lb.size() == 1
        lb.selection_set(0)
        win.update()
        lb.focus_force()
        win.update()
        lb.event_generate("<Return>")
        root.update()
        assert "تقرير المهمة" in w.chat.get("1.0", "end"), w.chat.get("1.0", "end")[-300:]
        # تراجع عن الكل: بلا سلسلة رسالة هادئة؛ مع سلسلة يطلب تأكيداً
        from tkinter import messagebox
        asked = []
        real_ask = messagebox.askyesno
        messagebox.askyesno = lambda *a, **k: asked.append(a) or False
        try:
            w._on_undo_all()
            assert "لا توجد سلسلة" in w.chat.get("1.0", "end") or c.chain_size() == 0
        finally:
            messagebox.askyesno = real_ask
    finally:
        root.destroy()

    c, ws, tmp = mk(lambda n, p: json.dumps({"steps": [st(1, "rename", f"{'ab'[n-1]}.txt", new_name=f"{'ab'[n-1]}2.txt")]}),
                    files=("a.txt", "b.txt"))
    run(c, "نفس الطلب")
    run(c, "نفس الطلب")
    root = tk.Tk()
    try:
        w = W.AssistantWidget(root, c)
        assert W.EMPTY_WORKSPACE_HINT not in w.chat.get("1.0", "end")
        from tkinter import messagebox
        asked = []
        real_ask = messagebox.askyesno
        messagebox.askyesno = lambda *a, **k: asked.append(a) or True
        try:
            w._on_undo_all()
        finally:
            messagebox.askyesno = real_ask
        assert asked and "مهمتين" in asked[0][1]
        end = time.time() + 5
        while time.time() < end and "في 2 مهمة" not in w.chat.get("1.0", "end"):
            for ev in c.drain_events():
                w.handle_event(ev)
            root.update()
            time.sleep(0.02)
        assert "تم التراجع عن عمليتين في مهمتين" in w.chat.get("1.0", "end")
    finally:
        root.destroy()
    print("ok 7: widget")

    print("PASS: test_b36_ui completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b36_ui", file=sys.stderr)
    sys.exit(1)
