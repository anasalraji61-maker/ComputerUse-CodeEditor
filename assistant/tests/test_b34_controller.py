"""B34: سباقات المتحكم (تراجع/إيقاف/موافقة/إعلان) واجهة التقرير ونافذة الموافقة. اختبارات حتمية بالبوّابات لا بالنوم.
الأجزاء التي تحتاج tkinter تُتخطى بطباعة SKIP."""
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
    _DATA = tempfile.mkdtemp(prefix="b34_data_")
    os.environ["COS_DATA_DIR"] = _DATA   # set_workspace يحفظ الإعدادات: لا نلوّث مجلد المستخدم

    import assistant.ui.controller as C
    from assistant.ui.controller import AssistantController, IDLE, CLARIFYING, PLANNING, PLAN_READY
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import ModelProvider
    from assistant.tools.files import FileManager
    from assistant import config as cfgmod

    class Prov(ModelProvider):
        """script(kind, prompt) -> str ؛ kind: 'clarify' | 'plan'."""
        def __init__(self, script):
            self.script = script
            self.cancel_check = None

        def name(self):
            return "b34"

        def generate(self, prompt, system_prompt=""):
            return self.script("clarify" if "CLARIFY" in system_prompt else "plan", prompt)

    def st(i, a, t, **p):
        return {"step_id": i, "action_type": a, "target": t, "params": p}

    NOQ = json.dumps({"questions": []})

    def mk(script, files=("a.txt", "b.txt", "c.txt"), timeout=2.0, tmp=None):
        tmp = pathlib.Path(tmp or tempfile.mkdtemp(prefix="b34_")).resolve()
        ws = tmp / "ws"
        ws.mkdir(exist_ok=True)
        for f in files:
            (ws / f).write_text(f)
        led = SQLiteLedger(tmp / "l.db", initial_balance=100, spending_limit=100)
        c = AssistantController(Prov(script), ws, led, approval_timeout=timeout)
        c.threads = []
        orig_spawn = c._spawn

        def spawn(fn, *a):
            t = orig_spawn(fn, *a)
            c.threads.append(t)
            return t
        c._spawn = spawn
        return c, ws, tmp

    def join_all(c, t=5.0):
        for th in list(c.threads):
            th.join(t)

    def wait(cond, t=5.0):
        end = time.time() + t
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.005)
        return cond()

    def snap(ws):
        return sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if ".trash" not in p.parts and not p.name.startswith(".undo_journal"))

    def record_emits(c):
        """يسجّل (النوع، هل القفل مملوك للخيط الباعث) لكل حدث."""
        log = []
        orig = c.emit

        def emit(t, **d):
            log.append((t, c._lock._is_owned(), d))
            orig(t, **d)
        c.emit = emit
        return log

    def rename_script(new="a2.txt", old="a.txt"):
        return lambda k, p: json.dumps({"steps": [st(1, "rename", old, new_name=new)]}) if k == "plan" else NOQ

    def run_task(c, text="x"):
        c.submit_request(text)
        assert c.wait_for("plan"), "no plan"
        c.start()
        rep = c.wait_for("report", 10)
        assert rep, "no report"
        assert wait(lambda: c.state == IDLE)
        return rep

    # ---------- 1) تراجعان متزامنان (r01) ----------
    c, ws, _ = mk(rename_script())
    run_task(c)
    c.drain_events()
    bar = threading.Barrier(2)
    orig_undo_op = FileManager._undo_op

    def slow_op(self, op):
        r = orig_undo_op(self, op)
        try:
            bar.wait(0.4)   # لو تداخل الخيطان لاجتازا الحاجز معاً قبل سحب السجل
        except threading.BrokenBarrierError:
            pass
        return r
    FileManager._undo_op = slow_op
    errs = []

    def undo_t():
        try:
            c.undo_last()
        except BaseException as e:  # noqa: BLE001
            errs.append(repr(e))
    try:
        ths = [threading.Thread(target=undo_t) for _ in range(2)]
        [t.start() for t in ths]
        [t.join(10) for t in ths]
    finally:
        FileManager._undo_op = orig_undo_op
    assert not errs, errs
    evs = c.drain_events()
    assert [e["type"] for e in evs].count("undo") == 1, evs
    assert snap(ws) == ["a.txt", "b.txt", "c.txt"], snap(ws)
    print("ok 1: concurrent undo serialised")

    # ---------- 2) تراجع مقابل طلب جديد (r02) ----------
    plans = [[st(1, "rename", "a.txt", new_name="a2.txt")], [st(1, "rename", "b.txt", new_name="b2.txt")]]
    calls = {"n": 0}

    def script2(k, p):
        if k == "clarify":
            return NOQ
        i = calls["n"]
        calls["n"] += 1
        return json.dumps({"steps": plans[i]})
    c, ws, _ = mk(script2)
    run_task(c, "one")
    gate_in, gate_go = threading.Event(), threading.Event()

    def slow_op2(self, op):
        r = orig_undo_op(self, op)
        gate_in.set()
        gate_go.wait(5)
        return r
    FileManager._undo_op = slow_op2
    try:
        ut = threading.Thread(target=c.undo_last)
        ut.start()
        assert gate_in.wait(3)
        st_t = threading.Thread(target=lambda: c.submit_request("two"))   # يجب أن يُحجب/يُرفض حتى ينتهي التراجع
        st_t.start()
        time.sleep(0.15)
        assert not any(e["type"] == "plan" for e in c.drain_events()), "request ran during undo"
        gate_go.set()
        ut.join(10)
        st_t.join(10)
    finally:
        FileManager._undo_op = orig_undo_op
        gate_go.set()
    if c.wait_for("plan", 3):   # قُبل الطلب بعد انتهاء التراجع
        c.start()
        assert c.wait_for("report", 10)
        assert wait(lambda: c.state == IDLE)
    fm = FileManager(ws)
    fm.undo_all()
    assert snap(ws) == ["a.txt", "b.txt", "c.txt"], snap(ws)
    print("ok 2: undo vs new request")

    # ---------- 3) الأحداث تُعلَن داخل القفل: questions / plan / report / كلفة / workspace (r03, r05, r07, r09, r10) ----------
    c, ws, _ = mk(lambda k, p: json.dumps({"questions": ["أي مجلد؟"]}) if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
    log = record_emits(c)
    c.submit_request("x")
    assert c.wait_for("questions", 5)
    assert [o for t, o, _ in log if t == "questions"] == [True], "questions emitted outside lock"
    c.stop()

    class CostProv(Prov):
        n = 0

        def totals(self):
            return {"usd": 0.01 * self.n, "calls": self.n, "points": 1.0}

        def generate(self, prompt, system_prompt=""):
            self.n += 1
            return self.script("clarify" if "CLARIFY" in system_prompt else "plan", prompt)
    c, ws, _ = mk(lambda k, p: NOQ if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
    c.provider = c.runner.provider = c.runner.planner.provider = CostProv(c.provider.script)
    log = record_emits(c)
    c.submit_request("x")
    assert c.wait_for("plan", 5)
    assert wait(lambda: c.state == PLAN_READY)
    costs = [o for t, o, d in log if t == "status" and "كلفة" in d.get("text", "")]
    assert costs and all(costs), "cost status outside lock"
    assert [o for t, o, _ in log if t == "plan"] == [True]
    c.start()
    assert c.wait_for("report", 10)
    assert wait(lambda: c.state == IDLE)
    assert all(o for t, o, _ in log if t in ("report",)), "report outside lock"
    c2 = tempfile.mkdtemp(prefix="b34_ws2_")
    log = record_emits(c)
    assert c.set_workspace(c2)
    assert [o for t, o, _ in log if t == "workspace"] == [True]
    print("ok 3: events emitted under lock")

    # ---------- 4) حالة كلفة من عامل قديم بعد الإيقاف (r09) ----------
    go, inn = threading.Event(), threading.Event()

    class P2(CostProv):
        def generate(self, prompt, system_prompt=""):
            self.n += 1
            if "CLARIFY" in system_prompt:
                return NOQ
            inn.set()
            go.wait(5)
            return json.dumps({"steps": [st(1, "inspect", "a.txt")]})
    c, ws, _ = mk(lambda k, p: "")
    c.provider = c.runner.provider = c.runner.planner.provider = P2(lambda k, p: "")
    c.submit_request("x")
    assert inn.wait(3)
    c.stop()
    c.drain_events()
    go.set()
    join_all(c)
    evs = c.drain_events()
    assert not any(e["type"] in ("plan", "report", "error") or (e["type"] == "status" and "كلفة" in e.get("text", "")) for e in evs), evs
    assert c.state == IDLE
    print("ok 4: stale cost status suppressed")

    # ---------- 5) ملاحظة التخطيط القديمة لا تتسرب (r06) ----------
    slow_go, slow_in = threading.Event(), threading.Event()

    def script5(k, p):
        if k == "clarify":
            return NOQ
        if "old" in p:
            slow_in.set()
            slow_go.wait(5)
            return json.dumps({"steps": [st(1, "inspect", "a.txt")], "note": "NOTE-OF-OLD-REQUEST"})
        return json.dumps({"steps": [st(1, "inspect", "b.txt")], "note": None})
    c, ws, _ = mk(script5)
    c.submit_request("old")
    assert slow_in.wait(3)
    c.stop()
    old_threads = list(c.threads)
    orig_est = c._estimate
    g_in, g_go = threading.Event(), threading.Event()

    def est(*a, **k):
        r = orig_est(*a, **k)
        g_in.set()
        g_go.wait(5)
        return r
    c._estimate = est
    c.submit_request("new")
    assert g_in.wait(3)
    slow_go.set()
    for t in old_threads:
        t.join(5)   # العامل القديم عاد من المزوّد وكتب ملاحظته
    g_go.set()
    ev = c.wait_for("plan", 5)
    assert ev and ev["note"] is None, ev

    # المسار الذري يُفضَّل إن وُجد، وإلا الاحتياطي القديم
    import types
    c.runner = types.SimpleNamespace(make_plan=lambda t: ["P"], last_plan_note="LEGACY")
    assert c._plan_with_note("t") == (["P"], "LEGACY")
    c.runner = types.SimpleNamespace(make_plan=lambda t: ["P"], last_plan_note="LEGACY",
                                     make_plan_with_note=lambda t: (["Q"], "ATOMIC"))
    assert c._plan_with_note("t") == (["Q"], "ATOMIC")
    print("ok 5: plan note")

    # ---------- 6) «report» قبل «plan» التالية (r07) ----------
    c, ws, _ = mk(lambda k, p: NOQ if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
    c.submit_request("1")
    assert c.wait_for("plan")
    orig_emit = c.emit
    g_in, g_go = threading.Event(), threading.Event()

    def emit6(t, **d):
        if t == "report":
            g_in.set()
            g_go.wait(5)
        orig_emit(t, **d)
    c.emit = emit6
    c.start()
    assert g_in.wait(3)
    sub = threading.Thread(target=lambda: c.submit_request("2"))
    sub.start()
    time.sleep(0.1)
    g_go.set()
    sub.join(10)
    assert wait(lambda: c.state == PLAN_READY)
    types_seen = [e["type"] for e in c.drain_events() if e["type"] in ("plan", "report")]
    assert types_seen[-2:] == ["report", "plan"], types_seen
    print("ok 6: report precedes next plan")

    # ---------- 7) موافقة عند حدّ المهلة (r08) ----------
    tmo, go7 = threading.Event(), threading.Event()

    class Ev(threading.Event):
        def wait(self, timeout=None):
            r = super().wait(timeout)
            if not r:
                tmo.set()
                go7.wait(5)
            return r
    c, ws, _ = mk(lambda k, p: NOQ if k == "clarify" else json.dumps({"steps": [st(1, "delete", "a.txt")]}), timeout=0.3)
    c._approval_event = Ev()
    c.submit_request("x")
    assert c.wait_for("plan")
    c.start()
    req = c.wait_for("approval_request", 5)
    aid = req["step"]["approval_id"]
    assert tmo.wait(3)
    c.respond_approval(True, aid)
    go7.set()
    rep = c.wait_for("report", 10)
    assert rep and rep["success"], rep
    assert not (ws / "a.txt").exists(), "approved delete was skipped as timeout"
    print("ok 7: approval at timeout boundary")

    # ---------- 8) حمولة التوضيح (r04, r05) ----------
    for payload in ({"questions": "هل تريد المتابعة؟"}, {"questions": {"k": "v"}}, {"questions": None}):
        c, ws, _ = mk(lambda k, p, payload=payload: json.dumps(payload) if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
        c.submit_request("x")
        e = c.next_event(5)
        while e and e["type"] == "status":
            e = c.next_event(5)
        assert e and e["type"] == "plan", (payload, e)
    c, ws, _ = mk(lambda k, p: json.dumps({"questions": ["؟"], "slots": "bad"}) if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
    c.submit_request("x")
    q = c.wait_for("questions", 5)
    assert q and q["questions"] == ["؟"] and c._slots == [], (q, c._slots)
    for bad in (None, 5, {"a": 1}):
        c, ws, _ = mk(lambda k, p: json.dumps({"questions": ["؟"]}) if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
        c.submit_request("x")
        assert c.wait_for("questions", 5)
        c.answer_questions(bad)   # لا TypeError ولا تعليق في PLANNING
        assert c.wait_for("plan", 5), bad
        assert c.state == PLAN_READY
    c, ws, _ = mk(lambda k, p: json.dumps({"questions": ["؟"]}) if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
    c.submit_request("x")
    assert c.wait_for("questions", 5)
    c.answer_questions("نص مباشر")
    assert c.wait_for("plan", 5)
    print("ok 8: clarify payload types")

    # ---------- 9) set_workspace متزامن (r10) ----------
    c, ws, tmp = mk(lambda k, p: "")
    A, B = tmp / "A", tmp / "B"
    A.mkdir()
    B.mkdir()
    orig_save = C.save_config
    gA_in, gA_go = threading.Event(), threading.Event()

    def save(cfg):
        if cfg["workspace"] == str(A.resolve()):
            gA_in.set()
            gA_go.wait(5)
        orig_save(cfg)
    C.save_config = save
    try:
        t1 = threading.Thread(target=c.set_workspace, args=(str(A),))
        t1.start()
        assert gA_in.wait(3)
        t2 = threading.Thread(target=c.set_workspace, args=(str(B),))
        t2.start()
        time.sleep(0.15)
        gA_go.set()
        t1.join(10)
        t2.join(10)
    finally:
        C.save_config = orig_save
        gA_go.set()
    assert c.workspace.name == pathlib.Path(cfgmod.load_config()["workspace"]).name, (c.workspace, cfgmod.load_config())
    print("ok 9: set_workspace atomic")

    # ---------- 10) إلغاء إعادة محاولات مزوّد لخيط قديم رغم تصفير مفتاح الإيقاف (r11) ----------
    state = {"first": threading.Event(), "exited": threading.Event(), "n": 0}

    class P11(Prov):
        def generate(self, prompt, system_prompt=""):
            if "CLARIFY" in system_prompt:
                state["n"] += 1
                if state["n"] == 1:
                    state["first"].set()
                    end = time.time() + 4
                    while time.time() < end:
                        if self.cancel_check and self.cancel_check():
                            break
                        time.sleep(0.02)
                    state["exited"].set()
                    return NOQ
                return json.dumps({"questions": ["؟"]})
            return json.dumps({"steps": [st(1, "inspect", "a.txt")]})
    c, ws, _ = mk(lambda k, p: "")
    c.provider = c.runner.provider = c.runner.planner.provider = P11(lambda k, p: "")
    c._wire_cancel(c.provider)
    c.submit_request("1")
    assert state["first"].wait(2)
    c.stop()
    c.submit_request("2")   # يصفّر مفتاح الإيقاف المشترك
    assert state["exited"].wait(1.5), "stale worker's retry loop was un-cancelled"
    assert c.wait_for("questions", 5)
    assert not c._cancel_requested(), "main thread must not look cancelled"
    print("ok 10: per-generation cancel")

    # ---------- R1-1) قراءات الرصيد/الاستهلاك خارج القفل ----------
    class TotProv(Prov):
        def totals(self):
            owned.append(("totals", c._lock._is_owned()))
            return {"usd": 0.0, "calls": 1, "points": 1.0}
    owned = []
    c, ws, _ = mk(lambda k, p: NOQ if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
    c.provider = c.runner.provider = c.runner.planner.provider = TotProv(c.provider.script)
    orig_bal = c.ledger.get_balance

    def bal():
        owned.append(("balance", c._lock._is_owned()))
        return orig_bal()
    c.ledger.get_balance = bal
    c.submit_request("x")
    assert c.wait_for("plan", 5)
    owned.clear()
    c.start()
    assert c.wait_for("report", 10)
    assert wait(lambda: c.state == IDLE)
    assert owned and not any(o for _, o in owned), owned
    print("ok R1-1: balance/totals read outside lock")

    # ---------- R1-2) فحص السقف في خيط العامل لا خيط الواجهة ----------
    class Meter:
        def exceeded(self):
            return True

        def message(self):
            return "سقف الإنفاق اليومي"
    c, ws, _ = mk(lambda k, p: json.dumps({"questions": ["؟"]}) if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
    seen = []
    flag = {"on": False}

    def cap(text=""):
        seen.append((threading.current_thread() is threading.main_thread(), c._lock._is_owned()))
        return flag["on"]
    c._cap_exceeded = cap
    c.submit_request("x")
    assert c.wait_for("questions", 5)
    flag["on"] = True
    c.usage_meter = Meter()
    c.answer_questions(["y"])
    e = c.wait_for("error", 5)
    assert e and "سقف" in e["text"], e
    assert wait(lambda: c.state == IDLE)
    c.submit_request("z")
    e = c.wait_for("error", 5)
    assert e and "سقف" in e["text"], e
    assert wait(lambda: c.state == IDLE)
    assert seen and not any(m for m, _ in seen) and not any(o for _, o in seen), seen
    print("ok R1-2: cap check on worker")

    # ---------- R1-3) موت العامل لا يترك المتحكم عالقاً ----------
    class Fatal(BaseException):
        pass
    old_hook = threading.excepthook
    threading.excepthook = lambda args: None   # الاستثناء المعاد رفعه في الخيط لا يلوّث المخرجات
    try:
        for exc_cls in (Fatal, RuntimeError):
            def boom(k, p, exc_cls=exc_cls):
                if k == "clarify":
                    return NOQ
                raise exc_cls("planner died")
            c, ws, _ = mk(boom)
            c.runner.planner.provider = c.provider
            c.submit_request("x")
            e = c.wait_for("error", 5)
            assert e, exc_cls
            assert wait(lambda: c.state == IDLE), (exc_cls, c.state)
        # استثناء عام في بقية جسم التخطيط (بعد النجاح) أيضاً
        c, ws, _ = mk(lambda k, p: NOQ if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))

        def bad_est(*a, **k):
            raise Fatal("est")
        c._estimate = bad_est
        c.submit_request("x")
        assert c.wait_for("error", 5)
        assert wait(lambda: c.state == IDLE)
    finally:
        threading.excepthook = old_hook

    c, ws, _ = mk(lambda k, p: NOQ if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))

    def no_thread(*a):
        raise RuntimeError("can't start new thread")
    c._spawn = no_thread
    c.submit_request("x")
    e = c.wait_for("error", 5)
    assert e and "تعذّر" in e["text"] and c.state == IDLE, (e, c.state)
    c, ws, _ = mk(lambda k, p: NOQ if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
    c.submit_request("x")
    assert c.wait_for("plan", 5)
    c._spawn = no_thread
    c.start()
    e = c.wait_for("error", 5)
    assert e and "تعذّر" in e["text"] and c.state == IDLE, (e, c.state)
    print("ok R1-3: worker death recovers")

    # ---------- R1-6/7/8) لقطة answer_questions، stop خارج القفل، هدف التقرير سطر واحد ----------
    c, ws, _ = mk(lambda k, p: json.dumps({"questions": ["؟"]}) if k == "clarify" else json.dumps({"steps": [st(1, "inspect", "a.txt")]}))
    log = record_emits(c)
    c.submit_request("اطلب  \u2028شيئاً")
    assert c.wait_for("questions", 5)
    c.answer_questions(["الجواب"])
    assert c.wait_for("plan", 5)
    c.stop()   # PLAN_READY
    assert [o for t, o, d in log if t == "status" and "الإيقاف" in d.get("text", "")] == [False], "stop status emitted under lock"
    c.submit_request("a")
    assert c.wait_for("questions", 5)
    c.answer_questions(["b"])
    assert c.wait_for("plan", 5)
    c.start()
    rep = c.wait_for("report", 10)
    assert rep and "توضيحات المستخدم" in rep["text"], rep
    assert "\nتوضيحات" not in rep["text"], "objective spans lines in report header"
    print("ok R1-6/7/8: snapshot, stop outside lock, one-line objective")

    # ---------- 11) الواجهة (تحتاج tkinter) ----------
    try:
        import tkinter as tk
        from assistant.ui.widget import AssistantWidget, clean_line, escape_name, approval_detail
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: widget parts need tkinter ({type(exc).__name__}: {exc})")
        print("PASS: test_b34_controller completed successfully")
        sys.exit(0)

    # pretty_report: U+2028/U+0085 لا تصنع أقساماً مزوّرة (لا يحتاج شاشة)
    forged = "# تقرير\n- ملف a ## قسم مزوّر\u0085# عنوان مزوّر‮\n- ب\r\n## قسم حقيقي"
    out = AssistantWidget.pretty_report(forged)
    assert "▸ قسم مزوّر" not in out and "━━ عنوان مزوّر" not in out, out
    assert " " not in out and "\u0085" not in out and "‮" not in out
    assert out.splitlines()[0] == "━━ تقرير ━━" and "▸ قسم حقيقي" in out, out
    assert clean_line("a b\x00c‏d\te") == "a bcd e"
    esc = escape_name("evil‮txt.exe")
    assert "‮" not in esc and "\\u202e" in esc, esc
    long_esc = escape_name("n" * 1000 + ".exe")
    assert len(long_esc) <= 121 and long_esc.endswith(".exe"), long_esc
    d = approval_detail({"dest": "d" * 500, "cmd": ["c" * 500, "x"], "new_name": "n" * 500})
    assert all(len(l) < 330 for l in d.split("\n")) and "…" in d
    print("ok 11a: report/escape helpers")

    try:
        probe = tk.Tk()
        probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: no display for dialog test ({type(exc).__name__}: {exc})")
        print("PASS: test_b34_controller completed successfully")
        sys.exit(0)

    c, ws, _ = mk(lambda k, p: "")
    root = tk.Tk()
    try:
        w = AssistantWidget(root, c)
        step = {"step_id": 1, "action": "delete", "target": "x" * 3000 + "/" + "ab‮" + "cod.exe",
                "params": {"dest": "d" * 900, "cmd": ["c" * 900]}, "approval_id": 7}
        w._ask_approval(step)
        win = w._approval_win
        win.update()
        text = w._approval_label.cget("text")
        assert len(text) < 900, len(text)
        assert "‮" not in text and "\\u202e" in text, text
        assert int(w._approval_label.cget("wraplength")) > 0
        buttons = [x for x in win.winfo_children() if isinstance(x, tk.Frame)]
        assert win.winfo_children()[-1] is buttons[0], "button frame must stay the last child (b31 relies on it)"
        assert buttons, "button frame missing"
        win.update_idletasks()
        for b in buttons[0].winfo_children():
            assert b.winfo_ismapped(), "button not visible"
            assert b.winfo_rooty() + b.winfo_height() <= win.winfo_rooty() + win.winfo_height() + 1, "button clipped"
        w._close_approval()
        # الموضع يُقيَّد بحدود الشاشة حتى لو كان الجذر على الحافة
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("+%d+%d" % (sw - 30, sh - 30))
        root.update()
        w._ask_approval(step)
        win = w._approval_win
        win.update()
        assert win.winfo_x() + win.winfo_reqwidth() <= sw + 1 and win.winfo_y() + win.winfo_reqheight() <= sh + 1, (
            win.winfo_x(), win.winfo_y(), win.winfo_reqwidth(), win.winfo_reqheight(), sw, sh)
        w._close_approval()
    finally:
        root.destroy()
    print("ok 11b: approval dialog")

    print("PASS: test_b34_controller completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b34_controller", file=sys.stderr)
    sys.exit(1)
