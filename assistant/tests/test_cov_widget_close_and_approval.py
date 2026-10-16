"""تغطية الواجهة (يُتخطى بلا شاشة): إغلاق النافذة أثناء مهمة جارية يسأل ويوقف ولا يغلق عند الرفض، نافذة الموافقة (نعم معطّل لحظياً، إغلاقها = رفض،
وطلب جديد يستبدل القديم ولا يجعل «نعم» القديمة تُوافق خطوة أخرى)، رفض طلب جديد أثناء الانشغال/خطة جاهزة/طول زائد دون فقد نص المستخدم."""
import pathlib
import sys
import tempfile
import time
import traceback
from unittest import mock

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

    from assistant.ui.controller import AssistantController, MAX_REQUEST_CHARS
    from assistant.ui.widget import AssistantWidget
    from assistant.ledger import SQLiteLedger
    from assistant.models.demo import DemoProvider

    def pump(root, cond, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            root.update()
            if cond():
                return True
            time.sleep(0.02)
        return cond()

    def buttons(win):
        out = []
        stack = [win]
        while stack:
            w = stack.pop()
            for ch in w.winfo_children():
                stack.append(ch)
                if isinstance(ch, tk.Button):
                    out.append(ch)
        return out

    def find(win, text):
        return next(b for b in buttons(win) if text in b.cget("text"))

    def chat(w):
        return w.chat.get("1.0", "end")

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ctl = AssistantController(DemoProvider(), base / "ws", SQLiteLedger(base / "l.db", initial_balance=100, spending_limit=100))
        root = tk.Tk()
        w = AssistantWidget(root, ctl)
        decisions = []
        ctl.respond_approval = lambda ok, aid=None: decisions.append((ok, aid))

        # 1. نافذة الموافقة: «نعم» معطّل أولاً (حماية النقر المزدوج)، ثم يُفعَّل
        w._ask_approval({"action": "delete", "target": "a.txt", "approval_id": 11, "params": {}})
        win1 = w._approval_win
        yes = find(win1, "نعم")
        assert str(yes.cget("state")) == "disabled"
        assert pump(root, lambda: str(yes.cget("state")) == "normal", 3.0)

        # 2. طلب موافقة جديد يستبدل القديم؛ والرد على الجديد يحمل معرّفه هو
        w._ask_approval({"action": "delete", "target": "b.txt", "approval_id": 12, "params": {}})
        win2 = w._approval_win
        assert win2 is not win1 and not win1.winfo_exists() and win2.approval_id == 12
        # حدث إغلاق بمعرّف مختلف لا يغلق النافذة الحالية؛ بالمعرّف نفسه يغلقها
        w.handle_event({"type": "approval_closed", "approval_id": 999})
        assert w._approval_win is win2
        w.handle_event({"type": "approval_closed", "approval_id": 12})
        assert w._approval_win is None and not win2.winfo_exists()

        # 3. الرفض الصريح ورفض الإغلاق
        w._ask_approval({"action": "delete", "target": "c.txt", "approval_id": 13, "params": {}})
        find(w._approval_win, "ارفض").invoke()
        assert decisions[-1] == (False, 13) and w._approval_win is None
        w._ask_approval({"action": "delete", "target": "d.txt", "approval_id": 14, "params": {}})
        win = w._approval_win
        cmd = win.protocol("WM_DELETE_WINDOW")
        win.tk.call(cmd)                                   # زر X في إطار النافذة
        assert decisions[-1] == (False, 14) and w._approval_win is None

        # 4. إغلاق النافذة أثناء مهمة جارية
        stopped = []

        def fake_stop(*a, **k):
            stopped.append(1)
            ctl.state = "IDLE"
        ctl.stop = fake_stop
        ctl.state = "RUNNING"
        with mock.patch("tkinter.messagebox.askyesno", return_value=False):
            w._on_close()
        assert stopped == [] and root.winfo_exists() and ctl.state == "RUNNING"        # رفض الإغلاق: لا إيقاف ولا تدمير

        # 5. رفض طلب جديد دون فقد نص المستخدم
        ctl.state = "RUNNING"
        w.entry.delete(0, "end")
        w.entry.insert(0, "طلب جديد")
        w._on_send()
        assert "مشغول" in chat(w) and w.entry.get() == "طلب جديد"
        ctl.state = "PLAN_READY"
        w._on_send()
        assert "خطة جاهزة" in chat(w) and w.entry.get() == "طلب جديد"
        ctl.state = "IDLE"
        w.entry.delete(0, "end")
        w.entry.insert(0, "س" * (MAX_REQUEST_CHARS + 1))
        submitted = []
        ctl.submit_request = lambda t: submitted.append(t)
        w._on_send()
        assert submitted == [] and "طويل جداً" in chat(w) and len(w.entry.get()) == MAX_REQUEST_CHARS + 1
        w.entry.delete(0, "end")
        w.entry.insert(0, "   ")
        w._on_send()
        assert submitted == [] and w.entry.get() == ""
        # التراجع أثناء الانشغال يُرفض برسالة
        ctl.state = "RUNNING"
        w._on_undo_all()
        assert "لا يمكن التراجع الآن" in chat(w)
        ctl.state = "IDLE"

        # 6. إغلاق أثناء مهمة وبموافقة المستخدم: يوقف ثم يُدمّر الجذر
        ctl.state = "RUNNING"
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            w._on_close()
        assert stopped == [1]
        try:
            root.winfo_exists()
            alive = bool(root.winfo_exists())
        except tk.TclError:
            alive = False
        assert not alive

    print("PASS: test_cov_widget_close_and_approval completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_widget_close_and_approval", file=sys.stderr)
    sys.exit(1)
