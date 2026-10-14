"""المتحكم (Controller) — يربط الواجهة بدورة العمل في خيوط منفصلة وطابور أحداث، بلا أي اعتماد على tkinter.

الأحداث (قواميس في event_queue، المفتاح "type"):
  status{text} · questions{questions} · plan{task_id,steps} · step_start · step_done ·
  approval_request{step} · report{success,text,undo_available} · undo{count} · error{text}
"""
import json
import queue
import threading
import uuid
from typing import Any, Dict, List, Optional

from assistant.models.base import ModelProvider, CONTEXT_MARKER
from assistant.models.rules import parse_context, strip_format_chars, merge_clarification, INFO_PREFIX
from assistant.executor import ApprovalTimedOut
from assistant.planner import PlanStep, PlanParseError, sanitize_display
from assistant.runner import TaskRunner
from assistant.safety import KillSwitch, SafetyGuard
from assistant.ledger import SQLiteLedger
from assistant.config import validate_workspace, load_config, save_config, WorkspaceError

MAX_QUESTIONS = 3
MAX_REQUEST_CHARS = 4000
APPROVAL_TIMEOUT_SEC = 120.0

CLARIFY_SYSTEM = (
    "CLARIFY — أنت مساعد مهام مكتبية. إن كان الطلب ناقصاً أو غامضاً فأعد JSON بالشكل "
    '{"questions": ["سؤال قصير", ...]} بحد أقصى 3 أسئلة قصيرة، وإلا أعد {"questions": []}.'
)

# حالات المتحكم
IDLE, CLARIFYING, PLANNING, PLAN_READY, RUNNING = "IDLE", "CLARIFYING", "PLANNING", "PLAN_READY", "RUNNING"


class AssistantController:
    def __init__(self, provider: ModelProvider, allowed_root, ledger: SQLiteLedger, winget_runner=None,
                 approval_timeout: float = APPROVAL_TIMEOUT_SEC, usage_meter=None):
        self.provider = provider
        self.usage_meter = usage_meter
        self._approval_seq = 0
        self._pending_approval = None
        self.kill_switch = KillSwitch()
        self.event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.state = IDLE
        self._gen = 0  # جيل الطلب: الإيقاف يرفع الرقم فيُهمَل أي ناتج متأخر من خيط قديم
        self._lock = threading.RLock()
        self._request = ""
        self._slots: List[Optional[str]] = []
        self._plan: Optional[List[PlanStep]] = None
        self._task_id: Optional[str] = None
        self.last_task_id: Optional[str] = None
        self._approval_event = threading.Event()
        self._approval_result = False
        self._approval_timeout = approval_timeout
        self.ledger = ledger
        self.winget_runner = winget_runner
        self.runner = self._make_runner(allowed_root)
        self.safety = SafetyGuard(self.kill_switch)
        self._wire_cancel(self.provider)
        self._accounted = self._usd_total()  # أعلى إجمالي كلفة سُجّل في العدّاد من هذا المزوّد (ما قبل الآن ليس لهذه الجلسة)

    def _make_runner(self, root) -> TaskRunner:
        return TaskRunner(
            allowed_root=root, provider=self.provider, ledger=self.ledger,
            kill_switch=self.kill_switch, approver=self._approve, winget_runner=self.winget_runner,
        )

    def _wire_cancel(self, prov):
        """يربط زر الإيقاف بإعادة محاولات HTTP في المزوّد (وفي مزوّدي الموجّه)."""
        check = lambda: self.kill_switch.is_triggered  # noqa: E731
        inner = getattr(prov, "fallback", None)   # RuleFirstProvider(ModelRouter(...)) في الإنتاج
        chain = [prov] + ([inner] if inner is not None else [])
        provs = []
        for q in chain:
            provs.append(q)
            provs.extend(getattr(q, "providers", {}).values())
        for p in provs:
            if hasattr(p, "cancel_check") and p.cancel_check is None:
                p.cancel_check = check

    @property
    def workspace(self):
        return self.runner.allowed_root

    def set_workspace(self, path) -> bool:
        """تغيير مجلد العمل (فقط أثناء الخمول) بعد تحقق صارم، مع حفظ الاختيار."""
        with self._lock:
            if self.state != IDLE:
                self.emit("error", text="لا يمكن تغيير المجلد أثناء مهمة جارية.")
                return False
            try:
                p = validate_workspace(path)
            except WorkspaceError as exc:
                self.emit("error", text=f"مجلد غير مقبول: {exc}")
                return False
            try:
                new_runner = self._make_runner(p)
            except Exception as exc:  # مثل رابط باسم .trash: نرفض بدل أن ينهار التطبيق
                self.emit("error", text=f"مجلد غير مقبول: {sanitize_display(exc, 300)}")
                return False
            self.runner = new_runner
            self.last_task_id = None
        cfg = load_config()
        cfg["workspace"] = str(p)
        try:
            save_config(cfg)
        except OSError:
            pass
        self.emit("workspace", path=str(p))
        return True

    # ---------- أحداث ----------
    def emit(self, type_: str, **data):
        self.event_queue.put({"type": type_, **data})

    def drain_events(self) -> List[Dict[str, Any]]:
        out = []
        while True:
            try:
                out.append(self.event_queue.get_nowait())
            except queue.Empty:
                return out

    def next_event(self, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """للاختبارات: انتظار الحدث التالي."""
        try:
            return self.event_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_for(self, type_: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        import time
        end = time.time() + timeout
        while time.time() < end:
            ev = self.next_event(timeout=max(0.01, end - time.time()))
            if ev and ev["type"] == type_:
                return ev
        return None

    def _spawn(self, fn, *args):
        t = threading.Thread(target=fn, args=args, daemon=True)
        t.start()
        return t

    def _go(self, state: str):
        self.state = state

    # ---------- 1) الطلب ----------
    def submit_request(self, text: str):
        text = strip_format_chars(text or "").strip()  # علامات الاتجاه/العرض الصفري تُحذف قبل أي فهم أو تخطيط
        with self._lock:
            if self.state not in (IDLE,):
                return self.emit("error", text="المساعد مشغول بمهمة حالية. أوقفها أو انتظر انتهاءها.")
            if not text:
                return self.emit("error", text="اكتب طلبك أولاً.")
            if len(text) > MAX_REQUEST_CHARS:
                return self.emit("error", text=f"الطلب طويل جداً ({len(text)} حرفاً). اختصره إلى أقل من {MAX_REQUEST_CHARS} حرف.")
            if self._cap_exceeded(text):
                return self.emit("error", text=self.usage_meter.message())
            self.kill_switch.reset()
            self._gen += 1
            gen = self._gen
            self._request = text
            self._plan = None
            self._go(PLANNING)
        self.emit("status", text="أفهم طلبك…")
        self._spawn(self._clarify_then_plan, text, gen)

    def _cap_exceeded(self, text: str = "") -> bool:
        """سقف الإنفاق اليومي يخص المزوّدات المدفوعة فقط؛ والطلب الذي تفهمه القواعد المحلية مجاني فلا يُحجب به."""
        if self.usage_meter is None or not callable(getattr(self.provider, "totals", None)):
            return False
        if not self.usage_meter.exceeded():
            return False
        rules = getattr(self.provider, "rules", None)
        if text and rules is not None:
            try:
                ctx = parse_context(CONTEXT_MARKER + "\n" + self.runner.workspace_listing())
                if rules.understands(text, ctx):
                    return False
            except Exception:
                pass
        return True

    def _is_stale(self, gen: int) -> bool:
        """الإيقاف (أو طلب أحدث) رفع الجيل: ناتج هذا الخيط يُهمَل بلا أثر."""
        with self._lock:
            return gen != self._gen

    def _usd_total(self) -> float:
        fn = getattr(self.provider, "totals", None)
        try:
            return float(fn().get("usd", 0.0)) if callable(fn) else 0.0
        except Exception:
            return 0.0

    def _record_spend(self, before: float):
        """يسجّل كلفة استدعاءات النموذج (فهم/تخطيط) في عدّاد الاستهلاك اليومي حتى لو فشل الطلب.
        علامة مائية مقفلة: خيطان متداخلان (إيقاف ثم طلب جديد) لا يحسب أحدهما كلفة الآخر مرتين."""
        total = self._usd_total()
        with self._lock:
            spent = max(0.0, total - self._accounted)
            self._accounted = max(self._accounted, total)
        if spent > 0 and self.usage_meter is not None:
            try:
                self.usage_meter.add(spent)
            except Exception:
                pass
        return spent

    def _clarify_then_plan(self, text: str, gen: int):
        before = self._usd_total()
        questions = self._ask_questions(text)
        self._record_spend(before)
        if questions:
            with self._lock:
                if gen != self._gen:
                    return
                self._go(CLARIFYING)
            self.emit("questions", questions=questions)
            return
        if self._is_stale(gen):
            return
        self._make_plan(text, gen)

    def _ask_questions(self, text: str) -> List[str]:
        self._slots = []
        try:
            raw = self.provider.generate(
                text, system_prompt=CLARIFY_SYSTEM + "\n\n" + CONTEXT_MARKER + "\n" + self.runner.workspace_listing())
            data = json.loads(raw)
            qs = data.get("questions", []) if isinstance(data, dict) else []
            raw_slots = data.get("slots", []) if isinstance(data, dict) else []
            slots = [raw_slots[i] if i < len(raw_slots) and isinstance(raw_slots[i], str) else None for i in range(len(qs))]
            pairs = [(c, sl) for c, sl in ((sanitize_display(q, 400), sl) for q, sl in zip(qs, slots) if isinstance(q, str)) if c]
            self._slots = [sl for _, sl in pairs][:MAX_QUESTIONS]   # نوع الخانة الناقصة لكل سؤال (من القواعد فقط)
            return [c for c, _ in pairs][:MAX_QUESTIONS]
        except Exception:
            return []  # أي عطب في مرحلة التوضيح لا يمنع التخطيط

    def answer_questions(self, answers: List[str]):
        with self._lock:
            if self.state != CLARIFYING:
                return self.emit("error", text="لا توجد أسئلة بانتظار الإجابة.")
            if self._cap_exceeded(self._request + " " + " ".join(a for a in answers if isinstance(a, str))):
                self._plan = None
                self._go(IDLE)
                return self.emit("error", text=self.usage_meter.message())
            self._gen += 1
            gen = self._gen
            self._go(PLANNING)
        clean = [c for c in (sanitize_display(a, 300) for a in answers if isinstance(a, str)) if c]
        extra = " | ".join(clean)
        slots = getattr(self, "_slots", None) or []
        merged = None
        if len(clean) == 1 and len(slots) == 1:   # جواب عارٍ على سؤال بخانة معروفة ← جملة صريحة («إلى X» / «باسم X»…) بدل لصقه نصاً حراً
            merged = merge_clarification(self._request, slots[0], clean[0])
        full = (merged if merged else self._request + (f"\nتوضيحات المستخدم: {extra}" if extra else ""))[:MAX_REQUEST_CHARS + 600]
        self.emit("status", text="أُعدّ الخطة…")
        self._spawn(self._make_plan, full, gen)

    # ---------- 2) الخطة ----------
    def _make_plan(self, text: str, gen: int):
        before = self._usd_total()
        try:
            plan = self.runner.make_plan(text)
        except Exception as exc:
            self._record_spend(before)
            with self._lock:
                if gen != self._gen:
                    return
                self._go(IDLE)
            msg = sanitize_display(exc, 400)
            if isinstance(exc, PlanParseError) and msg.startswith(INFO_PREFIX):
                # لا شيء للتنفيذ (مثل «لم أجد ملفات مكررة» أو «موجودة أصلاً»): نتيجة عادية لا خطأ
                self.emit("status", text=msg)
                return self.emit("info", text=msg)
            what = "تعذّر فهم الخطة" if isinstance(exc, PlanParseError) else "تعذّر التخطيط"
            return self.emit("error", text=f"{what}: {msg}")
        spent = self._record_spend(before)
        if spent > 0:
            self.emit("status", text=f"كلفة التخطيط: {spent:.4f}$")
        steps = [
            {"step_id": s.step_id, "action": s.action_type, "target": s.target,
             "level": self.safety.classify_action(s.action_type).value,
             "criteria": s.acceptance_criteria, "params": s.params}
            for s in plan
        ]
        estimate = self._estimate(text, len(plan))
        note = self.runner.last_plan_note
        with self._lock:  # الفحص والنشر والإعلان في كتلة واحدة: لا تظهر خطة (ولا يُفعَّل «ابدأ») بعد الإيقاف
            if gen != self._gen:
                return
            self._plan = plan
            self._request_full = text
            self._task_id = "task_" + uuid.uuid4().hex[:10]
            self._go(PLAN_READY)
            self.emit("plan", task_id=self._task_id, steps=steps, estimate=estimate, note=note)

    def _estimate(self, objective: str, n_steps: int) -> Dict[str, Any]:
        """كلفة المهمة المتوقعة بالنقاط والرصيد الحالي وهل يكفيان، لتُعرض قبل الضغط على «ابدأ»."""
        try:
            points = self.ledger.estimate_cost(objective, steps_count=n_steps)
            return {"points": points, "balance": self.ledger.get_balance(), "affordable": bool(self.ledger.can_afford(points))}
        except Exception:
            return {"points": None, "balance": None, "affordable": True}

    def cancel_plan(self):
        with self._lock:
            if self.state in (PLAN_READY, CLARIFYING):
                self._plan = None
                self._go(IDLE)
                self.emit("status", text="أُلغي الطلب.")

    # ---------- 3) التنفيذ ----------
    def start(self):
        with self._lock:
            if self.state != PLAN_READY or not self._plan:
                return self.emit("error", text="لا توجد خطة جاهزة للبدء.")
            plan, task_id, objective = self._plan, self._task_id, getattr(self, "_request_full", self._request)
            self.kill_switch.reset()
            self._go(RUNNING)
        self.emit("status", text="بدأ التنفيذ…")
        self._spawn(self._run, task_id, objective, plan)

    def _run(self, task_id, objective, plan):
        event = None
        try:
            report = self.runner.run(task_id, objective, plan=plan, progress=self._on_progress)
            if report.undo_available:  # مهمة لم تغيّر شيئاً لا تحجب التراجع عن سابقتها
                self.last_task_id = task_id
            event = ("report", dict(success=report.success, text=report.render_arabic_text(),
                                    undo_available=report.undo_available, task_id=task_id))
        except Exception as exc:
            event = ("error", dict(text=f"خطأ غير متوقع: {type(exc).__name__}: {sanitize_display(exc, 400)}"))
        finally:
            with self._lock:  # الحالة تعود لـ IDLE قبل إعلان النتيجة حتى لا يصطدم الطلب التالي
                self._plan = None
                self._go(IDLE)
        if event:
            self.emit(event[0], **event[1])
            try:
                self.emit("status", text=f"رصيدك: {self.runner.ledger.get_balance()} نقطة")
            except Exception:
                pass
            totals = getattr(self.provider, "totals", None)
            if callable(totals):
                t = totals()
                if t.get("calls"):
                    self.emit("status", text=f"استهلاك النموذج حتى الآن: ≈ {t['points']:.2f} نقطة ({t['usd']:.4f}$)")

    def _on_progress(self, ev: Dict[str, Any]):
        kind = ev.pop("event", "progress")
        self.emit(kind, **ev)

    # ---------- 4) الموافقة لكل خطوة خطرة ----------
    def _approve(self, step: PlanStep) -> bool:
        """تُستدعى من خيط التنفيذ: تنتظر قرار المستخدم؛ الإيقاف أو المهلة = رفض. لكل طلب معرّف فريد."""
        with self._lock:
            self._approval_seq += 1
            aid = self._approval_seq
            self._pending_approval = aid
            self._approval_event.clear()
            self._approval_result = False
            if self.kill_switch.is_triggered:  # الإيقاف سبق الطلب (لا ننتظر 120 ثانية)
                self._pending_approval = None
                return False
        self.emit("approval_request", step={
            "step_id": step.step_id, "action": step.action_type, "target": step.target, "params": step.params,
            "approval_id": aid})
        signalled = self._approval_event.wait(timeout=self._approval_timeout)
        with self._lock:
            result = self._approval_result and not self.kill_switch.is_triggered and self._pending_approval == aid
            self._pending_approval = None  # أي ردّ متأخر بعد هذه اللحظة يُتجاهل
            self._approval_event.clear()
            self._approval_result = False
        self.emit("approval_closed", approval_id=aid)  # تُغلق الواجهة النافذة المعلّقة (مهلة/إيقاف/نهاية)
        if not signalled and not self.kill_switch.is_triggered:
            raise ApprovalTimedOut()  # انتهت المهلة دون ردّ: تُعرض في التقرير كمهلة لا كرفض
        return result

    def respond_approval(self, approved: bool, approval_id=None):
        """approval_id: ردّ متأخر أو مكرر على طلب آخر يُتجاهل (لا يُحتسب لخطوة أخرى)."""
        with self._lock:  # الفحص والكتابة معاً: ردّ متأخر لا يتسلل إلى طلب لاحق
            if approval_id is None or approval_id != self._pending_approval:
                return  # بلا معرّف مطابق للطلب المعلّق لا يُقبل ردّ (يمنع ربط موافقة قديمة بخطوة أخرى)
            self._approval_result = bool(approved)
            self._approval_event.set()

    # ---------- 5) الإيقاف والتراجع ----------
    def stop(self, reason: str = "ضغط المستخدم على زر الإيقاف"):
        with self._lock:  # داخل القفل: لا يستطيع start()/submit_request() تصفير إيقاف أحدث منه
            self.kill_switch.trigger(reason)
            self._approval_result = False
            self._approval_event.set()  # يحرر أي نافذة موافقة معلّقة بالرفض
            was_planning = self.state == PLANNING
            if was_planning:
                self._gen += 1  # يُهمل ناتج خيط الفهم/التخطيط الجاري ويتحرر المتحكم فوراً
            if self.state in (PLAN_READY, CLARIFYING, PLANNING):
                self._plan = None
                self._go(IDLE)
        self.emit("status", text="تم الضغط على الإيقاف." + (" أُلغي الطلب." if was_planning else ""))

    def history(self, limit: int = 10):
        try:
            rows = self.ledger.get_history(limit)
        except Exception as exc:
            return self.emit("error", text=f"تعذّر قراءة السجل: {exc}")
        self.emit("history", rows=rows)

    def undo_last(self):
        if self.state == RUNNING:
            return self.emit("error", text="لا يمكن التراجع أثناء التنفيذ. أوقف المهمة أولاً.")
        task_id = self.last_task_id or self.runner.last_journaled_task()  # بعد إعادة التشغيل نقرأ الوسم من السجل المحفوظ
        fm = self.runner.file_manager
        untagged = (not task_id) and bool(fm.journal) and fm.journal[-1].get("task") is None
        if not task_id and not untagged:
            return self.emit("error", text="لا توجد مهمة للتراجع عنها.")
        fm.undo_notes.clear()
        n = self.runner.undo_untagged() if untagged else self.runner.undo_task(task_id)
        notes = list(self.runner.file_manager.undo_notes)
        self.runner.file_manager.undo_notes.clear()
        if self.last_task_id == task_id:
            self.last_task_id = None  # تراجع ثانٍ يستهدف المهمة السابقة لا المهمة نفسها
        if n == 0:
            for note in notes:
                self.emit("status", text=sanitize_display(note, 300))
            return self.emit("error", text="لا يوجد ما يمكن التراجع عنه لهذه المهمة (تم سابقاً، أو اختفى ناتجها، أو توجد مهام أحدث).")
        for note in notes:
            self.emit("status", text=sanitize_display(note, 300))
        self.emit("undo", count=n, task_id=task_id or "")
