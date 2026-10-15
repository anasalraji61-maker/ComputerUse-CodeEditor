import sys

try:
    from assistant.app import main
except BaseException as _exc:   # pythonw/exe بلا كونسول: فشل الاستيراد لا يجوز أن يمرّ بصمت
    import os
    import traceback

    _msg = "تعذّر تشغيل المساعد (فشل تحميل البرنامج): " + type(_exc).__name__ + ": " + str(_exc)
    _tb = "".join(traceback.format_exception(type(_exc), _exc, _exc.__traceback__))
    for _d in (os.environ.get("COS_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".cos_assistant"), os.getcwd()):
        try:
            os.makedirs(_d, exist_ok=True)
            with open(os.path.join(_d, "crash.log"), "a", encoding="utf-8") as _f:
                _f.write(_msg + "\n" + _tb + "\n")
            break
        except Exception:
            continue
    try:
        if not os.environ.get("COS_NO_DIALOG"):   # الاختبارات الآلية تعطّل النافذة المنبثقة
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, _msg, "مساعد المهام", 0x10 | 0x180000)
    except Exception:
        pass
    try:
        if sys.stderr is not None:
            sys.stderr.write(_msg + "\n" + _tb)
    except Exception:
        pass
    sys.exit(1)

sys.exit(main())
