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
        w.entry.insert(0, "أنشئ مجلد تجربة")
        w._on_send()
        assert pump(root, lambda: len(w._plan_rows) == 1), "لم تظهر الخطة"
        assert str(w.start_btn.cget("state")) == "normal"
        assert "الكلفة المتوقعة" in chat_text(w) and "رصيدك" in chat_text(w), chat_text(w)  # تقدير قبل «ابدأ»
        w.start_btn.invoke()
        assert pump(root, lambda: "تم بنجاح ✔" in chat_text(w)), chat_text(w)
        assert (base / "ws" / "تجربة").is_dir()
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
        w.undo_btn.invoke()  # المهمتان الأخيرتان لم تغيّرا شيئاً؛ التراجع يعود لآخر مهمة غيّرت (إنشاء «تجربة»)
        assert pump(root, lambda: "تم التراجع عن 1" in chat_text(w))
        assert not (base / "ws" / "تجربة").exists()
        w.undo_btn.invoke()  # لا شيء بعدها يُتراجع عنه
        assert pump(root, lambda: "لا يوجد ما يمكن التراجع" in chat_text(w))
        # 4ب. تراجع حقيقي عن مهمة ناجحة
        w.entry.insert(0, "أنشئ مجلد للتراجع")
        w._on_send()
        assert pump(root, lambda: chat_text(w).count("هذه الخطة") == 4)
        w.start_btn.invoke()
        assert pump(root, lambda: chat_text(w).count("تم بنجاح ✔") == 2)
        assert (base / "ws" / "للتراجع").is_dir()
        w.undo_btn.invoke()
        assert pump(root, lambda: chat_text(w).count("تم التراجع عن 1") == 2)
        assert not (base / "ws" / "للتراجع").exists()
        # 3ب. الأمثلة السريعة تملأ الحقل، وتغيير المجلد يعمل ويرفض غير الصالح
        w.chips[0].invoke()
        assert w.entry.get() == "رتّب الملفات حسب النوع"
        w.entry.delete(0, "end")
        newws = base / "ws2"; newws.mkdir()
        assert w.apply_workspace(str(newws)) is True
        assert pump(root, lambda: str(newws)[-20:] in w.ws_lbl.cget("text") and "مجلد العمل الآن" in chat_text(w))
        assert w.apply_workspace(str(base / "ghost")) is False
        assert pump(root, lambda: "مجلد غير مقبول" in chat_text(w))
        assert "الرصيد" in w.balance_lbl.cget("text")
        # سجل المهام
        w.history_btn.invoke()
        assert pump(root, lambda: "آخر المهام" in chat_text(w) or "لا توجد مهام" in chat_text(w))
        assert w.pretty_report("# عنوان\n## قسم\n- بند").splitlines() == ["━━ عنوان ━━", "▸ قسم", "  • بند"]

        # خطة ضخمة (50 خطوة بأسماء طويلة) لا تدفع أزرار البدء وحقل الإدخال خارج النافذة
        big = [{"step_id": i, "action": "move", "target": "تقرير_المبيعات_السنوي_" + str(i) + ".pdf", "level": "REVERSIBLE",
                "params": {"dest": "مستندات/تقرير_المبيعات_السنوي_" + str(i) + ".pdf"}} for i in range(1, 51)]
        w._show_plan(big)
        root.update_idletasks(); root.update()
        for widget_ in (w.start_btn, w.entry, w.send_btn):
            assert widget_.winfo_ismapped()
            assert widget_.winfo_rooty() + widget_.winfo_height() <= root.winfo_rooty() + root.winfo_height() + 2
        w._clear_plan()

        from assistant.ui.widget import step_detail
        assert step_detail({"cmd": 5, "dest": "x"}) == "← x" and step_detail(None) == ""
        assert "الأمر الفعلي: whoami" in step_detail({"cmd": ["whoami"]})
        # حدث تالف لا يُفقد بقية الأحداث
        w.controller.event_queue.put({"type": "plan", "task_id": "t", "steps": [{"bad": 1}]})
        w.controller.event_queue.put({"type": "status", "text": "بعد الحدث التالف"})
        assert pump(root, lambda: "بعد الحدث التالف" in w.status.cget("text"))

        root.destroy()

    print("PASS: test_ui_widget completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_ui_widget", file=sys.stderr)
    sys.exit(1)
