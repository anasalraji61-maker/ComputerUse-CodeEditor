"""نقطة دخول للتغليف (PyInstaller) والتشغيل المباشر: python launch.py"""
import sys

for _s in (sys.stdout, sys.stderr):   # أنابيب/كونسول غير UTF-8 (cp1252) أو pythonw بلا كونسول: الطباعة العربية لا تُسقط البرنامج
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

try:
    from assistant.app import main
except BaseException as _exc:   # pythonw/exe بلا كونسول: فشل الاستيراد لا يجوز أن يمرّ بصمت
    import os
    import tempfile
    import traceback

    def _redact(_t):
        """مفاتيح الخدمة لا تدخل crash.log: assistant.redact إن أمكن استيراده، وإلا نمط احتياطي مضمّن (فشل الاستيراد هو سبب وجودنا هنا)."""
        try:
            from assistant.redact import redact as _r
            return _r(_t, os.environ)
        except BaseException:
            pass
        import re
        for _v in sorted({str(_x) for _k, _x in os.environ.items()
                          if re.search(r"(?i)(^|_)(api_?key|token|secret|password)s?($|_)|_key$", str(_k)) and len(str(_x)) >= 12}, key=len, reverse=True):
            _t = _t.replace(_v, "***")
        return re.sub(r"(?i)(?<![A-Za-z0-9_\-])(?:sk-[A-Za-z0-9_\-]{10,}|AIza[0-9A-Za-z_\-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                      r"|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|bearer\s+[A-Za-z0-9._~+/=\-]{20,})", "***", _t)

    _msg = "تعذّر تشغيل المساعد (فشل تحميل البرنامج): " + type(_exc).__name__ + ": " + str(_exc)
    _tb = "".join(traceback.format_exception(type(_exc), _exc, _exc.__traceback__))
    _msg, _tb = _redact(_msg), _redact(_tb)

    def _append(_dir, _name, _text):
        """إلحاق بلا اتباع رابط رمزي ولا فتح ملف يملكه غيرنا (مجلد temp مشترك قد يحوي ملفاً/رابطاً زرعه آخر)."""
        _p = os.path.join(_dir, _name)
        if os.path.islink(_p):
            raise OSError("symlink")
        _flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        _fd = os.open(_p, _flags, 0o600)
        try:
            if hasattr(os, "getuid") and os.fstat(_fd).st_uid != os.getuid():
                raise OSError("foreign owner")
            os.write(_fd, _text.encode("utf-8", "replace"))
        finally:
            os.close(_fd)

    _text = _msg + "\n" + _tb + "\n"
    _written = False
    # المسار الاحتياطي مجلد الملفات المؤقتة لا المجلد الحالي (قد يكون مجلد المستخدم أو مجلد برامج غير قابل للكتابة أو مجلد عمل المستخدم)
    for _d in (os.environ.get("COS_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".cos_assistant"), tempfile.gettempdir()):
        try:
            os.makedirs(_d, exist_ok=True)
            _append(_d, "crash.log", _text)
            _written = True
            break
        except Exception:
            continue
    if not _written:   # ملف مؤقت جديد بالاسم الفريد (O_EXCL داخل mkstemp): لا يمكن أن يكون مزروعاً مسبقاً
        try:
            _fd, _p = tempfile.mkstemp(prefix="cos_crash_", suffix=".log")
            with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                _f.write(_text)
        except Exception:
            pass
    try:
        if not os.environ.get("COS_NO_DIALOG"):   # الاختبارات الآلية تعطّل النافذة المنبثقة
            import ctypes
            _user = ctypes.WinDLL("user32", use_last_error=True)
            _user.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            _user.MessageBoxW.restype = ctypes.c_int
            _user.MessageBoxW(None, "تعذّر تشغيل المساعد: فشل تحميل البرنامج. أعد تثبيته، وإن تكرر أرسل ملف crash.log إلى المطوّر.", "مساعد المهام", 0x10 | 0x180000)
    except Exception:
        pass
    try:
        if sys.stderr is not None:
            sys.stderr.write(_msg + "\n" + _tb)
    except Exception:
        pass
    sys.exit(1)

if __name__ == "__main__":
    sys.exit(main())
