"""المربع العائم (tkinter) — محادثة، خطة، تقدّم، تقرير، زر إيقاف أحمر دائم، ونافذة موافقة للخطوات الخطرة."""
import os
import re
import sys
import time
import unicodedata
import tkinter as tk
from tkinter import ttk, filedialog
from typing import Any, Dict, List

from assistant.models.rules import strip_format_chars
from assistant.planner import sanitize_display
from assistant.ui.controller import (AssistantController, MAX_REQUEST_CHARS, MSG_UNDO_BUSY, MSG_WORKSPACE_BUSY)
from assistant.arabic import n_units, ACTION_AR   # ACTION_AR: جدول واحد مشترك مع المنفّذ والتقارير
from assistant.ui import settings as _settings

FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
LEVEL_COLORS = {"READ": "#2e7d32", "REVERSIBLE": "#1565c0", "DANGEROUS": "#ef6c00", "FORBIDDEN": "#c62828"}
LEVEL_AR = {"READ": "قراءة فقط", "REVERSIBLE": "قابل للتراجع", "DANGEROUS": "تحتاج موافقتك", "FORBIDDEN": "محظور"}
EXAMPLES = ["رتّب الملفات حسب النوع", "افحص المجلد", "أنشئ مشروع my_app", "ابحث عن الملفات المكررة"]

def questions_header(questions) -> str:
    """سؤال حقيقي (ينتهي بعلامة استفهام) يُجاب في سطر؛ غير ذلك نصيحة/رفض لا يُطلب جوابه."""
    qs = [str(q).strip() for q in (questions or [])]
    if qs and all(q.endswith(("؟", "?")) for q in qs):
        return "أحتاج توضيحاً قبل الخطة (أجب في سطر واحد):"
    return "لم أستطع تنفيذ طلبك كما هو:"


def points_phrase(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return n_units(int(f), "نقطة") if f == int(f) else f"{f:g} نقطة"


def steps_phrase(n: int) -> str:
    """عدّ عربي سليم: خطوة واحدة / خطوتان / 3–10 خطوات / 11+ خطوة."""
    if n == 1:
        return "خطوة واحدة"
    if n == 2:
        return "خطوتان"
    return f"{n} خطوات" if 3 <= n <= 10 else f"{n} خطوة"


CANCEL_WORDS = {"الغاء", "إلغاء", "ألغ", "الغ", "لا", "cancel", "stop", "ايقاف", "إيقاف"}


def step_detail(params, isolate_names_: bool = False) -> str:
    """ملخص بارامترات الخطوة (الوجهة/الاسم الجديد/الأمر) لتظهر للمستخدم قبل التنفيذ والموافقة.
    isolate_names_=True للعرض (يعزل الأسماء)؛ الافتراضي نص خام تعتمد عليه الاختبارات."""
    if not isinstance(params, dict):
        return ""
    wrap = isolate if isolate_names_ else (lambda t: t)
    cmd = params.get("cmd")
    params = {**params, "cmd": [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else None}
    parts = []
    if params.get("dest"):
        parts.append(f"← {wrap(clean_line(params['dest']) if isolate_names_ else params['dest'])}")
    if params.get("new_name"):
        parts.append(f"← الاسم الجديد: {wrap(clean_line(params['new_name']) if isolate_names_ else params['new_name'])}")
    if params.get("cmd"):
        full = " ".join(str(c) for c in params["cmd"])
        parts.append("الأمر الفعلي: " + wrap(clean_line(full) if isolate_names_ else full))
    return "  ".join(parts)


APPROVAL_TARGET_MAX = 260   # أقصى طول يُعرض لهدف الخطوة في نافذة الموافقة (الباقي بعلامة «…»)
APPROVAL_PARAM_MAX = 120    # للوجهة/الاسم الجديد
APPROVAL_CMD_MAX = 260      # للأمر الفعلي (الأمر الكامل أهم ما يقرؤه المستخدم قبل الموافقة)
APPROVAL_NAME_MAX = 120


FSI, PDI = "\u2068", "\u2069"   # عزل ثنائي الاتجاه: اسم لاتيني داخل جملة عربية لا يقلب ترتيب الأجزاء المجاورة
RLM = "\u200f"   # علامة اتجاه من اليمين لليسار: تثبّت اتجاه الفقرة لسطر يبدأ بحرف غير عربي (للعرض فقط)
_ISOLATE_CHARS = "\u2066\u2067\u2068\u2069\u200f"
_NAME_TOKEN = re.compile(r"(?:[A-Za-z]:[\\/][^\s«»\"]*|[\w\-.]+(?:[\\/][\w\-. ]+)*\.[A-Za-z0-9]{1,8}\b)")


_bidi_cache = None


def bidi_enabled() -> bool:
    """مفتاح إيقاف العزل: متغير البيئة COS_NO_BIDI_ISOLATES=1 أو مفتاح الإعدادات bidi_isolates=false (لأنظمة تعرض العوازل مربعات)."""
    global _bidi_cache
    if _bidi_cache is None:
        on = str(os.environ.get("COS_NO_BIDI_ISOLATES", "")).strip().lower() not in ("1", "true", "yes", "on")
        if on:
            try:
                from assistant.config import load_config
                on = load_config().get("bidi_isolates", True) is not False
            except Exception:
                pass
        _bidi_cache = on
    return _bidi_cache


def reset_bidi_cache() -> None:
    global _bidi_cache
    _bidi_cache = None


def isolate(text) -> str:
    """يلفّ نصاً (اسم ملف/مسار/أمر) بعازل FSI..PDI. يُستدعى بعد التنظيف دائماً (التنظيف يحذف كل Cf بما فيها العوازل)."""
    t = str(text)
    return f"{FSI}{t}{PDI}" if t and bidi_enabled() else t


def strip_isolates(text) -> str:
    return "".join(ch for ch in str(text) if ch not in _ISOLATE_CHARS)


def rtl_para(text) -> str:
    """علامة RLM في بداية كل سطر لا يبدأ بحرف عربي (فقرة عربية تبدأ برقم/رمز/لاتيني تبقى من اليمين). للعرض فقط؛ يعطّلها COS_NO_BIDI_ISOLATES."""
    t = str(text)
    if not bidi_enabled():
        return t

    def one(ln):
        if not ln.strip():
            return ln
        c = ln.lstrip()[0]
        return ln if "\u0600" <= c <= "\u06ff" else RLM + ln
    return "\n".join(one(x) for x in t.split("\n"))


def isolate_names(line: str) -> str:
    """يعزل في سطر تقرير كل ما يشبه اسم ملف بامتداد أو مسار قرص، ثم السطر كله (اتجاهه من أول حرف قوي؛ RLM يثبّته من اليمين)."""
    if not line:
        return line
    return isolate(rtl_para(_NAME_TOKEN.sub(lambda m: isolate(m.group(0)), line)))


def wheel_units(delta: int, num: int = 0) -> int:
    """وحدات تمرير من حدث العجلة. لوحة اللمس ترسل delta صغيرة (<120): الخطوة لا تقل عن واحدة بإشارة صحيحة."""
    if delta:
        steps = max(1, abs(int(delta)) // 120)
        return -steps if delta > 0 else steps
    return 1 if num == 5 else -1


_KEYCODE_ACTIONS = {86: "paste", 67: "copy", 88: "cut", 65: "select_all"}   # V C X A (رمز المفتاح الفيزيائي في ويندوز)


def keycode_action(keycode: int, keysym: str):
    """اختصار Ctrl مع تخطيط عربي: keysym يكون حرفاً عربياً فلا تعمل روابط Tk الافتراضية. اللاتيني يعالجه Tk (لا نكرره)."""
    if str(keysym).lower() in ("v", "c", "x", "a"):
        return None
    return _KEYCODE_ACTIONS.get(int(keycode))


def keycode_shortcut(state, keycode, keysym, platform=None):
    """قرار خالص: أي اختصار Ctrl (paste/copy/cut/select_all أو None) لحدث (state, keycode, keysym) على المنصة.
    ويندوز: 0x8 هو NumLock (لا يعطّل الاختصار) وAlt = 0x20000؛ X11: 0x8 = Alt. Alt/AltGr (=Ctrl+Alt) حرف يُكتب لا اختصار."""
    plat = sys.platform if platform is None else platform
    alt = 0x20000 if plat == "win32" else 0x8
    try:
        if int(state or 0) & alt:
            return None
        return keycode_action(int(keycode), keysym)
    except (TypeError, ValueError):
        return None


def clamp_rect(x: int, y: int, w: int, h: int, bounds) -> tuple:
    l, t, r, b = bounds
    return min(max(l, x), max(l, r - w)), min(max(t, y), max(t, b - h))


def monitor_bounds(root) -> tuple:
    """حدود الشاشة التي عليها نافذة الجذر (ويندوز: MonitorFromWindow؛ غيره: الشاشة الافتراضية الكاملة)."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            hwnd = u32.GetParent(root.winfo_id()) or root.winfo_id()
            hmon = u32.MonitorFromWindow(wintypes.HWND(hwnd), 2)   # MONITOR_DEFAULTTONEAREST

            class MI(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                            ("dwFlags", wintypes.DWORD)]
            mi = MI()
            mi.cbSize = ctypes.sizeof(MI)
            if u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcWork   # منطقة العمل: بلا شريط المهام
                return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    try:
        vw, vh = root.winfo_vrootwidth(), root.winfo_vrootheight()
        if vw > 0 and vh > 0:
            vx, vy = root.winfo_vrootx(), root.winfo_vrooty()
            return (vx, vy, vx + vw, vy + vh)
    except Exception:
        pass
    return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())


def ui_scale(root) -> float:
    """معامل DPI نسبة إلى 96 (مقيّد 1.0–2.5) لتكبير الأحجام الثابتة بالبكسل."""
    try:
        return max(1.0, min(2.5, float(root.winfo_fpixels("1i")) / 96.0))
    except Exception:
        return 1.0


EMPTY_WORKSPACE_HINT = "المجلد فارغ — انسخ ملفاتك إليه أو اضغط «تغيير المجلد»."


def workspace_is_empty(path) -> bool:
    """True إن لم يحوِ مجلد العمل ملفات المستخدم (تُتجاهل الملفات المخفية و.trash وسجل التراجع)."""
    try:
        with os.scandir(str(path)) as it:
            return not any(not e.name.startswith(".") for e in it)
    except OSError:
        return False


KEEP_JOINERS = "\u200c\u200d"


def clean_line(text) -> str:
    """نص مشتق من أسماء ملفات: يحذف محارف التحكم والتنسيق (Cc/Cf) ويستبدل فواصل الأسطر/الفقرات Unicode (Zl/Zp) بمسافة،
    وكذلك Cs/Co/Cn، فلا يستطيع اسم ملف تزوير سطر أو قسم في التقرير بـ U+2028/U+0085 أو علامات اتجاه. تبقى ZWNJ/ZWJ."""
    out = []
    for ch in str(text):
        cat = unicodedata.category(ch)
        if ch == "\t" or cat in ("Zl", "Zp"):
            out.append(" ")
        elif ch in KEEP_JOINERS:
            out.append(ch)   # ZWNJ/ZWJ مشروعان في الفارسية/العربية؛ علامات الاتجاه وبقية Cf تُحذف
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def escape_name(name, limit: int = APPROVAL_NAME_MAX) -> str:
    """اسم الملف كما هو لكن كل محرف غير مرئي/تحكم/اتجاه يظهر رمزاً صريحاً (\\u202e) فلا يخدع المستخدم؛ مع بتر من الوسط يحفظ الامتداد."""
    out = []
    for ch in str(name):
        if ch == " " or (ch.isprintable() and unicodedata.category(ch) not in ("Cf", "Zl", "Zp", "Zs")):
            out.append(ch)
        else:
            out.append("\\x%02x" % ord(ch) if ord(ch) < 0x100 else "\\u%04x" % ord(ch) if ord(ch) < 0x10000 else "\\U%08x" % ord(ch))
    text = "".join(out)
    if len(text) > limit:
        head = limit // 3
        text = text[:head] + "…" + text[-(limit - head - 1):]   # الذيل (الامتداد) يبقى ظاهراً
    return text


def target_basename(target) -> str:
    return str(target).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def approval_detail(params) -> str:
    """مثل step_detail لكن بسقوف طول وتنظيف، لنافذة الموافقة."""
    if not isinstance(params, dict):
        return ""
    parts = []
    if params.get("dest"):
        parts.append(f"← {isolate(sanitize_display(params['dest'], APPROVAL_PARAM_MAX))}")
    if params.get("new_name"):
        parts.append(f"← الاسم الجديد: {isolate(sanitize_display(params['new_name'], APPROVAL_PARAM_MAX))}")
    cmd = params.get("cmd")
    if isinstance(cmd, (list, tuple)) and cmd:
        parts.append("الأمر الفعلي: " + isolate(sanitize_display(" ".join(str(c) for c in cmd), APPROVAL_CMD_MAX)))
    if params.get("warning"):
        parts.append("⚠ " + sanitize_display(params["warning"], APPROVAL_CMD_MAX))
    return "\n".join(parts)




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
        self._affordable = True

        self.scale = ui_scale(root)   # DPI: كل مقاس ثابت بالبكسل يُضرب بهذا المعامل
        self.plan_wrap = self.px(PLAN_WRAP)
        root.title("مساعد المهام")
        root.attributes("-topmost", True)
        root.configure(bg="#f5f5f5")
        sw_, sh_ = root.winfo_screenwidth(), root.winfo_screenheight()
        hmax = max(200, sh_ - self.px(80))   # لا تتجاوز النافذة الشاشة (150–200% DPI على شاشة 768/900/1080 بكسل)
        minw, minh = min(self.px(430), sw_), min(self.px(620), hmax)
        root.minsize(minw, minh)
        w, h = min(self.px(460), sw_), self.px(720)
        h = max(min(self.px(560), hmax), min(h, sh_ - self.px(120)))  # شاشة 768 بكسل: لا يُحجب حقل الإدخال خلف شريط المهام
        h = min(max(h, minh), hmax)
        x = max(0, sw_ - w - self.px(24))
        y = max(0, sh_ - h - self.px(70))
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.report_callback_exception = self._on_callback_exception   # استثناء في معالج Tk: crash.log + سطر في المحادثة
        self._build()
        welcome = "مرحباً! اكتب ما تريد تنفيذه وسأعرض عليك الخطة قبل البدء.\nمجلد العمل: " + isolate(clean_line(controller.workspace))
        if workspace_is_empty(controller.workspace):
            welcome += "\n" + EMPTY_WORKSPACE_HINT
        self.add_message(welcome, "assistant")
        self._alive = True
        self._poll_after = None
        root.bind("<Destroy>", self._on_root_destroy, add="+")
        self._poll_after = root.after(self.POLL_MS, self._poll)

    def _on_root_destroy(self, event=None):
        """إغلاق النافذة: نلغي جدولة _poll كي لا يبقى أمر after معلقاً ("invalid command name ..._poll")."""
        if event is not None and getattr(event, "widget", None) is not self.root:
            return
        self._alive = False
        aid, self._poll_after = getattr(self, "_poll_after", None), None
        if aid:
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass

    def px(self, n) -> int:
        return int(round(n * self.scale))

    def _on_callback_exception(self, exc_type, exc, tb):
        """exe بلا كونسول (stderr قد يكون None): لا يُترك الخطأ يختفي. نكتبه في crash.log ونُعلم المستخدم بالعربية."""
        try:
            import traceback
            from assistant.app import write_crash_log
            write_crash_log("Tk callback: " + "".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        try:
            self.add_message("حدث خطأ غير متوقع في الواجهة. سُجّل في ملف crash.log داخل مجلد بيانات التطبيق.", "error")
        except Exception:
            pass
        try:
            if sys.stderr is not None:
                sys.stderr.write(f"Tk callback error: {exc_type}: {exc}\n")
        except Exception:
            pass

    def _copy_chat_selection(self, event=None):
        """النسخ من المحادثة بلا عوازل الاتجاه (لا تتسرب إلى الحافظة ولا إلى ملفات يلصق فيها المستخدم)."""
        try:
            if self.chat.tag_ranges("sel"):
                text = strip_isolates(self.chat.get("sel.first", "sel.last"))
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
        except Exception:
            pass
        return "break"

    def _bind_keycodes(self, widget):
        """ويندوز: Ctrl+V/C/X/A تعمل بالرمز الفيزيائي أيضاً حين يكون التخطيط عربياً (keysym حرف عربي). في غيره لا شيء."""
        if sys.platform != "win32":
            return

        def on_ctrl(e):
            act = keycode_shortcut(getattr(e, "state", 0), getattr(e, "keycode", 0), getattr(e, "keysym", ""))
            if act is None:
                return None
            if act == "select_all":
                if isinstance(widget, tk.Entry):
                    widget.select_range(0, "end")
                    widget.icursor("end")
                else:
                    widget.tag_add("sel", "1.0", "end")
            else:
                widget.event_generate({"paste": "<<Paste>>", "copy": "<<Copy>>", "cut": "<<Cut>>"}[act])
            return "break"
        widget.bind("<Control-KeyPress>", on_ctrl, add="+")

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
        tk.Label(header, text="مساعد المهام", bg="#263238", fg="white", font=("Segoe UI", 12, "bold")
                 ).pack(side="right", padx=12)
        self.balance_lbl = tk.Label(header, text="", bg="#263238", fg="#b0bec5", font=FONT)
        self.balance_lbl.pack(side="left", padx=4)

        # مجلد العمل: عرض + تغيير + فتح
        wsrow = tk.Frame(r, bg="#eceff1")
        wsrow.pack(fill="x")
        self.ws_open_btn = tk.Button(wsrow, text="فتح المجلد", font=FONT, relief="flat", command=self._open_workspace)
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
        self.entry.bind("<KP_Enter>", lambda e: self._on_send())   # Enter لوحة الأرقام
        self.entry.focus_set()
        self._bind_keycodes(self.entry)

        chips = tk.Frame(r, bg="#f5f5f5")
        chips.pack(side="bottom", fill="x", padx=8)
        self.chips = []
        chips.columnconfigure(0, weight=1)
        chips.columnconfigure(1, weight=1)
        for i, ex in enumerate(EXAMPLES):  # شبكة من عمودين: لا قصّ للنصوص الطويلة
            b = tk.Button(chips, text=ex, font=("Segoe UI", 9), relief="groove", bg="#e3f2fd", wraplength=self.px(200),
                          command=lambda t=ex: self._use_example(t))
            b.grid(row=i // 2, column=1 - (i % 2), sticky="ew", padx=2, pady=2)
            self.chips.append(b)

        actions = tk.Frame(r, bg="#f5f5f5")
        actions.pack(side="bottom", fill="x", padx=8, pady=4)
        self.start_btn = tk.Button(actions, text="ابدأ التنفيذ", font=FONT_B, bg="#2e7d32", fg="white",
                                   relief="flat", state="disabled", command=self.controller.start)
        self.start_btn.pack(side="right", padx=2)
        self.cancel_btn = tk.Button(actions, text="إلغاء الخطة", font=FONT, relief="flat", state="disabled",
                                    command=self._on_cancel_plan)
        self.cancel_btn.pack(side="right", padx=2)
        self.undo_btn = tk.Button(actions, text="↩ تراجع", font=FONT, relief="flat",
                                  command=self._on_undo)
        self.undo_btn.pack(side="left", padx=2)
        self.history_btn = tk.Button(actions, text="السجل", font=FONT, relief="flat", command=self.controller.history)
        self.history_btn.pack(side="left", padx=2)

        actions2 = tk.Frame(r, bg="#f5f5f5")
        actions2.pack(side="bottom", fill="x", padx=8)
        self.undo_all_btn = tk.Button(actions2, text="تراجع عن الكل", font=FONT, relief="flat", command=self._on_undo_all)
        self.undo_all_btn.pack(side="left", padx=2)
        self.reports_btn = tk.Button(actions2, text="التقارير", font=FONT, relief="flat", command=self._on_reports)
        self.reports_btn.pack(side="left", padx=2)
        self.settings_btn = tk.Button(actions2, text="الإعدادات", font=FONT, relief="flat", command=self._on_settings)
        self.settings_btn.pack(side="right", padx=2)
        self.ping_btn = tk.Button(actions2, text="فحص اتصال الخدمة", font=FONT, relief="flat", command=self._on_ping)
        self.ping_btn.pack(side="right", padx=2)

        self.status = tk.Label(r, text="جاهز", bg="#f5f5f5", fg="#546e7a", font=FONT, anchor="e")
        self.status.pack(side="bottom", fill="x", padx=10)
        self.progress = ttk.Progressbar(r, mode="determinate", maximum=100)
        self.progress.pack(side="bottom", fill="x", padx=8, pady=2)

        # الخطة: ارتفاع ثابت مع شريط تمرير (خطة من 50 خطوة لا تخفي شيئاً من الواجهة)
        self.plan_frame = tk.LabelFrame(r, text=" الخطة ", bg="#f5f5f5", font=FONT, labelanchor="ne")
        self.plan_frame.pack(side="bottom", fill="x", padx=8, pady=4)
        self.plan_canvas = tk.Canvas(self.plan_frame, height=self.px(PLAN_HEIGHT), bg="#f5f5f5", highlightthickness=0)
        self.plan_scroll = ttk.Scrollbar(self.plan_frame, orient="vertical", command=self.plan_canvas.yview)
        self.plan_canvas.configure(yscrollcommand=self.plan_scroll.set)
        self.plan_scroll.pack(side="left", fill="y")
        self.plan_canvas.pack(side="right", fill="both", expand=True)
        self.plan_inner = tk.Frame(self.plan_canvas, bg="#f5f5f5")
        self._plan_win = self.plan_canvas.create_window((0, 0), window=self.plan_inner, anchor="nw")
        self.plan_inner.bind("<Configure>", lambda e: self.plan_canvas.configure(scrollregion=self.plan_canvas.bbox("all")))
        self.plan_canvas.bind("<Configure>", lambda e: self.plan_canvas.itemconfigure(self._plan_win, width=e.width))
        # العجلة: ربط محلي على اللوحة وإطارها وصفوفها (لا bind_all على الجذر: لا يمس عجلة المحادثة ولا نوافذ الحوار ولا يترك ربطاً عاماً عالقاً)
        for wdg in (self.plan_frame, self.plan_canvas, self.plan_inner):
            self._bind_plan_wheel(wdg)

        self.chat = tk.Text(r, wrap="word", height=6, state="disabled", font=FONT, bg="white", relief="flat",
                            padx=8, pady=6)
        self.chat_scroll = ttk.Scrollbar(r, orient="vertical", command=self.chat.yview)
        self.chat.configure(yscrollcommand=self.chat_scroll.set)
        self.chat_scroll.pack(side="left", fill="y", pady=(8, 4))
        self.chat.pack(side="top", fill="both", expand=True, padx=(0, 8), pady=(8, 4))
        self.chat.tag_configure("user", justify="right", foreground="#0d47a1", rmargin=4)
        self.chat.tag_configure("assistant", justify="right", foreground="#212121", rmargin=4)
        self.chat.tag_configure("error", justify="right", foreground="#b71c1c", rmargin=4)
        self.chat.tag_configure("ok", justify="right", foreground="#1b5e20", rmargin=4)
        self.chat.tag_configure("report", justify="right", foreground="#37474f", rmargin=4, font=("Segoe UI", 10))
        self.chat.tag_configure("warn", justify="right", foreground="#e65100", rmargin=4)
        self._bind_keycodes(self.chat)
        self.chat.bind("<<Copy>>", self._copy_chat_selection)
        self.chat.bind("<<Cut>>", self._copy_chat_selection)   # النص للقراءة فقط: القص = نسخ

    # ---------- عرض ----------
    def add_message(self, text: str, tag: str = "assistant"):
        self.chat.configure(state="normal")
        self.chat.insert("end", rtl_para(text) + "\n\n", tag)
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def set_status(self, text: str):
        self.status.configure(text=text)

    def _bind_plan_wheel(self, wdg):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):   # الأخيران لـ X11
            wdg.bind(seq, self._plan_wheel, add="+")

    def _plan_wheel(self, e):
        self.plan_canvas.yview_scroll(wheel_units(getattr(e, "delta", 0), getattr(e, "num", 0)), "units")
        return "break"

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
            row = tk.Label(self.plan_inner, bg="#f5f5f5", font=FONT, anchor="e", justify="right", wraplength=self.plan_wrap,
                           text=f"{s['step_id']}. {action}: {isolate(clean_line(s['target']))} {step_detail(s.get('params'), True)}  [{LEVEL_AR.get(lvl, lvl)}]",
                           fg=LEVEL_COLORS.get(lvl, "#000"))
            row.pack(fill="x", padx=6)
            self._bind_plan_wheel(row)   # الصفوف لا تمرّر الحدث إلى اللوحة تلقائياً
            self._plan_rows[s["step_id"]] = row

    def _reveal(self, row):
        """يمرّر قائمة الخطة لتظهر الخطوة الجارية."""
        try:
            self.plan_canvas.update_idletasks()
            total = max(1, self.plan_inner.winfo_height())
            self.plan_canvas.yview_moveto(max(0.0, (row.winfo_y() - 20) / total))
        except Exception:
            pass

    def _set_buttons(self, plan_ready: bool, clarifying: bool = False):
        can_start = plan_ready and self._affordable
        self.start_btn.configure(state="normal" if can_start else "disabled")
        self.cancel_btn.configure(state="normal" if (plan_ready or clarifying) else "disabled")

    # ---------- مجلد العمل وأمثلة ----------
    def _refresh_workspace_label(self):
        path = str(self.controller.workspace)
        short = path if len(path) <= 44 else "…" + path[-43:]
        self.ws_lbl.configure(text=f"مجلد العمل: {isolate(clean_line(short))}")
        try:
            self.balance_lbl.configure(text=f"الرصيد: {n_units(self.controller.ledger.get_balance(), 'نقطة')}")
        except Exception:
            pass

    def _choose_workspace(self):
        if self.controller.state != "IDLE":
            self.add_message(MSG_WORKSPACE_BUSY, "assistant")
            return
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
            path = str(self.controller.workspace)
            if hasattr(os, "startfile"):
                os.startfile(path)  # ويندوز
            else:
                import subprocess
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self.add_message("تعذّر فتح المجلد تلقائياً. المسار: " + isolate(clean_line(self.controller.workspace)), "error")

    def _use_example(self, text: str):
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        self.entry.focus_set()

    @staticmethod
    def pretty_report(text: str, isolate_lines: bool = False) -> str:
        """isolate_lines=True للعرض: أسماء الملفات والأسطر تُعزل بعد التنظيف؛ الافتراضي نص خام للاختبارات."""
        out = []
        # العنوان الرئيس «# » للسطر الأول فقط؛ «# » لاحق (مثلاً من اسم ملف فيه سطر جديد) يبقى نصاً عادياً
        for ln in (clean_line(x) for x in str(text).split("\n")):   # split("\n") لا splitlines(): U+2028/U+0085 لا تصنع أسطراً مزوّرة
            if ln.startswith("# ") and not out:
                out.append("━━ " + ln[2:] + " ━━")
            elif ln.startswith("## "):
                out.append("▸ " + ln[3:])
            elif ln.startswith("- "):
                out.append("  • " + ln[2:])
            else:
                out.append(ln)
        if isolate_lines:
            out = [isolate_names(x) if x.strip() else x for x in out]
        return "\n".join(out)

    @staticmethod
    def outcome_message(ev: Dict[str, Any]):
        """(الرسالة، الوسم): نجاح كامل، أو «جزئي» حين رُفضت/تُخطّيت خطوات بقرار المستخدم، أو فشل مع الأعداد."""
        done, failed, skipped = int(ev.get("executed") or 0), int(ev.get("failed") or 0), int(ev.get("skipped") or 0)
        bits = []
        if done:
            bits.append(f"نُفّذت {n_units(done, 'خطوة')}")
        if failed:
            bits.append(f"فشلت {n_units(failed, 'خطوة')}")
        if skipped:
            bits.append(f"تُخطّيت {n_units(skipped, 'خطوة')}")
        counts = "، ".join(bits)
        if done == 0 and skipped > 0 and failed == 0:
            return "لم يُنفَّذ شيء — لم تتم الموافقة على أي خطوة", "warn"
        if ev.get("success") and skipped > 0:
            return f"اكتملت جزئياً ⚠ — {counts}. التفاصيل في التقرير أعلاه.", "warn"
        if ev.get("success"):
            return "تم بنجاح ✔", "ok"
        if done or skipped:
            return f"لم تكتمل المهمة ✖ — {counts}. التفاصيل في التقرير أعلاه.", "error"
        return "فشلت المهمة ✖. التفاصيل في التقرير أعلاه.", "error"

    def _on_close(self):
        """إغلاق النافذة أثناء تنفيذ: نسأل ثم نوقف المهمة ونمهلها لحظات لتنتهي نظيفة بدل قتل الخيط وترك نسخة ناقصة."""
        if self.controller.state == "RUNNING":
            try:
                from tkinter import messagebox
                if not messagebox.askyesno("إغلاق المساعد", "هناك مهمة قيد التنفيذ. هل تريد إيقافها وإغلاق المساعد؟", parent=self.root):
                    return
            except Exception:
                pass
            self.controller.stop()
            if self.controller.state == "RUNNING":   # نمهلها بلا تجميد Tk: استطلاع بـ after لا نوم داخل المعالج
                if getattr(self, "_closing", False):
                    return
                self._closing = True
                self._close_deadline = time.monotonic() + 5.0
                self.root.after(100, self._close_poll)
                return
        self.root.destroy()

    def _close_poll(self):
        if self.controller.state == "RUNNING" and time.monotonic() < self._close_deadline:
            try:
                self.root.after(100, self._close_poll)
                return
            except tk.TclError:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _on_stop(self):
        self.controller.stop()
        self._close_approval()
        self.awaiting_answers = False
        self._set_buttons(False)

    # ---------- إرسال ----------
    def _on_send(self):
        text = strip_format_chars(self.entry.get()).strip()
        if not text:
            self.entry.delete(0, "end")
            return
        state = self.controller.state
        if self.awaiting_answers and text.casefold() in CANCEL_WORDS:  # «إلغاء/لا» مخرج لا اسم مجلد
            self.entry.delete(0, "end")
            self._on_cancel_plan()
            return
        if not self.awaiting_answers:
            # الرفض يحتفظ بنص المستخدم ولا يغيّر الحالة النصية
            if state == "PLAN_READY":
                self.add_message("لديك خطة جاهزة: اضغط «ابدأ التنفيذ» أو «إلغاء الخطة» قبل طلب جديد.", "assistant")
                return
            if state != "IDLE":
                self.add_message("المساعد مشغول الآن. انتظر أو اضغط «إيقاف».", "assistant")
                return
            if len(text) > MAX_REQUEST_CHARS:
                self.add_message(f"الطلب طويل جداً ({len(text)} حرفاً). اختصره إلى أقل من {MAX_REQUEST_CHARS} حرف.", "error")
                return
        self.entry.delete(0, "end")
        self.add_message(text, "user")
        if self.awaiting_answers:
            self.awaiting_answers = False
            self.controller.answer_questions([text])
        else:
            self.controller.submit_request(text)

    def _on_cancel_plan(self):
        self.controller.cancel_plan()
        self.awaiting_answers = False
        self._close_approval()
        self._clear_plan()
        self._set_buttons(False)

    def _on_undo(self):
        if self.controller.state != "IDLE":
            self.add_message(MSG_UNDO_BUSY, "assistant")
            return
        self.undo_btn.configure(state="disabled")  # نقرتان متتاليتان لا تلغيان مهمتين
        self.root.after(1500, lambda: self.undo_btn.configure(state="normal"))
        self.controller.undo_last()

    def _on_undo_all(self):
        if self.controller.state != "IDLE":
            self.add_message(MSG_UNDO_BUSY, "assistant")
            return
        n = self.controller.chain_size()
        if n < 1:
            self.add_message("لا يوجد ما يُتراجع عنه دفعة واحدة. اضغط «تراجع» للتراجع عن آخر مهمة.", "assistant")
            return
        try:
            from tkinter import messagebox
            if not messagebox.askyesno("تراجع عن الكل", f"سيتراجع المساعد عن {n_units(n, 'مهمة', 'gen')} من آخر طلب (الأحدث أولاً). هل تريد المتابعة؟", parent=self.root):
                return
        except Exception:
            pass
        self.undo_all_btn.configure(state="disabled")
        self.root.after(1500, lambda: self.undo_all_btn.configure(state="normal"))
        self.controller.undo_chain()

    def _on_reports(self):
        """قائمة التقارير المحفوظة لمهام سابقة؛ النقر المزدوج أو «عرض» يعرض التقرير في المحادثة."""
        rows = self.controller.list_reports(30)
        if not rows:
            self.add_message("لا توجد تقارير محفوظة بعد. يُحفظ تقرير كل مهمة تُنفَّذ.", "assistant")
            return
        win = tk.Toplevel(self.root)
        win.title("التقارير المحفوظة")
        win.attributes("-topmost", True)
        lb = tk.Listbox(win, width=60, height=min(14, len(rows)), font=FONT)
        try:
            lb.configure(justify="right")   # خيار justify غير متاح في بعض إصدارات Tk
        except tk.TclError:
            pass
        for r in rows:
            lb.insert("end", f"{r['when']} — {r['title'] or r['task_id']}")
        lb.pack(side="top", fill="both", expand=True, padx=8, pady=8)

        def show(_e=None):
            sel = lb.curselection()
            if not sel:
                return
            text = self.controller.read_report(rows[sel[0]]["task_id"])
            if text is None:
                self.add_message("تعذّرت قراءة هذا التقرير.", "error")
            else:
                self.add_message(self.pretty_report(text, True), "report")
            win.destroy()
        lb.bind("<Double-Button-1>", show)
        lb.bind("<Return>", show)
        tk.Button(win, text="عرض", font=FONT_B, command=show).pack(side="bottom", pady=(0, 8))
        self._reports_win = win

    def _on_settings(self):
        _settings.open_settings_dialog(self)

    def _on_ping(self):
        """فحص الاتصال في خيط عامل؛ الواجهة لا تُحجب. النتيجة تصل كحدث ping."""
        if self.controller.check_connection():
            self.ping_btn.configure(state="disabled")
            if self.controller.state == "IDLE":
                self.set_status("أفحص اتصال الخدمة…")

    # ---------- أحداث ----------
    def _poll(self):
        try:
            for ev in self.controller.drain_events():
                try:
                    self.handle_event(ev)
                except Exception as exc:  # عطب في عرض حدث لا يجب أن يُفقد بقية الأحداث (تقرير/حالة)
                    try:
                        self.add_message("تعذّر عرض جزء من النتيجة. التفاصيل في ملف crash.log.", "error")
                        try:
                            from assistant.app import write_crash_log
                            write_crash_log(f"عرض حدث {ev.get('type')}: {type(exc).__name__}: {exc}")
                        except Exception:
                            pass
                    except Exception:
                        pass
        finally:
            if getattr(self, "_alive", True):
                try:
                    self._poll_after = self.root.after(self.POLL_MS, self._poll)
                except tk.TclError:   # الجذر دُمّر أثناء المعالجة
                    self._alive = False

    def handle_event(self, ev: Dict[str, Any]):
        t = ev["type"]
        if t == "status":
            self.set_status(ev["text"])
        elif t == "info" and ev.get("notice"):   # تنبيه تعافٍ عند البدء: يُعرض ولا يغيّر الحالة/الأزرار
            self.add_message("تنبيه: " + str(ev["text"]), "warn")
        elif t == "info":  # نتيجة عادية بلا عمل («لا شيء للتنفيذ: …»): رسالة هادئة لا خطأ
            self.add_message(ev["text"], "assistant")
            self.set_status("لا شيء للتنفيذ")
            self._set_buttons(self.controller.state == "PLAN_READY")
        elif t == "error":
            self.add_message(ev["text"], "error")
            self.set_status("حدث خطأ")
            self._set_buttons(self.controller.state == "PLAN_READY", self.controller.state == "CLARIFYING")  # خطأ عابر لا يعطّل خطة ما زالت جاهزة
            self.undo_btn.configure(state="normal")
        elif t == "questions":
            self.awaiting_answers = self.controller.state == "CLARIFYING"  # حدث قديم بعد الإيقاف لا يبتلع الطلب التالي
            self.add_message(questions_header(ev["questions"]) + "\n" +
                             "\n".join(f"{i}. {q}" for i, q in enumerate(ev["questions"], 1)), "assistant")
            self.set_status("بانتظار إجابتك")
            self._set_buttons(False, clarifying=self.controller.state == "CLARIFYING")
        elif t == "plan":
            if self.controller.state != "PLAN_READY":  # خطة متأخرة بعد إيقاف/إلغاء: لا تُعرض ولا تُفعّل «ابدأ»
                return
            self._show_plan(ev["steps"])
            n_danger = sum(1 for s in ev["steps"] if s["level"] == "DANGEROUS")
            msg = f"هذه الخطة ({steps_phrase(len(ev['steps']))}). راجعها ثم اضغط «ابدأ التنفيذ»."
            if n_danger:
                msg += f"\nعدد الخطوات التي تحتاج موافقتك: {n_danger}. سأسألك عن كل خطوة على حدة."
            if ev.get("note"):
                msg += f"\nملاحظة: {ev['note']}"
            est = ev.get("estimate") or {}
            self._affordable = bool(est.get("affordable", True))
            if est.get("points") is not None:
                msg += f"\nالكلفة المتوقعة: {points_phrase(est['points'])} · رصيدك: {points_phrase(est['balance'])}"
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
                if str(ev.get("detail", "")).startswith("تخطّي"):   # رفضتَها بنفسك: لا تُلوَّن خضراء كأنها نُفّذت
                    row.configure(bg="#e0e0e0")
                else:
                    row.configure(bg="#c8e6c9" if ev["ok"] else "#ffcdd2")
            self.progress.configure(value=100.0 * ev["index"] / max(1, ev["total"]))
        elif t == "approval_request":
            self._ask_approval(ev["step"])
        elif t == "undo_confirm":
            self._ask_undo_confirm(ev)
        elif t == "approval_closed":
            if self._approval_win is not None and getattr(self._approval_win, "approval_id", None) == ev.get("approval_id"):
                self._close_approval()
        elif t == "report":
            self.add_message(self.pretty_report(ev["text"], True), "report")
            msg, tag = self.outcome_message(ev)
            self.add_message(msg, tag)
            self.set_status("انتهت المهمة")
            self._set_buttons(False)
            self._refresh_workspace_label()
        elif t == "history":
            STATUS_AR = {"SUCCESS": "نجحت", "FAILED": "فشلت", "STARTED": "لم تكتمل"}
            if not ev["rows"]:
                self.add_message("لا توجد مهام مسجلة بعد.", "assistant")
            else:
                lines = ["آخر المهام:"] + [
                    f"• {STATUS_AR.get(r['status'], r['status'])} — {str(r['task_type'])[:50]} — {points_phrase(r['charged_points'])}"
                    for r in ev["rows"]
                ]
                self.add_message("\n".join(lines), "assistant")
        elif t == "settings_applied":
            self.add_message(ev["text"], "ok")
            self._clear_plan()
            self._set_buttons(False)
            if self.controller.state == "IDLE":
                self.set_status("جاهز")
        elif t == "ping":
            self.ping_btn.configure(state="normal")
            self.add_message(ev["text"], "ok" if ev.get("ok") else "error")
            dlg = getattr(self, "_settings_dlg", None)
            if dlg is not None:
                dlg.show_result(ev["text"], bool(ev.get("ok")))
            if self.controller.state == "IDLE":
                self.set_status("جاهز" if ev.get("ok") else "تعذّر الاتصال")
        elif t == "workspace":
            self._refresh_workspace_label()
            self.add_message("مجلد العمل الآن: " + isolate(clean_line(ev["path"])), "ok")
            if workspace_is_empty(ev["path"]):
                self.add_message(EMPTY_WORKSPACE_HINT, "assistant")
        elif t == "undo":
            if ev.get("partial"):
                bits = [f"تم التراجع عن {n_units(ev['count'], 'عملية', 'acc')}"]
                if ev.get("chain_left"):
                    bits.append(f"بقيت {n_units(ev['chain_left'], 'مهمة')} من آخر طلب")
                if ev.get("remaining"):
                    bits.append(f"بقيت {n_units(ev['remaining'], 'عملية')} لم يُتراجع عنها")
                if ev.get("skipped"):
                    bits.append(f"تُخطّيت {n_units(ev['skipped'], 'عملية')} (ناتجها غير موجود أو تالف)")
                if ev.get("reason"):
                    bits.append(str(ev["reason"]))
                self.add_message("تم التراجع جزئياً ⚠ — " + " · ".join(bits) + ". انظر الرسائل أعلاه.", "warn")
            elif ev.get("tasks"):
                self.add_message(f"تم التراجع عن {n_units(ev['count'], 'عملية', 'acc')} في {n_units(ev['tasks'], 'مهمة', 'gen')}.", "ok")
            else:
                self.add_message(f"تم التراجع عن {n_units(ev['count'], 'عملية', 'acc')}.", "ok")
            self.undo_btn.configure(state="normal")

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
        win.geometry("+%d+%d" % (max(0, self.root.winfo_x() + 20), max(0, self.root.winfo_y() + 120)))   # يُضبط نهائياً بعد البناء
        action = ACTION_AR.get(step["action"], step["action"])
        shown = isolate(sanitize_display(step.get("target", ""), APPROVAL_TARGET_MAX))   # العزل بعد التنظيف
        base = escape_name(target_basename(step.get("target", "")))
        lines = [f"{action}: {shown}"]
        if base:
            lines.append(f"الاسم: {isolate(base)}")
        detail = approval_detail(step.get("params"))
        if detail:
            lines.append(detail)
        title_lbl = tk.Label(win, text="⚠ خطوة تحتاج موافقتك", bg="#fff3e0", fg="#e65100", font=("Segoe UI", 12, "bold"))
        self._approval_label = tk.Label(win, text="\n".join(lines), bg="#fff3e0", font=FONT, justify="right",
                                        wraplength=self.px(420))
        ask_lbl = tk.Label(win, text="هل تسمح بتنفيذ هذه الخطوة فقط؟", bg="#fff3e0", font=FONT)
        btns = tk.Frame(win, bg="#fff3e0")   # آخر ابن للنافذة؛ لكنه يُرزم أولاً في الأسفل فيبقى ظاهراً مهما طال النص
        btns.pack(side="bottom", pady=12)
        title_lbl.pack(side="top", padx=16, pady=(14, 6))
        self._approval_label.pack(side="top", padx=16)
        ask_lbl.pack(side="top", padx=16, pady=6)
        self._clamp_to_screen(win)
        win.after_idle(lambda: self._clamp_to_screen(win))   # بعد استقرار المقاس النهائي (التفاف النص والخطوط)

        def decide(ok: bool):
            self.controller.respond_approval(ok, step.get("approval_id"))
            self._close_approval()

        yes_btn = tk.Button(btns, text="نعم، نفّذ", bg="#2e7d32", fg="white", font=FONT_B, relief="flat", padx=12,
                            state="disabled", command=lambda: decide(True))
        yes_btn.pack(side="right", padx=6)
        tk.Button(btns, text="لا، ارفض", bg="#c62828", fg="white", font=FONT_B, relief="flat", padx=12,
                  command=lambda: decide(False)).pack(side="right", padx=6)

        def _enable():  # نقرة مزدوجة على «نعم» لا توافق على حذف تالٍ قبل قراءته
            try:
                yes_btn.configure(state="normal")
            except Exception:
                pass
        win.after(700, _enable)
        win.protocol("WM_DELETE_WINDOW", lambda: decide(False))  # إغلاق النافذة = رفض

    def _clamp_to_screen(self, win):
        """النافذة كلها داخل حدود الشاشة (الجذر قرب الحافة أو بمقاسات كبيرة لا تُخرج الأزرار من الشاشة)."""
        try:
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            x, y = clamp_rect(self.root.winfo_x() + 20, self.root.winfo_y() + 120, w, h, monitor_bounds(self.root))
            win.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def _ask_undo_confirm(self, ev: Dict[str, Any]):
        """عمليات تراجع لا يمكن التحقق من أصلها: لا تُنفَّذ بصمت. «لا» هو الافتراضي، و«نعم» يُفعَّل بعد 700ms كنافذة الموافقة."""
        self._close_undo_confirm()
        n = int(ev.get("unverified") or 0)
        win = tk.Toplevel(self.root)
        self._undo_confirm_win = win
        win.title("تأكيد التراجع")
        win.attributes("-topmost", True)
        win.configure(bg="#fff3e0")
        text = (f"لا يمكن التأكد من مصدر {n_units(n, 'عملية', 'gen')} سابقة. قد يكون مجلد العمل نُقل من جهاز آخر. "
                "هل تريد التراجع عنها بعد فحص حالة الملفات الآن؟")
        if ev.get("reason"):
            text += "\n" + sanitize_display(ev["reason"], 300)
        title_lbl = tk.Label(win, text="⚠ تراجع غير مؤكَّد المصدر", bg="#fff3e0", fg="#e65100", font=("Segoe UI", 12, "bold"))
        self._undo_confirm_label = tk.Label(win, text=text, bg="#fff3e0", font=FONT, justify="right", wraplength=self.px(420))
        btns = tk.Frame(win, bg="#fff3e0")   # آخر ابن للنافذة؛ يُرزم أولاً في الأسفل
        btns.pack(side="bottom", pady=12)
        title_lbl.pack(side="top", padx=16, pady=(14, 6))
        self._undo_confirm_label.pack(side="top", padx=16, pady=(0, 6))
        scope = ev.get("scope")

        def decide(ok: bool):
            self._close_undo_confirm()
            if ok:
                if scope == "chain":
                    self.controller.undo_chain(allow_unverified=True)
                else:
                    self.controller.undo_last(allow_unverified=True)

        yes_btn = tk.Button(btns, text="نعم، تراجع", bg="#c62828", fg="white", font=FONT_B, relief="flat", padx=12,
                            state="disabled", command=lambda: decide(True))
        yes_btn.pack(side="right", padx=6)
        no_btn = tk.Button(btns, text="لا", bg="#2e7d32", fg="white", font=FONT_B, relief="flat", padx=12,
                           command=lambda: decide(False))
        no_btn.pack(side="right", padx=6)
        self._undo_confirm_yes, self._undo_confirm_no = yes_btn, no_btn
        no_btn.focus_set()
        win.bind("<Return>", lambda e: decide(False))
        win.bind("<Escape>", lambda e: decide(False))
        win.protocol("WM_DELETE_WINDOW", lambda: decide(False))
        win.after(700, lambda: yes_btn.winfo_exists() and yes_btn.configure(state="normal"))
        self._clamp_to_screen(win)
        win.after_idle(lambda: self._clamp_to_screen(win))

    def _close_undo_confirm(self):
        w = getattr(self, "_undo_confirm_win", None)
        if w is not None:
            try:
                w.destroy()
            finally:
                self._undo_confirm_win = None

    def _close_approval(self):
        if self._approval_win is not None:
            try:
                self._approval_win.destroy()
            finally:
                self._approval_win = None
