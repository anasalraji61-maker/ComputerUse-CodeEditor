"""المربع العائم (tkinter) — محادثة، خطة، تقدّم، تقرير، زر إيقاف أحمر دائم، ونافذة موافقة للخطوات الخطرة."""
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List

from assistant.ui.controller import AssistantController

FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
LEVEL_COLORS = {"READ": "#2e7d32", "REVERSIBLE": "#1565c0", "DANGEROUS": "#ef6c00", "FORBIDDEN": "#c62828"}
LEVEL_AR = {"READ": "قراءة", "REVERSIBLE": "قابل للتراجع", "DANGEROUS": "خطر", "FORBIDDEN": "محظور"}
ACTION_AR = {
    "inspect": "فحص", "create_dir": "إنشاء مجلد", "move": "نقل", "copy": "نسخ", "rename": "إعادة تسمية",
    "delete": "نقل للسلة", "trash": "نقل للسلة", "winget_upgrade": "تحديث برنامج", "run_command": "تشغيل أمر",
}


class AssistantWidget:
    POLL_MS = 100

    def __init__(self, root: tk.Tk, controller: AssistantController):
        self.root = root
        self.controller = controller
        self.awaiting_answers = False
        self._approval_win = None
        self._plan_rows: Dict[int, tk.Label] = {}

        root.title("مساعد المهام")
        root.attributes("-topmost", True)
        root.configure(bg="#f5f5f5")
        root.minsize(360, 520)
        w, h = 400, 600
        x = max(0, root.winfo_screenwidth() - w - 24)
        y = max(0, root.winfo_screenheight() - h - 70)
        root.geometry(f"{w}x{h}+{x}+{y}")
        self._build()
        self.add_message("مرحباً! اكتب المهمة التي تريد تنفيذها، وسأعرض عليك الخطة قبل البدء.", "assistant")
        root.after(self.POLL_MS, self._poll)

    # ---------- البناء ----------
    def _build(self):
        r = self.root
        header = tk.Frame(r, bg="#263238")
        header.pack(fill="x")
        # زر الإيقاف الأحمر الدائم
        self.stop_btn = tk.Button(header, text="⏹ إيقاف", bg="#d32f2f", fg="white", activebackground="#b71c1c",
                                  activeforeground="white", font=FONT_B, relief="flat", padx=14, pady=6,
                                  command=self._on_stop)
        self.stop_btn.pack(side="left", padx=8, pady=8)
        tk.Label(header, text="مساعد المهام المكتبية", bg="#263238", fg="white", font=("Segoe UI", 12, "bold")
                 ).pack(side="right", padx=12)

        self.chat = tk.Text(r, wrap="word", height=12, state="disabled", font=FONT, bg="white", relief="flat",
                            padx=8, pady=6)
        self.chat.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.chat.tag_configure("user", justify="right", foreground="#0d47a1", rmargin=4)
        self.chat.tag_configure("assistant", justify="right", foreground="#212121", rmargin=4)
        self.chat.tag_configure("error", justify="right", foreground="#b71c1c", rmargin=4)
        self.chat.tag_configure("ok", justify="right", foreground="#1b5e20", rmargin=4)
        self.chat.tag_configure("report", justify="right", foreground="#37474f", rmargin=4, font=("Consolas", 9))

        self.plan_frame = tk.LabelFrame(r, text=" الخطة ", bg="#f5f5f5", font=FONT, labelanchor="ne")
        self.plan_frame.pack(fill="x", padx=8, pady=4)

        self.progress = ttk.Progressbar(r, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=8, pady=2)
        self.status = tk.Label(r, text="جاهز", bg="#f5f5f5", fg="#546e7a", font=FONT, anchor="e")
        self.status.pack(fill="x", padx=10)

        actions = tk.Frame(r, bg="#f5f5f5")
        actions.pack(fill="x", padx=8, pady=4)
        self.start_btn = tk.Button(actions, text="ابدأ التنفيذ", font=FONT_B, bg="#2e7d32", fg="white",
                                   relief="flat", state="disabled", command=self.controller.start)
        self.start_btn.pack(side="right", padx=2)
        self.cancel_btn = tk.Button(actions, text="إلغاء الخطة", font=FONT, relief="flat", state="disabled",
                                    command=self.controller.cancel_plan)
        self.cancel_btn.pack(side="right", padx=2)
        self.undo_btn = tk.Button(actions, text="↩ تراجع", font=FONT, relief="flat",
                                  command=self.controller.undo_last)
        self.undo_btn.pack(side="left", padx=2)

        inp = tk.Frame(r, bg="#f5f5f5")
        inp.pack(fill="x", padx=8, pady=(0, 8))
        self.send_btn = tk.Button(inp, text="إرسال", font=FONT_B, bg="#1565c0", fg="white", relief="flat",
                                  command=self._on_send)
        self.send_btn.pack(side="left", padx=(0, 4))
        self.entry = tk.Entry(inp, font=FONT, justify="right", relief="solid", bd=1)
        self.entry.pack(side="right", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", lambda e: self._on_send())
        self.entry.focus_set()

    # ---------- عرض ----------
    def add_message(self, text: str, tag: str = "assistant"):
        self.chat.configure(state="normal")
        self.chat.insert("end", text + "\n\n", tag)
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def set_status(self, text: str):
        self.status.configure(text=text)

    def _clear_plan(self):
        for w in self.plan_frame.winfo_children():
            w.destroy()
        self._plan_rows.clear()
        self.progress.configure(value=0)

    def _show_plan(self, steps: List[Dict[str, Any]]):
        self._clear_plan()
        for s in steps:
            lvl = s["level"]
            action = ACTION_AR.get(s["action"], s["action"])
            row = tk.Label(self.plan_frame, bg="#f5f5f5", font=FONT, anchor="e", justify="right",
                           text=f"{s['step_id']}. {action}: {s['target']}  [{LEVEL_AR.get(lvl, lvl)}]",
                           fg=LEVEL_COLORS.get(lvl, "#000"))
            row.pack(fill="x", padx=6)
            self._plan_rows[s["step_id"]] = row

    def _set_buttons(self, plan_ready: bool):
        self.start_btn.configure(state="normal" if plan_ready else "disabled")
        self.cancel_btn.configure(state="normal" if plan_ready else "disabled")

    def _on_stop(self):
        self.controller.stop()
        self._close_approval()
        self.awaiting_answers = False
        self._set_buttons(False)

    # ---------- إرسال ----------
    def _on_send(self):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.add_message(text, "user")
        if self.awaiting_answers:
            self.awaiting_answers = False
            self.controller.answer_questions([text])
        else:
            self.controller.submit_request(text)

    # ---------- أحداث ----------
    def _poll(self):
        try:
            for ev in self.controller.drain_events():
                self.handle_event(ev)
        finally:
            self.root.after(self.POLL_MS, self._poll)

    def handle_event(self, ev: Dict[str, Any]):
        t = ev["type"]
        if t == "status":
            self.set_status(ev["text"])
        elif t == "error":
            self.add_message(ev["text"], "error")
            self.set_status("حدث خطأ")
            self._set_buttons(False)
        elif t == "questions":
            self.awaiting_answers = True
            self.add_message("أحتاج توضيحاً قبل الخطة (أجب في سطر واحد):\n" +
                             "\n".join(f"{i}. {q}" for i, q in enumerate(ev["questions"], 1)), "assistant")
            self.set_status("بانتظار إجابتك")
        elif t == "plan":
            self._show_plan(ev["steps"])
            n_danger = sum(1 for s in ev["steps"] if s["level"] == "DANGEROUS")
            msg = f"هذه الخطة ({len(ev['steps'])} خطوات). راجعها ثم اضغط «ابدأ التنفيذ»."
            if n_danger:
                msg += f"\nتنبيه: {n_danger} خطوة خطرة ستطلب موافقتك واحدة واحدة."
            self.add_message(msg, "assistant")
            self.set_status("الخطة جاهزة")
            self._set_buttons(True)
        elif t == "step_start":
            self._set_buttons(False)
            self.set_status(f"تنفيذ الخطوة {ev['index']} من {ev['total']}…")
            row = self._plan_rows.get(ev["step_id"])
            if row:
                row.configure(bg="#fff59d")
        elif t == "step_done":
            row = self._plan_rows.get(ev["step_id"])
            if row:
                row.configure(bg="#c8e6c9" if ev["ok"] else "#ffcdd2")
            self.progress.configure(value=100.0 * ev["index"] / max(1, ev["total"]))
        elif t == "approval_request":
            self._ask_approval(ev["step"])
        elif t == "report":
            self.add_message(ev["text"], "report")
            self.add_message("تم بنجاح ✔" if ev["success"] else "فشلت المهمة ✖ (راجع التقرير أعلاه)",
                             "ok" if ev["success"] else "error")
            self.set_status("انتهت المهمة")
            self._set_buttons(False)
        elif t == "undo":
            self.add_message(f"تم التراجع عن {ev['count']} عملية.", "ok")

    # ---------- نافذة الموافقة ----------
    def _ask_approval(self, step: Dict[str, Any]):
        if self._approval_win is not None:
            return
        win = tk.Toplevel(self.root)
        self._approval_win = win
        win.title("موافقة مطلوبة")
        win.attributes("-topmost", True)
        win.configure(bg="#fff3e0")
        win.geometry("+%d+%d" % (self.root.winfo_x() + 20, self.root.winfo_y() + 120))
        action = ACTION_AR.get(step["action"], step["action"])
        tk.Label(win, text="⚠ خطوة خطرة تحتاج موافقتك", bg="#fff3e0", fg="#e65100", font=("Segoe UI", 12, "bold")
                 ).pack(padx=16, pady=(14, 6))
        tk.Label(win, text=f"{action}: {step['target']}", bg="#fff3e0", font=FONT, justify="right").pack(padx=16)
        tk.Label(win, text="هل تسمح بتنفيذ هذه الخطوة فقط؟", bg="#fff3e0", font=FONT).pack(padx=16, pady=6)
        btns = tk.Frame(win, bg="#fff3e0")
        btns.pack(pady=12)

        def decide(ok: bool):
            self.controller.respond_approval(ok)
            self._close_approval()

        tk.Button(btns, text="نعم، نفّذ", bg="#2e7d32", fg="white", font=FONT_B, relief="flat", padx=12,
                  command=lambda: decide(True)).pack(side="right", padx=6)
        tk.Button(btns, text="لا، ارفض", bg="#c62828", fg="white", font=FONT_B, relief="flat", padx=12,
                  command=lambda: decide(False)).pack(side="right", padx=6)
        win.protocol("WM_DELETE_WINDOW", lambda: decide(False))  # إغلاق النافذة = رفض

    def _close_approval(self):
        if self._approval_win is not None:
            try:
                self._approval_win.destroy()
            finally:
                self._approval_win = None
