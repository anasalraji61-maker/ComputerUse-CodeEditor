"""نقطة تشغيل التطبيق: python -m assistant (أو run_assistant.bat)."""
import os
import sys
from pathlib import Path


def default_paths():
    from assistant.config import current_workspace, data_dir
    return current_workspace(), data_dir()


def build_controller(provider=None):
    """يعيد (المتحكم، ملاحظة للمستخدم). المزوّد من COS_PROVIDER؛ أي خلل في الإعداد يعود للوضع التجريبي مع تنبيه."""
    from assistant.ledger import SQLiteLedger
    from assistant.models.demo import DemoProvider
    from assistant.models.factory import build_provider
    from assistant.models.http_base import ProviderConfigError
    from assistant.ui.controller import AssistantController

    notice = ""
    if provider is None:
        try:
            provider, notice = build_provider()
        except ProviderConfigError as exc:
            provider, notice = DemoProvider(), f"تنبيه: {exc} — يعمل الآن بالوضع التجريبي."

    workspace, data_dir = default_paths()
    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger = SQLiteLedger(db_path=data_dir / "ledger.db", initial_balance=100, spending_limit=100)
    ensure = getattr(ledger, "ensure_trial_balance", None)
    if callable(ensure):   # شحن تجريبي تلقائي (COS_TRIAL_TOPUP=0 يعطّله)؛ المتحكم يكرره قبل كل فحص قدرة على الدفع
        ensure()
    elif ledger.get_balance() < 20:
        ledger.top_up(100, "شحن تجريبي تلقائي")
    from assistant.usage import UsageMeter
    meter = UsageMeter(data_dir / "usage.json")
    rec = str(getattr(ledger, "recovery_message", "") or "").strip()
    if rec:   # دفتر نقاط تالف/فارغ أُعيد إنشاؤه: يجب أن يعرف المستخدم
        notice = (notice + "\n" if notice else "") + "تنبيه: " + rec
    return AssistantController(provider=provider, allowed_root=workspace, ledger=ledger, usage_meter=meter,
                               reports_dir=data_dir / "reports"), notice


MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30
MB_RTL = 0x180000   # MB_RIGHT | MB_RTLREADING: نص عربي يُقرأ من اليمين في مربع رسالة ويندوز


def write_crash_log(msg: str) -> None:
    """يلحق سطراً بـ crash.log في مجلد البيانات؛ لا يرفع أبداً (قد لا يتوفر القرص أو المجلد)."""
    try:
        from assistant.config import data_dir
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        import time
        msg = "".join(ch for ch in str(msg) if ch not in "\u2066\u2067\u2068\u2069")   # عوازل العرض لا تدخل السجلات
        with open(d / "crash.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def show_message(msg: str, flags: int = MB_ICONERROR) -> None:
    """مربع رسالة ويندوز (إن وُجد) بقراءة من اليمين؛ آمن في exe بلا كونسول وعلى غير ويندوز."""
    try:
        if os.environ.get("COS_NO_DIALOG"):   # الاختبارات الآلية تعطّل النافذة المنبثقة
            return
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "مساعد المهام", flags | MB_RTL)
    except Exception:
        pass


def report_fatal(msg: str, dialog: bool = True) -> None:
    """خطأ قاتل يصل المستخدم حتى في exe بلا كونسول (stderr قد يكون None): سجل crash.log + مربع رسالة ويندوز + stderr إن وُجد.
    dialog=False حين سيعرض المستدعي نافذته الخاصة (حتى لا تظهر نافذتان)."""
    write_crash_log(msg)
    if dialog:
        show_message(msg, MB_ICONERROR)
    try:
        if sys.stderr is not None:
            print(msg, file=sys.stderr)
    except Exception:
        pass


def enable_dpi_awareness() -> bool:
    """ويندوز فقط: قبل إنشاء tk.Tk() كي لا تُمدَّد النافذة ضبابية على الشاشات عالية الدقة. True إن نجح."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            return True
        except Exception:
            return bool(ctypes.windll.user32.SetProcessDPIAware())
    except Exception:
        return False


# ---------- منع تشغيل نسختين ----------
ALREADY_RUNNING_MSG = "التطبيق يعمل بالفعل. ابحث عن نافذة «مساعد المهام» (قد تكون خلف نوافذ أخرى)."
_held_locks = []   # يُبقي مقبض القفل حياً طوال عمر العملية (يُفرج عنه النظام عند انتهائها)


def instance_key(data_dir) -> str:
    """من مجلد البيانات وحده: نسختان لا تتشاركان دفتر النقاط/الاستهلاك/الإعدادات حتى لو اختلف مجلد العمل."""
    import hashlib
    raw = os.path.normcase(os.path.abspath(str(data_dir)))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _pid_alive(pid: int) -> bool:
    """بلا os.kill(pid, 0): على ويندوز يُنهي العملية فعلاً. نستعمل فحص الأداة المشتركة (OpenProcess على ويندوز)."""
    try:
        from assistant.tools.files import pid_alive
        return bool(pid_alive(int(pid)))
    except Exception:
        return True   # لا نعرف: نعدّه حياً (الأحوط: نرفض التشغيل المزدوج)


def _proc_fingerprint(pid: int) -> str:
    """بصمة بداية العملية (تميّز العملية عن أخرى أُعيد استعمال رقمها)؛ فارغة إن تعذّرت."""
    try:
        from assistant.tools.files import proc_start
        ps = proc_start(int(pid))
        return str(ps[0]) if ps else ""
    except Exception:
        return ""


def _read_lock(path):
    """(pid، بصمة) من ملف القفل؛ غير مقروء/فارغ/ضخم/غير موجب = (0، "") أي قفل قديم يُتجاوز."""
    try:
        with open(path, "rb") as f:
            f.seek(_LOCK_DATA_OFFSET)
            raw = f.read(200).decode("ascii", "ignore").strip().strip("\x00")
        head, _, fp = raw.partition(":")
        pid = int(head)
        return (pid, fp.strip()) if 0 < pid < 2 ** 31 else (0, "")
    except Exception:
        return (0, "")


def _read_pid(path) -> int:
    return _read_lock(path)[0]


def _lock_is_live(pid: int, fp: str) -> bool:
    """القفل تحمله عملية أخرى حيّة فعلاً: الرقم حيّ وبصمة بدايتها تطابق المسجّلة (رقم أُعيد استعماله = قفل يتيم)."""
    if not pid or pid == os.getpid():
        return False
    if not _pid_alive(pid):
        return False
    if fp:
        now = _proc_fingerprint(pid)
        if now and now != fp:
            return False
    return True


_LOCK_DATA_OFFSET = 0
_held_paths = set()   # مسارات أقفال ملفية تحملها هذه العملية (يمنع التشغيل المزدوج داخل العملية الواحدة أيضاً)


def _release_pid_file(path, pid) -> None:
    try:
        if _read_pid(path) == pid:
            os.unlink(path)
    except Exception:
        pass
    _held_paths.discard(os.path.normcase(os.path.abspath(str(path))))


def _release_msvcrt(fd, path) -> None:
    try:
        import msvcrt
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass
    _held_paths.discard(os.path.normcase(os.path.abspath(str(path))))


def _lock_payload() -> bytes:
    return f"{os.getpid()}:{_proc_fingerprint(os.getpid())}".encode("ascii", "ignore").ljust(64, b" ")


def acquire_instance_lock(key: str, lock_dir, *, use_mutex=None) -> bool:
    """True = نحن النسخة الوحيدة (القفل محجوز لعمر العملية)، False = نسخة أخرى تعمل (في هذه العملية أو غيرها).
    ويندوز: mutex مسمّى (Local\\COS_Assistant_<key>) عبر ctypes. غيره/عند الفشل: ملف قفل. الأولوية: flock (يونكس)، ثم
    msvcrt.locking على أول بايت (قفل نظام حقيقي على ويندوز)، ثم ملف يحوي «pid:بصمة بداية العملية» (حيّ فقط إن كان الرقم حيّاً
    وبصمته تطابق). أي عطل غير التنازع (قرص/صلاحيات/قفل غير مدعوم) = نبدأ بلا قفل."""
    if use_mutex is None:
        use_mutex = os.name == "nt"
    if use_mutex:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetLastError(0)
            handle = k32.CreateMutexW(None, False, f"Local\\COS_Assistant_{key}")
            if handle:
                if k32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
                    k32.CloseHandle(handle)
                    return False
                _held_locks.append(handle)
                return True
        except Exception:
            pass   # نرجع لملف القفل
    try:
        d = Path(lock_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"instance_{key}.lock"
        pkey = os.path.normcase(os.path.abspath(str(path)))
        if pkey in _held_paths:
            return False   # هذه العملية نفسها تحمله
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl is not None:
            import errno
            f = open(path, "a+")
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                f.close()
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    return False   # قفل محجوز فعلاً = نسخة أخرى
                return True   # ENOLCK/ENOTSUP… (نظام ملفات شبكي): لا نمنع المستخدم من العمل
            f.seek(0)
            f.truncate()
            f.write(f"{os.getpid()}:{_proc_fingerprint(os.getpid())}")
            f.flush()
            _held_locks.append(f)
            _held_paths.add(pkey)
            return True
        try:
            import msvcrt
        except ImportError:
            msvcrt = None
        import atexit
        if msvcrt is not None and hasattr(msvcrt, "locking"):
            import errno
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
            try:
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # القفل على البايت الأول: قفل نظام لكل مقبض (حتى داخل العملية الواحدة)
            except OSError as exc:
                os.close(fd)
                if exc.errno in (errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK), errno.EAGAIN):
                    return False   # محجوز بمقبض آخر = نسخة أخرى
                return True   # فشل غير تنازعي: لا نمنع المستخدم
            try:   # بيانات للتشخيص فقط (من البايت الثاني، فلا تتعارض مع بايت القفل)
                os.lseek(fd, 1, 0)
                os.write(fd, _lock_payload())
            except OSError:
                pass
            _held_paths.add(pkey)
            atexit.register(_release_msvcrt, fd, str(path))
            return True
        # بلا flock ولا msvcrt: ملف «pid:بصمة»
        me = os.getpid()
        for _ in range(2):
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                old, fp = _read_lock(path)
                if _lock_is_live(old, fp):
                    return False
                try:
                    os.unlink(path)   # يتيم (عملية ميتة/رقم أُعيد استعماله/غير مقروء): يُزال ونعيد المحاولة
                except OSError:
                    return True
                continue
            with os.fdopen(fd, "wb") as f:
                f.write(_lock_payload())
            _held_paths.add(pkey)
            atexit.register(_release_pid_file, str(path), me)
            return True
        return True
    except Exception:
        return True


def _install_crash_hooks() -> None:
    """استثناء غير ملتقط (خيط/سطر رئيسي) يصل crash.log بدل أن يختفي في exe بلا كونسول."""
    import traceback
    prev = sys.excepthook

    def hook(et, ev, tb):
        write_crash_log("".join(traceback.format_exception(et, ev, tb)))
        try:
            if sys.stderr is not None:
                prev(et, ev, tb)
        except Exception:
            pass
    sys.excepthook = hook
    try:
        import threading
        prev_t = threading.excepthook

        def thook(args):
            write_crash_log("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
            try:
                if sys.stderr is not None:
                    prev_t(args)
            except Exception:
                pass
        threading.excepthook = thook
    except Exception:
        pass


def main(lock_fn=None) -> int:
    """lock_fn(key, lock_dir) -> bool: قابل للحقن في الاختبارات (الافتراضي acquire_instance_lock)."""
    _install_crash_hooks()
    enable_dpi_awareness()   # قبل أول tk.Tk() في أي مسار
    try:
        from assistant.config import data_dir
        data0 = data_dir()
        if not (lock_fn or acquire_instance_lock)(instance_key(data0), data0):
            show_message(ALREADY_RUNNING_MSG, MB_ICONWARNING)
            try:
                if sys.stderr is not None:
                    print(ALREADY_RUNNING_MSG, file=sys.stderr)
            except Exception:
                pass
            return 0
    except Exception:
        pass   # فشل آلية القفل نفسها لا يمنع التشغيل
    try:
        import tkinter as tk
    except ImportError:
        report_fatal("tkinter غير متوفر في هذا البايثون. ثبّت بايثون الرسمي من python.org (يتضمن tkinter).")
        return 1
    from assistant.ui.widget import AssistantWidget

    try:
        controller, notice = build_controller()
    except Exception as exc:  # مجلد عمل غير آمن أو عطب في التهيئة: نعرض السبب ونتيح اختيار مجلد آخر بدل الخروج
        msg = f"تعذّر تشغيل المساعد: {exc}"
        report_fatal(msg, dialog=False)   # سجل فقط؛ النافذة الوحيدة هي رسالة اختيار المجلد أدناه
        controller = None
        try:
            from tkinter import messagebox, filedialog
            from assistant.config import validate_workspace, load_config, save_config
            r = tk.Tk(); r.withdraw()
            messagebox.showerror("مساعد المهام", msg + "\nاختر الآن مجلد عمل آخر.")
            chosen = filedialog.askdirectory(title="اختر مجلد العمل")
            if chosen:
                validate_workspace(chosen)
                cfg = load_config(); cfg["workspace"] = chosen; save_config(cfg)
                controller, notice = build_controller()
            r.destroy()
        except Exception as exc2:
            report_fatal(f"{msg}\nتعذّر: {exc2}")   # فشلت نافذة الاختيار نفسها: هذه هي النافذة الوحيدة
            controller = None
        if controller is None:
            return 1
    try:
        root = tk.Tk()
        widget = AssistantWidget(root, controller)
    except Exception as exc:   # exe بلا كونسول: وإلا يختفي الخطأ بصمت
        import traceback
        report_fatal(f"تعذّر فتح النافذة: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-600:]}")
        return 1
    if notice:
        widget.add_message(notice, "assistant")
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
