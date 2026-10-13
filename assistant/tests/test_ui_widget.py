"""اختبار الواجهة العائمة فعلياً (يُتخطى بطباعة SKIP إن لم تتوفر شاشة أو tkinter)."""
import json
import sys
import tempfile
import time
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    try:
        import tkinter as tk
        probe = tk.Tk()
        probe.destroy()
    except Exception as exc:  # لا شاشة أو لا tkinter
        print(f"SKIP: no display/tkinter available ({type(exc).__name__}: {exc})")
        sys.exit(0)

    from assistant.ui.controller import AssistantController
    from assistant.ui.widget import AssistantWidget
    from assistant.ledger import SQLiteLedger
    from assistant.models.demo import DemoProvider

    def pump(root, cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            root.update()
            if cond():
                return True
            time.sleep(0.02)
        return False

    def chat_text(w):
        return w.chat.get("1.0", "end")

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ledger = SQLiteLedger(base / "l.db", initial_balance=100, spending_limit=100)
        ctl = AssistantController(DemoProvider(), base / "ws", ledger)
        root = tk.Tk()
        w = AssistantWidget(root, ctl)

        # زر الإيقاف الأحمر موجود ودائم الظهور
        assert w.stop_btn.cget("bg") == "#d32f2f" and w.stop_btn.winfo_exists()
        assert str(w.start_btn.cget("state")) == "disabled"

        # 1. طلب عادي: خطة بخطوتين ثم تنفيذ ناجح
        w.entry.insert(0, "أنشئ مجلداً تجريبياً")
        w._on_send()
        assert pump(root, lambda: len(w._plan_rows) == 2), "لم تظهر الخطة"
        assert str(w.start_btn.cget("state")) == "normal"
        w.start_btn.invoke()
        assert pump(root, lambda: "تم بنجاح ✔" in chat_text(w)), chat_text(w)
        assert (base / "ws" / "مجلد_تجريبي").is_dir()
        assert float(w.progress.cget("value")) == 100.0
        assert str(w.start_btn.cget("state")) == "disabled"

        # 2. خطوة خطرة: تظهر نافذة موافقة، الرفض يفشل المهمة
        w.entry.insert(0, "اعرض الهوية whoami")
        w._on_send()
        assert pump(root, lambda: chat_text(w).count("هذه الخطة") == 2)
        w.start_btn.invoke()
        assert pump(root, lambda: w._approval_win is not None), "لم تظهر نافذة الموافقة"
        deny = [b for b in w._approval_win.winfo_children()[-1].winfo_children() if "ارفض" in b.cget("text")][0]
        deny.invoke()
        assert pump(root, lambda: "فشلت المهمة" in chat_text(w))
        assert w._approval_win is None

        # 3. الإيقاف يغلق نافذة الموافقة المعلّقة ويقطع المهمة
        w.entry.insert(0, "اعرض الهوية whoami مرة أخرى")
        w._on_send()
        assert pump(root, lambda: chat_text(w).count("هذه الخطة") == 3)
        w.start_btn.invoke()
        assert pump(root, lambda: w._approval_win is not None)
        w.stop_btn.invoke()
        assert pump(root, lambda: w._approval_win is None and chat_text(w).count("فشلت المهمة") == 2)

        # 4. تراجع عن آخر مهمة
        w.undo_btn.invoke()
        assert pump(root, lambda: "تم التراجع" in chat_text(w))
        root.destroy()

    print("PASS: test_ui_widget completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_ui_widget", file=sys.stderr)
    sys.exit(1)
