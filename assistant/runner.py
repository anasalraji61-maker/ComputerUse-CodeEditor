"""المنسّق (TaskRunner) — دورة العمل الكاملة: دفتر ← تخطيط ← فحص مسبق ← تنفيذ وتدقيق لكل خطوة ← تقرير."""
import os
import re
import shutil
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from assistant.arabic import n_units
from assistant.models.base import ModelProvider
from assistant.planner import Planner, PlanStep, PlanParseError
from assistant.executor import Executor, Approver, StaleStepError
from assistant.verifier import Verifier, VersionProbe
from assistant.reporter import ExecutionReport
from assistant.safety import (
    SafetyGuard, SafetyLevel, KillSwitch,
    EmergencyStopTriggered, ActionForbiddenError, ApprovalRequiredError,
)
from assistant.budget import BudgetTracker, BudgetExceededError
from assistant.ledger import SQLiteLedger, InsufficientBalanceError, SpendingLimitExceededError
from assistant.tools.files import (FileManager, _is_link, tree_size, entry_kind, content_fingerprint, FP_FULL_LIMIT as FP_LIMIT_FULL, human_size,
                                   free_space_short, free_space_margin, name_problem, name_len,
                                   explain_error, UndoResult, MAX_NAME_UNITS, MAX_PATH_CHARS, IS_WINDOWS,
                                   WorkspaceBusyError, UNDO_LOCK_TIMEOUT_SECONDS, BUSY_UNDO_REASON)
from assistant.tools.shell import ShellTool
from assistant.tools.winget import WingetTool


class BudgetCheck:
    """نتيجة محاكاة ميزانية النسخ: مجموع البايتات، السقف، هل تجاوزه، وأول خطوة تجاوزته.
    exact=False: تجاوز المسح حدّ العناصر/الزمن فالمجموع حدّ أدنى فقط (يُعامل كتجاوز للسقف ويطلب موافقة صريحة)."""
    def __init__(self, total: int, cap: Optional[int], over_cap: bool, over_step: Optional[int], exact: bool = True):
        self.total, self.cap, self.over_cap, self.over_step, self.exact = total, cap, over_cap, over_step, exact


_SIM_MAX_ENTRIES = 400_000     # أقصى عناصر تُمسح على القرص أثناء المحاكاة
_SIM_MAX_SECONDS = 10.0
_LISTING_COUNT_MAX = 200_000    # حدّ عدّ عناصر القائمة بعد الاقتطاع (المجموع حدّ أدنى إن بلغه)
_LISTING_COUNT_SECONDS = 3.0
_DUP_OBJECTIVE = re.compile(r"(?<!\w)[وف]?(?:ال)?(?:مكرر\w*|متكرر\w*|تكرار|duplicates?)(?!\w)", re.I)   # طلب «المكررات» (يطابق ما تقرؤه القواعد)
_DUP_STAMP_SECONDS = 8.0       # حدّ زمن بصمات المكررات وقت التخطيط
_DUP_STAMP_BYTES = 256 << 20   # أقصى بايتات تُقرأ كاملةً لبصمات المكررات (بعدها عيّنات فقط)
_DUP_DEST_DIRS = ("مكررات",)


class _VNode:
    """عقدة في نظام ملفات افتراضي غير قابل للتعديل (تعديل = نسخ المسار فقط): ملف (حجم) أو مجلد (أبناء، أو كسول من القرص)."""
    __slots__ = ("is_dir", "size", "kids", "disk", "sz")

    def __init__(self, is_dir: bool, size: int = 0, kids=None, disk: Optional[Path] = None):
        self.is_dir, self.size, self.kids, self.disk, self.sz = is_dir, size, kids, disk, None


class _VFS:
    """يمثّل مجلد العمل كما سيكون بعد كل خطوة: يُحمَّل من القرص كسولاً (lstat فقط) فلا تضيع مجلدات فرعية بعد النقل/إعادة التسمية/النسخ."""

    def __init__(self, root: Path, clock: Callable[[], float] = time.monotonic):
        self.root = _VNode(True, kids=None, disk=root)
        self._rp = root
        self.visited = 0
        self.exact = True
        self._clock = clock
        self._deadline = clock() + _SIM_MAX_SECONDS

    def _load(self, node: _VNode) -> None:
        if not node.is_dir or node.kids is not None:
            return
        kids: Dict[str, Tuple[str, _VNode]] = {}
        node.kids = kids
        if node.disk is None:
            return
        try:
            with os.scandir(str(node.disk)) as it:
                for e in it:
                    if node.disk == self._rp and (e.name == ".trash" or e.name.startswith(".undo_journal")):
                        continue
                    self.visited += 1
                    if self.visited > _SIM_MAX_ENTRIES or self._clock() > self._deadline:
                        self.exact = False   # حدّ أدنى فقط: لا نتابع المسح
                        return
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    import stat as _stat
                    if _stat.S_ISDIR(st.st_mode):
                        kids[e.name.casefold()] = (e.name, _VNode(True, kids=None, disk=Path(e.path)))
                    else:
                        kids[e.name.casefold()] = (e.name, _VNode(False, size=int(st.st_size)))
        except OSError:
            pass

    def get(self, parts: Tuple[str, ...]) -> Optional[_VNode]:
        node = self.root
        for part in parts:
            if not node.is_dir:
                return None
            self._load(node)
            hit = node.kids.get(part.casefold())
            if hit is None:
                return None
            node = hit[1]
        return node

    def size(self, node: Optional[_VNode]) -> int:
        if node is None:
            return 0
        stack = [node]
        order: List[_VNode] = []
        seen: set = set()
        while stack:   # تمرير أول: تحميل كل شيء وتجميع العقد التي لا حجم لها (العقدة المشتركة تُزار مرة)
            n = stack.pop()
            if n.sz is not None or id(n) in seen:
                continue
            seen.add(id(n))
            if not n.is_dir:
                n.sz = n.size
                continue
            self._load(n)
            order.append(n)
            stack.extend(c for _name, c in n.kids.values())
        for n in reversed(order):   # الأبناء قبل الآباء
            n.sz = sum((c.sz if c.sz is not None else 0) for _name, c in n.kids.values())
        return node.sz or 0

    def _set(self, node: _VNode, parts: Tuple[str, ...], i: int, new: Optional[_VNode], name: str) -> _VNode:
        self._load(node)
        kids = dict(node.kids)
        key = parts[i].casefold()
        if i == len(parts) - 1:
            if new is None:
                kids.pop(key, None)
            else:
                kids[key] = (name, new)
        else:
            child = kids.get(key)
            if child is None or not child[1].is_dir:
                child = (parts[i], _VNode(True, kids={}))
            kids[key] = (child[0], self._set(child[1], parts, i + 1, new, name))
        return _VNode(True, kids=kids)

    def put(self, parts: Tuple[str, ...], node: _VNode) -> None:
        if parts:
            self.root = self._set(self.root, parts, 0, node, parts[-1])

    def remove(self, parts: Tuple[str, ...]) -> None:
        if parts and self.get(parts) is not None:
            self.root = self._set(self.root, parts, 0, None, "")

    def mkdirs(self, parts: Tuple[str, ...]) -> None:
        if parts and self.get(parts) is None:
            self.put(parts, _VNode(True, kids={}))


class _CopySim:
    """محاكاة الخطة على نظام ملفات افتراضي: كل خطوة copy/move/rename/delete/create_dir تُطبَّق فعلاً على النسخة الافتراضية،
    فحجم أي مسار هو حجم ما فيه بعد الخطوات السابقة (مجلدات فرعية وإعادة تسمية ونقل ونسخ). الأحجام بـ lstat فقط وبلا سقف مسح يقلّل الرقم."""

    def __init__(self, fm: FileManager, root: Path, cap: Optional[int]):
        self.fm, self.root, self.cap = fm, root, cap
        self.vfs = _VFS(root)
        self.entries: List[Dict[str, Any]] = []   # copy|move: dk وجهة، sk مصدر، src مسار المصدر (لسلسلة الاشتقاق)
        self.pending_feeds: List[Tuple[str, ...]] = []
        self.total_new = 0
        self.over_step: Optional[int] = None

    @property
    def exact(self) -> bool:
        return self.vfs.exact

    def key(self, p: Path) -> Tuple[str, ...]:
        return tuple(x.casefold() for x in self.parts(p))

    def parts(self, p: Path) -> Tuple[str, ...]:
        try:
            return tuple(p.relative_to(self.root).parts)
        except ValueError:
            return tuple(Path(os.path.relpath(str(p), str(self.root))).parts)

    @staticmethod
    def inside(child: Tuple[str, ...], parent: Tuple[str, ...]) -> bool:
        return len(child) > len(parent) and child[:len(parent)] == parent

    @staticmethod
    def contains(parent: Tuple[str, ...], child: Tuple[str, ...]) -> bool:
        return len(child) >= len(parent) and child[:len(parent)] == parent

    def _cover(self, p: Path, upto: int) -> Optional[Tuple[int, Path]]:
        """أحدث حدث (copy/move) قبل upto وجهته تغطي p: يعيد (فهرسه، المسار الأصلي المقابل)."""
        k = self.key(p)
        for i in range(upto - 1, -1, -1):
            e = self.entries[i]
            if k == e["dk"] or self.inside(k, e["dk"]):
                rel = self.parts(p)[len(e["dk"]):]
                return i, e["src"].joinpath(*rel)
        return None

    def lineage(self, p: Path, upto: int, depth: int = 0) -> List[Tuple[str, ...]]:
        """الأشجار التي اشتقت منها بيانات p عبر نسخ سابق (p نفسه أولاً)."""
        trees = [self.key(p)]
        if depth < 64 and not os.path.lexists(str(p)):
            hit = self._cover(p, upto)
            if hit:
                e = self.entries[hit[0]]
                if e["kind"] == "copy":
                    trees.append(e["sk"])
                trees += self.lineage(hit[1], hit[0], depth + 1)
        return trees

    def _unique(self, parts: Tuple[str, ...]) -> Tuple[str, ...]:
        if self.vfs.get(parts) is None:
            return parts
        stem, dot, suffix = parts[-1].rpartition(".") if "." in parts[-1][1:] else (parts[-1], "", "")
        c = 1
        while True:
            cand = parts[:-1] + (f"{stem} ({c}){dot}{suffix}",)
            if self.vfs.get(cand) is None:
                return cand
            c += 1

    def step(self, st: PlanStep) -> None:
        a, fm, vfs = st.action_type, self.fm, self.vfs
        if a == "create_dir":
            vfs.mkdirs(self.parts(fm.validate_path(st.target)))
            return
        if a in ("delete", "trash"):
            src = fm.validate_path(st.target)
            sk = self.key(src)
            vfs.remove(self.parts(src))
            self.pending_feeds = [f for f in self.pending_feeds if not self.contains(sk, f)]
            return
        if a not in ("copy", "move", "rename"):
            return
        src = fm.validate_path(st.target)
        sparts, sk = self.parts(src), self.key(src)
        node = vfs.get(sparts)
        n = len(self.entries)
        if a == "rename":
            dparts = sparts[:-1] + (str(st.params["new_name"]),)
        else:
            dparts = self.parts(fm.validate_path(st.params["dest"]))
        dk = tuple(x.casefold() for x in dparts)
        if a in ("copy", "move") and node is not None and node.is_dir and (self.contains(sk, dk)):
            raise ValueError(f"الخطوة {st.step_id}: لا يمكن {'نسخ' if a == 'copy' else 'نقل'} المجلد '{st.target}' إلى داخل نفسه؛ مرفوض.")
        size = vfs.size(node)
        if a == "copy":
            # حلقة تغذية: صبّ بيانات مشتقة من شجرة T داخل T ثم إعادة نسخ T مراراً يضاعف البيانات (مرة واحدة محسوبة في الميزانية فقط)
            recopies = [f for f in self.pending_feeds if self.contains(sk, f)]
            if recopies:
                raise ValueError(
                    f"الخطوة {st.step_id}: نسخ '{st.target}' يعيد نسخ بيانات صُبّت سابقاً داخل شجرة مصدرها نفسه "
                    f"(حلقة تغذية تضاعف البيانات)؛ مرفوض.")
            if size > 0 and any(self.inside(dk, t) for t in self.lineage(src, n)):
                self.pending_feeds.append(dk)
        if a != "rename":
            dnode = vfs.get(dparts)
            if dnode is not None and dnode.is_dir and dparts != sparts:   # الوجهة مجلد موجود: النسخة تدخله باسم المصدر
                dparts = dparts + (sparts[-1],)
                if a in ("copy", "move") and node is not None and node.is_dir and self.contains(sk, tuple(x.casefold() for x in dparts)):
                    raise ValueError(f"الخطوة {st.step_id}: لا يمكن {'نسخ' if a == 'copy' else 'نقل'} المجلد '{st.target}' إلى داخل نفسه؛ مرفوض.")
        if node is None:
            return   # المصدر غير موجود: سيفشل التنفيذ لاحقاً بنفس الأسلوب المعتاد
        if a == "copy":
            self.total_new += size
            if self.cap is not None and self.total_new > self.cap and self.over_step is None:
                self.over_step = st.step_id
        else:
            vfs.remove(sparts)
            self.pending_feeds = [f for f in self.pending_feeds if not self.contains(sk, f)]
        dparts = self._unique(dparts)
        vfs.put(dparts, node)
        self.entries.append({"kind": "copy" if a == "copy" else "move", "dk": tuple(x.casefold() for x in dparts),
                             "sk": sk, "src": src, "size": size})


class TaskRunner:
    """ينسّق مهمة واحدة أو أكثر على جذر مسموح. كل الاعتماديات قابلة للحقن (R21)."""

    def __init__(
        self,
        allowed_root: Path,
        provider: ModelProvider,
        ledger: SQLiteLedger,
        kill_switch: Optional[KillSwitch] = None,
        approver: Optional[Approver] = None,
        winget_runner: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
        version_probe: Optional[VersionProbe] = None,
        shell_tool: Optional[ShellTool] = None,
        max_steps: int = 420,
        max_tokens: int = 4000,
        max_seconds: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
        usage_meter=None,
        review_enabled: Optional[bool] = None,
    ):
        self.usage_meter = usage_meter            # لحجز كلفة المراجعة المستقلة الاختيارية (verify_review)
        self.review_enabled = review_enabled      # None: من التهيئة (config verify_model / COS_VERIFY_MODEL)، الافتراضي مغلق
        self.allowed_root = Path(allowed_root).resolve()
        self.provider = provider
        self.ledger = ledger
        self.kill_switch = kill_switch or KillSwitch()
        self.approver = approver
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.max_seconds = max_seconds  # سقف زمن المهمة (يشمل انتظار الموافقات): تقصير الخسارة لو علقت أداة
        self._clock = clock
        self.safety = SafetyGuard(kill_switch=self.kill_switch)
        self.file_manager = FileManager(allowed_root=self.allowed_root, kill_switch=self.kill_switch, known_task_ids=self._ledger_knows_task)
        self.shell = shell_tool or ShellTool(reject_roots=(str(self.allowed_root),))
        stop_check = lambda: self.kill_switch.is_triggered  # noqa: E731
        self.winget = WingetTool(runner=winget_runner, stop_check=stop_check) if winget_runner else WingetTool(stop_check=stop_check, reject_roots=(str(self.allowed_root),))
        # فحص winget المستقل الافتراضي: winget list -q <id>؛ مع نفس المشغّل القابل للاستبدال
        self.version_probe = version_probe or (lambda pkg: self.winget.list_packages(pkg))
        bind = getattr(provider, "bind_workspace", None)
        if callable(bind):
            bind(self.allowed_root)  # يسمح لمخطط القواعد بقراءة أحجام/تواريخ الملفات (قراءة فقط)
        self.planner = Planner(provider=provider)
        self.verifier = Verifier(allowed_root=self.allowed_root, version_probe=self.version_probe, kill_switch=self.kill_switch)
        self.last_plan_note: Optional[str] = None
        self.last_listing_skipped: List[str] = []   # عناصر لم تُعرض للمخطط لأن أسماءها غير صالحة (للتبليغ لا للإسقاط الصامت)
        self.last_listing_total: int = 0             # عدد عناصر مجلد العمل (مستويان) التي كان يمكن عرضها: يتجاوز المعروض عند الاقتطاع
        self.last_listing_truncated: bool = False    # هل اقتُطعت القائمة المعروضة؟ (للقواعد: لا تعميم على «كل الملفات» حين True)
        self.last_listing_total_exact: bool = True   # False: توقف العدّ عند حدّ الزمن/العدد فالمجموع حدّ أدنى
        self._journal_starts: Dict[str, int] = {}
        self._journal_ends: Dict[str, int] = {}

    def _short_error(self, exc: BaseException) -> str:
        """رسالة عربية قصيرة لعطب غير متوقع. أخطاء نظام الملفات (OSError) تُصنَّف بنوعها/winerror/errno أولاً فلا يُؤخذ نصها الخام أبداً
        (قد يحوي مساراً إنجليزياً)؛ اختصار «فيه عربي = رسالتنا» لغير OSError فقط، وتُنظَّف منه المسارات المطلقة."""
        why = explain_error(exc)
        if why:
            return why
        msg = str(exc) or ""
        root = str(self.allowed_root)
        msg = msg.replace(root, "").replace("\\", "/")
        msg = re.sub(r"(?:[A-Za-z]:)?(?:/[^/\s'\"،:]+){2,}/([^/\s'\"،:]+)", r"\1", msg)
        msg = re.sub(r"\[Errno \d+\]\s*", "", msg).strip()
        if re.search(r"[\u0600-\u06FF]", msg):
            return msg[:160]
        return "خطأ غير متوقع أثناء التنفيذ"

    def workspace_listing(self, max_entries: int = 60) -> str:
        """قائمة مختصرة بمحتويات مجلد العمل (مستويان) لتزويد المخطط بالسياق. لا تدخل الروابط/التقاطعات.
        عنصر اسمه لا يقبله المخطط (محارف مخادعة/غير صالحة…) لا يُسقَط بصمت: يُحصى في last_listing_skipped ويُلحق بالقائمة سطر «…»
        (يجعل القواعد والمخطط يعاملان القائمة ناقصة فلا يعمّمان على «كل الملفات»)."""
        out: List[str] = []
        skipped: List[str] = []
        truncated = False

        def visible(p: Path, rel: Path) -> Optional[str]:
            if rel.parts[0] == ".trash" or rel.parts[0].startswith(".undo_journal") or _is_link(p):
                return None
            real = rel.as_posix()
            too_long = (any(name_len(part) > MAX_NAME_UNITS for part in rel.parts)
                        or (IS_WINDOWS and len(str(self.allowed_root / rel)) > MAX_PATH_CHARS))
            if too_long or real != real.strip() or name_problem(real):
                skipped.append(real)   # اسم لا يقبله المخطط نفسه (فحص موحّد): لا يُعرض لكنه يُبلَّغ عنه
                return None
            return real + ("/" if p.is_dir() else "")

        total = 0
        exact = True
        deadline = time.monotonic() + _LISTING_COUNT_SECONDS

        def counted(p: Path, rel: Path) -> bool:
            """بعد الاقتطاع: عدّ فقط (بلا فحص أسماء) لعناصر ما كانت لتُخفى (السلة/السجل/الروابط)."""
            return not (rel.parts[0] == ".trash" or rel.parts[0].startswith(".undo_journal") or _is_link(p))

        try:
            for p in sorted(self.allowed_root.iterdir()):
                if truncated:
                    if total >= _LISTING_COUNT_MAX or time.monotonic() > deadline:
                        exact = False
                        break
                    if counted(p, p.relative_to(self.allowed_root)):
                        total += 1
                        if p.is_dir():
                            try:
                                with os.scandir(str(p)) as it:
                                    for c in it:
                                        if not _is_link(Path(c.path)):
                                            total += 1
                                        if total >= _LISTING_COUNT_MAX:
                                            break
                            except OSError:
                                pass
                    continue
                name = visible(p, p.relative_to(self.allowed_root))
                if name is None:
                    continue
                out.append(name)
                total += 1
                if len(out) >= max_entries:
                    truncated = True
                    continue
                if p.is_dir():
                    for c in sorted(p.iterdir()):
                        if truncated:   # بعد الاقتطاع داخل هذا المجلد: نُكمل العدّ فقط (بلا فحص أسماء ولا إضافة لـ skipped)
                            total += counted(c, c.relative_to(self.allowed_root))
                            continue
                        cname = visible(c, c.relative_to(self.allowed_root))
                        if cname is None:
                            continue
                        total += 1
                        out.append(cname)
                        if len(out) >= max_entries:
                            truncated = True
        except OSError:
            pass
        self.last_listing_total, self.last_listing_truncated, self.last_listing_total_exact = total, truncated, exact
        self.last_listing_skipped = skipped
        if truncated:
            out.append("… (القائمة مقتطعة)")
        if skipped:
            shown = "، ".join(sk if len(sk) <= 40 else sk[:37] + "…" for sk in (self._printable(x) for x in skipped[:5]))
            out.append(f"… ({len(skipped)} عنصراً باسم غير صالح للتعامل الآمن لم يُعرض: {shown})")   # صيغة ثابتة: تقرأها القواعد (rules.py)
        return "\n".join(out) if out else "(فارغ)"

    @staticmethod
    def _printable(name: str) -> str:
        """اسم للعرض بلا محارف خفية/تحكمية (تُستبدل بـ ?)."""
        return "".join(c if c.isprintable() and unicodedata.category(c)[0] != "C" else "?" for c in name)

    def make_plan_with_note(self, objective: str) -> Tuple[List[PlanStep], Optional[str]]:
        """يعيد (الخطة، ملاحظتها) ذرّياً: لا قراءة من حالة مشتركة قد يبدّلها طلب آخر متزامن."""
        context = self.workspace_listing()
        pwn = getattr(self.planner, "plan_with_note", None)
        if callable(pwn):
            plan, note = pwn(objective, context=context)
        else:  # مخطط بديل قديم
            plan = self.planner.plan(objective, context=context)
            note = getattr(self.planner, "last_note", None)
        self.last_plan_note = note  # للتوافق فقط (مشتركة)
        try:
            self._stamp_plan(plan, objective)
        except Exception:
            pass   # البصمات حراسة إضافية: عطبها لا يُسقط التخطيط
        return plan, note

    # ---------- بصمات الخطة: ما تغيّر بعد وضع الخطة لا يُنفَّذ ----------
    def _stamp_plan(self, plan: List[PlanStep], objective: str) -> None:
        """يضع في كل خطوة حذف/نقل/نسخ/إعادة تسمية بصمة هدفها وقت التخطيط (النوع + حجم/mtime للملف) فيتخطّى المنفّذ الخطوة إن استُبدل العنصر بمجلد
        (أو العكس) أو عُدّل بعد أن رآها المستخدم. وللحذف/النقل المأخوذ من طلب «المكررات» (أو نقل إلى مجلد «مكررات») يُضاف بصمة المحتوى
        والنسخة المُبقاة (يجب أن تبقى موجودة بمحتواها): تعديل المستخدم لملف بعد التخطيط أو حذف النسخة المُبقاة لا يُتلف شيئاً.
        مسار لمسته خطوة سابقة في الخطة نفسها (وجهة/إنشاء) لا يُبصَم: حالته وقت التنفيذ ناتجة عن الخطة لا عن القرص الحالي."""
        fm = self.file_manager
        touched: List[str] = []

        def norm(x: str) -> str:
            return "/".join(part for part in str(x).replace("\\", "/").split("/") if part not in ("", ".")).casefold()

        def overlaps(rel: str) -> bool:
            return any(rel == t or rel.startswith(t + "/") or t.startswith(rel + "/") for t in touched)

        dup_ctx = bool(_DUP_OBJECTIVE.search(objective or ""))
        victims: List[Tuple[PlanStep, Path]] = []
        for st in plan:
            a = st.action_type
            rel = norm(st.target)
            if a in ("delete", "trash", "move", "copy", "rename") and rel and not overlaps(rel):
                try:
                    p = fm.validate_path(st.target)
                except Exception:
                    p = None
                kind = entry_kind(p) if p is not None else None
                if kind in ("file", "dir"):
                    sig: Dict[str, Any] = {"k": kind}
                    if kind == "file":
                        try:
                            stt = os.lstat(str(p))
                            sig["s"], sig["m"] = int(stt.st_size), int(stt.st_mtime_ns)
                        except OSError:
                            sig = {}
                    if sig:
                        st.sig = sig
                        dest = str(st.params.get("dest", "")).replace("\\", "/")
                        dupish_move = a == "move" and dest.startswith("مكررات/") and dest == "مكررات/" + st.target.replace("\\", "/").replace("/", "_")
                        if (kind == "file" and a in ("delete", "trash", "move") and sig.get("s", 0) > 0
                                and (dup_ctx or dupish_move) and "guard" not in st.params):
                            victims.append((st, p))
            if a in ("create_dir", "copy", "move", "rename"):
                if a == "rename":
                    touched.append(norm(str(Path(st.target).parent / str(st.params.get("new_name", "")))))
                elif a == "create_dir":
                    touched.append(rel)
                else:
                    touched.append(norm(str(st.params.get("dest", ""))))
        if victims:
            self._stamp_duplicates(victims)

    @staticmethod
    def _rel_norm(x: str) -> str:
        return "/".join(part for part in str(x).replace("\\", "/").split("/") if part not in ("", ".")).casefold()

    def _depends_on_skipped(self, step: PlanStep, blocked: List[str]) -> Optional[str]:
        """سبب عربي إن كانت الخطوة تستهدف وجهة خطوة سبق تخطّيها (فلا يوجد ما تعمل عليه)."""
        if not blocked or step.action_type not in ("move", "copy", "rename", "delete", "trash", "inspect"):
            return None
        t = self._rel_norm(step.target)
        if t and any(t == b or t.startswith(b + "/") for b in blocked if b):
            return "الخطوة السابقة التي كانت ستجهّزه لم تُنفَّذ"
        return None

    def _stamp_duplicates(self, victims: List[Tuple[PlanStep, Path]]) -> None:
        """بصمة محتوى كل ضحية ونسخة مُبقاة مطابقة لها (ملف آخر ليس ضحية في الخطة). بلا نسخة مطابقة: تُحرس الضحية وحدها."""
        root = self.allowed_root
        deadline = time.monotonic() + _DUP_STAMP_SECONDS
        budget = [_DUP_STAMP_BYTES]

        def fp(path: Path, size: int) -> Optional[str]:
            full = FP_LIMIT_FULL if budget[0] > 0 else 0   # بعد استنفاد ميزانية القراءة: عيّنات فقط
            budget[0] -= min(size, full)
            return content_fingerprint(path, full)

        vset = {str(p) for _st, p in victims}
        by_size: Dict[int, List[Tuple[PlanStep, Path]]] = {}
        for st, p in victims:
            h = fp(p, st.sig["s"])
            if h:
                st.sig["h"] = h
                by_size.setdefault(st.sig["s"], []).append((st, p))
        # مسح مجلد العمل: ملفات بحجم أحد الضحايا وليست ضحية (ولا في .trash/السجل/«مكررات»/روابط)
        seen = 0
        cand: Dict[int, List[Tuple[str, Path]]] = {}
        stack = [root]
        while stack and time.monotonic() < deadline and seen < _SIM_MAX_ENTRIES:
            d = stack.pop()
            try:
                it = os.scandir(str(d))
            except OSError:
                continue
            with it:
                for e in it:
                    seen += 1
                    if d == root and (e.name == ".trash" or e.name.startswith(".undo_journal") or e.name.casefold() in _DUP_DEST_DIRS):
                        continue
                    try:
                        if e.is_symlink() or _is_link(Path(e.path)):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            sz = int(e.stat(follow_symlinks=False).st_size)
                            if sz in by_size and e.path not in vset:
                                cand.setdefault(sz, []).append((os.path.relpath(e.path, str(root)).replace(os.sep, "/"), Path(e.path)))
                    except OSError:
                        continue
        for sz, group in by_size.items():
            options = sorted(cand.get(sz, ()), key=lambda t: (t[0].count("/"), len(t[0]), t[0].casefold()))
            keep_h: Dict[str, Optional[str]] = {}
            for st, p in group:
                for rel, kp in options:
                    if time.monotonic() > deadline:
                        break
                    try:
                        self.file_manager.validate_path(rel)   # اسم لا يقبله المدير (محجوز/نقطتان…) لا يصلح نسخة مُبقاة تُفحص لاحقاً
                    except Exception:
                        continue
                    h = keep_h.get(rel)
                    if h is None and rel not in keep_h:
                        h = keep_h[rel] = fp(kp, sz)
                    if h and h == st.sig.get("h"):
                        st.sig["keep"], st.sig["ks"], st.sig["kh"] = rel, sz, h
                        break

    def make_plan(self, objective: str) -> List[PlanStep]:
        return self.make_plan_with_note(objective)[0]

    def estimate_plan_cost(self, objective: str, plan: List[PlanStep], skipped_ids=()) -> int:
        """كلفة الخطة بعد طيّ خطوات القراءة/عديمة الأثر المتطابقة (لا حشو لرفع الكلفة)."""
        steps = [s for s in plan if s.step_id not in set(skipped_ids)]
        return self.ledger.estimate_cost(objective, steps_count=len(steps), steps=steps)

    # ---------- التراجع الخاص بالمهمة (R22) ----------
    def _ledger_knows_task(self, task_id: str) -> bool:
        """هل هذا المعرّف مهمة سجّلها الدفتر (قاعدة بيانات التطبيق خارج مجلد العمل)؟ إدخال تراجع غير موثَّق يُنفَّذ بلا تأكيد فقط إن كان كذلك."""
        try:
            with self.ledger._get_connection() as conn:
                return conn.execute("SELECT 1 FROM transactions WHERE task_id = ?", (str(task_id),)).fetchone() is not None
        except Exception:
            return False

    def undo_task(self, task_id: str, allow_unverified: bool = False) -> UndoResult:
        """يتراجع فقط عن عمليات هذه المهمة (من نهاية السجل حتى بدايتها). تعيد UndoResult: تساوي عدد ما تُراجِع عنه (متوافقة مع int)
        ومعها .remaining (ما بقي من عمليات المهمة) و.reason (أول سبب عربي لتوقف أو None) و.notes و.complete و.to_dict()."""
        fm = self.file_manager
        mine = lambda: sum(1 for e in fm.journal if e.get("task") == task_id)   # noqa: E731
        try:
            fm.refresh_journal(UNDO_LOCK_TIMEOUT_SECONDS)   # انتظار قصير: التراجع تفاعلي ولا يجمّد الواجهة وراء نافذة أخرى
        except WorkspaceBusyError:
            return UndoResult(0, remaining=mine(), reason=BUSY_UNDO_REASON)
        except Exception:
            pass
        journal = fm.journal
        n_notes = len(fm.undo_notes)
        start = self._journal_starts.get(task_id)
        if start is None:  # بعد إعادة التشغيل/تبديل المجلد: نستعيد حدود المهمة من وسوم السجل المحفوظ على القرص
            i = len(journal)
            while i > 0 and journal[i - 1].get("task") == task_id:
                i -= 1
            if i == len(journal):
                left = mine()
                return UndoResult(0, remaining=left, reason=("توجد عمليات أحدث بعد هذه المهمة؛ تراجع عنها أولاً" if left else None))
            start = i
        end = self._journal_ends.get(task_id, len(journal))
        if len(journal) > end:  # توجد مهام أحدث بعدها: يجب التراجع عنها أولاً حتى لا تُلغى عملياتها بالخطأ
            return UndoResult(0, remaining=mine(), reason="توجد مهام أحدث بعد هذه المهمة؛ تراجع عنها أولاً")
        def gate(skip: int):
            if len(fm.journal) - skip <= start:
                return False, None
            tag = fm.journal[-1 - skip].get("task")
            if tag is not None and tag != task_id:
                return False, "عملية أحدث تخص مهمة أخرى تعترض الطريق؛ تراجع عنها أولاً"   # حدود قديمة: لا نمسّ عمليات مهمة أخرى
            return True, None

        res = fm.undo_run(gate, UNDO_LOCK_TIMEOUT_SECONDS, allow_unverified=allow_unverified)
        if len(fm.journal) <= start:  # انتهت المهمة: لا تبقى حدودها صالحة لتراجع ثانٍ
            self._journal_starts.pop(task_id, None)
            self._journal_ends.pop(task_id, None)
        return UndoResult(res.done, remaining=max(0, len(fm.journal) - start), reason=res.reason,
                          notes=fm.undo_notes[n_notes:], skipped=res.skipped, missing=res.missing, failures=res.failures,
                          unverified=res.unverified)

    def undo_untagged(self, allow_unverified: bool = False) -> UndoResult:
        """إدخالات آخر السجل بلا وسم مهمة (سجلات قديمة/استخدام مباشر): يمكن التراجع عنها دون معرّف مهمة."""
        fm = self.file_manager
        n_notes = len(fm.undo_notes)
        res = fm.undo_run(lambda skip: (len(fm.journal) > skip and fm.journal[-1 - skip].get("task") is None, None), UNDO_LOCK_TIMEOUT_SECONDS,
                          allow_unverified=allow_unverified)
        rem = 0
        for e in reversed(fm.journal):
            if e.get("task") is not None:
                break
            rem += 1
        return UndoResult(res.done, remaining=rem, reason=res.reason, notes=fm.undo_notes[n_notes:], skipped=res.skipped,
                          missing=res.missing, failures=res.failures, unverified=res.unverified)

    def last_journaled_task(self) -> Optional[str]:
        """معرّف آخر مهمة غيّرت الملفات (من وسم آخر إدخال في السجل المحفوظ)، أو None."""
        journal = self.file_manager.journal
        return journal[-1].get("task") if journal and isinstance(journal[-1].get("task"), str) else None

    def _preflight_paths(self, step: PlanStep) -> None:
        """يرفع استثناءً إن كان أي مسار في الخطوة مرفوضاً (خارج الجذر/محجوز/محمي/رابط) دون أي تنفيذ."""
        fm = self.file_manager
        a = step.action_type
        if a in ("create_dir", "move", "copy", "rename", "delete", "trash") or (a == "inspect" and step.target not in (".", "./")):
            src = fm.validate_path(step.target)
            if a in ("move", "copy", "delete", "trash") and src.exists():
                fm._scan_protected(src)   # مجلد يحوي .git/.ssh… يُرفض الآن لا في منتصف الخطة
            if a == "copy" and src.exists():
                fm._scan_cloud(src)
                fm._scan_links(src)
            if a in ("move", "copy"):
                fm.validate_path(step.params["dest"])
            elif a == "rename":
                fm.validate_path(src.parent / str(step.params["new_name"]))

    def _preflight_budget(self, plan: List[PlanStep], max_bytes: Optional[int] = None) -> "BudgetCheck":
        """يحاكي بايتات النسخ وحركة النقل/الحذف/إنشاء المجلدات عبر الخطة قبل أي تنفيذ. يرفع ValueError بعربية عند:
        حلقة تغذية ذاتية (نسخ يصبّ داخل شجرة مصدره ثم تُنسخ تلك الشجرة ثانيةً) أو وجهة داخل مصدر الخطوة نفسها أو نقص المساحة الحرة
        أو امتلاء السلة. وإن تجاوز المجموع السقف فقط يعيد BudgetCheck.over_cap=True ليطلب run() موافقة صريحة."""
        fm = self.file_manager
        cap = fm.max_copy_bytes if max_bytes is None else max_bytes
        sim = _CopySim(fm, self.allowed_root, cap)
        for st in plan:
            sim.step(st)
        res = BudgetCheck(sim.total_new, cap, (cap is not None and sim.total_new > cap) or not sim.exact, sim.over_step, sim.exact)
        if sim.total_new > 0:
            try:
                free = shutil.disk_usage(str(self.allowed_root)).free
            except OSError:
                free = None
            if free_space_short(sim.total_new, free, fm.max_free_margin):
                raise ValueError(
                    f"المساحة الحرة على القرص ({human_size(free)}) لا تكفي لنسخ {human_size(sim.total_new)} "
                    f"مع هامش أمان ({human_size(free_space_margin(sim.total_new, fm.max_free_margin))}) — لم يُنفَّذ شيء.")
            if fm.trash_cap_bytes is not None:
                tsize = fm.trash_size(fm.trash_cap_bytes)
                if tsize > fm.trash_cap_bytes:
                    raise ValueError(f"سلة المحذوفات .trash ممتلئة ({human_size(tsize)}). أفرغها يدوياً ثم أعد المحاولة — لم يُنفَّذ شيء.")
        return res

    def _approve_large_copy(self, plan: List[PlanStep], check: "BudgetCheck", executor: Executor) -> bool:
        """موافقة صريحة على نسخ كبير عبر قناة موافقات الخطوات الخطرة نفسها (تُسجَّل في approval_log ويظهر سبب الرفض/المهلة/الإيقاف).
        بلا approver = رفض."""
        first = next((s for s in plan if s.action_type == "copy" and s.step_id == check.over_step), None) \
            or next((s for s in plan if s.action_type == "copy"), None)
        if first is None:
            return False
        what = (f"نسخ كبير: مجموع الخطة {human_size(check.total)}" + ("+" if not check.exact else "") + " "
                + (f"يتجاوز السقف الافتراضي {human_size(check.cap)}" if check.cap is not None and check.total > check.cap
                   else "بحجم تعذّر تحديده بدقة (شجرة ضخمة)"))
        warn = PlanStep(step_id=first.step_id, action_type="copy", target=first.target,
                        params=dict(first.params, warning=what), acceptance_criteria="موافقة على حجم نسخ كبير")
        return bool(executor._decide_approval(warn))

    # ---------- التنفيذ ----------
    def run(
        self,
        task_id: str,
        objective: str,
        plan: Optional[List[PlanStep]] = None,
        progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> ExecutionReport:
        """plan: خطة جاهزة (معروضة سابقاً على المستخدم) فلا يُعاد التخطيط؛ progress: ردّ نداء للواجهة."""
        def emit(**ev):
            if progress is not None:
                try:
                    progress(ev)
                except Exception:
                    pass  # عطب الواجهة لا يجب أن يكسر التنفيذ

        fm = self.file_manager
        try:
            fm.refresh_journal()   # نسخة أخرى قد تكون غيّرت السجل: حدود المهمة تُحسب على الحالة الفعلية
        except Exception:
            pass
        journal_start = len(fm.journal)
        prev_start = self._journal_starts.get(task_id)
        prev_end = self._journal_ends.get(task_id)
        self._journal_starts[task_id] = journal_start
        fm.current_task = task_id
        executor = Executor(
            file_manager=fm,
            safety_guard=self.safety,
            budget_tracker=BudgetTracker(
                max_steps=self.max_steps, max_tokens=self.max_tokens, max_points=self.max_steps
            ),
            kill_switch=self.kill_switch,
            shell_tool=self.shell,
            winget_tool=self.winget,
            approver=self.approver,
        )
        evidence: List[Dict[str, Any]] = []
        failures: List[str] = []
        charge = 0
        started = False
        platform_failure = False

        skipped: List[str] = []
        skipped_ids: set = set()
        stale_ids: set = set()      # خطوات تخطّيناها لأن العنصر تغيّر بعد التخطيط
        blocked: List[str] = []     # وجهات خطوات متخطّاة: خطوة لاحقة تستهدفها تُتخطّى أيضاً بدل أن تفشل الخطة

        def finish(success: bool) -> ExecutionReport:
            fm.current_task = None
            if started:
                fm.reset_copy_budget(task_id)
            if len(fm.journal) == journal_start and prev_start is not None:
                # لم تغيّر هذه المحاولة شيئاً: لا نكسر حدود مهمة سابقة بنفس المعرّف
                self._journal_starts[task_id] = prev_start
                if prev_end is not None:
                    self._journal_ends[task_id] = prev_end
            else:
                self._journal_ends[task_id] = len(fm.journal)
            if started:
                if success:
                    try:
                        self.ledger.record_task_success(task_id, charged_points=charge)
                    except (SpendingLimitExceededError, InsufficientBalanceError, ValueError, sqlite3.Error) as exc:
                        failures.append(f"تعذّر تسجيل الكلفة في الدفتر: {exc}. ملاحظة: الملفات تغيّرت فعلاً (يمكنك التراجع).")
                        success = False
                        try:
                            self.ledger.record_task_failure(task_id, reason=str(exc), is_platform_failure=True)
                        except Exception:
                            pass  # الملفات تغيّرت فعلاً والتقرير يوضح ذلك؛ التراجع متاح
                else:
                    try:
                        self.ledger.record_task_failure(
                            task_id, reason="; ".join(failures) or "فشل", is_platform_failure=platform_failure
                        )
                    except Exception as exc:  # عطب الدفتر لا يُسقط التقرير ولا يخفي ما جرى فعلاً على الملفات
                        failures.append(f"تعذّر تسجيل الفشل في الدفتر: {exc}")
            report = ExecutionReport(
                task_id=task_id,
                objective=objective,
                success=success,
                executed_steps=list(executor.executed_log),
                evidence=evidence,
                failures=failures if not success else [],
                points_charged=charge if success else 0,
                undo_available=len(fm.journal) > journal_start,
                undo_journal_path=str(fm.journal_path),
                approvals=list(executor.approval_log),
                skipped=list(skipped),
            )
            if started:   # بعد احتساب الكلفة في الدفتر: مراجعة استشارية اختيارية (مغلقة افتراضياً) لا تغيّر النجاح أبداً
                from assistant.verify_review import attach_review
                attach_review(report, self.provider, self.usage_meter, enabled=self.review_enabled)
            return report

        # 1. فحص زر الإيقاف والقدرة المالية قبل أي شيء
        try:
            self.kill_switch.check()
            est0 = self.ledger.estimate_cost(objective, steps_count=1)
            self.ledger.record_task_start(task_id, objective, est0)
            started = True
            fm.reset_copy_budget(task_id)  # بعد نجاح بدء الدفتر فقط: معرّف مكرر مرفوض لا يمسح ميزانية مهمة جارية بالاسم نفسه
        except EmergencyStopTriggered as exc:
            failures.append(f"تم الإيقاف: {exc}")
            return finish(False)
        except (InsufficientBalanceError, SpendingLimitExceededError) as exc:
            failures.append(f"لم تبدأ المهمة: {exc}")
            return finish(False)
        except Exception as exc:  # معرّف مكرر / قاعدة بيانات مقفلة: لا نترك حالة عالقة ولا نكسر تراجع مهمة سابقة بنفس المعرّف
            failures.append(f"تعذّر بدء المهمة: {exc}")
            if prev_start is None:
                self._journal_starts.pop(task_id, None)
            else:
                self._journal_starts[task_id] = prev_start
            fm.current_task = None
            return ExecutionReport(task_id=task_id, objective=objective, success=False, executed_steps=[],
                                   evidence=[], failures=failures, points_charged=0, undo_available=False,
                                   undo_journal_path=str(fm.journal_path), approvals=[], skipped=[])

        # 2. التخطيط (يُتخطى إن وُفّرت خطة جاهزة)
        try:
            if plan is None:
                plan = self.make_plan(objective)
            else:
                plan = list(plan)
        except PlanParseError as exc:
            failures.append(f"خطة غير صالحة: {exc}")
            platform_failure = True  # خطأ نموذج لا ذنب للمستخدم فيه: لا خصم
            return finish(False)
        except Exception as exc:
            failures.append(f"تعذّر التخطيط: {exc}")
            platform_failure = True
            return finish(False)

        # 3. فحص مسبق للخطة كاملة قبل تنفيذ أي خطوة (محظور / ميزانية / كلفة)
        for s in plan:
            if self.safety.classify_action(s.action_type) == SafetyLevel.FORBIDDEN:
                failures.append(f"الخطة تحوي إجراءً محظوراً '{s.action_type}' (الخطوة {s.step_id}) — لم يُنفَّذ شيء.")
                return finish(False)
        for s in plan:  # كل المسارات تُفحص قبل البدء: لا تنفيذ جزئي ثم رفض في منتصف الخطة
            try:
                self._preflight_paths(s)
            except Exception as exc:
                failures.append(f"مسار مرفوض قبل التنفيذ في الخطوة {s.step_id}: {exc} — لم يُنفَّذ شيء.")
                return finish(False)
        try:
            budget = self._preflight_budget(plan)
        except Exception as exc:
            failures.append(f"خطة مرفوضة قبل التنفيذ (ميزانية النسخ): {exc}")
            return finish(False)
        if len(plan) > self.max_steps:
            failures.append(f"عدد خطوات الخطة {len(plan)} يتجاوز سقف الميزانية {self.max_steps} — لم يُنفَّذ شيء.")
            return finish(False)
        charge = self.estimate_plan_cost(objective, plan)
        if not self.ledger.can_afford(charge):
            failures.append(f"الرصيد أو سقف الإنفاق لا يكفي لكلفة المهمة ({charge} نقطة) — لم يُنفَّذ شيء.")
            charge = 0
            return finish(False)
        if budget.over_cap:  # يتجاوز السقف الافتراضي: يلزم قرارك الصريح (المساحة الحرة تبقى حدّاً صارماً)
            if self._approve_large_copy(plan, budget, executor):
                fm.allow_large_copy(task_id, budget.total + (1 << 20) if budget.exact else (1 << 62))
            else:
                why = {"timeout": "انتهت مهلة الموافقة على الحجم الكبير دون ردّ منك",
                       "stopped": "أُوقفت المهمة أثناء انتظار الموافقة على الحجم الكبير"}.get(executor.last_denial or "", "رفضتَ الموافقة على الحجم الكبير")
                failures.append(f"الخطة تنسخ {human_size(budget.total)}{'' if budget.exact else '+'} وهذا يتجاوز الحدّ المعتاد لمهمة واحدة "
                                f"({human_size(budget.cap) if budget.cap is not None else 'غير محدد'}): {why} — لم يُنفَّذ شيء.")
                return finish(False)

        # 4. تنفيذ وتدقيق مستقل لكل خطوة فور تنفيذها
        befores: Dict[int, Dict[str, Any]] = {}
        infos: Dict[int, Dict[str, Any]] = {}
        ws_fn = getattr(self.verifier, "workspace_snapshot", None)
        try:
            ws_before = ws_fn() if ws_fn is not None else None     # D7: لقطة مجلد العمل كله قبل الخطة (أسماء + أحجام)
            total = len(plan)
            t_start = self._clock()
            for idx, step in enumerate(plan, 1):
                self.kill_switch.check()
                if self._clock() - t_start > self.max_seconds:
                    raise BudgetExceededError(f"تجاوزت المهمة الحد الزمني ({int(self.max_seconds)} ثانية)؛ توقفت قبل الخطوة {step.step_id}.")
                emit(event="step_start", index=idx, total=total, step_id=step.step_id,
                     action=step.action_type, target=step.target)
                before = self.verifier.snapshot(step)
                befores[step.step_id] = before
                dep = self._depends_on_skipped(step, blocked)
                try:
                    if dep:
                        raise StaleStepError(dep)
                    info = executor.execute_step(step)
                except StaleStepError as exc:
                    # العنصر تغيّر بعد وضع الخطة (أو خطوة سابقة تخطّيناها): تُتخطّى هذه الخطوة وحدها وتكمل البقية
                    skipped.append(f"{step.target} ({exc.reason}) فلم يُنفَّذ")
                    skipped_ids.add(step.step_id)
                    stale_ids.add(step.step_id)
                    if step.action_type in ("move", "copy"):
                        blocked.append(self._rel_norm(str(step.params.get("dest", ""))))
                    elif step.action_type == "rename":
                        blocked.append(self._rel_norm(str(Path(step.target).parent / str(step.params.get("new_name", "")))))
                    evidence.append({"item": step.target, "type": "تخطّي: تغيّر بعد التخطيط", "value": exc.reason})
                    emit(event="step_done", index=idx, total=total, step_id=step.step_id, ok=True, detail="تخطّي: " + exc.reason)
                    continue
                except ApprovalRequiredError as exc:
                    if step.action_type.strip().lower() in ("delete", "trash") and executor.last_denial == "denied":
                        # رفض صريح لحذف واحد: نتخطاه ونعرض الباقي بدل إلغاء المهمة كلها
                        skipped.append(f"{step.target} (رفضتَ حذفه فلم يُنفَّذ)")
                        skipped_ids.add(step.step_id)
                        evidence.append({"item": step.target, "type": "تخطّي بقرارك", "value": "لم يُحذف"})
                        emit(event="step_done", index=idx, total=total, step_id=step.step_id, ok=True, detail="تخطّي بقرارك")
                        continue
                    raise
                if isinstance(info, dict):
                    infos[step.step_id] = info
                ok, ev, err = self.verifier.verify_step(step, info, before)
                evidence.append(ev)
                emit(event="step_done", index=idx, total=total, step_id=step.step_id, ok=ok, detail=ev.get("value", ""))
                if not ok:
                    failures.append(err or f"فشل تدقيق الخطوة {step.step_id}")
                    return finish(False)
            self.kill_switch.check()  # الإيقاف أثناء الخطوة الأخيرة لا يُعلَن نجاحاً صامتاً
            if skipped_ids and len(skipped_ids) == len(plan):
                failures.append("لم تُنفَّذ أي خطوة لأن العناصر تغيّرت بعد وضع الخطة (أو رفضتَ بعضها)؛ أعد الطلب لتُبنى خطة جديدة." if stale_ids
                                else "لم تُنفَّذ أي خطوة لأنك رفضت كل الخطوات الخطرة.")
                return finish(False)
            if skipped_ids:  # الكلفة على ما نُفّذ فعلاً فقط
                charge = self.estimate_plan_cost(objective, plan, skipped_ids)
            j_ok, j_ev, j_err = self.verifier.verify_journal(
                [s for s in plan if s.step_id not in skipped_ids], befores, journal_start)
            evidence.append(j_ev)
            if not j_ok:
                failures.append(j_err or "فشل تدقيق سجل التراجع")
                return finish(False)
            if ws_fn is not None:   # D7: أي فرق في مجلد العمل لا تفسّره الخطوات المنفّذة يُفشل التدقيق
                w_ok, w_ev, w_err = self.verifier.verify_workspace(plan, infos, skipped_ids, ws_before, ws_fn())
                evidence.append(w_ev)
                if not w_ok:
                    failures.append(w_err or "تغييرات غير متوقعة في مجلد العمل")
                    return finish(False)
        except EmergencyStopTriggered as exc:
            failures.append(f"تم الإيقاف أثناء التنفيذ: {exc}")
            return finish(False)
        except ApprovalRequiredError as exc:
            why = {"timeout": "انتهت مهلة الموافقة دون ردّ منك فتوقفت المهمة (لم تُنفَّذ الخطوة).",
                   "stopped": "تم الإيقاف أثناء انتظار الموافقة."}.get(executor.last_denial or "", f"رُفضت الموافقة: {exc}")
            failures.append(why)
            return finish(False)
        except ActionForbiddenError as exc:
            failures.append(f"إجراء محظور: {exc}")
            return finish(False)
        except BudgetExceededError as exc:
            failures.append(f"تجاوز الميزانية: {exc}")
            return finish(False)
        except Exception as exc:
            done = len(executor.executed_log)
            if done:
                rest = len(plan) - done
                failures.append(f"فشل التنفيذ: {self._short_error(exc)}. اكتمل تنفيذ {done} من {n_units(len(plan), 'خطوة', 'gen')}"
                                + ("" if rest <= 0 else f"، والمتبقي بلا تنفيذ: {n_units(rest, 'خطوة')}") + "؛ ما نُفّذ يمكن التراجع عنه.")
            else:
                failures.append(f"فشل التنفيذ: {self._short_error(exc)}. لم تُنفَّذ أي خطوة ولم يتغيّر شيء في ملفاتك.")
            return finish(False)

        return finish(True)
