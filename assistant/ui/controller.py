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
from assistant.planner import PlanStep, PlanParseError
from assistant.runner import TaskRunner
from assistant.safety import KillSwitch, SafetyGuard
from assistant.ledger import SQLiteLedger
from assistant.config import validate_workspace, load_config, save_config, WorkspaceError

MAX_QUESTIONS = 3
APPROVAL_TIMEOUT_SEC = 120.0

CLARIFY_SYSTEM = (
    "CLARIFY — أنت مساعد مهام مكتبية. إن كان الطلب ناقصاً أو غامضاً فأعد JSON بالشكل "
    '{"questions": ["سؤال قصير", ...]} بحد أقصى 3 أسئلة قصيرة، وإلا أعد {"questions": []}.'
)

# حالات المتحكم
IDLE, CLARIFYING, PLANNING, PLAN_READY, RUNNING = "IDLE", "CLARIFYING", "PLANNING", "PLAN_READY", "RUNNING"


class AssistantController:
    def __init__(self, provider: ModelProvider, allowed_root, ledger: SQLiteLedger, winget_runner=None,
                 approval_timeout: float = APPROVAL_TIMEOUT_SEC):
        self.provider = provider
        self.kill_switch = KillSwitch()
        self.event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.state = IDLE
        self._planning_cancelled = False
        self._lock = threading.RLock()
        self._request = ""
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

    def _make_runner(self, root) -> TaskRunner:
        return TaskRunner(
            allowed_root=root, provider=self.provider, ledger=self.ledger,
            kill_switch=self.kill_switch, approver=self._approve, winget_runner=self.winget_runner,
        )

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
            self.runner = self._make_runner(p)
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
        text = (text or "").strip()
        with self._lock:
            if self.state not in (IDLE,):
                return self.emit("error", text="المساعد مشغول بمهمة حالية. أوقفها أو انتظر انتهاءها.")
            if not text:
                return self.emit("error", text="اكتب طلبك أولاً.")
            self.kill_switch.reset()
            self._planning_cancelled = False
            self._request = text
            self._plan = None
            self._go(PLANNING)
        self.emit("status", text="أفهم طلبك…")
        self._spawn(self._clarify_then_plan, text)

    def _cancel_if_stopped(self) -> bool:
        """الإيقاف أثناء الفهم/التخطيط يلغي الطلب ولا يتحول إلى خطة جاهزة."""
        with self._lock:
            if self._planning_cancelled:
                self._plan = None
                self._go(IDLE)
                cancelled = True
            else:
                cancelled = False
        if cancelled:
            self.emit("status", text="أُلغي الطلب بسبب الإيقاف.")
        return cancelled

    def _clarify_then_plan(self, text: str):
        questions = self._ask_questions(text)
        if self._cancel_if_stopped():
            return
        if questions:
            with self._lock:
                self._go(CLARIFYING)
            self.emit("questions", questions=questions)
            return
        self._make_plan(text)

    def _ask_questions(self, text: str) -> List[str]:
        try:
            raw = self.provider.generate(
                text, system_prompt=CLARIFY_SYSTEM + "\n\n" + CONTEXT_MARKER + "\n" + self.runner.workspace_listing())
            data = json.loads(raw)
            qs = data.get("questions", []) if isinstance(data, dict) else []
            clean = [q.strip()[:140] for q in qs if isinstance(q, str) and q.strip()]
            return clean[:MAX_QUESTIONS]
        except Exception:
            return []  # أي عطب في مرحلة التوضيح لا يمنع التخطيط

    def answer_questions(self, answers: List[str]):
        with self._lock:
            if self.state != CLARIFYING:
                return self.emit("error", text="لا توجد أسئلة بانتظار الإجابة.")
            self._planning_cancelled = False
            self._go(PLANNING)
        extra = " | ".join(a.strip() for a in answers if a and a.strip())
        full = self._request + (f"\nتوضيحات المستخدم: {extra}" if extra else "")
        self.emit("status", text="أُعدّ الخطة…")
        self._spawn(self._make_plan, full)

    # ---------- 2) الخطة ----------
    def _make_plan(self, text: str):
        try:
            plan = self.runner.make_plan(text)
        except PlanParseError as exc:
            with self._lock:
                self._go(IDLE)
            return self.emit("error", text=f"تعذّر فهم الخطة: {exc}")
        except Exception as exc:
            with self._lock:
                self._go(IDLE)
            return self.emit("error", text=f"تعذّر التخطيط: {exc}")
        if self._cancel_if_stopped():
            return
        with self._lock:
            self._plan = plan
            self._request_full = text
            self._task_id = "task_" + uuid.uuid4().hex[:10]
            self._go(PLAN_READY)
        steps = [
            {"step_id": s.step_id, "action": s.action_type, "target": s.target,
             "level": self.safety.classify_action(s.action_type).value,
             "criteria": s.acceptance_criteria, "params": s.params}
            for s in plan
        ]
        self.emit("plan", task_id=self._task_id, steps=steps)

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
            self.last_task_id = task_id
            event = ("report", dict(success=report.success, text=report.render_arabic_text(),
                                    undo_available=report.undo_available, task_id=task_id))
        except Exception as exc:
            event = ("error", dict(text=f"خطأ غير متوقع: {type(exc).__name__}: {exc}"))
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
        """تُستدعى من خيط التنفيذ: تنتظر قرار المستخدم؛ الإيقاف أو المهلة = رفض."""
        self._approval_event.clear()
        self._approval_result = False
        self.emit("approval_request", step={
            "step_id": step.step_id, "action": step.action_type, "target": step.target, "params": step.params})
        self._approval_event.wait(timeout=self._approval_timeout)
        result = self._approval_result and not self.kill_switch.is_triggered
        self._approval_event.clear()
        self._approval_result = False
        return result

    def respond_approval(self, approved: bool):
        self._approval_result = bool(approved)
        self._approval_event.set()

    # ---------- 5) الإيقاف والتراجع ----------
    def stop(self, reason: str = "ضغط المستخدم على زر الإيقاف"):
        self.kill_switch.trigger(reason)
        self._approval_result = False
        self._approval_event.set()  # يحرر أي نافذة موافقة معلّقة بالرفض
        with self._lock:
            if self.state == PLANNING:
                self._planning_cancelled = True
            if self.state in (PLAN_READY, CLARIFYING):
                self._plan = None
                self._go(IDLE)
        self.emit("status", text="تم الضغط على الإيقاف.")

    def history(self, limit: int = 10):
        try:
            rows = self.ledger.get_history(limit)
        except Exception as exc:
            return self.emit("error", text=f"تعذّر قراءة السجل: {exc}")
        self.emit("history", rows=rows)

    def undo_last(self):
        if self.state == RUNNING:
            return self.emit("error", text="لا يمكن التراجع أثناء التنفيذ. أوقف المهمة أولاً.")
        if not self.last_task_id:
            return self.emit("error", text="لا توجد مهمة للتراجع عنها.")
        n = self.runner.undo_task(self.last_task_id)
        if n == 0:
            return self.emit("error", text="لا يوجد ما يمكن التراجع عنه لهذه المهمة (تم سابقاً، أو اختفى ناتجها، أو توجد مهام أحدث).")
        self.emit("undo", count=n, task_id=self.last_task_id)
