"""المخطط المحلي بالقواعد (RuleProvider): يفهم الطلبات العربية الشائعة ويبني خطة من محتويات المجلد الفعلية.

بلا شبكة ولا مفاتيح ولا كلفة نموذج. يُستعمل وحده افتراضياً، أو أولاً قبل النموذج الحقيقي (RuleFirstProvider)
فلا يُصرف رصيد API على طلب معروف الشكل. ما لا يفهمه يسأل عنه سؤالاً واحداً (أو يُحال للنموذج في الوضع المختلط).
"""
import json
import re
import datetime
from typing import Callable, Dict, List, Optional, Tuple

from .base import ModelProvider, CONTEXT_MARKER

MAX_AUTO_STEPS = 48  # أقل من حد الخطة (50) بهامش

CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "صور": ("jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "heic", "tiff", "ico"),
    "مستندات": ("pdf", "doc", "docx", "txt", "rtf", "odt", "md"),
    "جداول": ("xls", "xlsx", "csv", "ods"),
    "عروض": ("ppt", "pptx", "odp"),
    "فيديو": ("mp4", "mkv", "avi", "mov", "wmv", "flv", "webm"),
    "صوتيات": ("mp3", "wav", "flac", "aac", "ogg", "m4a"),
    "مضغوطات": ("zip", "rar", "7z", "tar", "gz"),
    "برامج": ("exe", "msi", "apk", "dmg"),
}
EXT_TO_CAT = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
OTHER = "أخرى"
HELP_QUESTION = ("لم أفهم الطلب. اكتبه بإحدى هذه الصيغ: «رتّب الملفات حسب النوع» · «انسخ <اسم> إلى <مجلد>» · "
                 "«أعد تسمية ملفات <مجلد> بإضافة البادئة <نص>» · «أنشئ مشروع <اسم>» · «افحص <اسم>» · «حدّث البرنامج <المعرّف>».")

_DIACRITICS = re.compile(r"[\u064B-\u0652\u0640]")


def norm(text: str) -> str:
    """توحيد الحروف العربية (همزات، ياء، ألف مقصورة) وحذف التشكيل لتسهيل المطابقة."""
    t = _DIACRITICS.sub("", text)
    t = re.sub("[أإآ]", "ا", t).replace("ى", "ي").replace("ة", "ه")
    return t.lower()


def parse_context(system_prompt: str) -> List[str]:
    if CONTEXT_MARKER not in system_prompt:
        return []
    block = system_prompt.split(CONTEXT_MARKER, 1)[1]
    return [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.startswith("…") and ln.strip() != "(فارغ)"]


def _step(i: int, action: str, target: str, criteria: str, **params) -> dict:
    return {"step_id": i, "action_type": action, "target": target, "params": params, "acceptance_criteria": criteria}


def _safe_name(name: str) -> Optional[str]:
    name = name.strip().strip("«»\"'،,.؛:")
    if not name or name in (".", "..") or re.search(r'[\\/:*?"<>|]', name):
        return None
    return name


class Analysis:
    def __init__(self, steps=None, question=None, note=None):
        self.steps, self.question, self.note = steps, question, note


class RuleProvider(ModelProvider):
    """يحوّل الطلب العربي إلى خطة JSON. الأنواع: ترتيب حسب النوع، إعادة تسمية بادئة، نسخ، إنشاء، فحص، winget، whoami."""

    def __init__(self, today: Optional[Callable[[], datetime.date]] = None):
        self._today = today or datetime.date.today

    def name(self) -> str:
        return "RuleProvider(Local)"

    # ---------- واجهة المزوّد ----------
    def generate(self, prompt: str, system_prompt: str = "") -> str:
        a = self.analyze(prompt, parse_context(system_prompt))
        if "CLARIFY" in system_prompt:
            return json.dumps({"questions": [a.question] if a.question else []}, ensure_ascii=False)
        if a.steps:
            return json.dumps({"steps": a.steps}, ensure_ascii=False)
        return json.dumps({"steps": [], "note": a.note or a.question or HELP_QUESTION}, ensure_ascii=False)

    def understands(self, prompt: str, context: List[str]) -> bool:
        a = self.analyze(prompt, context)
        return bool(a.steps)

    # ---------- التحليل ----------
    def analyze(self, prompt: str, context: List[str]) -> Analysis:
        p = norm(prompt)
        top_files = [e for e in context if "/" not in e]
        top_dirs = [e.rstrip("/") for e in context if e.endswith("/") and e.count("/") == 1]
        nested = [e for e in context if e.count("/") >= 1 and not e.endswith("/")]

        def has(*words):
            return any(norm(w) in p for w in words)

        if has("whoami", "من انا", "هويه", "اسم المستخدم"):
            return Analysis([_step(1, "run_command", "whoami", "رمز الخروج 0", cmd=["whoami"])])
        pkg = re.search(r"\b[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9.]*\b", prompt)
        if has("حدث", "تحديث", "ترقيه", "winget", "upgrade") and pkg:
            return Analysis([_step(1, "winget_upgrade", pkg.group(0), "رمز الخروج 0 والحزمة ظاهرة في winget list")])
        if has("اعد تسميه", "غير اسم", "تسميه") and has("بادئه", "prefix", "بدايه", "بإضافه", "باضافه", "اضافه"):
            return self._rename(prompt, p, context, top_dirs)
        if has("انسخ", "نسخ ", "نسخه احتياطيه", "backup"):
            return self._copy(prompt, p, context, top_dirs)
        if has("رتب", "ترتيب", "نظم", "تنظيم", "صنف", "فرز", "organize"):
            return self._organize(top_files)
        if has("انشئ", "انشاء", "اعمل", "جهز") and has("مجلد", "مشروع", "هيكل", "folder", "project"):
            return self._create(prompt)
        if has("افحص", "اعرض", "فحص", "تحقق", "inspect", "ماذا يوجد", "ما محتوي"):
            return self._inspect(p, context)
        return Analysis(question=HELP_QUESTION)

    # ---------- النوايا ----------
    def _organize(self, top_files: List[str]) -> Analysis:
        files = [f for f in top_files if not f.startswith(".")]
        if not files:
            return Analysis(note="لا توجد ملفات مبعثرة في مجلد العمل لترتيبها.")
        moves, cats = [], []
        for f in sorted(files, key=str.lower)[:MAX_AUTO_STEPS]:
            ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
            cat = EXT_TO_CAT.get(ext, OTHER)
            if cat not in cats:
                cats.append(cat)
            moves.append((f, cat))
        # حد الخطوات: المجلدات المطلوبة + النقلات
        while len(cats) + len(moves) > MAX_AUTO_STEPS:
            moves.pop()
            cats = [c for c in cats if any(m[1] == c for m in moves)]
        steps, i = [], 1
        for c in cats:
            steps.append(_step(i, "create_dir", c, "وجود المجلد")); i += 1
        for f, c in moves:
            steps.append(_step(i, "move", f, "المصدر اختفى والوجهة مطابقة", dest=f"{c}/{f}")); i += 1
        return Analysis(steps)

    def _rename(self, prompt: str, p: str, context: List[str], top_dirs: List[str]) -> Analysis:
        m = (re.search(r"(?:بادئه|بادئة|prefix)\s*[:：]?\s*[«\"']?([^\s«»\"'،,]+)", prompt)
             or re.search(r"(?:بإضافة|باضافة|بإضافه|اضافة|إضافة)\s+[«\"']?([^\s«»\"'،,]+)[»\"']?\s+(?:في|الى|إلى|ل)?\s*(?:بداية|اول|أول)", prompt))
        prefix = None
        if norm("التاريخ") in p or "date" in p:
            prefix = self._today().isoformat() + "_"
        elif m:
            prefix = _safe_name(m.group(1))
        if not prefix:
            return Analysis(question="ما البادئة التي تريد إضافتها لأسماء الملفات؟ مثال: «بادئة 2026_»")
        folder = next((d for d in sorted(top_dirs, key=len, reverse=True) if norm(d) in p), None)
        if folder:
            files = [e for e in context if e.startswith(folder + "/") and e.count("/") == 1 and not e.endswith("/")]
        else:
            files = [e for e in context if "/" not in e]
        files = [f for f in files if not f.split("/")[-1].startswith(prefix) and not f.split("/")[-1].startswith(".")]
        if not files:
            return Analysis(note="لا توجد ملفات مناسبة لإعادة التسمية" + (f" داخل {folder}." if folder else "."))
        steps = [_step(i, "rename", f, "الاسم الجديد موجود والمحتوى مطابق", new_name=prefix + f.split("/")[-1])
                 for i, f in enumerate(sorted(files)[:MAX_AUTO_STEPS], 1)]
        return Analysis(steps)

    def _find_entry(self, p: str, context: List[str]) -> Optional[str]:
        best = None
        for e in context:
            if e.count("/") > (1 if e.endswith("/") else 1):
                continue
            base = e.rstrip("/").split("/")[-1]
            if base and norm(base) in p and (best is None or len(base) > len(best.rstrip("/").split("/")[-1])):
                best = e
        return best.rstrip("/") if best else None

    def _copy(self, prompt: str, p: str, context: List[str], top_dirs: List[str]) -> Analysis:
        src = self._find_entry(p.split(norm("الى"))[0] if norm("الى") in p else p, context)
        if not src:
            return Analysis(question="ما اسم الملف أو المجلد الذي تريد نسخه؟ اكتبه كما يظهر في مجلد العمل.")
        base = src.split("/")[-1]
        m = re.search(r"(?:الى|إلى)\s+(?:مجلد\s+)?[«\"']?([^\s«»\"'،,]+)", prompt)
        dest_dir = _safe_name(m.group(1)) if m else "نسخة_احتياطية"
        if not dest_dir:
            return Analysis(question="ما اسم المجلد الذي تريد النسخ إليه؟")
        steps, i = [], 1
        if dest_dir not in top_dirs:
            steps.append(_step(i, "create_dir", dest_dir, "وجود المجلد")); i += 1
        steps.append(_step(i, "copy", src, "الوجهة مطابقة للمصدر والمصدر سليم", dest=f"{dest_dir}/{base}"))
        return Analysis(steps)

    def _create(self, prompt: str) -> Analysis:
        is_project = bool(re.search(r"مشروع|هيكل|project", prompt))
        m = re.search(r"(?:باسم|اسمه|اسمها)\s+[«\"']?([^\s«»\"'،,]+)", prompt) or \
            re.search(r"(?:مشروع|مجلد|هيكل مشروع|folder|project)\s+(?:جديد\s+)?(?:باسم\s+)?[«\"']?([A-Za-z0-9_\-\u0600-\u06FF]+)", prompt)
        name = _safe_name(m.group(1)) if m else None
        if not name or name in ("جديد", "جديدا", "باسم", "تجريبي", "تجريبيا") and not m:
            name = None
        if not name:
            return Analysis(question="ما اسم المجلد أو المشروع الذي تريد إنشاءه؟")
        if is_project:
            return Analysis([
                _step(1, "create_dir", f"{name}/src", "وجود المجلد"),
                _step(2, "create_dir", f"{name}/docs", "وجود المجلد"),
                _step(3, "create_dir", f"{name}/tests", "وجود المجلد"),
            ])
        return Analysis([_step(1, "create_dir", name, "وجود المجلد")])

    def _inspect(self, p: str, context: List[str]) -> Analysis:
        e = self._find_entry(p, context)
        return Analysis([_step(1, "inspect", e or ".", "العنصر موجود فعلاً")])


class RuleFirstProvider(ModelProvider):
    """قواعد أولاً: الطلب المعروف يُخطَّط محلياً بلا كلفة، وغيره يُحال للنموذج الحقيقي."""

    def __init__(self, fallback: ModelProvider, rules: Optional[RuleProvider] = None):
        self.fallback = fallback
        self.rules = rules or RuleProvider()
        self.rule_hits = 0
        self.fallback_calls = 0

    def name(self) -> str:
        return f"RuleFirst({self.fallback.name()})"

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        ctx = parse_context(system_prompt)
        if self.rules.understands(prompt, ctx):
            self.rule_hits += 1
            return self.rules.generate(prompt, system_prompt)
        self.fallback_calls += 1
        return self.fallback.generate(prompt, system_prompt=system_prompt)

    def totals(self):
        fn = getattr(self.fallback, "totals", None)
        base = fn() if callable(fn) else {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "points": 0.0}
        return {**base, "rule_hits": self.rule_hits}

    @property
    def cache_size(self):
        return getattr(self.fallback, "cache_size", 0)

    @cache_size.setter
    def cache_size(self, v):
        if hasattr(self.fallback, "cache_size"):
            self.fallback.cache_size = v
