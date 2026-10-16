"""تغطية التطبيق: منع النسخة الثانية (قفل نظام بين عمليتين، وقفل الملف الاحتياطي: عملية حية بصمتها تطابق = مرفوض، ميتة/رقم أُعيد استعماله/تالف = يتيم يُتجاوز)،
وسجل الانهيار يكتب دون أن يرفع أبداً ويحذف عوازل العرض، وخطاف الاستثناءات يسجل."""
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    DATA = pathlib.Path(tempfile.mkdtemp(prefix="cov_app_data_")).resolve()
    os.environ["COS_DATA_DIR"] = str(DATA)
    os.environ["COS_NO_DIALOG"] = "1"
    import assistant.app as app

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:   # القفل المحجوز لعمر العملية يبقي المقبض مفتوحاً (ويندوز)
        base = pathlib.Path(tmp).resolve()

        # 1. قراءة ملف القفل: كل تالف = (0، "")
        lk = base / "x.lock"
        for content in (b"", b"abc", b"-5:fp", b"0:fp", b"99999999999:fp", b"\x00\x00", b"\xff\xfe" + b"9" * 500):
            lk.write_bytes(content)
            assert app._read_lock(lk) == (0, ""), content
        assert app._read_lock(base / "missing.lock") == (0, "")
        lk.write_bytes(b"1234:win:99  ")
        assert app._read_lock(lk) == (1234, "win:99")

        # 2. «القفل حيّ» فقط لعملية أخرى حيّة وبصمتها مطابقة
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
        try:
            fp = app._proc_fingerprint(child.pid)
            assert app._pid_alive(child.pid)
            assert app._lock_is_live(child.pid, fp) is True
            assert app._lock_is_live(child.pid, "") is True                       # بلا بصمة مسجّلة: يكفي أن الرقم حيّ
            if fp:
                assert app._lock_is_live(child.pid, fp + "-other") is False       # رقم أُعيد استعماله لعملية أخرى
            assert app._lock_is_live(os.getpid(), fp) is False                    # قفلنا نحن لا يمنعنا
            assert app._lock_is_live(0, "") is False

            # 3. قفل الملف الاحتياطي (بلا flock ولا msvcrt): حيّ يمنع، ويتيم/تالف يُتجاوز
            with mock.patch.dict(sys.modules, {"fcntl": None, "msvcrt": None}):
                d = base / "fb1"
                key = "fbkey1"
                d.mkdir()
                (d / f"instance_{key}.lock").write_bytes(f"{child.pid}:{fp}".encode("ascii").ljust(64, b" "))
                assert app.acquire_instance_lock(key, d, use_mutex=False) is False
                dead = 2 ** 22 - 7
                key2 = "fbkey2"
                (d / f"instance_{key2}.lock").write_bytes(f"{dead}:whatever".encode("ascii"))
                assert app.acquire_instance_lock(key2, d, use_mutex=False) is True        # صاحبه ميت: يتيم
                assert app.acquire_instance_lock(key2, d, use_mutex=False) is False       # وهذه العملية صارت صاحبه
                key3 = "fbkey3"
                (d / f"instance_{key3}.lock").write_bytes(b"garbage\x00\x00")
                assert app.acquire_instance_lock(key3, d, use_mutex=False) is True
                key4 = "fbkey4"
                (d / f"instance_{key4}.lock").write_bytes(f"{child.pid}:{fp}-reused".encode("ascii") if fp else b"")
                assert app.acquire_instance_lock(key4, d, use_mutex=False) is True        # الرقم حيّ لكن لعملية مختلفة
        finally:
            child.kill()
            child.wait()

        # 4. عطب القفل نفسه (مجلد القفل ملف عادي) لا يمنع التشغيل
        blocker = base / "blocker"
        blocker.write_text("file", encoding="utf-8")
        assert app.acquire_instance_lock("k_x", blocker / "sub", use_mutex=False) is True

        # 5. قفل نظام حقيقي بين عمليتين: الأولى تحمله، الثانية (هذه) ترفض، وبعد خروج الأولى يُفرج عنه
        lock_dir = base / "real"
        code = ("import sys, time\nsys.path.insert(0, sys.argv[1])\nimport assistant.app as app\n"
                "ok = app.acquire_instance_lock('realkey', sys.argv[2], use_mutex=False)\n"
                "print('HELD' if ok else 'DENIED', flush=True)\ntime.sleep(60)\n")
        holder = subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(lock_dir)], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                  text=True, env=dict(os.environ), encoding="utf-8", errors="replace")
        try:
            line = holder.stdout.readline().strip()
            assert line == "HELD", line
            assert app.acquire_instance_lock("realkey", lock_dir, use_mutex=False) is False
        finally:
            holder.kill()
            holder.wait()
        ok = False
        for _ in range(100):
            if app.acquire_instance_lock("realkey", lock_dir, use_mutex=False):
                ok = True
                break
            time.sleep(0.1)
        assert ok, "القفل لم يُفرج عنه بعد موت صاحبه"

    # 6. سجل الانهيار: يكتب، يحذف عوازل الاتجاه، ولا يرفع حتى لو تعذّر القرص
    log = DATA / "crash.log"
    app.write_crash_log("خطأ ⁦مخفي⁩ تجربة")
    txt = log.read_text(encoding="utf-8")
    assert "تجربة" in txt and "⁦" not in txt and "⁩" not in txt
    with tempfile.TemporaryDirectory() as tmp2:
        f = pathlib.Path(tmp2) / "afile"
        f.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"COS_DATA_DIR": str(f / "sub")}):
            app.write_crash_log("لن يُكتب")      # لا استثناء
    app.report_fatal("fatal-test-message", dialog=True)
    assert "fatal-test-message" in log.read_text(encoding="utf-8")

    # 7. خطاف الاستثناءات (الرئيسي والخيوط) يسجّل في crash.log
    old_hook, old_thook = sys.excepthook, threading.excepthook
    try:
        app._install_crash_hooks()
        try:
            raise ValueError("main-hook-boom")
        except ValueError:
            et, ev, tb = sys.exc_info()
        with mock.patch.object(sys, "stderr", None):
            sys.excepthook(et, ev, tb)
        t = threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError("thread-hook-boom")))
        with mock.patch.object(sys, "stderr", None):
            t.start()
            t.join()
        body = log.read_text(encoding="utf-8")
        assert "main-hook-boom" in body and "thread-hook-boom" in body, body
    finally:
        sys.excepthook, threading.excepthook = old_hook, old_thook

    print("PASS: test_cov_app_single_instance_crashlog completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_app_single_instance_crashlog", file=sys.stderr)
    sys.exit(1)
