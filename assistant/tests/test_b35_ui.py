"""B35: حارس النسخة الواحدة، DPI، سجل الأعطال، run_assistant.bat، رسائل النجاح الجزئي/التراجع الجزئي،
عزل الاتجاه، اختصارات لوحة المفاتيح، قصّ الشاشات المتعددة. أجزاء tkinter تُتخطى بطباعة SKIP."""
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
    DATA = pathlib.Path(tempfile.mkdtemp(prefix="b35_data_"))
    os.environ["COS_DATA_DIR"] = str(DATA)

    import assistant.app as app
    from assistant.ui.controller import AssistantController, IDLE, PLAN_READY
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import ModelProvider

    # ---------- 1) النسخة الواحدة ----------
    k1 = app.instance_key("/a/data")
    assert k1 == app.instance_key("/a/data") and len(k1) == 16
    assert k1 != app.instance_key("/b/data")   # المفتاح من مجلد البيانات وحده (لا مجلد العمل)
    lock_dir = pathlib.Path(tempfile.mkdtemp(prefix="b35_lock_"))
    # الأقفال الحقيقية في هذا النظام (flock على يونكس، msvcrt/ملف pid على ويندوز): الثانية في العملية نفسها تُرفض
    assert app.acquire_instance_lock("k_test", lock_dir, use_mutex=False) is True
    assert app.acquire_instance_lock("k_test", lock_dir, use_mutex=False) is False, "second instance must be refused"
    assert app.acquire_instance_lock("k_other", lock_dir, use_mutex=False) is True

    shown = []
    real_show = app.show_message
    app.show_message = lambda msg, flags=0: shown.append((msg, flags))
    hooks = (sys.excepthook, threading.excepthook)
    got = {}
    try:
        rc = app.main(lock_fn=lambda key, d: got.update(key=key, d=d) or False)
    finally:
        app.show_message = real_show
        sys.excepthook, threading.excepthook = hooks
    assert rc == 0 and shown and "يعمل بالفعل" in shown[0][0] and shown[0][1] == app.MB_ICONWARNING, (rc, shown)
    assert got["key"] == app.instance_key(got["d"]) and len(got["key"]) == 16
    assert app.MB_RTL == 0x180000
    print("ok 1: single instance guard")

    # ---------- 2) DPI / سجل الأعطال / stderr=None ----------
    dpi = app.enable_dpi_awareness()
    assert isinstance(dpi, bool) and (dpi is False or os.name == "nt")   # على غير ويندوز دائماً False
    app.write_crash_log("hello crash")
    assert "hello crash" in (DATA / "crash.log").read_text(encoding="utf-8")
    old_err = sys.stderr
    shown.clear()
    app.show_message = lambda msg, flags=0: shown.append(msg)
    sys.stderr = None
    try:
        app.report_fatal("fatal-without-stderr")   # لا يرفع
    finally:
        sys.stderr = old_err
        app.show_message = real_show
    assert shown == ["fatal-without-stderr"]
    assert "fatal-without-stderr" in (DATA / "crash.log").read_text(encoding="utf-8")
    print("ok 2: dpi/crash log")

    # ---------- 3) run_assistant.bat ----------
    bat = (ROOT / "run_assistant.bat").read_bytes()
    bat.decode("ascii")
    assert b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b"")
    txt = bat.decode("ascii")
    assert 'start "" pythonw -m assistant' in txt and 'start "" pyw -m assistant' in txt and "python -m assistant" in txt
    assert txt.index("pythonw") < txt.index("pyw -m") < txt.rindex("python -m assistant")
    print("ok 3: run_assistant.bat")

    # ---------- 4) التراجع الجزئي/الفاشل في المتحكم ----------
    class Prov(ModelProvider):
        def __init__(self, steps):
            self.steps = steps

        def name(self):
            return "b35"

        def generate(self, prompt, system_prompt=""):
            return json.dumps({"questions": []}) if "CLARIFY" in system_prompt else json.dumps({"steps": self.steps})

    def st(i, a, t, **p):
        return {"step_id": i, "action_type": a, "target": t, "params": p}

    def mk(steps, files=("a.txt", "b.txt", "c.txt")):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="b35_")).resolve()
        ws = tmp / "ws"
        ws.mkdir()
        for f in files:
            (ws / f).write_text(f)
        led = SQLiteLedger(tmp / "l.db", initial_balance=100, spending_limit=100)
        return AssistantController(Prov(steps), ws, led), ws

    def run(c):
        c.submit_request("x")
        assert c.wait_for("plan", 5)
        c.start()
        rep = c.wait_for("report", 10)
        assert rep
        end = time.time() + 5
        while c.state != IDLE and time.time() < end:
            time.sleep(0.01)
        return rep

    two = [st(1, "rename", "a.txt", new_name="a2.txt"), st(2, "rename", "b.txt", new_name="b2.txt")]
    c, ws = mk(two)
    rep = run(c)
    assert rep["executed"] == 2 and rep["failed"] == 0 and rep["skipped"] == 0, rep
    n0 = len(c.runner.file_manager.journal)
    assert n0 == 2
    # فشل كامل: لا شيء استُعيد وبقيت العمليات
    real_undo = c.runner.undo_task
    c.runner.undo_task = lambda tid: 0
    c.undo_last()
    e = c.wait_for("error", 3)
    assert e and "تعذّر التراجع" in e["text"] and "2" in e["text"], e
    # جزئي: استُعيدت واحدة وبقيت واحدة
    fm = c.runner.file_manager
    c.runner.undo_task = lambda tid: (fm.undo_step(), 1)[1]
    c.undo_last()
    u = c.wait_for("undo", 3)
    assert u and u["count"] == 1 and u["remaining"] == 1 and u["partial"] is True and u["skipped"] == 0, u
    # تخطٍّ: أُزيلت عملية من السجل دون استعادة
    c.runner.undo_task = real_undo
    (ws / "a.txt").exists() or None
    c2, ws2 = mk(two)
    run(c2)
    fm2 = c2.runner.file_manager
    (ws2 / "b2.txt").unlink()   # ناتج العملية الأخيرة اختفى: تُخطّى
    c2.undo_last()
    u2 = c2.wait_for("undo", 3)
    assert u2 and u2["count"] == 1 and u2["skipped"] == 1 and u2["partial"] is True and u2["remaining"] == 0, u2
    # نجاح كامل
    c3, ws3 = mk(two)
    run(c3)
    c3.undo_last()
    u3 = c3.wait_for("undo", 3)
    assert u3 and u3["count"] == 2 and u3["partial"] is False and u3["remaining"] == 0, u3
    print("ok 4: controller undo wording data")


    # ---------- R4: القفل (يعمل على ويندوز ويونكس؛ ويحاكي ويندوز على يونكس) ----------
    import atexit
    import contextlib
    import errno
    import types
    import assistant.tools.files as _files
    try:
        import fcntl as real_fcntl
    except ImportError:
        real_fcntl = None
    _MISSING = object()

    @contextlib.contextmanager
    def mods(blocked=(), **fakes):
        """يحجب وحدات (import يرفع ImportError) أو يستبدلها بمزيّفة، ثم يعيد sys.modules كما كان."""
        names = list(blocked) + list(fakes)
        saved = {n: sys.modules.get(n, _MISSING) for n in names}
        for n in blocked:
            sys.modules[n] = None
        for n, m in fakes.items():
            sys.modules[n] = m
        try:
            yield
        finally:
            for n, v in saved.items():
                if v is _MISSING:
                    sys.modules.pop(n, None)
                else:
                    sys.modules[n] = v

    # أ) أخطاء flock غير التنازعية لا تمنع التشغيل (يونكس فقط)
    if real_fcntl is not None:
        d3 = pathlib.Path(tempfile.mkdtemp(prefix="b35_lock3_"))
        real_flock = real_fcntl.flock

        def flock_err(code):
            def f(fd, op):
                raise OSError(code, "x")
            return f
        try:
            real_fcntl.flock = flock_err(errno.ENOLCK)   # نظام ملفات لا يدعم القفل: نبدأ بلا قفل
            assert app.acquire_instance_lock("k_enolck", d3, use_mutex=False) is True
            real_fcntl.flock = flock_err(errno.EAGAIN)
            assert app.acquire_instance_lock("k_eagain", d3, use_mutex=False) is False
            real_fcntl.flock = flock_err(errno.EACCES)
            assert app.acquire_instance_lock("k_eacces", d3, use_mutex=False) is False
        finally:
            real_fcntl.flock = real_flock
    # ب) مجلد قفل لا يمكن إنشاؤه (تحته ملف عادي): لا نمنع التشغيل
    blocker = pathlib.Path(tempfile.mkdtemp(prefix="b35_blk_")) / "plainfile"
    blocker.write_text("x")
    assert app.acquire_instance_lock("k_bad", blocker / "sub", use_mutex=False) is True

    # ج) ويندوز بلا msvcrt/fcntl (ملف «pid:بصمة»): مُحاكى بدوال العمليات المزيّفة
    alive = {os.getpid(), 4242}
    starts = {os.getpid(): ("win:own", 0.0), 4242: ("win:111", 0.0)}
    real_win, real_pw, real_ps = _files.IS_WINDOWS, _files._pid_alive_windows, _files._proc_start_windows
    real_kill = os.kill
    _files.IS_WINDOWS = True
    _files._pid_alive_windows = lambda pid: int(pid) in alive
    _files._proc_start_windows = lambda pid: starts.get(int(pid))
    os.kill = lambda *a: (_ for _ in ()).throw(AssertionError("os.kill must not be called on Windows"))
    d4 = pathlib.Path(tempfile.mkdtemp(prefix="b35_lock4_"))
    lf = d4 / "instance_kp.lock"
    try:
        with mods(blocked=("fcntl", "msvcrt")):
            assert app._pid_alive(4242) is True and app._pid_alive(7777) is False
            me = os.getpid()
            assert app.acquire_instance_lock("kp", d4, use_mutex=False) is True
            assert lf.read_text().startswith(f"{me}:win:own"), lf.read_text()
            # الثانية في العملية نفسها (القفل بيدنا ورقمنا حيّ) = نسخة ثانية تُرفض
            assert app.acquire_instance_lock("kp", d4, use_mutex=False) is False
            app._release_pid_file(str(lf), me)   # ما يفعله atexit
            assert not lf.exists()
            assert app.acquire_instance_lock("kp", d4, use_mutex=False) is True   # بعد الإفراج يُقبل
            app._release_pid_file(str(lf), me)
            # عملية أخرى حيّة ببصمتها = تمنع؛ ببصمة مختلفة (رقم أُعيد استعماله) أو ميتة أو تالف = يتيم
            lf.write_text("4242:win:111")
            assert app.acquire_instance_lock("kp", d4, use_mutex=False) is False
            lf.write_text("4242")   # قفل قديم بلا بصمة: الرقم الحيّ يكفي
            assert app.acquire_instance_lock("kp", d4, use_mutex=False) is False
            for stale in ("4242:win:999", "7777:win:1", "", "999999999999999999999", "-1", "0", "abc", "9" * 5000):
                lf.write_text(stale)
                assert app.acquire_instance_lock("kp", d4, use_mutex=False) is True, repr(stale[:12])
                assert lf.read_text().startswith(f"{me}:"), lf.read_text()[:30]
                app._release_pid_file(str(lf), me)
                assert not lf.exists()
    finally:
        os.kill = real_kill
        _files.IS_WINDOWS, _files._pid_alive_windows, _files._proc_start_windows = real_win, real_pw, real_ps
        atexit.unregister(app._release_pid_file)
        app._held_paths.clear()

    # د) msvcrt.locking على أول بايت: قفل نظام حقيقي (مُحاكى على يونكس بـ flock على مقبض مستقل)
    if real_fcntl is not None:
        opened = []

        def fake_locking(fd, mode, nbytes):
            if mode == fake_msvcrt.LK_UNLCK:
                real_fcntl.flock(fd, real_fcntl.LOCK_UN)
                return
            opened.append(fd)
            try:
                real_fcntl.flock(fd, real_fcntl.LOCK_EX | real_fcntl.LOCK_NB)
            except BlockingIOError:
                raise OSError(errno.EACCES, "locked") from None
        fake_msvcrt = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=fake_locking)
        d5 = pathlib.Path(tempfile.mkdtemp(prefix="b35_lock5_"))
        try:
            with mods(blocked=("fcntl",), msvcrt=fake_msvcrt):
                assert app.acquire_instance_lock("km", d5, use_mutex=False) is True
                assert app.acquire_instance_lock("km", d5, use_mutex=False) is False   # سجل العملية
                app._held_paths.clear()   # كأنها عملية ثانية: لا يبقى إلا قفل النظام نفسه
                assert app.acquire_instance_lock("km", d5, use_mutex=False) is False
                # فشل غير تنازعي في القفل: لا نمنع المستخدم
                fake_msvcrt.locking = lambda fd, mode, n: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
                assert app.acquire_instance_lock("km2", d5, use_mutex=False) is True
        finally:
            atexit.unregister(app._release_msvcrt)
            app._held_paths.clear()
            for fd in opened:
                try:
                    os.close(fd)
                except OSError:
                    pass
    # هـ) لا os.kill على ويندوز (يُنهي العملية هناك)
    real_kill, real_win, real_pw = os.kill, _files.IS_WINDOWS, _files._pid_alive_windows
    _files.IS_WINDOWS = True
    _files._pid_alive_windows = lambda pid: True
    os.kill = lambda *a: (_ for _ in ()).throw(AssertionError("os.kill must not be called on Windows"))
    try:
        assert app._pid_alive(4242) is True
    finally:
        os.kill, _files.IS_WINDOWS, _files._pid_alive_windows = real_kill, real_win, real_pw
    print("ok R4-2/3/6: lock edge cases")

    # ---------- R4: فشل استيراد assistant.app يصل crash.log ولا يمرّ بصمت ----------
    import subprocess
    cwd = pathlib.Path(tempfile.mkdtemp(prefix="b35_cwd_"))
    dd = pathlib.Path(tempfile.mkdtemp(prefix="b35_dd_"))
    code = ("import sys; sys.path.insert(0, %r); sys.modules['assistant.app'] = None; import runpy; "
            "runpy.run_module('assistant', run_name='__main__')") % str(ROOT)
    r = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env={**os.environ, "COS_DATA_DIR": str(dd), "COS_NO_DIALOG": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 1 and "فشل تحميل البرنامج" in (dd / "crash.log").read_text(encoding="utf-8"), (r.returncode, r.stderr[-300:])
    for fname in ("launch.py",):
        code2 = ("import sys; sys.path.insert(0, %r); sys.modules['assistant.app'] = None; import runpy; "
                 "runpy.run_path(%r, run_name='__main__')") % (str(ROOT), str(ROOT / fname))
        r2 = subprocess.run([sys.executable, "-c", code2], cwd=str(cwd), env={**os.environ, "COS_DATA_DIR": str(dd), "COS_NO_DIALOG": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        assert r2.returncode == 1 and (dd / "crash.log").read_text(encoding="utf-8").count("فشل تحميل البرنامج") >= 2
    print("ok R4-5: import failure is reported")

    # ---------- R4: إشعارات التعافي ----------
    from assistant.models.demo import DemoProvider
    base = pathlib.Path(tempfile.mkdtemp(prefix="b35_rec_")).resolve()
    (base / "data").mkdir()
    (base / "ws").mkdir()
    (base / "data" / "ledger.db").write_bytes(b"garbage" * 100)
    (base / "data" / "usage.json").write_text("{not json")
    (base / "ws" / ".undo_journal.json").write_text("{corrupt")
    os.environ["COS_DATA_DIR"], os.environ["COS_WORKSPACE"] = str(base / "data"), str(base / "ws")
    try:
        ctl, notice = app.build_controller(DemoProvider())
    finally:
        os.environ["COS_DATA_DIR"] = str(DATA)
        os.environ.pop("COS_WORKSPACE", None)
    assert ctl.ledger.recovery_message and ctl.ledger.recovery_message in notice, notice
    infos = [e for e in ctl.drain_events() if e["type"] == "info" and e.get("notice")]
    assert len(infos) >= 2, infos   # تالف سجل التراجع + عدّاد الاستهلاك
    assert ctl.runner.file_manager.undo_notes, "startup undo notes must not be cleared"
    print("ok R4-8: recovery notices")

    # ---------- R4: undo يعرض السبب ولا يرفع ----------
    from assistant.tools.files import UndoResult
    c, ws = mk(two)
    run(c)
    c.runner.undo_task = lambda tid: UndoResult(0, remaining=2, reason="عملية أحدث تعترض الطريق")
    c.undo_last()
    e = c.wait_for("error", 3)
    assert e and "عملية أحدث تعترض الطريق" in e["text"] and "2" in e["text"], e
    c.runner.undo_task = lambda tid: UndoResult(1, remaining=1, reason="السبب س")
    c.undo_last()
    u = c.wait_for("undo", 3)
    assert u and u["reason"] == "السبب س" and u["partial"] and u["remaining"] == 1, u

    def raiser(tid):
        raise RuntimeError("مشغول")
    c.runner.undo_task = raiser
    c.undo_last()   # لا يرفع
    e = c.wait_for("error", 3)
    assert e and "RuntimeError" in e["text"], e
    print("ok R4-8b: undo robust + reason")

    # ---------- 5) أدوات الواجهة (tkinter) ----------
    try:
        import tkinter as tk
        from assistant.ui import widget as W
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: widget parts need tkinter ({type(exc).__name__}: {exc})")
        print("PASS: test_b35_ui completed successfully")
        sys.exit(0)

    # عجلة اللمس: خطوة واحدة على الأقل بإشارة صحيحة
    assert W.wheel_units(120) == -1 and W.wheel_units(-120) == 1 and W.wheel_units(240) == -2
    assert W.wheel_units(5) == -1 and W.wheel_units(-5) == 1 and W.wheel_units(0, 5) == 1 and W.wheel_units(0, 4) == -1
    # اختصارات Ctrl بالرمز الفيزيائي: العربي فقط (اللاتيني يعالجه Tk)
    assert W.keycode_action(86, "Arabic_ra") == "paste" and W.keycode_action(67, "x") is None
    assert W.keycode_action(65, "Arabic_sheen") == "select_all" and W.keycode_action(88, "Arabic_hamza") == "cut"
    assert W.keycode_action(67, "Arabic_tah") == "copy" and W.keycode_action(70, "Arabic_x") is None
    # القصّ على شاشة بإحداثيات سالبة (شاشة يسار الرئيسية) وعلى شاشة ثانوية
    assert W.clamp_rect(-1900, 50, 400, 300, (-1920, 0, 0, 1080)) == (-1900, 50)
    assert W.clamp_rect(-100, 900, 400, 300, (-1920, 0, 0, 1080)) == (-400, 780)
    assert W.clamp_rect(10, 10, 400, 300, (1920, 0, 3840, 1080)) == (1920, 10)
    assert W.clamp_rect(0, 0, 5000, 5000, (0, 0, 800, 600)) == (0, 0)
    # العزل ثنائي الاتجاه بعد التنظيف؛ والمدخل لا يستطيع تزوير عوازل
    iso = W.isolate("a.txt")
    assert iso == "\u2068a.txt\u2069" and W.strip_isolates(iso) == "a.txt"
    assert W.clean_line("x\u2068y\u2069z") == "xyz"
    line = W.isolate_names("نُقل report_2024.pdf إلى C:\\Users\\x\\Docs")
    assert "\u2068report_2024.pdf\u2069" in line and line.startswith("\u2068") and line.endswith("\u2069"), line
    assert W.strip_isolates(line) == "نُقل report_2024.pdf إلى C:\\Users\\x\\Docs"
    rep_txt = "# تقرير\n- نقل a.txt\n\n## ما فشل:"
    assert W.AssistantWidget.pretty_report(rep_txt) == "━━ تقرير ━━\n  • نقل a.txt\n\n▸ ما فشل:"   # الافتراضي خام
    shown_txt = W.AssistantWidget.pretty_report(rep_txt, True)
    assert "\u2068a.txt\u2069" in shown_txt and W.strip_isolates(shown_txt) == W.AssistantWidget.pretty_report(rep_txt)
    assert W.step_detail({"dest": "x"}) == "← x"
    assert W.step_detail({"dest": "x\u202ey"}, True) == "← \u2068xy\u2069"
    # رسالة النتيجة: نجاح كامل / جزئي / فشل بالأعداد
    om = W.AssistantWidget.outcome_message
    assert om({"success": True, "executed": 2, "failed": 0, "skipped": 0}) == ("تم بنجاح ✔", "ok")
    m, tag = om({"success": True, "executed": 2, "failed": 0, "skipped": 4})
    assert "جزئياً" in m and "تُخطّي 4" in m and "نُفّذ 2" in m and tag == "warn", m
    m, tag = om({"success": False, "executed": 1, "failed": 1, "skipped": 0})
    assert "فشلت" in m and "فشل 1" in m and tag == "error"
    assert om({"success": False}) == ("فشلت المهمة ✖ (راجع التقرير أعلاه)", "error")
    assert om({"success": False, "executed": 0, "failed": 0, "skipped": 3}) == ("لم يُنفَّذ شيء — رفضت كل الخطوات", "warn")
    assert om({"success": True, "executed": 0, "failed": 0, "skipped": 3})[0].startswith("لم يُنفَّذ شيء")
    # مفتاح إيقاف العزل
    try:
        os.environ["COS_NO_BIDI_ISOLATES"] = "1"
        W.reset_bidi_cache()
        assert W.isolate("a.txt") == "a.txt" and W.isolate_names("نقل a.txt") == "نقل a.txt"
        del os.environ["COS_NO_BIDI_ISOLATES"]
        W.reset_bidi_cache()
        assert W.isolate("a") == "\u2068a\u2069"
        (DATA / "config.json").write_text(json.dumps({"bidi_isolates": False}), encoding="utf-8")
        W.reset_bidi_cache()
        assert W.isolate("a") == "a"
    finally:
        os.environ.pop("COS_NO_BIDI_ISOLATES", None)
        (DATA / "config.json").unlink()
        W.reset_bidi_cache()
    assert W.isolate("a") == "\u2068a\u2069"
    # سجل الأعطال بلا عوازل
    app.write_crash_log("x \u2068secret.txt\u2069 y")
    assert "\u2068" not in (DATA / "crash.log").read_text(encoding="utf-8")
    print("ok 5a: widget helpers")

    try:
        probe = tk.Tk()
        probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: no display ({type(exc).__name__}: {exc})")
        print("PASS: test_b35_ui completed successfully")
        sys.exit(0)

    c, ws = mk([st(1, "inspect", "a.txt")])
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 2.0)   # 144 dpi -> معامل 1.5
        w = W.AssistantWidget(root, c)
        assert abs(w.scale - 1.5) < 0.01, w.scale
        assert w.plan_wrap == int(round(W.PLAN_WRAP * w.scale)) and abs(w.px(100) - 150) <= 1 and w.plan_wrap > W.PLAN_WRAP
        root.tk.call("tk", "scaling", 20.0)
        assert W.ui_scale(root) == 2.5   # سقف
        root.tk.call("tk", "scaling", 0.5)
        assert W.ui_scale(root) == 1.0   # حد أدنى
        # خطأ في معالج Tk: crash.log + سطر عربي في المحادثة
        assert root.report_callback_exception == w._on_callback_exception
        try:
            raise ValueError("boom-b35")
        except ValueError:
            w._on_callback_exception(*sys.exc_info())
        assert "boom-b35" in (DATA / "crash.log").read_text(encoding="utf-8")
        assert "حدث خطأ غير متوقع في الواجهة" in w.chat.get("1.0", "end")
        old = sys.stderr
        sys.stderr = None
        try:
            w._on_callback_exception(KeyError, KeyError("k"), None)   # stderr=None لا يكسر
        finally:
            sys.stderr = old
        # نتيجة جزئية في الدردشة
        w.handle_event({"type": "report", "success": True, "text": "# تقرير\n- نقل a.txt", "executed": 2, "failed": 0, "skipped": 4})
        txt = w.chat.get("1.0", "end")
        assert "اكتملت جزئياً" in txt and "تم بنجاح" not in txt, txt
        w.handle_event({"type": "undo", "count": 1, "remaining": 2, "skipped": 0, "partial": True})
        assert "تم التراجع جزئياً" in w.chat.get("1.0", "end")
        w.handle_event({"type": "undo", "count": 2, "remaining": 0, "skipped": 0, "partial": False})
        assert "تم التراجع عن 2 عملية" in w.chat.get("1.0", "end")
        # صف الخطة: الهدف معزول بعد التنظيف
        w._show_plan([{"step_id": 1, "action": "move", "target": "تقرير\u202e.pdf", "level": "REVERSIBLE",
                       "params": {"dest": "Docs"}}])
        row_text = w._plan_rows[1].cget("text")
        assert "\u2068تقرير.pdf\u2069" in row_text and "\u202e" not in row_text and "\u2068Docs\u2069" in row_text, row_text
        # نافذة الموافقة: الهدف معزول، والقصّ ضمن حدود الشاشة الافتراضية
        w._ask_approval({"step_id": 1, "action": "delete", "target": "dir/ملف a.txt", "params": {}, "approval_id": 3})
        lbl = w._approval_label.cget("text")
        assert "\u2068dir/ملف a.txt\u2069" in lbl and "\u2068ملف a.txt\u2069" in lbl, lbl
        w._close_approval()
        l, t, r, b = W.monitor_bounds(root)
        assert r > l and b > t
        # تسمية مجلد العمل معزولة؛ النسخ من المحادثة بلا عوازل
        assert "\u2068" in w.ws_lbl.cget("text") and W.strip_isolates(w.ws_lbl.cget("text")).endswith(str(c.workspace)[-30:])
        w.handle_event({"type": "report", "success": True, "text": "# تقرير\n- نقل report.pdf", "executed": 1, "failed": 0, "skipped": 0})
        assert "\u2068report.pdf\u2069" in w.chat.get("1.0", "end")
        w.chat.tag_add("sel", "1.0", "end")
        w.chat.event_generate("<<Copy>>")
        root.update()
        clip = root.clipboard_get()
        assert "report.pdf" in clip and not any(ch in clip for ch in "\u2066\u2067\u2068\u2069"), repr(clip)
        # صف خطوة متخطاة رماديّ لا أخضر
        w._show_plan([{"step_id": 1, "action": "delete", "target": "x.txt", "level": "DANGEROUS", "params": {}},
                      {"step_id": 2, "action": "inspect", "target": "y.txt", "level": "READ", "params": {}}])
        w.handle_event({"type": "step_done", "index": 1, "total": 2, "step_id": 1, "ok": True, "detail": "تخطّي بقرارك"})
        w.handle_event({"type": "step_done", "index": 2, "total": 2, "step_id": 2, "ok": True, "detail": "ok"})
        assert w._plan_rows[1].cget("bg") == "#e0e0e0" and w._plan_rows[2].cget("bg") == "#c8e6c9"
        # إشعار بدء لا يغيّر الحالة
        w.set_status("قبل")
        w.handle_event({"type": "info", "text": "سجل التراجع كان تالفاً", "notice": True})
        assert w.status.cget("text") == "قبل" and "سجل التراجع كان تالفاً" in w.chat.get("1.0", "end")
        assert w._poll_after and w._alive
        root.update()
    finally:
        root.destroy()
    # إغلاق نظيف بعد اختبار خطأ المعالج: لا يبقى أمر after (_poll) معلقاً ("invalid command name ..._poll")
    assert not w._alive and w._poll_after is None
    # النافذة لا تتجاوز الشاشة عند DPI مرتفع
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 216 / 72.0)   # 225%
        w2 = W.AssistantWidget(root, c)
        root.update()
        sh = root.winfo_screenheight()
        mw, mh = root.minsize()
        assert mh <= sh and mw <= root.winfo_screenwidth(), (mw, mh, sh)
        assert root.winfo_height() <= sh and root.winfo_reqheight() <= sh * 2
        assert w2._poll_after and w2._alive
    finally:
        root.destroy()
    assert not w2._alive and w2._poll_after is None
    # اختصارات Ctrl بالرمز: AltGr (Ctrl+Alt) لا يخطف الحرف
    root = tk.Tk()
    real_plat = sys.platform
    try:
        sys.platform = "win32"
        w3 = W.AssistantWidget(root, c)
        sys.platform = real_plat
        root.update()
        ent = w3.entry
        ent.focus_force()
        root.update()
        ent.delete(0, "end")
        ent.insert(0, "hello")
        # القرار نفسه دالة خالصة (Tk في ويندوز لا يولّد keysym عشوائياً: "no keycode for keysym") فنختبرها مباشرة بأحداث وهمية
        for plat, alt_bit in (("win32", 0x20000), ("linux", 0x8)):
            assert W.keycode_shortcut(0x4 | alt_bit, 65, "aogonek", plat) is None, plat       # AltGr (Ctrl+Alt) لا يخطف الحرف
            assert W.keycode_shortcut(0x4, 65, "Arabic_sheen", plat) == "select_all", plat    # Ctrl+A بتخطيط عربي
            assert W.keycode_shortcut(0x4, 86, "Arabic_ra", plat) == "paste", plat
            assert W.keycode_shortcut(0x4, 67, "Arabic_hamzaonalef", plat) == "copy", plat
            assert W.keycode_shortcut(0x4, 88, "Arabic_hamza", plat) == "cut", plat
            assert W.keycode_shortcut(0x4, 65, "a", plat) is None, plat                        # اللاتيني يعالجه Tk
            assert W.keycode_shortcut(0x4, 70, "Arabic_beh", plat) is None, plat               # مفتاح غير مدعوم
        assert W.keycode_shortcut(0x4 | 0x8, 65, "Arabic_sheen", "win32") == "select_all"      # NumLock على ويندوز ليس Alt
        assert W.keycode_shortcut(0x4 | 0x20000, 65, "Arabic_sheen", "linux") == "select_all"  # بت ويندوز بلا معنى في X11
        assert W.keycode_shortcut(None, "bad", "x", "win32") is None                           # مدخلات تالفة لا ترمي
        assert W.keycode_shortcut(0x4, 65, "Arabic_sheen") == W.keycode_shortcut(0x4, 65, "Arabic_sheen", real_plat)
        # الرابط الفعلي يستدعي الدالة: حدث حقيقي عبر Tk إن أمكن توليده على هذه المنصة (ويندوز يرفض بعض الـkeysyms)
        for keysym, state, expect_sel in (("aogonek", 0x4 | (0x20000 if real_plat == "win32" else 0x8), False),
                                          ("Arabic_sheen", 0x4, True)):
            ent.select_clear()
            try:
                ent.event_generate("<KeyPress>", keysym=keysym, keycode=65, state=state)
            except tk.TclError as exc:
                print(f"NOTE: event_generate keysym={keysym} not supported on this platform ({exc}); covered by the pure-function checks")
                continue
            root.update()
            assert bool(ent.selection_present()) == expect_sel, (keysym, expect_sel)
    finally:
        sys.platform = real_plat
        root.destroy()
    print("ok 5b: widget with display")

    print("PASS: test_b35_ui completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b35_ui", file=sys.stderr)
    sys.exit(1)
