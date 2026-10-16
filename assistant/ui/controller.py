"""المتحكم (Controller) — يربط الواجهة بدورة العمل في خيوط منفصلة وطابور أحداث، بلا أي اعتماد على tkinter.

الأحداث (قواميس في event_queue، المفتاح "type"):
  status{text} · questions{questions} · plan{task_id,steps} · step_start · step_done ·
  approval_request{step} · report{success,text,undo_available} · undo{count} · error{text}
"""
import contextlib
import json
import os
import queue
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from assistant.models.base import ModelProvider, CONTEXT_MARKER
from assistant.models.rules import parse_context, strip_format_chars, merge_clarification, INFO_PREFIX, is_undo_request
from assistant.models.http_base import ProviderError
from assistant.executor import ApprovalTimedOut
from assistant.planner import PlanStep, PlanParseError, sanitize_display
from assistant.runner import TaskRunner
from assistant.safety import KillSwitch, SafetyGuard
from assistant.ledger import SQLiteLedger
from assistant.config import validate_workspace, load_config, save_config, WorkspaceError
from assistant.tools.files import retry_io
from assistant.ui import settings as _settings
from assistant.arabic import n_units

MAX_QUESTIONS = 3
MAX_CLARIFY_ROUNDS = 3   # جولات توضيح متتالية كحد أقصى (سؤال ← جواب ← سؤال…) قبل التخطيط المباشر
MAX_REQUEST_CHARS = 4000
APPROVAL_TIMEOUT_SEC = 120.0

CLARIFY_SYSTEM = (
    "CLARIFY — أنت مساعد مهام مكتبية. إن كان الطلب ناقصاً أو غامضاً فأعد JSON بالشكل "
    '{"questions": ["سؤال قصير", ...]} بحد أقصى 3 أسئلة قصيرة، وإلا أعد {"questions": []}.'
)

# رسائل مشتركة (نص واحد لكل حالة مهما كان مكان اكتشافها)
MSG_UNDO_BUSY = "لا يمكن التراجع الآن. انتظر انتهاء المهمة أو اضغط «إيقاف»، وإن كانت هناك خطة جاهزة فاضغط «إلغاء الخطة»."
MSG_WORKSPACE_BUSY = "لا يمكن تغيير المجلد الآن. انتظر انتهاء المهمة أو اضغط «إلغاء الخطة» أولاً."
MSG_SETTINGS_BUSY = "لا يمكن تغيير الإعدادات الآن. انتظر انتهاء المهمة أو اضغط «إيقاف» ثم أعد المحاولة."
MSG_UNEXPECTED = "حدث خلل غير متوقع. لم يتغيّر شيء في ملفاتك؛ أعد المحاولة، وإن تكرر أرسل ملف crash.log إلى المطوّر."
MSG_UNDO_FAILED = "تعذّر التراجع الآن. تأكد أن الملفات غير مفتوحة في برنامج آخر ثم أعد المحاولة."


_OPEN_WORDS = re.compile(r"(?<!\w)(?:افتح|افتحلي|اعرض|اعرضلي|اظهر|show|open|view)(?!\w)", re.I)
MAX_UNDO_WORDS = 8   # طلب تراجع قصير فقط؛ الطويل (قد يخلط تراجعاً بعمل آخر) لا ينفّذ تراجعاً من الدردشة


def wants_undo(text: str) -> bool:
    """طلب نصي صريح للتراجع (نفس زر «تراجع»). لا نعدّه كذلك إن كان «افتح/اعرض سلة المحذوفات» أو طويلاً يخلط عملاً آخر."""
    t = str(text or "")
    if len(t.split()) > MAX_UNDO_WORDS or _OPEN_WORDS.search(t):
        return False
    try:
        return bool(is_undo_request(t))
    except Exception:
        return False


# حالات المتحكم
IDLE, CLARIFYING, PLANNING, PLAN_READY, RUNNING = "IDLE", "CLARIFYING", "PLANNING", "PLAN_READY", "RUNNING"


class AssistantController:
    def __init__(self, provider: ModelProvider, allowed_root, ledger: SQLiteLedger, winget_runner=None,
                 approval_timeout: float = APPROVAL_TIMEOUT_SEC, usage_meter=None, reports_dir=None):
        self.provider = provider
        self.reports_dir = reports_dir   # مجلد حفظ التقارير؛ None = لا حفظ (الاختبارات). التطبيق يمرر <data_dir>/reports
        self._chain: List[str] = []      # مهام سلسلة طلب واحد (الطلب نفسه يُعاد لإكمال الباقي) لزر «تراجع عن الكل»
        self._chain_key: Optional[str] = None
        self.usage_meter = usage_meter
        _settings.seed_env_secrets()   # مفاتيح البيئة تُحجب من الأحداث والسجلات منذ البدء
        self._incoming_secrets: List[str] = []   # مفتاح قيد التطبيق (قبل اعتماده): يُحجب من كل رسالة وحدث وسجل
        self._session_secrets: Dict[str, str] = {}   # مفاتيح API لهذه الجلسة فقط: في الذاكرة، لا تُكتب في قرص ولا سجل ولا حدث
        self.last_settings_message = ""
        self._pinging = False
        self._settings_gen = 0
        self._approval_seq = 0
        self._pending_approval = None
        self.kill_switch = KillSwitch()
        self.event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.state = IDLE
        self._gen = 0  # جيل الطلب: الإيقاف يرفع الرقم فيُهمَل أي ناتج متأخر من خيط قديم
        self._lock = threading.RLock()
        self._tl = threading.local()   # جيل الطلب الذي يخدمه الخيط الحالي (لإلغاء إعادة محاولات المزوّد لخيط قديم دون أثر زر إيقاف أحدث)
        self._request = ""
        self._clarify_round = 0
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
        self._announce_startup_notices(self.runner)
        self._accounted = self._usd_total()  # أعلى إجمالي كلفة سُجّل في العدّاد من هذا المزوّد (ما قبل الآن ليس لهذه الجلسة)

    def _announce_startup_notices(self, runner):
        """ملاحظات التعافي عند البدء (سجل تراجع تالف، عدّاد استهلاك أُعيد ضبطه…) تصل المستخدم كأحداث info ولا تُمسح قبل عرضها."""
        texts = []
        try:
            texts.extend(list(runner.file_manager.undo_notes))
        except Exception:
            pass
        try:
            fn = getattr(self.usage_meter, "notices", None)
            if callable(fn):
                try:
                    self.usage_meter.spent_today()   # القراءة الأولى تكشف الملف التالف (تُملأ الملاحظات كسولاً)
                except Exception:
                    pass
                texts.extend(fn())
        except Exception:
            pass
        for t in texts:
            c = sanitize_display(t, 400)
            if c:
                self.emit("info", text=c, notice=True)

    def _make_runner(self, root) -> TaskRunner:
        return TaskRunner(
            allowed_root=root, provider=self.provider, ledger=self.ledger,
            kill_switch=self.kill_switch, approver=self._approve, winget_runner=self.winget_runner,
        )

    def _wire_cancel(self, prov):
        """يربط زر الإيقاف بإعادة محاولات HTTP في المزوّد (وفي مزوّدي الموجّه)."""
        check = self._cancel_requested
        inner = getattr(prov, "fallback", None)   # RuleFirstProvider(ModelRouter(...)) في الإنتاج
        chain = [prov] + ([inner] if inner is not None else [])
        rules = getattr(prov, "rules", None)   # قواعد RuleFirstProvider: تجزئة المكررات تحترم زر الإيقاف
        if rules is not None:
            chain.append(rules)
        provs = []
        for q in chain:
            provs.append(q)
            provs.extend(getattr(q, "providers", {}).values())
        _settings.remember_secrets(_settings.provider_keys(prov))   # مفاتيح المزوّد نفسه (قد تأتي من قاموس بيئة لا من os.environ)
        for p in provs:
            if hasattr(p, "cancel_check") and p.cancel_check is None:
                p.cancel_check = check
            if self.usage_meter is not None and hasattr(p, "usage_meter") and getattr(p, "usage_meter", None) is None:
                p.usage_meter = self.usage_meter

    def _cancel_requested(self) -> bool:
        """يُستدعى من حلقات إعادة المحاولة في المزوّد (في خيط العامل نفسه): الإيقاف، أو خيط صار قديماً.
        الجيل لكل خيط: submit_request() يصفّر مفتاح الإيقاف المشترك، لكن خيطاً أُلغي طلبه يبقى ملغى لأن جيله لم يعد الحالي."""
        if self.kill_switch.is_triggered:
            return True
        g = getattr(self._tl, "gen", None)
        return g is not None and g != self._gen

    @contextlib.contextmanager
    def _bound_gen(self, gen: int):
        prev = getattr(self._tl, "gen", None)
        self._tl.gen = gen
        try:
            yield
        finally:
            self._tl.gen = prev

    @property
    def workspace(self):
        return self.runner.allowed_root

    def set_workspace(self, path) -> bool:
        """تغيير مجلد العمل (فقط أثناء الخمول) بعد تحقق صارم، مع حفظ الاختيار."""
        with self._lock:
            if self.state != IDLE:
                self.emit("error", text=MSG_WORKSPACE_BUSY)
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
            self._announce_startup_notices(new_runner)
            # الحفظ والإعلان تحت القفل نفسه: استدعاءان متزامنان لا يتركان المحفوظ مخالفاً للمجلد الفعّال
            try:
                cfg = load_config()
                cfg["workspace"] = str(p)
                save_config(cfg)
            except OSError:
                pass
            self.emit("workspace", path=str(p))
        return True

    # ---------- الإعدادات وفحص الاتصال ----------
    def _redact_secrets(self, text) -> str:
        """سجل المفاتيح المعروفة + قيم البيئة السرية + أنماط المفاتيح، ثم مفاتيح الجلسة والمفتاح الوارد."""
        return _settings.redact(_settings.redact_known(text), list(self._session_secrets.values()) + list(self._incoming_secrets))

    def _scrub(self, v, depth: int = 0):
        if isinstance(v, str):
            return self._redact_secrets(v) if v else v
        if depth < 4:
            if isinstance(v, list):
                return [self._scrub(x, depth + 1) for x in v]
            if isinstance(v, tuple):
                return tuple(self._scrub(x, depth + 1) for x in v)
            if isinstance(v, dict):
                return {k: self._scrub(x, depth + 1) for k, x in v.items()}
        return v

    def _log_exc(self, what: str, exc: BaseException) -> None:
        """التفاصيل التقنية (اسم الصنف ونصه) تذهب إلى crash.log فقط، بعد حجب المفاتيح؛ لا تصل المستخدم."""
        try:
            from assistant.app import write_crash_log
            write_crash_log(self._redact_secrets(f"{what}: {type(exc).__name__}: {sanitize_display(exc, 600)}"))
        except Exception:
            pass

    def provider_info(self) -> Dict[str, Any]:
        return _settings.describe_provider(self.provider, self.usage_meter.cap if self.usage_meter is not None else None)

    def current_settings(self) -> Dict[str, Any]:
        """قيم تعبئة نافذة الإعدادات (لا مفتاح فيها أبداً، فقط هل يوجد مفتاح جلسة)."""
        info = self.provider_info()
        saved = _settings.load_saved(load_config())
        choice = _settings.choice_of(self.provider)
        if info["kind"] == "rules" and saved.get("provider") not in (None, "demo"):
            choice = saved["provider"]   # عاد للقواعد لأن المفتاح ناقص: تبقى نيّة المستخدم ظاهرة ليلصق المفتاح فقط
        return {"provider": choice, "line": info["line"], "kind": info["kind"],
                "small_model": saved.get("small_model", ""), "large_model": saved.get("large_model", ""),
                "workspace": str(self.workspace),
                "daily_cap": self.usage_meter.cap if self.usage_meter is not None else None,
                "has_session_key": bool(self._session_secrets)}

    def apply_settings(self, values) -> bool:
        """يطبّق الإعدادات ذرّياً: يتحقق ويبني سلسلة المزوّدات الجديدة قبل أي تغيير، وأي فشل يترك كل شيء كما كان. فقط أثناء الخمول.
        المفتاح الوارد يُحجب من كل رسالة/حدث/سجل منذ اللحظة الأولى (حتى لو فشل التحقق قبل اعتماده)."""
        incoming = []
        try:
            raw = values.get("api_key") if hasattr(values, "get") else None
            if isinstance(raw, str) and raw.strip():
                incoming = [raw, raw.strip()]
        except Exception:
            incoming = []
        _settings.remember_secrets(incoming)
        with self._lock:
            self._incoming_secrets = incoming
            try:
                return self._apply_settings_locked(values)
            finally:
                self._incoming_secrets = []

    @staticmethod
    def _norm_path(p) -> str:
        """مقارنة مسارات مجلد العمل كما يراها النظام (ويندوز: حالة الأحرف والفواصل لا تُحدث فرقاً)."""
        return os.path.normcase(os.path.abspath(os.path.expanduser(str(p))))

    def _apply_settings_locked(self, values) -> bool:
        if self.state != IDLE:
            return self._settings_fail(MSG_SETTINGS_BUSY)
        secrets = dict(self._session_secrets)
        try:
            norm = _settings.normalize(values)
            if norm["clear_key"]:
                secrets = {}
            choice = norm["provider"]
            if norm["api_key"]:
                kind = norm["key_kind"]
                secrets[_settings.KEY_ENV[kind]] = norm["api_key"]
                if choice == "auto":
                    choice = kind   # المفتاح المُلصق يحدد المزوّد بدل أن يغلبه مفتاح محيط آخر
            env = _settings.build_env(os.environ, choice, secrets, norm["small_model"], norm["large_model"])
            if choice in ("gemini", "anthropic", "openai") and not _settings.key_available(env, choice):
                raise _settings.SettingsError(f"لا يوجد مفتاح لـ {_settings.KEY_LABEL[choice]}. الصق المفتاح في خانة مفتاح الخدمة (يُحفظ لهذه الجلسة فقط).")
            from assistant.models.factory import build_provider
            new_provider, note = build_provider(env)
            ws = norm["workspace"]
            new_ws = None
            if ws and self._norm_path(ws) != self._norm_path(self.workspace):
                new_ws = validate_workspace(ws)
                if self._norm_path(new_ws) == self._norm_path(self.workspace):
                    new_ws = None
        except (_settings.SettingsError, ProviderError, WorkspaceError) as exc:
            return self._settings_fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            self._log_exc("تطبيق الإعدادات", exc)
            return self._settings_fail(_settings.MSG_APPLY_FAILED)
        old_provider, old_runner = self.provider, self.runner
        self.provider = new_provider
        try:
            if new_ws is not None:
                if not self.set_workspace(new_ws):   # يصدر خطأه بنفسه؛ نتراجع عن المزوّد
                    raise RuntimeError("workspace")
            else:
                self.runner = self._make_runner(self.runner.allowed_root)
        except Exception as exc:  # noqa: BLE001
            self.provider, self.runner = old_provider, old_runner
            if str(exc) == "workspace":
                self.last_settings_message = "تعذّر تغيير مجلد العمل، فلم تُطبَّق أي إعدادات."
                return False
            self._log_exc("تجهيز المزوّد الجديد", exc)
            return self._settings_fail(_settings.MSG_APPLY_FAILED)
        self._wire_cancel(new_provider)
        self._accounted = self._usd_total()
        self._session_secrets = secrets
        self._settings_gen += 1
        if norm["daily_cap"] is not None and self.usage_meter is not None:
            self.usage_meter.cap = float(norm["daily_cap"])
        try:   # غير السري فقط
            save_config(_settings.safe_config_update(load_config(), norm))
        except OSError:
            pass
        info = self.provider_info()
        self.last_settings_message = ""
        self.emit("settings_applied", text="تم تطبيق الإعدادات. " + info["line"], kind=info["kind"], label=info["label"])
        return True

    def _settings_fail(self, msg: str) -> bool:
        msg = sanitize_display(self._redact_secrets(msg), 500)
        self.last_settings_message = msg
        self.emit("error", text=msg)
        return False

    def check_connection(self) -> bool:
        """فحص اتصال بطلب صغير جداً في خيط عامل (لا يحجب Tk). النتيجة حدث ping{ok,text,...}."""
        with self._lock:
            if self._pinging:   # فحص جارٍ: لا نكرره (الزر معطّل في الواجهة أيضاً)
                return False
            self._pinging = True
            prov, secrets = self.provider, list(self._session_secrets.values())
        def work():
            try:
                res = _settings.run_ping(prov, self.usage_meter, self._cancel_requested, secrets)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "text": f"فشل الفحص: خطأ غير متوقع ({type(exc).__name__}).", "kind": "other", "label": "", "model": None, "ms": 0, "usd": 0.0}
            finally:
                with self._lock:
                    self._pinging = False
            res["text"] = sanitize_display(self._redact_secrets(res.get("text", "")), 600)
            self.emit("ping", **res)
        threading.Thread(target=work, daemon=True).start()
        return True

    # ---------- أحداث ----------
    def emit(self, type_: str, **data):
        data = {k: self._scrub(v) for k, v in data.items()}   # خط دفاع أخير دائم: لا يخرج مفتاح (من البيئة أو الجلسة) في أي حدث
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

    def _fail_worker(self, gen, expected_states, what: str, exc: BaseException):
        """عامل مات (استثناء غير متوقع/فشل إطلاق الخيط): لا يبقى المتحكم عالقاً في PLANNING/RUNNING."""
        with self._lock:
            if gen is not None and gen != self._gen:
                return   # الإيقاف/طلب أحدث تولّى الحالة أصلاً
            if self.state in expected_states:
                self._plan = None
                self._slots = []
                self._go(IDLE)
            self._log_exc(what, exc)
            self.emit("error", text=f"{what}. {MSG_UNEXPECTED}")

    def _spawn_guarded(self, gen, expected_state, fn, *args):
        """يطلق عاملاً؛ إن فشل الإطلاق نفسه (مثل RuntimeError: can't start new thread) نعيد الحالة إلى IDLE ونُعلن الخطأ."""
        try:
            return self._spawn(fn, *args)
        except BaseException as exc:
            self._fail_worker(gen, (expected_state,), "تعذّر بدء المعالجة", exc)
            if not isinstance(exc, Exception):
                raise

    def _guarded(self, gen, fn, *args):
        """جسم العامل: أي استثناء (حتى BaseException) يُعيد الحالة إلى IDLE ويُعلن خطأ، فلا يعلق المتحكم."""
        try:
            fn(*args)
        except BaseException as exc:
            self._fail_worker(gen, (PLANNING,), "خطأ غير متوقع أثناء التخطيط", exc)
            if not isinstance(exc, Exception):
                raise

    def _go(self, state: str):
        self.state = state

    # ---------- 1) الطلب ----------
    def submit_request(self, text: str):
        text = strip_format_chars(text or "").strip()  # علامات الاتجاه/العرض الصفري تُحذف قبل أي فهم أو تخطيط
        with self._lock:
            if text and len(text) <= MAX_REQUEST_CHARS and wants_undo(text):   # «تراجع»/«ارجع اللي حذفته» كتابةً = زر «تراجع» نفسه (بما فيه الانشغال وتأكيد غير الموثَّق)؛ لا تخطيط ولا تنفيذ غيره
                return self.undo_last()
            if self.state not in (IDLE,):
                return self.emit("error", text="المساعد مشغول بمهمة حالية. انتظر انتهاءها أو اضغط «إيقاف».")
            if not text:
                return self.emit("error", text="اكتب طلبك أولاً.")
            if len(text) > MAX_REQUEST_CHARS:
                return self.emit("error", text=f"الطلب طويل جداً ({len(text)} حرفاً). اختصره إلى أقل من {MAX_REQUEST_CHARS} حرف.")
            self.kill_switch.reset()
            self._gen += 1
            gen = self._gen
            self._request = text
            self._clarify_round = 0
            self._slots = []
            self._plan = None
            self._go(PLANNING)
            self.emit("status", text="أفهم طلبك…")
        self._spawn_guarded(gen, PLANNING, self._clarify_then_plan, text, gen)

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

    def _clarify_then_plan(self, text: str, gen: int, chained: bool = False):
        self._guarded(gen, self._clarify_then_plan_body, text, gen, chained)

    def _cap_blocked(self, text: str, gen: int) -> bool:
        """سقف الإنفاق في خيط العامل (يستدعي تجزئة/قراءة قواعد قد تطول فلا تُنفَّذ على خيط الواجهة)."""
        if not self._cap_exceeded(text):
            return False
        with self._lock:
            if gen == self._gen:
                self._plan = None
                self._go(IDLE)
                self.emit("error", text=self.usage_meter.message())
        return True

    def _clarify_then_plan_body(self, text: str, gen: int, chained: bool = False):
        """chained=True: بعد جواب توضيح. نقبل سؤالاً جديداً من القواعد المحلية فقط (له خانات)؛ سؤال نموذج بلا خانات يُتجاهل
        فلا تدور المحادثة في حلقة، ويستمر التخطيط."""
        with self._bound_gen(gen):
            if self._cap_blocked(text, gen):
                return
            before = self._usd_total()
            questions, slots = self._ask_questions_slots(text)
            self._record_spend(before)
            if chained and not slots:
                questions = []
            if questions:
                with self._lock:
                    if gen != self._gen:
                        return
                    self._request = text   # الجواب التالي يُدمج مع الطلب المحدَّث (بعد دمج الجواب السابق)
                    self._slots = slots   # تُعيَّن تحت القفل بعد فحص الجيل: خيط قديم لا يلوّث خانات طلب أحدث
                    self._go(CLARIFYING)
                    self.emit("questions", questions=questions)   # داخل القفل: إيقاف/إلغاء لا يتخلل التغيير والإعلان
                return
            if self._is_stale(gen):
                return
            self._make_plan(text, gen)

    def _ask_questions(self, text: str) -> List[str]:
        return self._ask_questions_slots(text)[0]

    def _ask_questions_slots(self, text: str):
        """(الأسئلة، نوع الخانة لكل سؤال). لا يكتب حالة المتحكم؛ الاستدعاء يعيّنها تحت القفل."""
        try:
            raw = self.provider.generate(
                text, system_prompt=CLARIFY_SYSTEM + "\n\n" + CONTEXT_MARKER + "\n" + self.runner.workspace_listing())
            data = json.loads(raw)
            qs = data.get("questions", []) if isinstance(data, dict) else []
            raw_slots = data.get("slots", []) if isinstance(data, dict) else []
            if not isinstance(qs, list):   # نص أو قاموس يُمرَّر حرفاً حرفاً كأسئلة وهمية: نتجاهله
                return [], []
            slots_ok = isinstance(raw_slots, list)
            if not slots_ok:
                raw_slots = []
            slots = [raw_slots[i] if i < len(raw_slots) and isinstance(raw_slots[i], str) else None for i in range(len(qs))]
            pairs = [(c, sl) for c, sl in ((sanitize_display(q, 400), sl) for q, sl in zip(qs, slots) if isinstance(q, str)) if c]
            has_slots = slots_ok and "slots" in data   # مفتاح slots يدل على أن السؤال من القواعد المحلية
            return [c for c, _ in pairs][:MAX_QUESTIONS], ([sl for _, sl in pairs][:MAX_QUESTIONS] if has_slots else [])   # الخانة من القواعد فقط
        except Exception:
            return [], []  # أي عطب في مرحلة التوضيح لا يمنع التخطيط

    def answer_questions(self, answers: List[str]):
        if isinstance(answers, str):
            answers = [answers]
        elif not isinstance(answers, (list, tuple)):
            answers = []   # None أو نوع غريب: لا استثناء بعد تغيير الحالة (كان يعلق المتحكم في PLANNING)
        clean = [c for c in (sanitize_display(a, 300) for a in answers if isinstance(a, str)) if c]
        extra = " | ".join(clean)
        with self._lock:
            if self.state != CLARIFYING:
                return self.emit("error", text="لا توجد أسئلة بانتظار الإجابة.")
            request = self._request            # لقطة تحت القفل: لا يتغير الطلب/الخانات بين القراءة والدمج
            slots = list(self._slots or [])
            # أولاً: الجواب قد يكون طلباً جديداً كاملاً («احذف a.txt») أو ملء خانة معروفة («إلى X»، «Docs/a.txt»…)
            merged = None
            if len(clean) == 1:
                merged = merge_clarification(request, slots[0] if len(slots) == 1 else None, clean[0])
            if not merged and slots and all(sl is None for sl in slots):
                # سؤال القواعد بلا خانة (مثل «حدّد مجموعة أصغر»): جواب عارٍ لا يعالجه؛ نطلب طلباً معدّلاً كاملاً بدل لصقه فيفشل
                self._plan = None
                self._slots = []
                self._go(IDLE)
                self.emit("status", text="أعد صياغة الطلب.")
                return self.emit("info", text="هذا السؤال لا يُجاب بكلمة؛ عدّل طلبك (سمِّ الملفات أو قلّل العدد) وأرسله من جديد كاملاً.")
            full = (merged if merged else request + (f"\nتوضيحات المستخدم: {extra}" if extra else ""))[:MAX_REQUEST_CHARS + 600]
            self._clarify_round += 1
            chain = self._clarify_round < MAX_CLARIFY_ROUNDS
            self._request = full   # الطلب المحدَّث (يُبنى عليه سؤال/جواب لاحق)
            self._gen += 1
            gen = self._gen
            self._go(PLANNING)
            self.emit("status", text="أُعدّ الخطة…")
        if chain:   # قد يسأل سؤالاً ثانياً عادياً (حتى 3 أسئلة في الجولة) بدل أن ينتهي بـ«تعذّر فهم الخطة»
            self._spawn_guarded(gen, PLANNING, self._clarify_then_plan, full, gen, True)
        else:
            self._spawn_guarded(gen, PLANNING, self._make_plan_bound, full, gen, True)

    # ---------- 2) الخطة ----------
    @staticmethod
    def _planning_error_text(exc: BaseException, msg: str) -> str:
        """رسالة مزوّد النموذج عربية وكاملة (سبب 401/403/429/شبكة…): تُعرض كما هي بلا بادئة مكررة."""
        if isinstance(exc, ProviderError):
            return sanitize_display(exc, 700)
        if msg.startswith("تعذّر") or msg.startswith("تعذر"):
            return msg
        what = "تعذّر فهم الخطة" if isinstance(exc, PlanParseError) else "تعذّر التخطيط"
        return f"{what}: {msg}"

    def _make_plan_bound(self, text: str, gen: int, check_cap: bool = False):
        self._guarded(gen, self._make_plan_bound_body, text, gen, check_cap)

    def _make_plan_bound_body(self, text: str, gen: int, check_cap: bool):
        with self._bound_gen(gen):
            if check_cap and self._cap_blocked(text, gen):
                return
            self._make_plan(text, gen)

    def _plan_with_note(self, text: str):
        """(الخطة، الملاحظة) معاً. المسار الذري في runner إن وُجد، وإلا نقرأ الملاحظة فور التخطيط."""
        fn = getattr(self.runner, "make_plan_with_note", None)
        if callable(fn):
            plan, note = fn(text)
            return plan, note
        plan = self.runner.make_plan(text)
        return plan, self.runner.last_plan_note

    def _make_plan(self, text: str, gen: int):
        before = self._usd_total()
        try:
            plan, note = self._plan_with_note(text)
        except Exception as exc:
            self._record_spend(before)
            msg = sanitize_display(exc, 400)
            with self._lock:   # الفحص والتغيير والإعلان في كتلة واحدة: لا خطأ/معلومة من طلب قديم بعد طلب أحدث
                if gen != self._gen:
                    return
                self._go(IDLE)
                if isinstance(exc, PlanParseError) and msg.startswith(INFO_PREFIX):
                    # لا شيء للتنفيذ (مثل «لم أجد ملفات مكررة» أو «موجودة أصلاً»): نتيجة عادية لا خطأ
                    self.emit("status", text=msg)
                    return self.emit("info", text=msg)
                return self.emit("error", text=self._planning_error_text(exc, msg))
        spent = self._record_spend(before)
        steps = [
            {"step_id": s.step_id, "action": s.action_type, "target": s.target,
             "level": self.safety.classify_action(s.action_type).value,
             "criteria": s.acceptance_criteria, "params": s.params}
            for s in plan
        ]
        estimate = self._estimate(text, len(plan), plan)
        with self._lock:  # الفحص والنشر والإعلان في كتلة واحدة: لا تظهر خطة (ولا كلفة/«ابدأ») بعد الإيقاف
            if gen != self._gen:
                return
            if spent > 0:
                self.emit("status", text=f"كلفة التخطيط: نحو {spent:.2f} دولار")
            self._plan = plan
            self._request_full = text
            self._task_id = "task_" + uuid.uuid4().hex[:10]
            self._go(PLAN_READY)
            self.emit("plan", task_id=self._task_id, steps=steps, estimate=estimate, note=note)

    def _ensure_trial(self):
        """شحن تجريبي تلقائي (إن وُجد في الدفتر ومفعّل) قبل أي فحص للقدرة على الدفع."""
        fn = getattr(self.ledger, "ensure_trial_balance", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass

    def _estimate(self, objective: str, n_steps: int, plan=None) -> Dict[str, Any]:
        """كلفة المهمة المتوقعة بالنقاط والرصيد الحالي وهل يكفيان، لتُعرض قبل الضغط على «ابدأ»."""
        self._ensure_trial()
        try:
            est_fn = getattr(self.runner, "estimate_plan_cost", None)
            points = est_fn(objective, plan) if (plan is not None and callable(est_fn)) else self.ledger.estimate_cost(objective, steps_count=n_steps)
            return {"points": points, "balance": self.ledger.get_balance(), "affordable": bool(self.ledger.can_afford(points))}
        except Exception:
            return {"points": None, "balance": None, "affordable": True}

    def cancel_plan(self):
        with self._lock:
            if self.state in (PLAN_READY, CLARIFYING):
                self._plan = None
                self._slots = []
                self._go(IDLE)
                self.emit("status", text="أُلغي الطلب.")

    # ---------- 3) التنفيذ ----------
    def start(self):
        self._ensure_trial()   # خارج القفل (قاعدة بيانات)
        with self._lock:
            if self.state != PLAN_READY or not self._plan:
                return self.emit("error", text="لا توجد خطة جاهزة للبدء.")
            plan, task_id, objective = self._plan, self._task_id, getattr(self, "_request_full", self._request)
            self.kill_switch.reset()
            self._go(RUNNING)
            self.emit("status", text="بدأ التنفيذ…")
        self._spawn_guarded(None, RUNNING, self._run, task_id, objective, plan)

    def _run(self, task_id, objective, plan):
        event = None
        undo_available = False
        fatal = None
        try:
            objective = " ".join(str(objective).split())   # سطر واحد: لا أسطر مزوّرة في رأس التقرير («- المطلوب: …»)
            try:
                report = self.runner.run(task_id, objective, plan=plan, progress=self._on_progress)
            finally:
                self._record_spend(0.0)   # كلفة نداءات النموذج داخل التشغيل (مراجعة/تحقق) تدخل العدّاد اليومي حتى لو فشل التشغيل
            undo_available = bool(report.undo_available)
            event = ("report", dict(success=report.success, text=report.render_arabic_text(),
                                    undo_available=report.undo_available, task_id=task_id,
                                    executed=len(report.executed_steps or []), failed=len(report.failures or []),
                                    skipped=len(report.skipped or [])))
        except Exception as exc:
            self._log_exc("خطأ أثناء التنفيذ", exc)
            event = ("error", dict(text=MSG_UNEXPECTED))
        except BaseException as exc:   # الحالة تعود لـ IDLE دائماً ثم يُعاد رفع الاستثناء
            fatal = exc
        if event and event[0] == "report":
            self._save_report(task_id, event[1]["text"])   # IO خارج القفل
        # القراءات البطيئة (قاعدة الرصيد/عدّاد المزوّد) قبل القفل؛ داخله إعلانات فقط
        extra_status = []
        try:
            extra_status.append(f"رصيدك: {n_units(self.runner.ledger.get_balance(), 'نقطة')}")
        except Exception:
            pass
        try:
            totals = getattr(self.provider, "totals", None)
            if callable(totals):
                t = totals()
                if t.get("calls"):
                    extra_status.append(f"استهلاك النموذج الذكي حتى الآن: نحو {t['usd']:.2f} دولار")
        except Exception:
            pass
        # العودة لـ IDLE وإعلان النتيجة في كتلة واحدة: طلب تالٍ لا يستطيع أن يسبق «report» بحدث «plan» في الطابور
        with self._lock:
            if undo_available:  # مهمة لم تغيّر شيئاً لا تحجب التراجع عن سابقتها
                self.last_task_id = task_id
                key = " ".join(str(objective).split()).casefold()
                if key == self._chain_key:   # الطلب نفسه أُعيد لإكمال الباقي: يدخل السلسلة نفسها
                    self._chain.append(task_id)
                else:
                    self._chain_key, self._chain = key, [task_id]
            self._plan = None
            self._go(IDLE)
            if event:
                self.emit(event[0], **event[1])
            for text in extra_status:
                self.emit("status", text=text)
        if fatal is not None:
            raise fatal

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
            signalled = signalled or self._approval_event.is_set()   # موافقة وصلت بين انتهاء الانتظار وأخذ القفل ليست مهلة
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
                self._slots = []
                self._go(IDLE)
        self.emit("status", text="تم الضغط على الإيقاف." + (" أُلغي الطلب." if was_planning else ""))

    def history(self, limit: int = 10):
        try:
            rows = self.ledger.get_history(limit)
        except Exception as exc:
            return self.emit("error", text=f"تعذّر قراءة السجل: {exc}")
        self.emit("history", rows=rows)

    def undo_last(self, allow_unverified: bool = False):
        """allow_unverified=True فقط بعد تأكيد صريح من المستخدم لعمليات لا يمكن التحقق من أصلها (حدث undo_confirm)."""
        with self._lock:   # الفحص والتراجع والتصفير تحت القفل: لا يتداخل مع تشغيل/تراجع آخر ولا يُسحب سجل خاطئ
            try:
                return self._undo_last_locked(allow_unverified)
            except Exception as exc:   # لا يتسرب استثناء إلى معالج Tk
                self._log_exc("خطأ أثناء التراجع", exc)
                return self.emit("error", text=MSG_UNDO_FAILED)

    def chain_size(self) -> int:
        """عدد مهام آخر سلسلة طلب (الطلب نفسه المعاد لإكمال الباقي) القابلة للتراجع دفعة واحدة."""
        with self._lock:
            return len(self._chain)

    def undo_chain(self, allow_unverified: bool = False):
        """«تراجع عن الكل»: يتراجع عن مهام السلسلة الأخيرة من الأحدث إلى الأقدم ويعلن نتيجة واحدة؛ يتوقف عند أول إخفاق."""
        with self._lock:
            try:
                return self._undo_chain_locked(allow_unverified)
            except Exception as exc:
                self._log_exc("خطأ أثناء التراجع عن السلسلة", exc)
                return self.emit("error", text=MSG_UNDO_FAILED)

    def _undo_guard(self):
        """رسالة رفض إن كان التراجع غير ممكن الآن، وإلا None."""
        if self.state != IDLE:
            return MSG_UNDO_BUSY
        return None

    def _undo_core(self, task_id, untagged, allow_unverified=False):
        """ينفّذ تراجع مهمة واحدة (أو المقطع الأخير بلا وسم) ويعيد (n, remaining, skipped, reason, notes, unverified).
        unverified > 0: عمليات لا يمكن التحقق من أصلها لم تُنفَّذ بانتظار تأكيد المستخدم (أعد الاستدعاء بـ allow_unverified=True)."""
        fm = self.runner.file_manager

        def pending() -> int:   # عمليات هذه المهمة (أو المقطع الأخير بلا وسم) التي ما زالت في السجل
            if untagged:
                k = 0
                for e in reversed(fm.journal):
                    if e.get("task") is not None:
                        break
                    k += 1
                return k
            return sum(1 for e in fm.journal if e.get("task") == task_id)
        total = pending()
        n_notes = len(fm.undo_notes)   # لا نمسح الملاحظات (ملاحظات البدء عُرضت أصلاً)؛ نأخذ ما أُضيف في هذا التراجع فقط
        kw = {"allow_unverified": True} if allow_unverified else {}   # لا نمرّر الوسيط إلا عند الحاجة (توافق مع مشغّلين قدماء)
        res = self.runner.undo_untagged(**kw) if untagged else self.runner.undo_task(task_id, **kw)
        n = int(res)
        unverified = int(getattr(res, "unverified", 0) or 0) if getattr(res, "needs_confirmation", False) else 0
        remaining = int(res.remaining) if hasattr(res, "remaining") else pending()
        reason = sanitize_display(res.reason, 300) if getattr(res, "reason", None) else ""
        skipped = int(getattr(res, "skipped", 0) or 0) + int(getattr(res, "missing", 0) or 0) \
            if hasattr(res, "remaining") else max(0, total - n - remaining)
        notes = list(self.runner.file_manager.undo_notes[n_notes:])
        if self.last_task_id == task_id:
            self.last_task_id = None  # تراجع ثانٍ يستهدف المهمة السابقة لا المهمة نفسها
        return n, remaining, skipped, reason, notes, unverified

    def _target_task(self):
        task_id = self.last_task_id or self.runner.last_journaled_task()  # بعد إعادة التشغيل نقرأ الوسم من السجل المحفوظ
        fm = self.runner.file_manager
        untagged = (not task_id) and bool(fm.journal) and fm.journal[-1].get("task") is None
        return task_id, untagged

    def _emit_confirm(self, unverified, reason, scope):
        self.emit("undo_confirm", unverified=int(unverified), reason=reason or "", scope=scope)

    def _undo_last_locked(self, allow_unverified=False):
        # ملاحظة: يبقى التراجع متزامناً (تحت القفل) لأن اختبارات تستدعيه مرتين متتاليتين بلا انتظار؛ UNDOING غير مستعمل.
        why_not = self._undo_guard()
        if why_not:
            return self.emit("error", text=why_not)
        task_id, untagged = self._target_task()
        if not task_id and not untagged:
            return self.emit("error", text="لا توجد مهمة للتراجع عنها.")
        n, remaining, skipped, reason, notes, unverified = self._undo_core(task_id, untagged, allow_unverified)
        if task_id in self._chain and remaining == 0:
            self._chain.remove(task_id)
        for note in notes:
            self.emit("info", text=sanitize_display(note, 300), notice=True)   # رسالة دائمة في المحادثة لا سطر حالة يُمحى
        if unverified:   # لا نُنفّذ ما لا يمكن التحقق من أصله بصمت: نطلب تأكيداً
            if n:
                self.emit("undo", count=n, task_id=task_id or "", remaining=remaining, skipped=skipped, partial=True, reason="")
            return self._emit_confirm(unverified, reason, "last")
        if n == 0:
            if remaining > 0:
                why = f" — {reason}" if reason else " (قد تكون الملفات تغيّرت أو توجد مهام أحدث)"
                return self.emit("error", text=f"تعذّر التراجع: بقيت {n_units(remaining, 'عملية')} لم يُتراجع عنها{why}. انظر الرسائل أعلاه ثم أعد المحاولة.")
            why = f": {reason}" if reason else " (تم سابقاً، أو اختفى ناتجها، أو توجد مهام أحدث)"
            return self.emit("error", text=f"لا يوجد ما يمكن التراجع عنه لهذه المهمة{why}.")
        self.emit("undo", count=n, task_id=task_id or "", remaining=remaining, skipped=skipped,
                  partial=bool(remaining or skipped or reason), reason=reason)

    def _undo_chain_locked(self, allow_unverified=False):
        why_not = self._undo_guard()
        if why_not:
            return self.emit("error", text=why_not)
        # مهام السلسلة التي لم يبقَ لها أي إدخال في السجل (تُراجع سابقاً/اختفى السجل) لا تُبقي السلسلة عالقة
        fm = self.runner.file_manager
        self._chain[:] = [t for t in self._chain if any(e.get("task") == t for e in fm.journal)]
        if not self._chain:
            return self.emit("info", text="لا يوجد ما يمكن التراجع عنه في هذه السلسلة (تم التراجع عنها سابقاً أو السجل فارغ).")
        total_n = total_skipped = tasks = 0
        last_remaining, last_reason, all_notes, blocked = 0, "", [], False
        need_unverified, need_reason = 0, ""
        while self._chain:
            task_id, untagged = self._target_task()
            if not task_id or task_id not in self._chain:
                blocked = True
                break   # المهمة التالية في السجل ليست من هذه السلسلة (أو اختفت): نتوقف
            n, remaining, skipped, reason, notes, unverified = self._undo_core(task_id, untagged, allow_unverified)
            all_notes.extend(notes)
            total_n += n
            total_skipped += skipped
            if unverified:
                need_unverified, need_reason = unverified, reason
                break   # نتوقف وننتظر تأكيد المستخدم
            if remaining == 0:
                self._chain.remove(task_id)
                tasks += 1
            else:
                last_remaining, last_reason = remaining, reason
                break   # لم تُستعد كل عمليات هذه المهمة: لا نتجاوزها إلى الأقدم
        for note in all_notes:
            self.emit("status", text=sanitize_display(note, 300))
        left = len(self._chain)
        if need_unverified:
            if total_n:
                self.emit("undo", count=total_n, task_id="", remaining=0, skipped=total_skipped, tasks=tasks,
                          chain_left=left, partial=True, reason="")
            return self._emit_confirm(need_unverified, need_reason, "chain")
        if total_n == 0:
            if not (last_remaining or last_reason or blocked):
                self._chain.clear()   # لا شيء يُتراجع عنه وبلا عائق: السلسلة منتهية فلا تُعاد رسالة الخطأ كل مرة
                return self.emit("info", text="لا يوجد ما يمكن التراجع عنه في هذه السلسلة (تم التراجع عنها سابقاً أو اختفى ناتجها).")
            why = f" — {last_reason}" if last_reason else ""
            return self.emit("error", text=f"تعذّر التراجع عن آخر طلب{why}. انظر الرسائل أعلاه.")
        self.emit("undo", count=total_n, task_id="", remaining=last_remaining, skipped=total_skipped, tasks=tasks,
                  chain_left=left, partial=bool(last_remaining or total_skipped or last_reason or left), reason=last_reason)

    # ---------- 6) حفظ التقارير ----------
    _SECRET_RE = re.compile(
        r"(?<![A-Za-z0-9])(?:"
        r"sk-ant-[A-Za-z0-9_\-]{20,}|sk-proj-[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9]{20,}"   # Anthropic / OpenAI
        r"|AIza[0-9A-Za-z_\-]{30,}"                                                       # Google
        r"|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[bp]-[A-Za-z0-9\-]{10,}"   # GitHub / Slack
        r"|AKIA[0-9A-Z]{16}"                                                               # AWS
        r"|(?i:bearer)\s+[A-Za-z0-9._~+/\-]{20,}=*"                                       # ترويسة Authorization
        r")")

    @classmethod
    def _clean_report_text(cls, text: str) -> str:
        """نص آمن للحفظ: بلا عوازل اتجاه ولا محارف تحكم (عدا السطر الجديد) ولا ما يشبه مفاتيح API."""
        t = "".join(ch for ch in str(text) if ch not in "\u2066\u2067\u2068\u2069" and (ch == "\n" or ch >= " ") and ch != "\x7f")
        return cls._SECRET_RE.sub("[محجوب]", t)

    @staticmethod
    def _safe_report_name(task_id) -> str:
        return re.sub(r"[^A-Za-z0-9_\-]", "_", str(task_id))[:80] or "task"

    def _save_report(self, task_id, text):
        """يحفظ تقرير المهمة في <reports_dir>/<task_id>.txt (كتابة ذرّية، يحتفظ بآخر 200). لا يرفع أبداً."""
        if not self.reports_dir:
            return
        try:
            d = Path(self.reports_dir)
            d.mkdir(parents=True, exist_ok=True)
            name = self._safe_report_name(task_id)
            tmp = d / f".{name}.tmp"
            tmp.write_text(self._clean_report_text(text), encoding="utf-8")
            retry_io(os.replace, tmp, d / f"{name}.txt")   # ويندوز: قارئ/مضاد فيروسات قد يقفل الهدف لحظياً
            files = sorted(d.glob("*.txt"), key=lambda f: (f.stat().st_mtime, f.name))   # تعادل الوقت: بالاسم
            for old in files[:-200]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except Exception:
            pass

    def list_reports(self, limit: int = 20) -> List[Dict[str, Any]]:
        """آخر التقارير المحفوظة (الأحدث أولاً): task_id والوقت وعنوان السطر الأول."""
        out: List[Dict[str, Any]] = []
        if not self.reports_dir:
            return out
        try:
            files = sorted(Path(self.reports_dir).glob("*.txt"), key=lambda f: (f.stat().st_mtime, f.name), reverse=True)[:max(1, int(limit))]
            import time
            for f in files:
                try:
                    first = f.read_text(encoding="utf-8").split("\n", 1)[0]
                except OSError:
                    first = ""
                out.append({"task_id": f.stem, "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
                            "title": sanitize_display(first.lstrip("# "), 80)})
        except Exception:
            pass
        return out

    def read_report(self, task_id) -> Optional[str]:
        if not self.reports_dir:
            return None
        try:
            return (Path(self.reports_dir) / f"{self._safe_report_name(task_id)}.txt").read_text(encoding="utf-8")
        except Exception:
            return None
