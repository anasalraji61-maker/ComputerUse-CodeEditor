"""B40: لا تسرّب لمفتاح الخدمة في أي رسالة/حدث/سجل (حتى عند فشل التحقق المبكر)، مسار crash.log الاحتياطي، mutex ويندوز بأنواع صريحة،
علامات RLM للعرض فقط، وتدقيق النصوص العربية في الواجهة/المتحكم/الإعدادات. أجزاء tkinter تُتخطى بطباعة SKIP."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import traceback
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    DATA = pathlib.Path(tempfile.mkdtemp(prefix="b40_data_")).resolve()
    os.environ["COS_DATA_DIR"] = str(DATA)
    for k in ("COS_PROVIDER", "COS_SMALL_MODEL", "COS_LARGE_MODEL", "COS_MAX_USD_PER_DAY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
              "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "COS_OPENAI_API_KEY", "COS_OPENAI_BASE_URL", "COS_NO_BIDI_ISOLATES"):
        os.environ.pop(k, None)

    from assistant import app
    from assistant.ui import settings as S
    from assistant.ui import controller as C
    from assistant.ui.controller import AssistantController, IDLE
    from assistant.ledger import SQLiteLedger
    from assistant.usage import UsageMeter
    from assistant.models.demo import DemoProvider
    from assistant.models.http_base import ProviderError
    import assistant.models.factory as factory

    GKEY = "AI" + "za" + "W" * 35

    def mk():
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="b40_")).resolve()
        ws = tmp / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("a", encoding="utf-8")
        led = SQLiteLedger(tmp / "l.db", initial_balance=100, spending_limit=1000)
        return AssistantController(DemoProvider(), ws, led, usage_meter=UsageMeter(tmp / "usage.json", daily_cap_usd=5.0)), ws, tmp

    def all_output(c, tmp):
        """كل ما يخرج: الأحداث + آخر رسالة إعدادات + ملفات مجلد البيانات والمؤقت."""
        evs = c.drain_events()
        blob = json.dumps(evs, ensure_ascii=False) + c.last_settings_message
        for base in (DATA, tmp):
            for f in base.rglob("*"):
                if f.is_file():
                    blob += f.read_bytes().decode("utf-8", "replace")
        return blob

    # ---------- 1) المفتاح لا يظهر في أي مخرج ----------
    c, ws, tmp = mk()
    real_build, real_norm = factory.build_provider, S.normalize
    q = urllib.parse.quote(GKEY, safe="")
    try:
        # أ) المصنع يعيد المفتاح في رسالة خطئه
        factory.build_provider = lambda env: (_ for _ in ()).throw(ProviderError(f"رُفض {GKEY} وكذلك {q}"))
        assert c.apply_settings({"provider": "gemini", "api_key": "  " + GKEY + " "}) is False
        out = all_output(c, tmp)
        assert GKEY not in out and q not in out and "***" in c.last_settings_message, c.last_settings_message
        # ب) التحقق المبكر نفسه يعيد المفتاح (قبل أن يُعتمد المفتاح في أي قاموس)
        factory.build_provider = real_build
        S.normalize = lambda v: (_ for _ in ()).throw(S.SettingsError("مفتاح سيئ: " + GKEY))
        assert c.apply_settings({"provider": "gemini", "api_key": GKEY}) is False
        assert GKEY not in all_output(c, tmp) and "***" in c.last_settings_message
        # ج) استثناء غير متوقع في التحقق: لا اسم صنف ولا مفتاح للمستخدم؛ التفاصيل في crash.log بلا المفتاح
        S.normalize = lambda v: (_ for _ in ()).throw(KeyError(GKEY))
        assert c.apply_settings({"provider": "gemini", "api_key": GKEY}) is False
        assert c.last_settings_message == S.MSG_APPLY_FAILED and "KeyError" not in c.last_settings_message
        out = all_output(c, tmp)
        assert GKEY not in out and "KeyError" in (DATA / "crash.log").read_text(encoding="utf-8")
        S.normalize = real_norm
        # د) فشل تجهيز المشغّل بعد بناء المزوّد (فرع التراجع) والاستثناء يحوي المفتاح: ذرّية وبلا تسرب
        old_provider, old_runner = c.provider, c.runner
        real_make = c._make_runner
        c._make_runner = lambda root: (_ for _ in ()).throw(RuntimeError("boom " + GKEY))
        assert c.apply_settings({"provider": "gemini", "api_key": GKEY}) is False
        assert c.provider is old_provider and c.runner is old_runner and not c.current_settings()["has_session_key"]
        c._make_runner = real_make
        out = all_output(c, tmp)
        assert GKEY not in out and "RuntimeError" not in c.last_settings_message
        # هـ) نجاح ثم أي حدث لاحق يحوي المفتاح يُحجب عند الإصدار، وكذلك crash.log
        assert c.apply_settings({"provider": "gemini", "api_key": GKEY}) is True
        c.drain_events()
        c.emit("error", text="echo " + GKEY + " and " + q)
        assert GKEY not in json.dumps(c.drain_events())
        app.write_crash_log("traceback with " + GKEY)
        c._log_exc("اختبار", ValueError(GKEY))
        assert GKEY not in (DATA / "crash.log").read_text(encoding="utf-8")
        # و) فشل فحص الاتصال لا يعيد المفتاح
        c.provider.fallback.providers["small"].transport = lambda *a: (401, ("bad " + GKEY).encode())
        c.provider.fallback.providers["large"].transport = c.provider.fallback.providers["small"].transport
        c.check_connection()
        ev = c.wait_for("ping", 10)
        assert ev and GKEY not in json.dumps(ev, ensure_ascii=False)
        assert GKEY not in all_output(c, tmp)
    finally:
        factory.build_provider, S.normalize = real_build, real_norm
    # المفاتيح القصيرة لا تُقبل ولا تُستعمل في الحجب (لا يُتلف نص عادي)
    try:
        S.validate_key("abc")
        raise AssertionError("short key accepted")
    except S.SettingsError:
        pass
    assert S.redact("hello abc world", ["abc"]) == "hello abc world"
    print("ok 1: key never leaks")

    # ---------- 2) مسار crash.log الاحتياطي: الملفات المؤقتة لا المجلد الحالي ----------
    cwd = pathlib.Path(tempfile.mkdtemp(prefix="b40_cwd_")).resolve()
    tmpd = pathlib.Path(tempfile.mkdtemp(prefix="b40_tmp_")).resolve()
    blocker = cwd / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")    # COS_DATA_DIR تحت ملف عادي: makedirs يفشل
    envx = {**os.environ, "COS_DATA_DIR": str(blocker / "data"), "COS_NO_DIALOG": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
            "TMPDIR": str(tmpd), "TEMP": str(tmpd), "TMP": str(tmpd)}
    codes = [("import sys; sys.path.insert(0, %r); sys.modules['assistant.app'] = None; import runpy; "
              "runpy.run_module('assistant', run_name='__main__')") % str(ROOT),
             ("import sys; sys.path.insert(0, %r); sys.modules['assistant.app'] = None; import runpy; "
              "runpy.run_path(%r, run_name='__main__')") % (str(ROOT), str(ROOT / "launch.py"))]
    for code in codes:
        for f in tmpd.glob("*"):
            f.unlink()
        r = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=envx, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 1, (r.returncode, r.stderr[-300:])
        assert not (cwd / "crash.log").exists(), "crash.log must not be written to the current directory"
        assert "فشل تحميل البرنامج" in (tmpd / "crash.log").read_text(encoding="utf-8")
    for f in ("launch.py", "assistant/__main__.py"):
        assert "os.getcwd()" not in (ROOT / f).read_text(encoding="utf-8"), f
    print("ok 2: crash.log fallback uses the temp dir")

    # ---------- 3) mutex ويندوز بأنواع صريحة ----------
    import ctypes

    class Fn:
        def __init__(self, impl):
            self.impl, self.argtypes, self.restype = impl, None, None

        def __call__(self, *a):
            return self.impl(*a)

    class FakeK32:
        def __init__(self):
            self.calls = []
            self.CreateMutexW = Fn(lambda *a: self.calls.append(("create", a[1:])) or self.next_handle)
            self.CloseHandle = Fn(lambda h: self.calls.append(("close", h)) or 1)
            self.next_handle = 0x1_0000_0001   # يتجاوز 32 بت: لا يُبتر

    class FakeCT:
        c_void_p = ctypes.c_void_p
        last = 0

        def __init__(self):
            self.k32, self.winmeta = FakeK32(), []

        def WinDLL(self, name, use_last_error=False):
            self.winmeta.append((name, use_last_error))
            return self.k32

        def set_last_error(self, v):
            FakeCT.last = v

        def get_last_error(self):
            return FakeCT.last
    fct = FakeCT()
    api = app._WinMutexApi(fct)
    assert fct.winmeta == [("kernel32", True)], "use_last_error must be on"
    assert fct.k32.CreateMutexW.restype is ctypes.c_void_p and fct.k32.CloseHandle.argtypes == [ctypes.c_void_p]
    assert fct.k32.CreateMutexW.argtypes is not None and len(fct.k32.CreateMutexW.argtypes) == 3
    h, err = api.create("Local\\COS_Test")
    assert h == 0x1_0000_0001 and err == 0

    class Api:
        def __init__(self, handle, err):
            self.handle, self.err, self.closed = handle, err, []

        def create(self, name):
            assert name.startswith("Local\\COS_Assistant_")
            return self.handle, self.err

        def close(self, h):
            self.closed.append(h)
    lock_dir = pathlib.Path(tempfile.mkdtemp(prefix="b40_lock_")).resolve()
    held_before = len(app._held_locks)
    a = Api(0x77, app.ERROR_ALREADY_EXISTS)
    assert app.acquire_instance_lock("k_exists", lock_dir, use_mutex=True, mutex_api=a) is False and a.closed == [0x77]   # المقبض يُغلق
    a = Api(0x88, 0)
    assert app.acquire_instance_lock("k_new", lock_dir, use_mutex=True, mutex_api=a) is True and a.closed == [] and app._held_locks[-1] == 0x88
    app._held_locks.pop()
    assert len(app._held_locks) == held_before
    a = Api(0, 5)   # فشل الإنشاء: يعود لملف القفل ولا يرفع
    assert app.acquire_instance_lock("k_fail_mutex", lock_dir, use_mutex=True, mutex_api=a) is True
    assert app.acquire_instance_lock("k_fail_mutex", lock_dir, use_mutex=True, mutex_api=Api(0, 5)) is False   # ملف القفل يمنع الثانية

    class Boom:
        def create(self, name):
            raise OSError("no kernel32")
    assert app.acquire_instance_lock("k_boom", lock_dir, use_mutex=True, mutex_api=Boom()) is True   # عطل الواجهة لا يمنع التشغيل
    print("ok 3: windows mutex api")

    # ---------- 4) نصوص: لا أسماء أصناف ولا $ ولا جمع خاطئ ----------
    c, ws, tmp = mk()
    c.drain_events()
    c._fail_worker(None, (), "تعذّر بدء المعالجة", RuntimeError("سر تقني"))
    e = c.drain_events()[0]
    assert e["text"].startswith("تعذّر بدء المعالجة") and "RuntimeError" not in e["text"] and "سر تقني" not in e["text"] and "crash.log" in e["text"]
    assert "RuntimeError: سر تقني" in (DATA / "crash.log").read_text(encoding="utf-8")
    c.state = "RUNNING"
    c.undo_last()
    assert c.drain_events()[0]["text"] == C.MSG_UNDO_BUSY
    c.set_workspace(str(ws))
    assert c.drain_events()[0]["text"] == C.MSG_WORKSPACE_BUSY
    c.state = IDLE
    import assistant.arabic as AR
    assert AR.n_units(2, "نقطة") == "نقطتان" and AR.n_units(3, "مهمة") == "3 مهام"

    # واجهة بلا tkinter: دوال خالصة (تحتاج استيراد widget → tkinter)
    try:
        import tkinter as tk
        probe = tk.Tk()
        probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: no tkinter/display ({type(exc).__name__}: {exc})")
        print("PASS: test_b40_ui completed successfully")
        sys.exit(0)
    import assistant.ui.widget as W

    om = W.AssistantWidget.outcome_message
    assert om({"success": True, "executed": 3, "failed": 0, "skipped": 2})[0] == "اكتملت جزئياً ⚠ — نُفّذت 3 خطوات، تُخطّيت خطوتان. التفاصيل في التقرير أعلاه."
    assert om({"success": False, "executed": 0, "failed": 1, "skipped": 0})[0].startswith("فشلت المهمة")
    assert W.questions_header(["أي مجلد؟"]).startswith("أحتاج توضيحاً") and W.questions_header(["لا أستطيع الجمع بين نسخ وحذف."]) == "لم أستطع تنفيذ طلبك كما هو:"
    assert W.points_phrase(5) == "5 نقاط" and W.points_phrase(2) == "نقطتان" and W.points_phrase(2.5) == "2.5 نقطة" and W.points_phrase(None) == "None"
    assert W.LEVEL_AR["DANGEROUS"] == "تحتاج موافقتك" and "خطر" not in W.LEVEL_AR.values()

    # ---------- 5) RLM للعرض فقط ----------
    RLM = "‏"
    assert W.rtl_para("1. فتح") == RLM + "1. فتح" and W.rtl_para("نص عربي") == "نص عربي" and W.rtl_para("a.txt تم") == RLM + "a.txt تم"
    assert W.rtl_para("سطر\n- بند\n\nlast") == "سطر\n" + RLM + "- بند\n\n" + RLM + "last"
    assert W.strip_isolates(RLM + "x⁨y⁩") == "xy"
    line = W.isolate_names("report.docx تم")
    assert line.startswith("⁨" + RLM) and "⁨report.docx⁩" in line and line.endswith("⁩")
    rep = W.AssistantWidget.pretty_report("# تقرير\n- نقل a.txt\n1 ملف", True)
    assert all(ln.startswith(("⁨", "")) for ln in rep.split("\n")) and RLM in rep
    try:
        os.environ["COS_NO_BIDI_ISOLATES"] = "1"
        W.reset_bidi_cache()
        assert W.rtl_para("1. فتح") == "1. فتح" and W.isolate_names("report.docx تم") == "report.docx تم"   # مفتاح الإيقاف يعطّل RLM أيضاً
        assert RLM not in W.AssistantWidget.pretty_report("# تقرير\n- نقل a.txt", True)
    finally:
        os.environ.pop("COS_NO_BIDI_ISOLATES", None)
        W.reset_bidi_cache()

    c, ws, tmp = mk()
    root = tk.Tk()
    try:
        w = W.AssistantWidget(root, c)
        root.update()
        assert "Segoe UI" in str(w.chat.tag_cget("report", "font")) and "Consolas" not in str(w.chat.tag_cget("report", "font"))
        assert "المهمة التي" not in w.chat.get("1.0", "end") and "اكتب ما تريد تنفيذه" in w.chat.get("1.0", "end")
        assert "نقطة" in w.balance_lbl.cget("text") or "نقاط" in w.balance_lbl.cget("text")
        w.add_message("1. سطر يبدأ برقم", "assistant")
        txt = w.chat.get("1.0", "end")
        assert RLM + "1. سطر يبدأ برقم" in txt
        w.chat.tag_add("sel", "1.0", "end")
        w.chat.event_generate("<<Copy>>")
        root.update()
        clip = root.clipboard_get()
        assert "1. سطر يبدأ برقم" in clip and RLM not in clip, repr(clip)   # النسخ بلا علامات عرض
        # خطة بها خطوة تحتاج موافقة، تكلفة ورصيد بوحدات صحيحة
        c.state = "PLAN_READY"
        w.handle_event({"type": "plan", "task_id": "t", "steps": [{"step_id": 1, "action": "delete", "target": "a.txt", "level": "DANGEROUS", "params": {}}],
                        "estimate": {"points": 5, "balance": 2, "affordable": False}})
        txt = w.chat.get("1.0", "end")
        assert "عدد الخطوات التي تحتاج موافقتك: 1" in txt and "الكلفة المتوقعة: 5 نقاط · رصيدك: نقطتان" in txt, txt
        c.state = IDLE
        w.handle_event({"type": "undo", "count": 3, "remaining": 1, "skipped": 2, "partial": True, "chain_left": 2})
        txt = w.chat.get("1.0", "end")
        assert "تم التراجع عن 3 عمليات" in txt and "بقيت مهمتان من آخر طلب" in txt and "بقيت عملية واحدة لم يُتراجع عنها" in txt and "تُخطّيت عمليتان" in txt, txt
        assert "راجع الملاحظات" not in txt and "الشريط/السجل" not in txt
        w.handle_event({"type": "undo", "count": 1, "tasks": 3, "partial": False})
        assert "تم التراجع عن عملية واحدة في 3 مهام" in w.chat.get("1.0", "end")
        # عطب في عرض حدث: رسالة عربية بلا اسم الصنف، والتفاصيل في crash.log
        real_handle = w.handle_event
        w.handle_event = lambda ev: (_ for _ in ()).throw(ZeroDivisionError("تفصيل-تقني"))
        c.emit("status", text="x")
        w._poll_once = None
        root.update()
        import time
        end = time.time() + 3
        while "تعذّر عرض جزء من النتيجة" not in w.chat.get("1.0", "end") and time.time() < end:
            root.update()
            time.sleep(0.05)
        w.handle_event = real_handle
        assert "تعذّر عرض جزء من النتيجة" in w.chat.get("1.0", "end") and "ZeroDivisionError" not in w.chat.get("1.0", "end")
        assert "ZeroDivisionError: تفصيل-تقني" in (DATA / "crash.log").read_text(encoding="utf-8")
        # نافذة الإعدادات: تسميات واضحة بلا مصطلحات داخلية
        w._on_settings()
        texts = []

        def walk(x):
            for opt in ("text",):
                try:
                    texts.append(str(x.cget(opt)))
                except Exception:
                    pass
            for ch in x.winfo_children():
                walk(ch)
        walk(w._settings_dlg.win)
        blob = "\n".join(texts)
        assert "مفتاح الخدمة (API)" in blob and "النموذج السريع" in blob and "النموذج الأقوى" in blob and "الصغير" not in blob and "الكبير" not in blob
        assert "المزوّد" not in blob and "$" not in blob
        w._settings_dlg.close()
    finally:
        root.destroy()
    print("ok 4/5: UI texts, RLM, fonts")

    print("PASS: test_b40_ui completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b40_ui", file=sys.stderr)
    sys.exit(1)
