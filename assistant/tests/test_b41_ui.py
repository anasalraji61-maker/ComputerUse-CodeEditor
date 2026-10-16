"""B41: حجب مفاتيح البيئة والمزوّد في الأحداث وcrash.log (حتى بلا نمط معروف)، رفض مفتاح مورّد خاطئ، صيغ JSON/HTML المهرّبة،
قراءة حمولة القفل على ويندوز، crash.log الاحتياطي بلا اتباع رابط رمزي، إغلاق النافذة بـ after، عجلة اللوحة محلية، مقارنة المجلدات،
حفظ التقارير (retry + ترتيب ثابت)، وتسجيل كلفة المراجعة بعد التشغيل. أجزاء tkinter تُتخطى بطباعة SKIP."""
import html
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import traceback
import types
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    DATA = pathlib.Path(tempfile.mkdtemp(prefix="b41_data_")).resolve()
    os.environ["COS_DATA_DIR"] = str(DATA)
    for k in ("COS_PROVIDER", "COS_SMALL_MODEL", "COS_LARGE_MODEL", "COS_MAX_USD_PER_DAY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
              "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "COS_OPENAI_API_KEY", "COS_OPENAI_BASE_URL", "COS_NO_BIDI_ISOLATES"):
        os.environ.pop(k, None)

    from assistant import app
    from assistant import arabic as AR
    from assistant.ui import settings as S
    from assistant.ui.controller import AssistantController, IDLE, PLAN_READY
    from assistant.ledger import SQLiteLedger
    from assistant.usage import UsageMeter
    from assistant.models.demo import DemoProvider
    from assistant.models.openai_compat import OpenAICompatProvider

    ENVKEY = "sk-live-" + "Zq8" * 12      # بلا نمط معروف (sk-live-): لا يلتقطه إلا الحجب بقيمة المتغير/السجل
    ANTKEY = "sk" + "-ant-" + "E" * 40
    EXOTIC = 'ab"cd&ef<gh/ij'            # محارف تُهرَّب في JSON/HTML

    def mk(provider=None):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="b41_")).resolve()
        ws = tmp / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("a", encoding="utf-8")
        led = SQLiteLedger(tmp / "l.db", initial_balance=100, spending_limit=1000)
        meter = UsageMeter(tmp / "usage.json", daily_cap_usd=5.0)
        return AssistantController(provider or DemoProvider(), ws, led, usage_meter=meter, reports_dir=tmp / "reports"), ws, tmp, meter

    # ---------- 1) مفاتيح البيئة والمزوّد ----------
    S._KNOWN_SECRETS.clear()
    os.environ["COS_OPENAI_API_KEY"] = ENVKEY
    os.environ["ANTHROPIC_API_KEY"] = ANTKEY
    try:
        app.write_crash_log(f"env echoed {ENVKEY} and {ANTKEY}")
        log = (DATA / "crash.log").read_text(encoding="utf-8")
        assert ENVKEY not in log and ANTKEY not in log and "***" in log, log
        # الخطاف الاستثنائي يكتب ويطبع نسخة محجوبة
        app._install_crash_hooks()
        import io
        buf, real_err = io.StringIO(), sys.stderr
        sys.stderr = buf
        try:
            try:
                raise RuntimeError("upstream: " + ENVKEY)
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
        finally:
            sys.stderr = real_err
        assert ENVKEY not in buf.getvalue() and ENVKEY not in (DATA / "crash.log").read_text(encoding="utf-8")
        # المتحكم: الحدث لا يحمل مفتاح البيئة (حتى بلا نمط) ولا مفتاح المزوّد المبني من قاموس
        os.environ.pop("COS_OPENAI_API_KEY")
        S._KNOWN_SECRETS.clear()
        PKEY = "pk_" + "Qw7" * 12
        prov = OpenAICompatProvider("m", base_url="http://127.0.0.1:9/v1", local=True, key_required=False, api_key=PKEY,
                                    transport=lambda *a: (200, b"{}"))
        c, ws, tmp, meter = mk(prov)
        assert PKEY in S._KNOWN_SECRETS, "provider key must be registered"
        c.emit("error", text="x " + PKEY, rows=[{"a": "نص " + PKEY}], steps=("t", PKEY))
        ev = c.drain_events()[0]
        assert PKEY not in json.dumps(ev, ensure_ascii=False) and ev["rows"][0]["a"] == "نص ***", ev
        c2, ws, tmp, meter = mk()
        c2.emit("error", text="echo " + ANTKEY)   # مفتاح البيئة بنمط معروف
        assert ANTKEY not in json.dumps(c2.drain_events())
    finally:
        os.environ.pop("COS_OPENAI_API_KEY", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
    # launch.py/__main__ عند فشل الاستيراد: نمط احتياطي مضمّن حين لا يُستورد assistant.redact
    for entry in ("launch.py", "assistant/__main__.py"):
        T = pathlib.Path(tempfile.mkdtemp(prefix="b41_shim_")).resolve()
        shim = T / "assistant"
        shim.mkdir()
        (shim / "__init__.py").write_text("", encoding="utf-8")
        (shim / "app.py").write_text(f"raise ImportError('boot failed token {ANTKEY} and {ENVKEY}')\n", encoding="utf-8")
        src = (ROOT / entry).read_text(encoding="utf-8")
        script = T / "entry.py"
        script.write_text(src, encoding="utf-8")
        d2 = T / "data"
        r = subprocess.run([sys.executable, str(script)], cwd=str(T), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env={"PATH": os.environ["PATH"], "COS_DATA_DIR": str(d2), "COS_NO_DIALOG": "1", "COS_OPENAI_API_KEY": ENVKEY,
                                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONPATH": str(T)}, timeout=60)
        cl = (d2 / "crash.log").read_text(encoding="utf-8")
        assert r.returncode == 1 and "فشل تحميل البرنامج" in cl, (entry, r.returncode, r.stderr[-300:])
        assert ANTKEY not in cl and ENVKEY not in cl and ANTKEY not in r.stderr and ENVKEY not in r.stderr, (entry, cl)
    print("ok 1: env/provider keys redacted in events and crash logs")

    # ---------- 2) مفتاح مورّد خاطئ + صيغ مهرّبة ----------
    OAI = "sk-proj-" + "Q" * 40
    for choice in ("gemini", "anthropic"):
        try:
            S.normalize({"provider": choice, "api_key": OAI})
            raise AssertionError("openai key accepted for " + choice)
        except S.SettingsError:
            pass
    assert S.normalize({"provider": "openai", "api_key": OAI})["key_kind"] == "openai"
    assert S.normalize({"provider": "gemini", "api_key": "plainkey12345"})["key_kind"] == "gemini"   # غير معروف النوع: يُقبل للمختار
    for form in (EXOTIC, json.dumps(EXOTIC)[1:-1], json.dumps(EXOTIC)[1:-1].replace("/", "\\/"), html.escape(EXOTIC), html.escape(EXOTIC, quote=False)):
        out = S.redact(f"before {form} after", [EXOTIC])
        assert form not in out and "***" in out, (form, out)
    assert EXOTIC not in S.redact(json.dumps({"detail": EXOTIC}), [EXOTIC])
    print("ok 2: vendor mismatch + escaped forms")

    # ---------- 3) حمولة القفل على ويندوز (البايت 0 مقفل) ----------
    lk = DATA / "lockfmt.lock"
    lk.write_bytes(b"\x00" + b"4321:fp:7".ljust(64, b" "))        # مسار msvcrt: NUL ثم الحمولة
    assert app._read_lock(lk) == (4321, "fp:7")
    lk.write_bytes(b"1234:win:99  ")                              # مسار flock/pid-file: من البايت 0
    assert app._read_lock(lk) == (1234, "win:99")
    lk.write_bytes(b"\x00" + b"777:x".ljust(64, b" "))

    class LockedFile:
        """يحاكي ويندوز: قراءة تمر على البايت 0 المقفل تفشل، والقراءة من البايت 1 تنجح."""
        def __init__(self, data):
            self.data, self.pos = data, 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def seek(self, n):
            self.pos = n

        def read(self, n=-1):
            if self.pos == 0:
                raise PermissionError(13, "locked region")
            out = self.data[self.pos:self.pos + n]
            self.pos += len(out)
            return out
    with mock.patch("builtins.open", lambda *a, **k: LockedFile(lk.read_bytes())):
        assert app._read_lock(lk) == (777, "x")
    print("ok 3: lock payload offset")

    # ---------- 4) crash.log الاحتياطي لا يلحق برابط رمزي مزروع ----------
    cwd = pathlib.Path(tempfile.mkdtemp(prefix="b41_cwd_")).resolve()
    tmpd = pathlib.Path(tempfile.mkdtemp(prefix="b41_tmp_")).resolve()
    victim = cwd / "victim.txt"
    victim.write_text("VICTIM-ORIGINAL", encoding="utf-8")
    blocker = cwd / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    try:
        os.symlink(str(victim), str(tmpd / "crash.log"))
        planted = True
    except (OSError, NotImplementedError, AttributeError):
        planted = False
    if planted:
        envx = {**os.environ, "COS_DATA_DIR": str(blocker / "data"), "COS_NO_DIALOG": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                "TMPDIR": str(tmpd), "TEMP": str(tmpd), "TMP": str(tmpd)}
        code = ("import sys; sys.path.insert(0, %r); sys.modules['assistant.app'] = None; import runpy; "
                "runpy.run_path(%r, run_name='__main__')") % (str(ROOT), str(ROOT / "launch.py"))
        r = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=envx, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 1, r.stderr[-300:]
        assert victim.read_text(encoding="utf-8") == "VICTIM-ORIGINAL", "crash.log fallback appended to a planted symlink target"
        made = list(tmpd.glob("cos_crash_*.log"))
        assert made and "فشل تحميل البرنامج" in made[0].read_text(encoding="utf-8"), list(tmpd.iterdir())
        print("ok 4: planted symlink not followed")
    else:
        print("NOTE 4: symlinks unavailable here; skipped planted-symlink case")

    # ---------- 5) المتحكم: مقارنة المجلدات، التقارير، كلفة المراجعة ----------
    c, ws, tmp, meter = mk()
    real_norm = os.path.normcase
    try:
        os.path.normcase = lambda p: str(p).lower().replace("/", "\\")     # محاكاة ويندوز
        assert AssistantController._norm_path(str(ws)) == AssistantController._norm_path(str(ws).upper())
        c.drain_events()
        assert c.apply_settings({"provider": "demo", "workspace": str(ws).upper()}) is True    # نفس المجلد بحالة أحرف مختلفة: لا تغيير ولا فحص
        kinds = [e["type"] for e in c.drain_events()]
        assert "workspace" not in kinds and "settings_applied" in kinds, kinds
    finally:
        os.path.normcase = real_norm
    # التقارير: replace عابر الفشل يُعاد، وترتيب ثابت عند تساوي الوقت
    rd = tmp / "reports"
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "locked")
        return real_replace(a, b)
    with mock.patch("os.replace", flaky), mock.patch("assistant.tools.files._RETRY_DELAYS", (0.0, 0.0, 0.0)):
        c._save_report("task_first", "# تقرير\n- س")
    assert (rd / "task_first.txt").exists() and calls["n"] >= 2
    now = time.time()
    for i in range(205):
        f = rd / f"task_{i:04d}.txt"
        f.write_text("# r", encoding="utf-8")
        os.utime(f, (now, now))
    os.utime(rd / "task_first.txt", (now, now))
    c._save_report("task_zzzz", "# تقرير")
    names = sorted(p.name for p in rd.glob("*.txt"))
    assert len(names) == 200 and "task_zzzz.txt" in names and "task_0000.txt" not in names and "task_0004.txt" not in names, (len(names), names[:3])
    assert [r["task_id"] for r in c.list_reports(3)][0] == "task_zzzz"   # الأحدث (الاسم الأكبر عند التساوي) أولاً
    # كلفة نداءات النموذج داخل التشغيل تُسجَّل بعد runner.run
    class Spender(DemoProvider):
        usd = 0.0

        def totals(self):
            return {"usd": self.usd, "points": 0, "calls": 1}
    sp = Spender()
    c, ws, tmp, meter = mk(sp)
    report = types.SimpleNamespace(success=True, undo_available=False, render_arabic_text=lambda: "# r", executed_steps=[1], failures=[], skipped=[])

    def fake_run(*a, **k):
        sp.usd += 0.07
        return report
    c.runner.run = fake_run
    c._plan, c._task_id, c._request_full = [object()], "task_x", "طلب"
    c._go(PLAN_READY)
    c.start()
    assert c.wait_for("report", 10)
    end = time.time() + 5
    while c.state != IDLE and time.time() < end:
        time.sleep(0.02)
    assert abs(meter.spent_today() - 0.07) < 1e-9, meter.spent_today()
    print("ok 5: workspace compare, reports, review spend")

    # ---------- 6) الواجهة ----------
    try:
        import tkinter as tk
        probe = tk.Tk()
        probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: no tkinter/display ({type(exc).__name__}: {exc})")
        print("PASS: test_b41_ui completed successfully")
        sys.exit(0)
    import assistant.ui.widget as W
    assert W.ACTION_AR is AR.ACTION_AR

    c, ws, tmp, meter = mk()
    root = tk.Tk()
    try:
        w = W.AssistantWidget(root, c)
        root.update()
        # عجلة اللوحة محلية: لا ربط عام على الجذر
        assert root.bind_all("<MouseWheel>") == "", "no global MouseWheel binding"
        w._show_plan([{"step_id": 1, "action": "move", "target": "a.txt", "level": "REVERSIBLE", "params": {}}])
        assert w.plan_canvas.bind("<MouseWheel>") and w.plan_frame.bind("<MouseWheel>") and w._plan_rows[1].bind("<MouseWheel>")
        assert w.entry.bind("<KP_Enter>") and w.entry.bind("<Return>"), "KP_Enter must send like Return"   # (الحدث الفعلي يحتاج تركيزاً لا يوثق به تحت xvfb)
        # نافذة التقارير: justify غير مدعوم لا يكسر
        c.list_reports = lambda n: [{"task_id": "t1", "when": "now", "title": "عنوان"}]
        real_lb = tk.Listbox

        class NoJustify(real_lb):
            def configure(self, *a, **kw):
                if "justify" in kw:
                    raise tk.TclError('unknown option "-justify"')
                return super().configure(*a, **kw)
        tk.Listbox = NoJustify
        try:
            w._on_reports()
        finally:
            tk.Listbox = real_lb
        assert getattr(w, "_reports_win", None) is not None
        w._reports_win.destroy()
        # الإغلاق: لا نوم داخل المعالج؛ استطلاع بـ after ثم تدمير حين تنتهي المهمة
        stopped = []
        c.stop = lambda *a, **k: stopped.append(1)
        c.state = "RUNNING"
        with mock.patch("tkinter.messagebox.askyesno", return_value=True), mock.patch("time.sleep", side_effect=AssertionError("sleep in _on_close")):
            t0 = time.monotonic()
            w._on_close()
            assert time.monotonic() - t0 < 1.0 and stopped == [1]
            assert root.winfo_exists()
            w._on_close()                      # نقرة ثانية على X أثناء الانتظار: لا تكرار
        c.state = "IDLE"
        end = time.time() + 3
        alive = True
        while alive and time.time() < end:
            try:
                root.update()
                alive = bool(root.winfo_exists())
            except tk.TclError:
                alive = False
        assert not alive, "root must be destroyed once the task is no longer running"
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    # المهلة: مهمة لا تنتهي تُغلق النافذة بعد المهلة
    c, ws, tmp, meter = mk()
    root = tk.Tk()
    try:
        w = W.AssistantWidget(root, c)
        c.stop = lambda *a, **k: None
        c.state = "RUNNING"
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            w._on_close()
        w._close_deadline = time.monotonic() - 1      # المهلة انقضت
        end = time.time() + 3
        alive = True
        while alive and time.time() < end:
            try:
                root.update()
                alive = bool(root.winfo_exists())
            except tk.TclError:
                alive = False
        assert not alive
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    print("ok 6: widget wheel/close/KP_Enter/Listbox")

    print("PASS: test_b41_ui completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b41_ui", file=sys.stderr)
    sys.exit(1)
