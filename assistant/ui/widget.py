"""المربع العائم (tkinter) — محادثة، خطة، تقدّم، تقرير، زر إيقاف أحمر دائم، ونافذة موافقة للخطوات الخطرة."""
import os
import tkinter as tk
from tkinter import ttk, filedialog
from typing import Any, Dict, List

from assistant.ui.controller import AssistantController

FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
LEVEL_COLORS = {"READ": "#2e7d32", "REVERSIBLE": "#1565c0", "DANGEROUS": "#ef6c00", "FORBIDDEN": "#c62828"}
LEVEL_AR = {"READ": "قراءة", "REVERSIBLE": "قابل للتراجع", "DANGEROUS": "خطر", "FORBIDDEN": "محظور"}
EXAMPLES = ["رتّب الملفات حسب النوع", "افحص المجلد", "أنشئ مشروع my_app", "ابحث عن الملفات المكررة"]

def step_detail(params) -> str:
    """ملخص بارامترات الخطوة (الوجهة/الاسم الجديد/الأمر) لتظهر للمستخدم قبل التنفيذ والموافقة."""
    if not isinstance(params, dict):
        return ""
    cmd = params.get("cmd")
    params = {**params, "cmd": [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else None}
    parts = []
    if params.get("dest"):
        parts.append(f"← {params['dest']}")
    if params.get("new_name"):
        parts.append(f"← الاسم الجديد: {params['new_name']}")
    if params.get("cmd"):
        parts.append("الأمر الفعلي: " + " ".join(str(c) for c in params["cmd"]))
    return "  ".join(parts)


ACTION_AR = {
    "inspect": "فحص", "create_dir": "إنشاء مجلد", "move": "نقل", "copy": "نسخ", "rename": "إعادة تسمية",
    "delete": "نقل للسلة", "trash": "نقل للسلة", "winget_upgrade": "تحديث برنامج", "run_command": "تشغيل أمر",
}


PLAN_HEIGHT = 150  # بكسل: ارتفاع منطقة الخطة القابلة للتمرير
PLAN_WRAP = 340    # التفاف الأسطر الطويلة بدل قصّها خارج النافذة


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
        root.minsize(430, 620)
        w, h = 460, 720
        x = max(0, root.winfo_screenwidth() - w - 24)
        y = max(0, root.winfo_screenheight() - h - 70)
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
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
        self.balance_lbl = tk.Label(header, text="", bg="#263238", fg="#b0bec5", font=FONT)
        self.balance_lbl.pack(side="left", padx=4)

        # مجلد العمل: عرض + تغيير + فتح
        wsrow = tk.Frame(r, bg="#eceff1")
        wsrow.pack(fill="x")
        self.ws_open_btn = tk.Button(wsrow, text="فتح", font=FONT, relief="flat", command=self._open_workspace)
        self.ws_open_btn.pack(side="left", padx=(6, 2), pady=4)
        self.ws_change_btn = tk.Button(wsrow, text="تغيير المجلد", font=FONT, relief="flat", command=self._choose_workspace)
        self.ws_change_btn.pack(side="left", padx=2, pady=4)
        self.ws_lbl = tk.Label(wsrow, text="", bg="#eceff1", fg="#37474f", font=("Segoe UI", 9), anchor="e", justify="right")
        self.ws_lbl.pack(side="right", fill="x", expand=True, padx=8)
        self._refresh_workspace_label()

        # الأسفل أولاً (side=bottom) كي لا تُدفع أزرار البدء وحقل الإدخال خارج النافذة مهما طالت الخطة
        inp = tk.Frame(r, bg="#f5f5f5")
        inp.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self.send_btn = tk.Button(inp, text="إرسال", font=FONT_B, bg="#1565c0", fg="white", relief="flat",
                                  command=self._on_send)
        self.send_btn.pack(side="left", padx=(0, 4))
        self.entry = tk.Entry(inp, font=FONT, justify="right", relief="solid", bd=1)
        self.entry.pack(side="right", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", lambda e: self._on_send())
        self.entry.focus_set()

        chips = tk.Frame(r, bg="#f5f5f5")
        chips.pack(side="bottom", fill="x", padx=8)
        self.chips = []
        chips.columnconfigure(0, weight=1)
        chips.columnconfigure(1, weight=1)
        for i, ex in enumerate(EXAMPLES):  # شبكة من عمودين: لا قصّ للنصوص الطويلة
            b = tk.Button(chips, text=ex, font=("Segoe UI", 9), relief="groove", bg="#e3f2fd", wraplength=200,
                          command=lambda t=ex: self._use_example(t))
            b.grid(row=i // 2, column=1 - (i % 2), sticky="ew", padx=2, pady=2)
            self.chips.append(b)

        actions = tk.Frame(r, bg="#f5f5f5")
        actions.pack(side="bottom", fill="x", padx=8, pady=4)
        self.start_btn = tk.Button(actions, text="ابدأ التنفيذ", font=FONT_B, bg="#2e7d32", fg="white",
                                   relief="flat", state="disabled", command=self.controller.start)
        self.start_btn.pack(side="right", padx=2)
        self.cancel_btn = tk.Button(actions, text="إلغاء الخطة", font=FONT, relief="flat", state="disabled",
                                    command=self.controller.cancel_plan)
        self.cancel_btn.pack(side="right", padx=2)
        self.undo_btn = tk.Button(actions, text="↩ تراجع", font=FONT, relief="flat",
                                  command=self.controller.undo_last)
        self.undo_btn.pack(side="left", padx=2)
        self.history_btn = tk.Button(actions, text="السجل", font=FONT, relief="flat", command=self.controller.history)
        self.history_btn.pack(side="left", padx=2)

        self.status = tk.Label(r, text="جاهز", bg="#f5f5f5", fg="#546e7a", font=FONT, anchor="e")
        self.status.pack(side="bottom", fill="x", padx=10)
        self.progress = ttk.Progressbar(r, mode="determinate", maximum=100)
        self.progress.pack(side="bottom", fill="x", padx=8, pady=2)

        # الخطة: ارتفاع ثابت مع شريط تمرير (خطة من 50 خطوة لا تخفي شيئاً من الواجهة)
        self.plan_frame = tk.LabelFrame(r, text=" الخطة ", bg="#f5f5f5", font=FONT, labelanchor="ne")
        self.plan_frame.pack(side="bottom", fill="x", padx=8, pady=4)
        self.plan_canvas = tk.Canvas(self.plan_frame, height=PLAN_HEIGHT, bg="#f5f5f5", highlightthickness=0)
        self.plan_scroll = ttk.Scrollbar(self.plan_frame, orient="vertical", command=self.plan_canvas.yview)
        self.plan_canvas.configure(yscrollcommand=self.plan_scroll.set)
        self.plan_scroll.pack(side="left", fill="y")
        self.plan_canvas.pack(side="right", fill="both", expand=True)
        self.plan_inner = tk.Frame(self.plan_canvas, bg="#f5f5f5")
        self._plan_win = self.plan_canvas.create_window((0, 0), window=self.plan_inner, anchor="nw")
        self.plan_inner.bind("<Configure>", lambda e: self.plan_canvas.configure(scrollregion=self.plan_canvas.bbox("all")))
        self.plan_canvas.bind("<Configure>", lambda e: self.plan_canvas.itemconfigure(self._plan_win, width=e.width))
        self.plan_canvas.bind("<MouseWheel>", lambda e: self.plan_canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        self.chat = tk.Text(r, wrap="word", height=6, state="disabled", font=FONT, bg="white", relief="flat",
                            padx=8, pady=6)
        self.chat.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 4))
        self.chat.tag_configure("user", justify="right", foreground="#0d47a1", rmargin=4)
        self.chat.tag_configure("assistant", justify="right", foreground="#212121", rmargin=4)
        self.chat.tag_configure("error", justify="right", foreground="#b71c1c", rmargin=4)
        self.chat.tag_configure("ok", justify="right", foreground="#1b5e20", rmargin=4)
        self.chat.tag_configure("report", justify="right", foreground="#37474f", rmargin=4, font=("Consolas", 9))

    # ---------- عرض ----------
    def add_message(self, text: str, tag: str = "assistant"):
        self.chat.configure(state="normal")
        self.chat.insert("end", text + "\n\n", tag)
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def set_status(self, text: str):
        self.status.configure(text=text)

    def _clear_plan(self):
        for w in self.plan_inner.winfo_children():
            w.destroy()
        self.plan_canvas.yview_moveto(0)
        self._plan_rows.clear()
        self.progress.configure(value=0)

    def _show_plan(self, steps: List[Dict[str, Any]]):
        self._clear_plan()
        for s in steps:
            lvl = s["level"]
            action = ACTION_AR.get(s["action"], s["action"])
            row = tk.Label(self.plan_inner, bg="#f5f5f5", font=FONT, anchor="e", justify="right", wraplength=PLAN_WRAP,
                           text=f"{s['step_id']}. {action}: {s['target']} {step_detail(s.get('params'))}  [{LEVEL_AR.get(lvl, lvl)}]",
                           fg=LEVEL_COLORS.get(lvl, "#000"))
            row.pack(fill="x", padx=6)
            self._plan_rows[s["step_id"]] = row

    def _reveal(self, row):
        """يمرّر قائمة الخطة لتظهر الخطوة الجارية."""
        try:
            self.plan_canvas.update_idletasks()
            total = max(1, self.plan_inner.winfo_height())
            self.plan_canvas.yview_moveto(max(0.0, (row.winfo_y() - 20) / total))
        except Exception:
            pass

    def _set_buttons(self, plan_ready: bool):
        self.start_btn.configure(state="normal" if plan_ready else "disabled")
        self.cancel_btn.configure(state="normal" if plan_ready else "disabled")

    # ---------- مجلد العمل وأمثلة ----------
    def _refresh_workspace_label(self):
        path = str(self.controller.workspace)
        short = path if len(path) <= 44 else "…" + path[-43:]
        self.ws_lbl.configure(text=f"مجلد العمل: {short}")
        try:
            self.balance_lbl.configure(text=f"الرصيد: {self.controller.ledger.get_balance()}")
        except Exception:
            pass

    def _choose_workspace(self):
        path = filedialog.askdirectory(title="اختر مجلد العمل (مثل Downloads)", initialdir=str(self.controller.workspace))
        if path:
            self.apply_workspace(path)

    def apply_workspace(self, path: str) -> bool:
        ok = self.controller.set_workspace(path)
        if ok:
            self._clear_plan()
            self._set_buttons(False)
        return ok

    def _open_workspace(self):
        try:
            os.startfile(str(self.controller.workspace))  # ويندوز فقط
        except Exception:
            self.add_message("تعذّر فتح المجلد تلقائياً. المسار: " + str(self.controller.workspace), "error")

    def _use_example(self, text: str):
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        self.entry.focus_set()

    @staticmethod
    def pretty_report(text: str) -> str:
        out = []
        for ln in text.splitlines():
            if ln.startswith("# "):
                out.append("━━ " + ln[2:] + " ━━")
            elif ln.startswith("## "):
                out.append("▸ " + ln[3:])
            elif ln.startswith("- "):
                out.append("  • " + ln[2:])
            else:
                out.append(ln)
        return "\n".join(out)

    def _on_close(self):
        """إغلاق النافذة أثناء تنفيذ: نسأل ثم نوقف المهمة ونمهلها لحظات لتنتهي نظيفة بدل قتل الخيط وترك نسخة ناقصة."""
        if self.controller.state == "RUNNING":
            try:
                from tkinter import messagebox
                if not messagebox.askyesno("مهمة جارية", "توجد مهمة قيد التنفيذ. أوقفها وأغلق؟", parent=self.root):
                    return
            except Exception:
                pass
            self.controller.stop()
            import time
            end = time.time() + 5.0
            while self.controller.state == "RUNNING" and time.time() < end:
                time.sleep(0.1)
        self.root.destroy()

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
                try:
                    self.handle_event(ev)
                except Exception as exc:  # عطب في عرض حدث لا يجب أن يُفقد بقية الأحداث (تقرير/حالة)
                    try:
                        self.add_message(f"تعذّر عرض حدث ({ev.get('type')}): {type(exc).__name__}", "error")
                    except Exception:
                        pass
        finally:
            self.root.after(self.POLL_MS, self._poll)

    def handle_event(self, ev: Dict[str, Any]):
        t = ev["type"]
        if t == "status":
            self.set_status(ev["text"])
        elif t == "info":  # نتيجة عادية بلا عمل («لا شيء للتنفيذ: …»): رسالة هادئة لا خطأ
            self.add_message(ev["text"], "assistant")
            self.set_status("لا شيء للتنفيذ")
            self._set_buttons(self.controller.state == "PLAN_READY")
        elif t == "error":
            self.add_message(ev["text"], "error")
            self.set_status("حدث خطأ")
            self._set_buttons(self.controller.state == "PLAN_READY")  # خطأ عابر لا يعطّل خطة ما زالت جاهزة
        elif t == "questions":
            self.awaiting_answers = self.controller.state == "CLARIFYING"  # حدث قديم بعد الإيقاف لا يبتلع الطلب التالي
            self.add_message("أحتاج توضيحاً قبل الخطة (أجب في سطر واحد):\n" +
                             "\n".join(f"{i}. {q}" for i, q in enumerate(ev["questions"], 1)), "assistant")
            self.set_status("بانتظار إجابتك")
        elif t == "plan":
            self._show_plan(ev["steps"])
            n_danger = sum(1 for s in ev["steps"] if s["level"] == "DANGEROUS")
            msg = f"هذه الخطة ({len(ev['steps'])} خطوات). راجعها ثم اضغط «ابدأ التنفيذ»."
            if n_danger:
                msg += f"\nتنبيه: {n_danger} خطوة خطرة ستطلب موافقتك واحدة واحدة."
            if ev.get("note"):
                msg += f"\nملاحظة: {ev['note']}"
            est = ev.get("estimate") or {}
            if est.get("points") is not None:
                msg += f"\nالكلفة المتوقعة: {est['points']} نقطة · رصيدك: {est['balance']} نقطة"
                if not est.get("affordable", True):
                    msg += "\n⚠ الرصيد أو سقف الإنفاق لا يكفي لهذه المهمة، لذلك لن تبدأ."
            self.add_message(msg, "assistant")
            self.set_status("الخطة جاهزة")
            self._set_buttons(True)
        elif t == "step_start":
            self._set_buttons(False)
            self.set_status(f"تنفيذ الخطوة {ev['index']} من {ev['total']}…")
            row = self._plan_rows.get(ev["step_id"])
            if row:
                row.configure(bg="#fff59d")
                self._reveal(row)
        elif t == "step_done":
            row = self._plan_rows.get(ev["step_id"])
            if row:
                row.configure(bg="#c8e6c9" if ev["ok"] else "#ffcdd2")
            self.progress.configure(value=100.0 * ev["index"] / max(1, ev["total"]))
        elif t == "approval_request":
            self._ask_approval(ev["step"])
        elif t == "approval_closed":
            if self._approval_win is not None and getattr(self._approval_win, "approval_id", None) == ev.get("approval_id"):
                self._close_approval()
        elif t == "report":
            self.add_message(self.pretty_report(ev["text"]), "report")
            self.add_message("تم بنجاح ✔" if ev["success"] else "فشلت المهمة ✖ (راجع التقرير أعلاه)",
                             "ok" if ev["success"] else "error")
            self.set_status("انتهت المهمة")
            self._set_buttons(False)
            self._refresh_workspace_label()
        elif t == "history":
            STATUS_AR = {"SUCCESS": "نجحت", "FAILED": "فشلت", "STARTED": "لم تكتمل"}
            if not ev["rows"]:
                self.add_message("لا توجد مهام مسجلة بعد.", "assistant")
            else:
                lines = ["آخر المهام:"] + [
                    f"• {STATUS_AR.get(r['status'], r['status'])} — {str(r['task_type'])[:50]} — {r['charged_points']} نقطة"
                    for r in ev["rows"]
                ]
                self.add_message("\n".join(lines), "assistant")
        elif t == "workspace":
            self._refresh_workspace_label()
            self.add_message("مجلد العمل الآن: " + ev["path"], "ok")
        elif t == "undo":
            self.add_message(f"تم التراجع عن {ev['count']} عملية.", "ok")

    # ---------- نافذة الموافقة ----------
    def _ask_approval(self, step: Dict[str, Any]):
        if self._approval_win is not None:
            self._close_approval()  # نافذة قديمة معلّقة: تُستبدل بالجديدة ولا تُترك لتوافق على خطوة مختلفة
        win = tk.Toplevel(self.root)
        win.approval_id = step.get("approval_id")
        self._approval_win = win
        win.title("موافقة مطلوبة")
        win.attributes("-topmost", True)
        win.configure(bg="#fff3e0")
        win.geometry("+%d+%d" % (self.root.winfo_x() + 20, self.root.winfo_y() + 120))
        action = ACTION_AR.get(step["action"], step["action"])
        tk.Label(win, text="⚠ خطوة خطرة تحتاج موافقتك", bg="#fff3e0", fg="#e65100", font=("Segoe UI", 12, "bold")
                 ).pack(padx=16, pady=(14, 6))
        tk.Label(win, text=f"{action}: {step['target']}\n{step_detail(step.get('params'))}".rstrip(), bg="#fff3e0", font=FONT, justify="right", wraplength=420).pack(padx=16)
        tk.Label(win, text="هل تسمح بتنفيذ هذه الخطوة فقط؟", bg="#fff3e0", font=FONT).pack(padx=16, pady=6)
        btns = tk.Frame(win, bg="#fff3e0")
        btns.pack(pady=12)

        def decide(ok: bool):
            self.controller.respond_approval(ok, step.get("approval_id"))
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
