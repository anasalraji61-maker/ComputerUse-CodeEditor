"""اختبارات B31 (واجهة): إلغاء الخطة يمسحها، تراجع بنقرتين لا يلغي مهمتين، خطة متأخرة بعد الإيقاف، إرسال أثناء خطة جاهزة
يحتفظ بالنص، موافقة لا تُفعَّل «نعم» فوراً، عدّ عربي للخطوات (يُتخطى إن لم تتوفر شاشة)."""
import pathlib
import sys
import tempfile
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    try:
        import tkinter as tk
        probe = tk.Tk()
        probe.destroy()
    except Exception as exc:
        print(f"SKIP: no display/tkinter available ({type(exc).__name__}: {exc})")
        sys.exit(0)

    from assistant.ui.widget import steps_phrase
    assert steps_phrase(1) == "خطوة واحدة" and steps_phrase(2) == "خطوتان" and steps_phrase(5) == "5 خطوات" \
        and steps_phrase(11) == "11 خطوة" and steps_phrase(48) == "48 خطوة"
    from assistant.ledger import SQLiteLedger
    from assistant.models.demo import DemoProvider
    from assistant.ui.controller import AssistantController
    from assistant.ui.widget import AssistantWidget

    def pump(root, cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            root.update()
            if cond():
                return True
            time.sleep(0.02)
        return False

    def plan_ready():
        return ctl.state == "PLAN_READY" and len(w._plan_rows) == 1 and str(w.cancel_btn.cget("state")) == "normal"

    def chat_text(w):
        return w.chat.get("1.0", "end")

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ledger = SQLiteLedger(base / "l.db", initial_balance=100, spending_limit=100)
        ctl = AssistantController(DemoProvider(), base / "ws", ledger)
        root = tk.Tk()
        w = AssistantWidget(root, ctl)

        # 1. إلغاء الخطة: تُمسح الصفوف وتتعطل الأزرار
        w.entry.insert(0, "أنشئ مجلد أ1")
        w._on_send()
        assert pump(root, plan_ready)
        # إرسال أثناء خطة جاهزة: النص يبقى في الحقل ولا تتغير الحالة إلى خطأ
        w.entry.insert(0, "طلب آخر")
        w._on_send()
        assert w.entry.get() == "طلب آخر" and "لديك خطة جاهزة" in chat_text(w)
        w.entry.delete(0, "end")
        w.cancel_btn.invoke()
        root.update()
        assert not w._plan_rows and str(w.start_btn.cget("state")) == "disabled" and str(w.cancel_btn.cget("state")) == "disabled"

        # 2. تغيير المجلد أثناء خطة جاهزة: رسالة هادئة بلا مربع حوار
        w.entry.insert(0, "أنشئ مجلد أ2")
        w._on_send()
        assert pump(root, plan_ready)
        w._choose_workspace()
        assert "لا يمكن تغيير المجلد" in chat_text(w)
        w.cancel_btn.invoke()

        # 3. نقرتان متتاليتان على «تراجع» لا تلغيان إلا مهمة واحدة
        for k, name in enumerate(("م1", "م2"), 1):
            w.entry.insert(0, f"أنشئ مجلد {name}")
            w._on_send()
            assert pump(root, plan_ready)
            w.start_btn.invoke()
            assert pump(root, lambda: ctl.state == "IDLE" and chat_text(w).count("تم بنجاح ✔") >= k)
        assert (base / "ws" / "م1").is_dir() and (base / "ws" / "م2").is_dir()
        w.undo_btn.invoke()
        w.undo_btn.invoke()   # الثانية يجب أن تُتجاهل (الزر معطّل حتى يصل حدث التراجع)
        assert pump(root, lambda: "تم التراجع" in chat_text(w))
        root.update()
        assert (base / "ws" / "م1").is_dir() and not (base / "ws" / "م2").exists()

        # 4. التراجع أثناء خطة جاهزة مرفوض (الخطة ستصير قديمة)
        w.entry.insert(0, "أنشئ مجلد أ3")
        w._on_send()
        assert pump(root, plan_ready)
        w.undo_btn.configure(state="normal")
        w._on_undo()
        assert "لا يمكن التراجع الآن" in chat_text(w) and (base / "ws" / "م1").is_dir()
        w.cancel_btn.invoke()


        # 5. خطة متأخرة بعد الإيقاف لا تُعرض ولا تُفعّل «ابدأ»
        w.handle_event({"type": "plan", "steps": [{"step_id": 1, "action": "create_dir", "target": "x", "level": "REVERSIBLE", "params": {}}]})
        assert not w._plan_rows and str(w.start_btn.cget("state")) == "disabled"

        # 6. نافذة الموافقة: «نعم» غير مفعّلة فور الظهور
        w._ask_approval({"action": "delete", "target": "f.txt", "params": {}, "approval_id": 7})
        yes = [b for b in w._approval_win.winfo_children()[-1].winfo_children() if "نعم" in b.cget("text")][0]
        assert str(yes.cget("state")) == "disabled"
        assert pump(root, lambda: str(yes.cget("state")) == "normal", timeout=3)
        w._close_approval()
        root.destroy()
    print("PASS: test_b31_ui completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b31_ui", file=sys.stderr)
    sys.exit(1)
