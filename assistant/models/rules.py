"""المخطط المحلي بالقواعد (RuleProvider): يفهم الطلبات العربية الشائعة ويبني خطة من محتويات المجلد الفعلية.

بلا شبكة ولا مفاتيح ولا كلفة نموذج. يُستعمل وحده افتراضياً، أو أولاً قبل النموذج الحقيقي (RuleFirstProvider)
فلا يُصرف رصيد API على طلب معروف الشكل. ما لا يفهمه يسأل عنه سؤالاً واحداً (أو يُحال للنموذج في الوضع المختلط).

مبدأ الأمان في الفهم (B15): لا نخمّن. أسماء الملفات والعبارات المقتبسة تُحجب قبل البحث عن الأفعال (فلا يصير «الجمعة»
فعل جمع ولا «هوية» فعل هوية)، والأفعال تُطابَق ككلمات كاملة، وأي التباس ← سؤال واحد بدل تنفيذ:
أكثر من عملية في طلب واحد، أو استثناء/نفي («ما عدا»، «بدون»)، أو شرط لا ننفّذه («القديمة»، «الأكبر من»)،
أو اسم ملف مكتوب لا يوجد، أو اسم موجود في أكثر من مكان، أو مجلد غير موجود، أو وجهة من عدة كلمات بلا تنصيص.
"""
import hashlib
import json
import re
import datetime
import unicodedata
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .base import ModelProvider, CONTEXT_MARKER
from ..tools.files import _bad_component, check_not_big_cloud_file, CloudFileNotLocalError

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
CAT_ALIASES: Dict[str, Tuple[str, ...]] = {  # مرادفات شائعة (مُطبَّعة) تدل على الفئة نفسها
    "صور": ("صوره",), "مستندات": ("مستند", "وثائق"), "جداول": ("جدول",), "عروض": ("عرض",), "فيديو": ("فيديوهات", "افلام"),
    "صوتيات": ("صوت", "موسيقي", "اغاني"), "مضغوطات": ("مضغوط",), "برامج": ("برنامج", "تطبيقات"),
}
OTHER = "أخرى"
SYSTEM_FILES = {"desktop.ini", "thumbs.db"}  # ملفات نظام لا تُرتَّب ولا تُسمّى من جديد
HELP_QUESTION = ("لم أفهم الطلب. اكتبه بإحدى هذه الصيغ: «رتّب الملفات حسب النوع» · «انسخ <اسم> إلى <مجلد>» · "
                 "«أعد تسمية ملفات <مجلد> بإضافة البادئة <نص>» · «انقل <نوع أو اسم> إلى <مجلد>» · «احذف <اسم>» · «رتّب الملفات حسب الشهر» · «انقل المكررات» · «أنشئ مشروع <اسم>» · «افحص <اسم>» · «حدّث البرنامج <المعرّف>».")
MULTI_QUESTION = ("الطلب يحتوي أكثر من عملية. اطلب عملية واحدة في كل مرة (مثال: «انسخ a.txt إلى Backup») "
                  "ثم اطلب التالية بعد انتهائها.")
EXCEPT_QUESTION = ("الطلب فيه استثناء أو نفي (مثل «ما عدا» أو «بدون») ولا أنفّذه تلقائياً حتى لا أحذف أو أنقل غير المقصود. "
                   "سمِّ الملفات المطلوبة بأسمائها صراحة.")
QUALIFIER_QUESTION = ("الطلب فيه شرط (مثل «القديمة» أو «الأكبر من» أو «تنتهي بـ») لا أستطيع تقييمه تلقائياً. "
                      "سمِّ الملفات بأسمائها أو اطلب نوعاً كاملاً (مثل «الصور» أو «ملفات pdf»).")
QUOTE_QUESTION = "اسم المجلد يبدو من أكثر من كلمة. اكتبه بين علامتي تنصيص، مثال: «انقل الصور إلى «صور العائلة»»."

SINGULAR_QUESTION = ("«{tok}» بصيغة المفرد فلا أعرف أي ملف تقصد، وقد تقصد النوع كله. سمِّ الملف باسمه كما يظهر، "
                     "أو اطلب النوع كاملاً بصيغة الجمع (مثل «الصور» أو «ملفات pdf»).")
BARE_EXT_QUESTION = ("«ملف {ext}» عبارة عن ملف واحد بلا اسم، وقد تقصد كل ملفات هذا الامتداد. سمِّ الملف باسمه الكامل، "
                     "أو اكتب «ملفات {ext}» إن قصدت كلها.")
DUP_EXTRA_QUESTION = ("المكررات تُكشف في مجلد العمل كله بالمحتوى فقط، ولا أدمجها مع نوع أو اسم أو مجلد أو وجهة (فأتجاهلها بصمت). "
                      "اكتب «احذف المكررات» أو «انقل المكررات» وحدها.")
_SINGULAR_NOUNS = ("صوره", "مستند", "جدول", "عرض", "مضغوط", "برنامج")  # مفردات تدل على نوع لكنها قد تعني ملفاً واحداً
_SINGULAR_RE = re.compile(r"(?<![\w])(?:و)?(?:ال)?(" + "|".join(_SINGULAR_NOUNS) + r")(?![\w])")
_BARE_EXT_RE = re.compile(r"(?<![\w])(?:ال)?(?:ملف|file)\s+\.?([a-z0-9]{2,5})(?![\w./\\-])")


def _cat_re(cat: str):
    """نمط كلمة تدل على الفئة (مع مرادفاتها، وبادئتي «و» و«ال»، وحدّ نهاية)."""
    alts = "|".join(re.escape(x) for x in (norm(cat),) + tuple(CAT_ALIASES.get(cat, ())))
    return re.compile(r"(?<![\w])(?:و)?(?:ال)?(?:" + alts + r")(?:ات|هات|ها|ه)?(?![\w])")


_DIACRITICS = re.compile(r"[ً-ْـ]")
_QUOTE_RE = re.compile(r"[«\"“](.+?)[»\"”]")
_QUOTE_CHARS = re.compile(r"[«»\"“”]")
_POLITE = {"لو", "سمحت", "من", "فضلك", "الان", "الآن", "please", "now", "فورا", "فوراً", "الي", "لي"}
_STOP_AFTER_FROM = {"نوع", "انواع", "هذا", "هذه", "هذي", "ذلك", "الان", "اليوم", "امس", "بين", "فضلك", "سمحت"}
_FILLER = {"ملف", "ملفات", "الملف", "الملفات", "file", "files", "مجلد", "المجلد", "folder", "folders", "the", "this", "these",
           "كل", "جميع", "كافه", "نوع", "انواع", "من", "في", "داخل", "الي", "to", "into", "in", "from", "inside", "لو", "سمحت",
           "فضلك", "الان", "please", "now", "هذا", "هذه", "على", "عن", "ممكن", "هل", "can", "you", "could", "would", "ابغي",
           "اريد", "ابي", "i", "want", "احذف", "حذف", "امسح", "مسح", "ازل", "ازاله", "شيل", "تخلص", "تحذف", "تمسح", "delete",
           "remove", "erase", "انقل", "نقل", "حرك", "تنقل", "تحرك", "move", "ضع", "حط", "اجمع", "جمع", "put", "انسخ", "نسخ",
           "تنسخ", "copy", "backup", "نسخه", "احتياطيه", "اعمل", "اعملي", "سوي", "عمل", "كلها", "كله", "جميعها", "all"}
_CREATE_GENERIC = {"جديد", "جديده", "جديدا", "باسم", "بإسم", "بأسم", "تجريبي", "تجريبيا", "new", "named", "called", "name", "a", "an", "the"}
_CREATE_FILLER = {"انشئ", "انشاء", "اعمل", "جهز", "create", "make", "مجلد", "مجلدا", "مشروع", "مشروعا", "هيكل", "folder", "project",
                  "جديد", "جديده", "جديدا", "new", "باسم", "بإسم", "بأسم", "اسمه", "اسمها", "named", "called", "name", "a", "an",
                  "the", "لي", "for", "me", "لو", "سمحت", "فضلك", "please", "ممكن", "هل", "can", "you", "تجريبي", "تجريبيا", "ال",
                  "الان", "now", "ابغي", "اريد", "ابي", "i", "want", "تنشئ", "تعمل", "تجهز", "هيكلا", "داخل", "في", "in", "العمل", "مجلد"}
_ORGANIZE_FILLER = {"رتب", "ترتيب", "نظم", "تنظيم", "صنف", "فرز", "organize", "sort", "الملفات", "ملفات", "ملف", "file", "files",
                    "حسب", "بحسب", "according", "لو", "سمحت", "فضلك", "please", "ممكن", "هل", "can", "you", "الان", "now", "كل",
                    "جميع", "all", "my", "لي", "الكل", "تلقائيا", "اوتوماتيك", "تنظم", "ترتب", "the", "في", "المجلد", "مجلد",
                    "العمل", "الحالي", "هذا", "ال", "ملفاتي", "ملفاتك", "ملفاتنا", "ملفات", "الاشياء", "رتبهم", "رتبها", "نظمها", "نظمهم"}
_QUAL_AS_NAME = {"old", "new", "older", "newer", "big", "small", "large", "empty", "قديم", "جديد", "كبير", "صغير"}
_GENERIC_DEST = {"واحد", "جديد", "جديده", "خاص", "منفصل", "اخر", "اخري", "new", "one", "single", "separate", "other", "another", "a",
                 "هنا", "هناك", "فوق", "تحت", "مجلد", "المجلد", "folder", "هذا", "هذه", "الخارج", "خارج", "الاعلي", "اعلي", "الاب",
                 "الاصل", "parent", "الاول", "الثاني", "الثالث", "الاخير", "التالي", "السابق", "الاخر", "الاخري"}
_ROOT_TOKENS = {"العمل", "الحالي", "الرئيسي", "الرئيسيه", "هذا", "هذه", "الجذر", "المجلد"}
_ROOT_DEST = {"العمل", "الحالي", "الرئيسي", "الرئيسيه", "الجذر", "root", "workspace"}  # وجهة = جذر مجلد العمل نفسه (لا مجلد بهذا الاسم)
_DUP_FILL = (_FILLER - {"backup", "الي", "to", "into", "في", "داخل", "من", "in", "from", "inside", "احتياطيه"}) | {"النسخ", "الزائده", "ال", "لي"}
_DUP_HERE = re.compile(r"(?<!\w)(?:في|داخل|من)\s+(?:هذا\s+)?(?:ال)?(?:مجلد\s+)?(?:العمل|الحالي|الرئيسي|المجلد)(?!\w)")
_RENAME_FILL = (_FILLER | _ORGANIZE_FILLER | {"اضف", "ضيف", "اضافه", "زد", "add", "اعد", "تسميه", "غير", "اسم", "اسماء", "الاسماء", "rename",
                                              "بادئه", "البادئه", "prefix", "with", "for", "of", "by", "using", "ل", "ب", "بدايه", "البدايه", "اليوم",
                                              "today", "start", "beginning"})
_INSPECT_FILL = (_FILLER | _ORGANIZE_FILLER | {"ما", "هو", "هي", "ماذا", "يوجد", "محتويات", "محتوي", "العمل", "الحالي", "الرئيسي",
                                               "الجذر", "workspace", "root", "is", "what", "whats", "a", "of", "ماهي", "ماهو"})
_KNOWN_EXTS = set(EXT_TO_CAT) | {"log", "tmp", "bak", "ini", "db", "py", "js", "json", "html", "css", "xml", "yml", "yaml",
                                  "bat", "cmd", "ps1", "sh", "ts", "java", "c", "cpp", "h", "cs", "go", "rs"}


_DIGITS = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGITS.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})


def norm_nc(text: str) -> str:
    """توحيد الحروف العربية وحذف التشكيل مع إبقاء حالة الأحرف (الطول يطابق norm). الأرقام الهندية تُحوَّل لعربية-لاتينية."""
    t = _DIACRITICS.sub("", text)
    return re.sub("[أإآ]", "ا", t).replace("ى", "ي").replace("ة", "ه").translate(_DIGITS)


def strip_format_chars(text: str) -> str:
    """يحذف محارف التنسيق غير المرئية (Unicode Cf: علامات الاتجاه، العرض الصفري…) فلا تدخل في أسماء أو بادئات."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _unq(tok: str) -> str:
    """رمز غير منصَّص: نقطة/علامة ترقيم الجملة في آخره ليست من الاسم (ويندوز يمنع اسماً ينتهي بنقطة أصلاً)."""
    return re.sub(r"[.!؟?]+$", "", tok)


def norm(text: str) -> str:
    """توحيد الحروف العربية (همزات، ياء، ألف مقصورة) وحذف التشكيل لتسهيل المطابقة."""
    return norm_nc(text).lower()


def _build_file_cat_re():
    """«ملف الصوت / ملف الفيديو / ملف الصور»: ملف واحد مبهم، قد يقصد النوع كله."""
    words = []
    for cat in CATEGORIES:
        words.append(norm(cat))
        words.extend(CAT_ALIASES.get(cat, ()))
    return re.compile(r"(?<![\w])(?:ال)?(?:ملف|file)\s+(?:ال)?(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w])")


_FILE_CAT_RE = _build_file_cat_re()


def parse_context(system_prompt: str) -> List[str]:
    if CONTEXT_MARKER not in system_prompt:
        return []
    block = system_prompt.split(CONTEXT_MARKER, 1)[1]
    out = []
    for ln in block.splitlines():
        t = ln.strip()
        if not t or t == "(فارغ)":
            continue
        if t != ln.rstrip("\r") and not t.startswith("…"):
            continue  # اسم بمسافة في أوله: لا يمكن استهدافه بأمان بعد تنظيف الخطة، فلا يُعرض للقواعد
        out.append(t)
    return out


def _step(i: int, action: str, target: str, criteria: str, **params) -> dict:
    return {"step_id": i, "action_type": action, "target": target, "params": params, "acceptance_criteria": criteria}


def _safe_name(name: str) -> Optional[str]:
    """اسم عنصر صالح أو None. لا نحذف النقاط من أول الاسم أو آخره بصمت (فيتحول «.config» إلى config)؛ يُرفض فيُسأل المستخدم."""
    name = name.strip().strip("«»\"'،,؛:").strip()
    if not name or name in (".", "..") or name.startswith(".") or re.search(r'[\\/:*?"<>|]', name) or _bad_component(name):
        return None
    return name


def mentions(name: str, p: str) -> bool:
    """هل يرد الاسم كاملاً (لا جزءاً من اسم أطول) في نص الطلب المطبَّع؟"""
    n = norm(name)
    if not n:
        return False
    return _mention_re(n).search(p) is not None


def _mention_re(n: str):
    return re.compile(r"(?<![\w.\-])" + re.escape(n) + r"(?![\w\-]|\.\w)")


def _words(*words: str, prefix: str = "[وف]?"):
    alts = "|".join(re.escape(norm(w)) for w in words)
    return re.compile(r"(?<!\w)" + prefix + r"(?:" + alts + r")(?!\w)")


# ---- كلمات النوايا (بعد التطبيع؛ تُطابَق ككلمات كاملة لا كأجزاء) ----
W_DELETE = _words("احذف", "حذف", "امسح", "مسح", "ازل", "ازاله", "شيل", "تخلص من", "تحذف", "تمسح", "delete", "remove", "erase")
W_MOVE = _words("انقل", "نقل", "حرك", "تنقل", "تحرك", "move")
W_PUT = _words("ضع", "حط", "اجمع", "جمع", "put")
W_COPY = _words("انسخ", "نسخ", "تنسخ", "نسخه احتياطيه", "backup", "copy")
W_RENAME = _words("اعد تسميه", "غير اسم", "تسميه", "rename")
W_ADD_PREFIX = re.compile(r"(?<!\w)(?:اضف|ضيف|اضافه|زد|add)\s+(?:ال)?(?:بادئه|prefix|تاريخ|date)(?!\w)")
W_PREFIX = _words("بادئه", "prefix", "بدايه", "اضافه", prefix="(?:ب)?(?:ال)?")
W_ORGANIZE = _words("رتب", "رتبها", "رتبهم", "ترتيب", "نظم", "نظمها", "نظمهم", "تنظيم", "صنف", "فرز", "organize", "sort")
W_CREATE = _words("انشئ", "انشاء", "اعمل", "جهز", "create", "make")
W_CREATE_OBJ = _words("مجلد", "مشروع", "هيكل", "folder", "project", prefix="(?:ال)?")
W_INSPECT = _words("افحص", "اعرض", "فحص", "تحقق", "inspect", "ماذا يوجد", "ماذا في", "ما محتوي", "ما محتويات", "show", "list",
                   "what is in", "whats in", "what's in")
W_UPDATE = _words("حدث", "تحديث", "ترقيه", "winget", "upgrade", "update")
W_WHOAMI = re.compile(r"(?<!\w)(?:whoami|من انا|اسم المستخدم|(?:ال)?هويه)(?!\w)")
W_DUP = re.compile(r"(?<!\w)[وف]?(?:ال)?(?:مكرر\w*|duplicates?)(?!\w)")
_DUP_COPY_PHRASE = re.compile(r"(?<!\w)نسخ(?:ه|ات)?\s+(?:ال)?مكرر\w*")
W_THEN = _words("ثم", "بعدين", "بعدها", "بعد ذلك", "then", "afterwards", "and then")
W_NEG = _words("عدا", "ماعدا", "الا", "غير", "ليس", "ليست", "ليسوا", "لا", "بدون", "دون", "باستثناء", "استثناء", "سوي", "خلا",
               "لكن", "ولكن", "احتفظ", "ابق", "ابقي", "اترك", "keep", "besides", "other than", "apart from", "except", "not",
               "dont", "don't", "without", "excluding", "exclude", "but", "only", "just", prefix="[و]?")
_QUAL_WORDS = ("قديم", "كبير", "صغير", "جديد", "اكبر", "اصغر", "اقدم", "احدث", "تنتهي", "ينتهي", "تبدا", "يبدا", "تحتوي",
               "يحتوي", "تتجاوز", "اكثر", "اقل", "فارغ", "قبل", "بعد", "older", "newer", "larger", "smaller", "bigger",
               "empty", "oldest", "newest", "biggest", "smallest")
W_QUAL = re.compile(r"(?<!\w)(?:ال)?(?:" + "|".join(_QUAL_WORDS) + r")\w*(?!\w)")
W_ALL = _words("الكل", "كل شيء", "كل شي", "جميع", "كافه", "everything", "all", prefix="")
W_CONTENTS = _words("محتويات", "محتوي", "contents", prefix="(?:ال)?")
_DEST_TO = re.compile(r"(?<!\w)(?:إلى|الى|الي|to|into)(?!\w)", re.I)
_DEST_IN = re.compile(r"(?<!\w)(?:في|داخل|in|inside)(?!\w)", re.I)
_SCOPE_RE = re.compile(r"(?<!\w)(?:من|في|داخل|from|in|inside)\s+(?:مجلد\s+|folder\s+)?")
_PKG_RE = re.compile(r"(?<![\w./\\-])[A-Za-z0-9][A-Za-z0-9+_-]*\.[A-Za-z0-9+_-]*[A-Za-z][A-Za-z0-9+_.-]*(?![\w+])")
_FILELIKE = re.compile(r"(?<![\w§])[^\s§]*[\w؀-ۿ]\.[a-z0-9]{1,6}(?![\w])")
_PATHLIKE = re.compile(r"[^\s§]*[/\\][^\s§]*")
_FILELIKE_I = re.compile(_FILELIKE.pattern, re.I)


def _dest_mask(prompt: str) -> str:
    """نسخة من الطلب بالطول نفسه تُحجب فيها العبارات المقتبسة وما يشبه اسم ملف، فلا تُحسب كلمة «to» داخل اسم علامةَ وجهة."""
    t = _QUOTE_RE.sub(lambda m: "§" * len(m.group(0)), prompt)
    return _FILELIKE_I.sub(lambda m: "§" * len(m.group(0)), t)


def _visible(rel: str) -> bool:
    base = rel.split("/")[-1]
    return not base.startswith(".") and base.lower() not in SYSTEM_FILES


def _trunc_note(done: int, dropped: int, partial: bool = False):
    """تنبيه عند اقتطاع الخطة بسبب حد الخطوات، أو لأن قائمة مجلد العمل نفسها مقتطعة، حتى لا يظن المستخدم أن كل شيء شمله التنفيذ."""
    parts = []
    if dropped > 0:
        parts.append(f"اقتُصرت هذه الخطة على {done} عنصراً بسبب حد الخطوات؛ بقي {dropped} عنصراً. أعد الطلب بعد التنفيذ لإكمال الباقي.")
    if partial:
        parts.append(f"قائمة مجلد العمل مقتطعة، فشملت هذه الخطة الملفات الظاهرة لي فقط ({done} عنصراً). أعد الطلب بعد التنفيذ لإكمال ما لم يظهر.")
    return " ".join(parts) or None


def _unknown_token(work: str, fill, pre: str = "و", mask_unknown: bool = False) -> Optional[str]:
    """أول كلمة باقية في النص المحجوب لا نفهمها (غير § ولا من كلمات الحشو، مع بادئة/بادئات حرف الجر المسموحة) أو None.
    mask_unknown=True: العنصر المحجوب (§: اسم/تنصيص) نفسه يُعدّ باقياً غير مفهوم."""
    for tok in re.findall(r"[^\W_]+|§", work):
        if tok == "§":
            if mask_unknown:
                return tok
            continue
        if tok == "و" or tok in fill:
            continue
        if tok[:1] in pre and tok[1:] in fill:
            continue
        if "ل" in pre and tok.startswith("لل") and ("ال" + tok[2:]) in fill:
            continue
        return tok
    return None


def _unmatched_filelike(work: str) -> Optional[str]:
    """كلمة تشبه اسم ملف/مسار ولم تُطابَق مع شيء في مجلد العمل (أول واحدة) أو None."""
    for tok in re.split(r"[\s،,؛;؟?!()]+", work):
        tok = tok.strip(".:")
        if not tok or tok == "§":
            continue
        if "/" in tok or "\\" in tok or re.fullmatch(r"[^.\s]+(?:\.[^.\s]+)*\.[a-z0-9]{1,6}", tok):
            return tok
    return None


class Analysis:
    def __init__(self, steps=None, question=None, note=None):
        self.steps, self.question, self.note = steps, question, note


class Resolved:
    """نتيجة تحديد العناصر المقصودة في الطلب: القائمة + نوع المعيار (name/cat/ext) أو سؤال عند الالتباس."""

    def __init__(self, items=None, kind="", ask=None):
        self.items: List[str] = items or []
        self.kind = kind
        self.ask = ask


class Ents(dict):
    """عناصر مجلد العمل (المسار → هل هو مجلد). partial=True إذا كانت القائمة مقتطعة فلا يجوز التعميم على «كل الملفات»."""
    partial = False


def _entries(context: List[str]) -> Ents:
    out = Ents()
    for e in context:
        if e.startswith("…"):
            out.partial = True
            continue
        rel = e.rstrip("/")
        if rel and "\n" not in rel:
            out[rel] = e.endswith("/")
    return out


PARTIAL_QUESTION = ("مجلد العمل كبير والقائمة التي أراها مقتطعة، فلا أعمّم على «كل الملفات» حتى لا أفوّت شيئاً أو أنفّذ ناقصاً. "
                    "سمِّ الملفات بأسمائها أو اختر مجلد عمل أصغر.")


class RuleProvider(ModelProvider):
    """يحوّل الطلب العربي إلى خطة JSON. الأنواع: ترتيب حسب النوع، إعادة تسمية بادئة، نسخ، نقل، حذف، إنشاء، فحص، winget، whoami."""

    def __init__(self, today: Optional[Callable[[], datetime.date]] = None, workspace: Optional[Path] = None):
        self._today = today or datetime.date.today
        self.workspace: Optional[Path] = Path(workspace).resolve() if workspace else None

    def bind_workspace(self, root) -> None:
        """يربطه المنفّذ بمجلد العمل ليقرأ أحجام الملفات وتواريخها (قراءة فقط) عند التخطيط."""
        self.workspace = Path(root).resolve()

    def name(self) -> str:
        return "RuleProvider(Local)"

    # ---------- واجهة المزوّد ----------
    def generate(self, prompt: str, system_prompt: str = "") -> str:
        a = self.analyze(prompt, parse_context(system_prompt))
        if "CLARIFY" in system_prompt:
            # «لم أفهم الطلب» ليست سؤال توضيح: يُعاد الطلب كاملاً من المستخدم (لا يُدمج مع الجديد)
            qs = [a.question] if a.question and a.question != HELP_QUESTION else []
            return json.dumps({"questions": qs}, ensure_ascii=False)
        if a.steps:
            out = {"steps": a.steps}
            if a.note:
                out["note"] = a.note   # مثل «اقتُصرت الخطة على 48 عنصراً»: تصل للمستخدم مع الخطة
            return json.dumps(out, ensure_ascii=False)
        return json.dumps({"steps": [], "note": a.note or a.question or HELP_QUESTION}, ensure_ascii=False)

    def understands(self, prompt: str, context: List[str]) -> bool:
        a = self.analyze(prompt, context)
        return bool(a.steps)

    # ---------- التحليل ----------
    def _mask(self, prompt: str, ents: Ents, files_only: bool = False) -> str:
        """يحجب العبارات المقتبسة وأسماء العناصر الموجودة وكل ما يشبه اسم ملف/مسار، ليبقى نص الأفعال وحده."""
        t = norm(_QUOTE_RE.sub(" § ", prompt))
        names = set()
        for rel, is_dir in ents.items():
            if files_only and is_dir:
                continue  # مجلد اسمه Project لا يخفي كلمة project في «أنشئ project»
            names.add(rel)
            names.add(rel.split("/")[-1])
        for n in sorted(names, key=len, reverse=True):
            nn = norm(n)
            if nn and nn in t:
                t = _mention_re(nn).sub(" § ", t)
        t = _PATHLIKE.sub(" § ", t)
        t = _FILELIKE.sub(" § ", t)
        return t

    def analyze(self, prompt: str, context: List[str]) -> Analysis:
        prompt = strip_format_chars(prompt)
        prompt = re.sub(r"\s*\n?توضيحات المستخدم:\s*", " ", prompt).replace(" | ", " ")  # إجابات أسئلة التوضيح تُدمج بالطلب
        if len(prompt) > 600:
            return Analysis(question="الطلب طويل جداً. اكتبه بجملة قصيرة واحدة (عملية واحدة).")
        prompt = re.sub(r"(?<![\w.])\./", "", prompt.replace("\\", "/"))  # Docs\a.txt و ./a.txt كما في ويندوز
        ents = _entries(context)
        top_files = [r for r, d in ents.items() if not d and "/" not in r]
        top_dirs = [r for r, d in ents.items() if d and "/" not in r]
        text = _DUP_COPY_PHRASE.sub(" مكررات ", self._mask(prompt, ents))

        intents = set()
        if W_DELETE.search(text):
            intents.add("delete")
        if W_MOVE.search(text):
            intents.add("move")
        if W_COPY.search(text):
            intents.add("copy")
        if (W_RENAME.search(text) and W_PREFIX.search(text)) or W_ADD_PREFIX.search(text):
            intents.add("rename")
        elif W_RENAME.search(text):
            intents.add("rename1")
        if W_ORGANIZE.search(text):
            intents.add("organize")
        if W_CREATE.search(text) and W_CREATE_OBJ.search(self._mask(prompt, ents, files_only=True)):
            intents.add("create")
        if W_INSPECT.search(text):
            intents.add("inspect")
        if W_UPDATE.search(text):
            intents.add("update")
        if W_WHOAMI.search(text):
            intents.add("whoami")
            intents.discard("inspect")
        if W_DUP.search(text):
            intents.add("dup")
        if "move" not in intents and W_PUT.search(text) and (_DEST_TO.search(prompt) or _DEST_IN.search(prompt)):
            intents.add("move")

        if "dup" in intents:
            others = intents - {"dup", "delete", "move"}
            if others or {"delete", "move"} <= intents:
                return Analysis(question=MULTI_QUESTION)
        elif len(intents) > 1:
            return Analysis(question=MULTI_QUESTION)
        if not intents:
            return Analysis(question=HELP_QUESTION)
        if W_THEN.search(text):
            return Analysis(question=MULTI_QUESTION)

        if "whoami" in intents:
            return Analysis([_step(1, "run_command", "whoami", "رمز الخروج 0", cmd=["whoami"])])
        if "update" in intents:
            pkgs = [m.rstrip(".") for m in _PKG_RE.findall(prompt)]
            pkgs = [m for m in pkgs if "." in m
                    and m.rsplit(".", 1)[-1].lower() not in _KNOWN_EXTS and norm(m) not in {norm(r.split("/")[-1]) for r in ents}]
            if not pkgs:
                return Analysis(question="ما معرّف الحزمة المراد تحديثها؟ مثال: «حدّث برنامج Git.Git»")
            return Analysis([_step(1, "winget_upgrade", pkgs[0], "رمز الخروج 0 والحزمة ظاهرة في winget list")])
        if "create" in intents:
            return self._create(prompt, ents)
        if "inspect" in intents:
            return self._inspect(prompt, ents)

        # العمليات التي تغيّر الملفات: لا نخمّن عند الاستثناء أو الشرط
        stripped = re.sub(r"(?<!\w)غير\s+اسم", " ", text)
        if W_NEG.search(stripped):
            return Analysis(question=EXCEPT_QUESTION)
        if W_QUAL.search(stripped):
            return Analysis(question=QUALIFIER_QUESTION)

        if "dup" in intents:
            left = _DUP_HERE.sub(" ", re.sub(r"(?<!\w)تخلص\s+من(?!\w)", " ", W_DUP.sub(" ", text)))
            if _DEST_TO.search(_dest_mask(prompt)) or _unknown_token(left, _DUP_FILL, mask_unknown=True):
                return Analysis(question=DUP_EXTRA_QUESTION)  # نوع/اسم/نطاق/وجهة مع «المكررات»: لا نتجاهله بصمت
            if ents.partial:
                return Analysis(question=PARTIAL_QUESTION)
            return self._duplicates("delete" in intents, ents)
        if "rename" in intents:
            return self._rename(prompt, ents)
        if "rename1" in intents:
            return self._rename_one(prompt, ents)
        if "delete" in intents:
            return self._delete(prompt, text, ents)
        if "move" in intents:
            return self._move(prompt, ents, top_dirs, top_files)
        if "copy" in intents:
            return self._copy(prompt, ents, top_dirs, top_files)
        if "organize" in intents:
            by_date = re.search(r"(?<!\w)(?:ب)?(?:ال)?(?:تاريخ|شهر|سنه)(?!\w)|(?<!\w)(?:date|month|year)(?!\w)", text)
            by_type = re.search(r"(?<!\w)(?:ال)?(?:نوع|type)(?!\w)", text)
            left = re.sub(r"(?<!\w)(?:ب)?(?:ال)?(?:تاريخ|شهر|سنه|نوع)(?!\w)|(?<!\w)(?:date|month|year|type|by)(?!\w)", " ", text)
            for tok in re.findall(r"[^\W_]+|§", left):
                if tok in _ORGANIZE_FILLER or (tok.startswith("و") and tok[1:] in _ORGANIZE_FILLER):
                    continue
                return Analysis(question=("الترتيب يشمل كل ملفات مجلد العمل فقط، وبمعيار واحد: النوع أو الشهر أو السنة. "
                                          "اكتب مثلاً «رتّب الملفات حسب النوع»."))
            if by_date and by_type:
                return Analysis(question="اختر معياراً واحداً للترتيب: النوع أو التاريخ.")
            if by_date:
                return self._organize_by_date(text, ents)
            return self._organize(ents)
        return Analysis(question=HELP_QUESTION)

    # ---------- تحديد العناصر ----------
    @staticmethod
    def _dir_at(rest: str, dirs: List[str]):
        """هل يبدأ النص بمجلد معروف؟ مسار كامل أولاً ثم الاسم المجرّد (إن تكرر ← التباس). يعيد (المسار أو None، سؤال أو None، الطول المستهلك)."""
        def ends(n: str) -> bool:
            return not re.match(r"[\w\-/]", rest[len(n):len(n) + 1] or " ")
        for d in dirs:
            n = norm(d)
            if rest.startswith(n) and ends(n):
                return d, None, len(n)
        bases: Dict[str, List[str]] = {}
        for d in dirs:
            bases.setdefault(norm(d.split("/")[-1]), []).append(d)
        for b in sorted(bases, key=len, reverse=True):
            if b and rest.startswith(b) and ends(b):
                if len(bases[b]) > 1:
                    return None, f"المجلد «{b}» موجود في أكثر من مكان ({'، '.join(sorted(bases[b])[:3])}). اكتب المسار الكامل.", len(b)
                return bases[b][0], None, len(b)
        return None, None, 0

    def _resolve(self, head: str, ents: Ents, names_only: bool = False) -> Resolved:
        """يحدّد العناصر المسمّاة (مسار كامل ثم اسم) أو الفئة/الامتداد، مع نطاق «من/في مجلد X».
        لا رجوع لامتداد عند اسم غير موجود، وأي كلمة متبقية غير مفهومة ← سؤال (لا اختيار جزئي صامت)."""
        quoted = {norm(q).strip() for q in _QUOTE_RE.findall(head)}  # اسم منصَّص = مقصود حرفياً (لا يُعدّ كلمة نوع)
        text = norm(_QUOTE_CHARS.sub(" ", head))
        text = re.sub(r"(?<!\w)تخلص\s+من(?!\w)", " ", text)
        dirs = sorted((r for r, d in ents.items() if d), key=len, reverse=True)
        all_exts = {r.rsplit(".", 1)[-1].lower() for r, d in ents.items() if not d and "." in r.split("/")[-1]}

        # 1) النطاق (لا يُستعمل في الفحص: «افحص Docs» تعني المجلد نفسه)
        scope: Optional[str] = None
        pos = 0
        while not names_only:
            m = _SCOPE_RE.search(text, pos)
            if not m:
                break
            rest = text[m.end():]
            hit, ask, used = self._dir_at(rest, dirs)
            if ask:
                return Resolved(ask=ask)
            if hit:
                if scope and scope != hit:
                    return Resolved(ask="ذكرت أكثر من مجلد كنطاق. اطلب مجلداً واحداً في كل مرة.")
                scope = hit
                text = text[:m.start()] + " § " + rest[used:]
                pos = m.start() + 3
                continue
            tok = rest.split()[0].strip("،,؛;:.") if rest.split() else ""
            if tok in _ROOT_TOKENS:
                text = text[:m.start()] + " § " + rest[len(tok):]
                pos = m.start() + 3
                continue
            if not tok or tok in _STOP_AFTER_FROM or tok.startswith("§") or any(
                    norm(r.split("/")[-1]) == tok for r in ents):
                pos = m.end()
                continue
            return Resolved(ask=f"لم أجد مجلداً باسم «{tok}» في مجلد العمل. اكتب اسمه كما يظهر.")

        # 2) الأسماء: المسار الكامل أولاً ثم الاسم المجرّد (الأضحل مساراً؛ داخل النطاق الأبناء المباشرون أولاً)
        names: List[str] = []
        work = text

        def dir_context_ask(rel: str, start: int, end: int) -> Optional[str]:
            if ents.get(rel) and (re.search(r"(?:ملفات|الملفات|files|محتويات|contents)\s*$", work[:start])
                                  or re.match(r"\s*(?:files|contents)(?!\w)", work[end:])):
                return f"«{rel}» مجلد. إن قصدت ملفات بداخله فاكتب «من {rel}» مع أسمائها أو نوعها، وإن قصدت المجلد كله فاكتب «مجلد {rel}»."
            return None

        def kind_clash_ask(rel: str, nb: str, start: int) -> Optional[str]:
            """مجلد اسمه كلمة نوع (صور، pdf…): الكلمة المجرّدة تحتمل المجلد والملفات معاً ← سؤال."""
            if names_only or not ents.get(rel) or nb in quoted:
                return None
            is_kind = any(_cat_re(c).fullmatch(nb) for c in CATEGORIES) or (nb.lstrip(".") in all_exts and re.fullmatch(r"\.?[a-z0-9]{2,5}", nb))
            if not is_kind or re.search(r"(?:^|\s)(?:ال)?مجلد\s*$|(?:^|\s)folder\s*$", work[:start]):
                return None
            return (f"«{nb}» اسم مجلد موجود في مجلد العمل، وهو أيضاً اسم نوع ملفات. إن قصدت المجلد فاكتب «مجلد {nb}»، "
                    f"وإن قصدت الملفات فسمِّها بأسمائها كما تظهر.")

        for rel in sorted((r for r in ents if "/" in r), key=len, reverse=True):
            nr = norm(rel)
            if nr in work:
                rx = _mention_re(nr)
                mm = rx.search(work)
                if mm:
                    if scope and not rel.startswith(scope + "/"):
                        return Resolved(ask=f"«{rel}» ليس داخل «{scope}».")
                    q = dir_context_ask(rel, mm.start(), mm.end())
                    if q:
                        return Resolved(ask=q)
                    names.append(rel)
                    work = rx.sub(" § ", work)
        groups: Dict[str, List[str]] = {}
        for rel in ents:
            groups.setdefault(norm(rel.split("/")[-1]), []).append(rel)
        for nb in sorted(groups, key=len, reverse=True):
            if not nb or nb not in work:
                continue
            rx = _mention_re(nb)
            mm = rx.search(work)
            if not mm:
                continue
            cands = groups[nb]
            if scope:
                cands = [c for c in cands if c.startswith(scope + "/")]
                if not cands:
                    return Resolved(ask=f"لم أجد «{nb}» داخل «{scope}».")
                direct = [c for c in cands if c.count("/") == scope.count("/") + 1]
                cands = direct or cands
            else:
                shallow = min(c.count("/") for c in cands)
                cands = [c for c in cands if c.count("/") == shallow]
                if ents.partial and shallow > 0:
                    return Resolved(ask=f"قائمة مجلد العمل مقتطعة ولم أجد «{nb}» في المستوى الأول منها (وجدته داخل «{sorted(cands)[0]}» فقط)، "
                                        f"وقد يكون المقصود ملفاً في الجذر لم يظهر لي. اكتب المسار الكامل.")
            if len(cands) > 1:
                return Resolved(ask=f"الاسم «{nb}» موجود في أكثر من مكان ({'، '.join(sorted(cands)[:3])}). اكتب المسار الكامل.")
            q = dir_context_ask(cands[0], mm.start(), mm.end()) or kind_clash_ask(cands[0], nb, mm.start())
            if q:
                return Resolved(ask=q)
            names.append(cands[0])
            work = rx.sub(" § ", work)

        # 3) كلمات تشبه اسم ملف/مسار ولم تُطابَق ← لا نخمّن
        tok = _unmatched_filelike(work)
        if tok:
            return Resolved(ask=f"لم أجد «{tok}» في مجلد العمل. اكتب اسمه كما يظهر.")

        pool = [r for r, d in ents.items() if not d and _visible(r) and
                ((r.startswith(scope + "/") and r.count("/") == scope.count("/") + 1) if scope else "/" not in r)]
        cats: List[str] = []
        exts: set = set()
        if not names_only:
            sg = _SINGULAR_RE.search(work) or _FILE_CAT_RE.search(work)
            if sg:
                return Resolved(ask=SINGULAR_QUESTION.format(tok=sg.group(0).strip()))
            pool_exts = {r.rsplit(".", 1)[-1].lower() for r in pool if "." in r.split("/")[-1]}
            be = _BARE_EXT_RE.search(work)
            if be and be.group(1) in pool_exts:
                return Resolved(ask=BARE_EXT_QUESTION.format(ext=be.group(1)))
            for cat in CATEGORIES:
                rx = _cat_re(cat)
                if rx.search(work):
                    cats.append(cat)
                    work = rx.sub(" § ", work)

            def blank_ext(m):
                if m.group(1) in pool_exts:
                    exts.add(m.group(1))
                    return " § "
                return m.group(0)
            work = re.sub(r"(?<![\w./\\-])\.?([a-z0-9]{2,5})(?![\w./\\-])", blank_ext, work)
            # 4) كلمة باقية لا نفهمها ← لا اختيار جزئي صامت
            tok = _unknown_token(work, _FILLER)
            if tok:
                return Resolved(ask=f"لم أفهم «{tok}» في الطلب. سمِّ الملفات بأسمائها كما تظهر، أو اطلب نوعاً (مثل «الصور» أو «ملفات pdf»).")
        if names and (cats or exts):
            return Resolved(ask="حدّد بالأسماء أو بالنوع، لا بالاثنين معاً في طلب واحد.")
        if len(names) > 1 and any(ents.get(n) and norm(n.split("/")[-1]) in _QUAL_AS_NAME for n in names):
            return Resolved(ask="اسم أحد المجلدات (مثل Old أو New) يشبه وصفاً للملفات، فلا أعرف إن كنت تقصد المجلد أم ملفات «قديمة/جديدة». "
                                "اكتب «مجلد <اسم>» بالتنصيص أو سمِّ الملفات وحدها.")
        if names:
            kept = [n for n in dict.fromkeys(names) if not any(n.startswith(o + "/") for o in names if o != n)]
            return Resolved(kept, "name")
        if cats or exts:
            sel = [r for r in pool if (EXT_TO_CAT.get(r.rsplit(".", 1)[-1].lower() if "." in r.split("/")[-1] else "") in cats)
                   or (r.rsplit(".", 1)[-1].lower() in exts and "." in r.split("/")[-1])]
            return Resolved(sorted(dict.fromkeys(sel)), "cat" if cats and not exts else "ext")
        return Resolved()

    # ---------- الوجهة ----------
    def _verb_count(self, seg: str, ents: Ents) -> int:
        """عدد أفعال العمليات (حذف/نقل/نسخ/وضع) في مقطع من الطلب بعد حجب الأسماء والعبارات المقتبسة."""
        t = self._mask(seg, ents)
        t = _DUP_COPY_PHRASE.sub(" ", re.sub(r"نسخه\s+احتياطيه", " ", t))
        return sum(len(w.findall(t)) for w in (W_DELETE, W_MOVE, W_PUT, W_COPY))

    def _parse_dest(self, prompt: str, ents: Ents, top_dirs: List[str], top_files: List[str],
                    allow_in: bool) -> Tuple[Optional[str], Optional[str], str]:
        """يعيد (اسم مجلد الوجهة أو None، سؤال أو None، نص المصدر قبل الوجهة). الوجهة "." = جذر مجلد العمل نفسه."""
        masked = _dest_mask(prompt)  # علامات الوجهة داخل التنصيص/أسماء الملفات لا تُحسب
        ms = list(_DEST_TO.finditer(masked))
        if not ms and allow_in:
            ms = list(_DEST_IN.finditer(masked))
        if not ms:
            return None, None, prompt
        if len(ms) > 1:  # «انقل a إلى X وb إلى Y»: عمليتان، ولا ندمج المصدرين في وجهة واحدة
            return None, MULTI_QUESTION, prompt
        m = ms[0]
        head, tail = prompt[:m.start()], prompt[m.end():].strip()
        if self._verb_count(head, ents) > 1:
            return None, MULTI_QUESTION, head
        dest = None
        rest = ""
        quoted = False

        def known_at(t: str):
            nt = norm(t)
            return next((d for d in sorted(top_dirs, key=len, reverse=True)
                         if nt.startswith(norm(d)) and not re.match(r"[\w\-]", nt[len(norm(d)):len(norm(d)) + 1] or " ")), None)
        known = known_at(tail)  # مجلد موجود اسمه «مجلد جديد» يُطابَق قبل قصّ كلمة «مجلد»
        if known:
            dest, rest = tail[:len(known)], tail[len(known):]
        else:
            tail = re.sub(r"^(?:(?:ال)?مجلد|folder)\s+(?:(?:اسمه|باسم|بإسم|بأسم|المسمى|named|called)\s*[:：]?\s*)?", "", tail, flags=re.I)
            q = re.match(r"^[«\"“'](.+?)[»\"”']", tail)
            if q:
                dest, rest, quoted = q.group(1).strip(), tail[q.end():], True
            else:
                known = known_at(tail)
                if known:
                    dest, rest = tail[:len(known)], tail[len(known):]
                else:
                    t = re.match(r"^([^\s«»\"'،,؛;]+)", tail)
                    if t:
                        dest, rest = _unq(t.group(1)), tail[t.end():]
        if not dest:
            return None, "ما اسم المجلد الذي تريد الوجهة إليه؟", head
        extra = [w for w in re.split(r"[\s،,؛;.!؟?]+", rest) if w and w not in _POLITE]
        if extra:
            return None, (MULTI_QUESTION if extra[0].startswith("و") else QUOTE_QUESTION), head
        if not quoted and not known and norm(dest) in _ROOT_DEST:
            return ".", None, head  # «إلى الجذر / المجلد الرئيسي / مجلد العمل»: جذر مجلد العمل لا مجلد جديد بهذا الاسم
        name = _safe_name(dest)
        if not name:
            return None, "ما اسم المجلد الذي تريد الوجهة إليه؟ (اسم عادي بلا مسارات ولا رموز ممنوعة ولا نقطة في أوله أو آخره)", head
        nn = norm(name)
        if nn in _GENERIC_DEST and not any(norm(d) == nn for d in top_dirs):
            return None, f"«{name}» ليس اسم مجلد. ما اسم المجلد الذي تريده؟ (إن كان من عدة كلمات فاكتبه بين تنصيص)", head
        for d in top_dirs:  # ويندوز لا يميّز حالة الأحرف، ونطابق بالتوحيد نفسه المستعمل للمصادر (أ/ا، ى/ي، ة/ه، الأرقام الهندية)
            if norm(d) == nn:
                return d, None, head
        if "." in name and name.rsplit(".", 1)[-1].lower() in _KNOWN_EXTS:
            return None, f"«{name}» يبدو اسم ملف لا مجلد. اكتب اسم مجلد الوجهة.", head
        if any(norm(f) == nn for f in top_files):
            return None, f"يوجد ملف باسم «{name}» فلا أستطيع إنشاء مجلد بالاسم نفسه. اختر اسماً آخر.", head
        return name, None, head

    # ---------- النوايا ----------
    def _organize(self, ents: Ents) -> Analysis:
        files = [r for r, d in ents.items() if not d and "/" not in r and _visible(r)]
        if not files:
            return Analysis(note="لا توجد ملفات مبعثرة في مجلد العمل لترتيبها.")
        lower_files = {f.casefold() for f in files}
        if any(c.casefold() in lower_files for c in list(CATEGORIES) + [OTHER]):
            return Analysis(question="يوجد ملف باسم أحد مجلدات الترتيب (مثل «صور» أو «أخرى»). غيّر اسمه أولاً.")
        existing = {r.casefold() for r in ents}
        moves, cats = [], []
        dropped = max(0, len(files) - MAX_AUTO_STEPS)
        for f in sorted(files, key=str.lower)[:MAX_AUTO_STEPS]:
            ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
            cat = EXT_TO_CAT.get(ext, OTHER)
            if f"{cat}/{f}".casefold() in existing:
                continue  # الوجهة موجودة أصلاً: نتركه بدل أن تفشل الخطة في منتصفها
            if cat not in cats:
                cats.append(cat)
            moves.append((f, cat))
        while len(cats) + len(moves) > MAX_AUTO_STEPS:
            moves.pop()
            dropped += 1
            cats = [c for c in cats if any(m[1] == c for m in moves)]
        if not moves:
            return Analysis(note="كل الملفات مرتّبة أو وجهاتها موجودة أصلاً.")
        steps, i = [], 1
        for c in cats:
            steps.append(_step(i, "create_dir", c, "وجود المجلد")); i += 1
        for f, c in moves:
            steps.append(_step(i, "move", f, "المصدر اختفى والوجهة مطابقة", dest=f"{c}/{f}")); i += 1
        return Analysis(steps, note=_trunc_note(len(moves), dropped, ents.partial))

    def _rename(self, prompt: str, ents: Ents) -> Analysis:
        """إعادة تسمية كل ملفات مجلد (أو مجلد العمل) بإضافة بادئة. لا يدعم اختيار ملفات بعينها: يسأل بدل أن يعمّم.
        أي كلمة باقية لا نفهمها (مجلد بتهجئة خاطئة، ملف غير موجود، «للملف الأول»…) ← سؤال، لا رجوع صامت لكل ملفات الجذر."""
        dirs = sorted((r for r, d in ents.items() if d), key=len, reverse=True)
        m = (re.search(r"(?:بادئه|بادئة|prefix)\s*[:：]?\s*[«\"“](.+?)[»\"”]", prompt)
             or re.search(r"(?:بادئه|بادئة|prefix)\s*[:：]?\s*([^\s«»\"'،,]+)", prompt)
             or re.search(r"(?:بإضافة|باضافة|بإضافه|اضافة|إضافة)\s+[«\"']?([^\s«»\"'،,]+)[»\"']?\s+(?:في|الى|إلى|ل)?\s*(?:بداية|اول|أول)", prompt))
        # المقطع الذي يحوي البادئة يُحجب كله، وما يبقى هو ما نبحث فيه عن المجلد والكلمات الزائدة
        rest = norm(_QUOTE_CHARS.sub(" ", prompt[:m.start()] + " § " + prompt[m.end():])) if m else norm(_QUOTE_CHARS.sub(" ", prompt))
        prefix = _safe_name(m.group(1)) if m else None
        if m and not prefix:
            return Analysis(question="البادئة غير صالحة (بلا مسارات ولا رموز ممنوعة، ولا نقطة في أولها أو آخرها). اكتب بادئة مثل «2026_».")
        # المجلد المقصود: مسار كامل أو اسم مجرّد فريد
        folder = None
        for d in dirs:
            if _mention_re(norm(d)).search(rest):
                folder = d
                rest = _mention_re(norm(d)).sub(" § ", rest)
                break
        if folder is None:
            by_base: Dict[str, List[str]] = {}
            for d in dirs:
                by_base.setdefault(norm(d.split("/")[-1]), []).append(d)
            for b in sorted(by_base, key=len, reverse=True):
                if _mention_re(b).search(rest):
                    if len(by_base[b]) > 1:
                        return Analysis(question=f"المجلد «{b}» موجود في أكثر من مكان. اكتب المسار الكامل.")
                    folder = by_base[b][0]
                    rest = _mention_re(b).sub(" § ", rest)
                    break
        # «تاريخ/date» تعني بادئة التاريخ فقط حين لا توجد بادئة صريحة، وبعد حجب اسم المجلد (مجلد اسمه «التاريخ» ليس طلب تاريخ)
        date_rx = re.compile(r"(?<!\w)(?:ال)?تاريخ(?!\w)|(?<!\w)date(?!\w)")
        if not m and date_rx.search(rest):
            prefix = self._today().isoformat() + "_"
            rest = date_rx.sub(" § ", rest)
        if not prefix:
            return Analysis(question="ما البادئة التي تريد إضافتها لأسماء الملفات؟ مثال: «بادئة 2026_»")
        # أي اختيار لملفات بعينها (اسم/نوع/امتداد) غير مدعوم هنا
        for rel, is_dir in ents.items():
            if not is_dir and _mention_re(norm(rel.split("/")[-1])).search(rest):
                return Analysis(question="إعادة التسمية بالبادئة تُطبَّق على كل ملفات المجلد فقط، لا على ملفات بعينها. اذكر مجلداً بدل اسم ملف.")
        for cat in CATEGORIES:
            if _cat_re(cat).search(rest):
                return Analysis(question="إعادة التسمية بالبادئة تُطبَّق على كل ملفات المجلد فقط، لا على نوع معيّن. اذكر مجلداً.")
        sibling_exts = {r.rsplit(".", 1)[-1].lower() for r in ents if "." in r.split("/")[-1]}
        if any(t in sibling_exts for t in re.findall(r"(?<![\w.])\.?([a-z0-9]{2,5})(?![\w.])", rest)):
            return Analysis(question="إعادة التسمية بالبادئة تُطبَّق على كل ملفات المجلد فقط، لا على امتداد معيّن. اذكر مجلداً.")
        bad = _unmatched_filelike(rest)
        if bad:
            return Analysis(question=f"لم أجد «{bad}» في مجلد العمل. اكتب اسمه كما يظهر.")
        tok = _unknown_token(rest, _RENAME_FILL, pre="وبل")
        if tok:
            return Analysis(question=(f"لم أفهم «{tok}» في الطلب، فلا أطبّق البادئة على كل ملفات الجذر بالتخمين. "
                                      "اذكر اسم مجلد موجود كما يظهر، أو اكتب «أضف بادئة x_» وحدها لملفات مجلد العمل."))
        if folder:
            files = [e for e, d in ents.items() if not d and e.rsplit("/", 1)[0] == folder and "/" in e]
        else:
            files = [e for e, d in ents.items() if "/" not in e and not d]
        files = [f for f in files if _visible(f) and not f.split("/")[-1].startswith(prefix)]
        if not files:
            return Analysis(note="لا توجد ملفات مناسبة لإعادة التسمية" + (f" داخل {folder}." if folder else "."))
        if len(files) > MAX_AUTO_STEPS:
            return Analysis(question=f"عدد الملفات كبير ({len(files)}). حدّد مجلداً أصغر لإعادة التسمية.")
        existing = {r.casefold() for r in ents}
        for f in files:
            new = (f.rsplit("/", 1)[0] + "/" if "/" in f else "") + prefix + f.split("/")[-1]
            if new.casefold() in existing:
                return Analysis(question=f"الاسم الجديد «{new}» موجود أصلاً. غيّر البادئة أو أعد تسمية الموجود أولاً.")
        steps = [_step(i, "rename", f, "الاسم الجديد موجود والمحتوى مطابق", new_name=prefix + f.split("/")[-1])
                 for i, f in enumerate(sorted(files), 1)]
        return Analysis(steps, note=_trunc_note(len(steps), 0, ents.partial))

    def _rename_one(self, prompt: str, ents: Ents) -> Analysis:
        """«غير اسم a.txt إلى b.txt»: إعادة تسمية عنصر واحد باسم صريح (يبقى في مجلده)."""
        ms = list(_DEST_TO.finditer(prompt)) or list(re.finditer(r"(?<!\w)ب(?=\s*[A-Za-z0-9«\"“])", prompt))
        if not ms:
            return Analysis(question="ما الاسم الجديد؟ مثال: «غير اسم a.txt إلى b.txt»")
        m = ms[-1]
        head, tail = prompt[:m.start()], prompt[m.end():].strip()
        q = re.match(r"^[«\"“'](.+?)[»\"”']", tail)
        if q:
            new, rest = q.group(1).strip(), tail[q.end():]
        else:
            t = re.match(r"^([^\s«»\"'،,؛;]+)", tail)
            new, rest = (_unq(t.group(1)), tail[t.end():]) if t else ("", "")
        if [w for w in re.split(r"[\s،,؛;.!؟?]+", rest) if w and w not in _POLITE]:
            return Analysis(question="الاسم الجديد يبدو من أكثر من كلمة. اكتبه بين علامتي تنصيص: «غير اسم a.txt إلى «اسم جديد.txt»».")
        res = self._resolve(head, ents, names_only=True)
        if res.ask:
            return Analysis(question=res.ask)
        if len(res.items) != 1:
            return Analysis(question="حدّد عنصراً واحداً لإعادة تسميته باسمه كما يظهر في مجلد العمل.")
        src = res.items[0]
        name = _safe_name(new)
        if not name:
            return Analysis(question="الاسم الجديد غير صالح (بلا مسارات ولا رموز ممنوعة ولا أسماء ويندوز المحجوزة).")
        base = src.split("/")[-1]
        if name == base:
            return Analysis(note="الاسم الجديد مطابق للاسم الحالي؛ لا شيء لتغييره.")
        if "." in base and not ents.get(src) and "." not in name:
            return Analysis(question=f"الاسم الجديد «{name}» بلا امتداد. هل تقصد «{name}.{base.rsplit('.', 1)[-1]}»؟ اكتبه كاملاً.")
        parent = src.rsplit("/", 1)[0] + "/" if "/" in src else ""
        if (parent + name).casefold() in {r.casefold() for r in ents} and name.casefold() != base.casefold():
            return Analysis(question=f"يوجد «{name}» أصلاً في المكان نفسه. اختر اسماً آخر.")
        return Analysis([_step(1, "rename", src, "الاسم الجديد موجود والمحتوى مطابق", new_name=name)])

    def _copy(self, prompt: str, ents: Ents, top_dirs: List[str], top_files: List[str]) -> Analysis:
        dest_dir, ask, head = self._parse_dest(prompt, ents, top_dirs, top_files, allow_in=False)
        if ask:
            return Analysis(question=ask)
        res = self._resolve(head, ents)
        if res.ask:
            return Analysis(question=res.ask)
        if ents.partial and res.kind in ("cat", "ext"):
            return Analysis(question=PARTIAL_QUESTION)
        if not res.items:
            return Analysis(question="ما اسم الملف أو المجلد الذي تريد نسخه؟ اكتبه كما يظهر في مجلد العمل.")
        if len(res.items) > MAX_AUTO_STEPS - 1:
            return Analysis(question=f"عدد العناصر كبير ({len(res.items)}). حدّد مجموعة أصغر.")
        if not dest_dir:
            dest_dir = "نسخة_احتياطية"
            if any(f.casefold() == dest_dir.casefold() for f in top_files):
                return Analysis(question="يوجد ملف باسم «نسخة_احتياطية» فلا أنشئ مجلداً بالاسم نفسه. اكتب مجلد الوجهة: «انسخ <اسم> إلى <مجلد>».")
        return self._transfer("copy", res.items, dest_dir, ents, top_dirs, "الوجهة مطابقة للمصدر والمصدر سليم")

    def _move(self, prompt: str, ents: Ents, top_dirs: List[str], top_files: List[str]) -> Analysis:
        dest_dir, ask, head = self._parse_dest(prompt, ents, top_dirs, top_files, allow_in=True)
        if ask:
            return Analysis(question=ask)
        if not dest_dir:
            return Analysis(question="إلى أي مجلد تريد النقل؟ مثال: «انقل ملفات pdf إلى مستندات»")
        res = self._resolve(head, ents)
        if res.ask:
            return Analysis(question=res.ask)
        if ents.partial and res.kind in ("cat", "ext"):
            return Analysis(question=PARTIAL_QUESTION)
        if not res.items:
            return Analysis(question="ما الذي تريد نقله؟ اكتب اسم الملف/المجلد أو نوعه (مثل pdf أو الصور).")
        if len(res.items) > MAX_AUTO_STEPS - 1:
            return Analysis(question=f"عدد العناصر كبير ({len(res.items)}). حدّد مجموعة أصغر.")
        return self._transfer("move", res.items, dest_dir, ents, top_dirs, "المصدر اختفى والوجهة مطابقة")

    def _transfer(self, action: str, sources: List[str], dest_dir: str, ents: Ents,
                  top_dirs: List[str], criteria: str) -> Analysis:
        existing = {r.casefold() for r in ents}
        if dest_dir == ".":  # جذر مجلد العمل: العنصر يوضع باسمه في الجذر
            nested = [s for s in sources if "/" in s]
            if not nested:
                return Analysis(note="العناصر المطلوبة موجودة أصلاً في جذر مجلد العمل.")
            if len(nested) != len(sources) and action == "copy":
                return Analysis(question="بعض العناصر في الجذر أصلاً فلا يمكن نسخها إليه. سمِّ العناصر التي داخل المجلدات فقط.")
            bases = [s.split("/")[-1].casefold() for s in nested]
            if len(set(bases)) != len(bases):
                return Analysis(question="في الاختيار عنصران بالاسم نفسه من مجلدين مختلفين فيتعارضان في الجذر. اطلب أحدهما.")
            for s in nested:
                if s.split("/")[-1].casefold() in existing:
                    return Analysis(question=f"الجذر فيه «{s.split('/')[-1]}» أصلاً. أعد تسميته أولاً أو اختر وجهة أخرى.")
            return Analysis([_step(i, action, src, criteria, dest=src.split("/")[-1]) for i, src in enumerate(sorted(nested), 1)],
                            note=("العناصر الموجودة في الجذر أصلاً تُركت كما هي." if len(nested) != len(sources) else None))
        sources = [s for s in sources if s.casefold() != dest_dir.casefold()]
        if action == "move":
            sources = [s for s in sources if s.casefold() != f"{dest_dir}/{s.split('/')[-1]}".casefold()]
            if not sources:
                return Analysis(note=f"العناصر المطلوبة موجودة أصلاً داخل {dest_dir}.")
        if not sources:
            return Analysis(question="لا يمكن نسخ المجلد إلى نفسه. اختر وجهة أخرى.")
        bases = [s.split("/")[-1].casefold() for s in sources]
        if len(set(bases)) != len(bases):
            return Analysis(question="في الاختيار عنصران بالاسم نفسه من مجلدين مختلفين فيتعارضان في الوجهة. اطلب أحدهما.")
        for s in sources:
            if f"{dest_dir}/{s.split('/')[-1]}".casefold() in existing:
                return Analysis(question=f"الوجهة «{dest_dir}» فيها «{s.split('/')[-1]}» أصلاً. غيّر الوجهة أو أعد تسميته أولاً.")
        steps, i = [], 1
        if dest_dir.casefold() not in {d.casefold() for d in top_dirs}:
            steps.append(_step(i, "create_dir", dest_dir, "وجود المجلد")); i += 1
        for src in sorted(sources):
            steps.append(_step(i, action, src, criteria, dest=f"{dest_dir}/{src.split('/')[-1]}")); i += 1
        return Analysis(steps)

    MAX_HASH_BYTES = 512 * 1024 * 1024

    def _stat(self, rel: str):
        if self.workspace is None:
            return None
        try:
            fp = (self.workspace / rel).resolve()
            if self.workspace not in fp.parents or not fp.is_file():
                return None
            return fp, fp.stat()
        except OSError:
            return None

    def _organize_by_date(self, text: str, ents: Ents) -> Analysis:
        """ترتيب حسب تاريخ آخر تعديل: مجلد لكل شهر (2026-10) أو لكل سنة عند ذكر «السنة»."""
        if self.workspace is None:
            return Analysis(note="الترتيب حسب التاريخ يحتاج ربط المساعد بمجلد العمل.")
        by_year = bool(re.search(r"(?<!\w)(?:ال)?سنه(?!\w)|(?<!\w)year(?!\w)", text))
        files = [r for r, d in ents.items() if not d and "/" not in r and _visible(r)]
        existing = {r.casefold() for r in ents}
        moves, folders = [], []
        for f in sorted(files, key=str.lower):
            st = self._stat(f)
            if not st:
                continue
            d = datetime.date.fromtimestamp(st[1].st_mtime)
            key = f"{d.year}" if by_year else f"{d.year}-{d.month:02d}"
            if key.casefold() in {x.casefold() for x in files} or f"{key}/{f}".casefold() in existing:
                continue
            if key not in folders:
                folders.append(key)
            moves.append((f, key))
        n_before = len(moves)
        moves = moves[: max(0, MAX_AUTO_STEPS - len(folders))]
        dropped = n_before - len(moves)
        folders = [k for k in folders if any(m[1] == k for m in moves)]
        if not moves:
            return Analysis(note="لا توجد ملفات لترتيبها حسب التاريخ في مجلد العمل.")
        steps, i = [], 1
        for k in folders:
            steps.append(_step(i, "create_dir", k, "وجود المجلد")); i += 1
        for f, k in moves:
            steps.append(_step(i, "move", f, "المصدر اختفى والوجهة مطابقة", dest=f"{k}/{f}")); i += 1
        return Analysis(steps, note=_trunc_note(len(moves), dropped, ents.partial))

    def _duplicates(self, delete: bool, ents: Ents) -> Analysis:
        """يكشف الملفات المكررة بمحتواها (حجم ثم SHA-256) ويُبقي نسخة واحدة (الأقصر مساراً ثم الأقل عمقاً أصلاً)؛
        يحذف المكررات (بموافقة) أو ينقلها لمجلد «مكررات». الملفات المخفية وملفات النظام والفارغة تُتجاهل."""
        if self.workspace is None:
            return Analysis(note="كشف المكررات يحتاج ربط المساعد بمجلد العمل.")
        import time as _t
        ckey = (delete, tuple(ents.items()))
        hit = getattr(self, "_dup_cache", None)
        if hit and hit[0] == ckey and _t.monotonic() - hit[1] < 3.0:
            return hit[2]   # نفس الطلب يمرّ على التحليل أكثر من مرة (فهم/توضيح/تخطيط): مسح واحد يكفي
        res = self._duplicates_scan(delete, ents)
        self._dup_cache = (ckey, _t.monotonic(), res)
        return res

    def _duplicates_scan(self, delete: bool, ents: Ents) -> Analysis:
        files = [r for r, d in ents.items() if not d and not any(part.startswith(".") for part in r.split("/"))
                 and r.split("/")[-1].lower() not in SYSTEM_FILES and r.split("/")[0] != "مكررات"]
        by_size: Dict[int, List[Tuple[str, Path]]] = {}
        for f in files:
            st = self._stat(f)
            if st and 0 < st[1].st_size <= self.MAX_HASH_BYTES:
                by_size.setdefault(st[1].st_size, []).append((f, st[0]))
        dups: List[str] = []
        for group in by_size.values():
            if len(group) < 2:
                continue
            by_hash: Dict[str, List[str]] = {}
            for rel, fp in group:
                h = hashlib.sha256()
                try:
                    check_not_big_cloud_file(fp)  # لا نُنزّل ملفات OneDrive الكبيرة لمجرد البحث عن مكررات
                except CloudFileNotLocalError:
                    continue
                try:
                    with open(fp, "rb") as fh:
                        while chunk := fh.read(1 << 20):
                            h.update(chunk)
                except OSError:
                    continue
                by_hash.setdefault(h.hexdigest(), []).append(rel)
            for same in by_hash.values():
                if len(same) > 1:
                    same.sort(key=lambda r: (r.count("/"), len(r), r.lower()))
                    dups.extend(same[1:])
        dups = sorted(dups, key=str.lower)
        if not dups:
            return Analysis(note="لم أجد ملفات مكررة المحتوى في مجلد العمل.")
        if len(dups) > MAX_AUTO_STEPS - 1:
            return Analysis(question=f"وجدت {len(dups)} ملفاً مكرراً وهذا أكثر من حد الخطة. نظّف جزءاً منها ثم أعد الطلب.")
        if delete:
            return Analysis([_step(i, "delete", d, "العنصر اختفى من مكانه وصار في السلة بنفس المحتوى") for i, d in enumerate(dups, 1)])
        flat = [d.replace("/", "_") for d in dups]
        if len(set(x.casefold() for x in flat)) != len(flat):
            return Analysis(question="بعض المكررات ستتعارض أسماؤها داخل «مكررات». أعد تسمية بعضها أو احذفها بدل النقل.")
        taken = {r.casefold() for r in ents}
        if any(f"مكررات/{fl}".casefold() in taken for fl in flat):
            return Analysis(question="مجلد «مكررات» فيه ملفات بنفس الأسماء المنتظرة. أفرغه أو غيّر أسماءها أولاً.")
        steps = [_step(1, "create_dir", "مكررات", "وجود المجلد")]
        for i, (d, fl) in enumerate(zip(dups, flat), 2):
            steps.append(_step(i, "move", d, "المصدر اختفى والوجهة مطابقة", dest=f"مكررات/{fl}"))
        return Analysis(steps)

    def _delete(self, prompt: str, text: str, ents: Ents) -> Analysis:
        """الحذف = نقل إلى .trash (قابل للتراجع) ويحتاج موافقة لكل خطوة. لا حذف شامل ولا «محتويات» بلا تحديد."""
        if W_CONTENTS.search(text):
            return Analysis(question="حذف «محتويات» مجلد غير مدعوم تلقائياً. سمِّ الملفات بأسمائها أو اطلب نوعاً (مثل «الصور»).")
        res = self._resolve(prompt, ents)
        if res.ask:
            return Analysis(question=res.ask)
        if ents.partial and res.kind in ("cat", "ext"):
            return Analysis(question=PARTIAL_QUESTION)
        if W_ALL.search(text) and (not res.items or any(ents.get(t) for t in res.items)):
            return Analysis(question="الحذف الشامل غير مدعوم تلقائياً. سمِّ ما تريد حذفه بالاسم أو بالنوع (مثل «الصور»).")
        if not res.items:
            return Analysis(question="ما الملف أو المجلد الذي تريد حذفه؟ اكتب اسمه كما يظهر في مجلد العمل.")
        if len(res.items) > MAX_AUTO_STEPS:
            return Analysis(question=f"عدد العناصر كبير ({len(res.items)}). حدّد مجموعة أصغر للحذف.")
        steps = [_step(i, "delete", t, "العنصر اختفى من مكانه وصار في السلة بنفس المحتوى")
                 for i, t in enumerate(sorted(res.items), 1)]
        return Analysis(steps)

    def _create(self, prompt: str, ents: Ents) -> Analysis:
        name = None
        q = _QUOTE_RE.search(prompt)
        raw_used = ""
        if q:
            name, raw_used = q.group(1).strip(), q.group(0)
        else:
            m = (re.search(r"(?<![\w])(?:باسم|بإسم|بأسم|اسمه|اسمها|named|called|name)\s*[:：]?\s*([^\s«»\"'،,؛;]+)", prompt, re.I)
                 or re.search(r"(?:مشروع|مجلد|هيكل مشروع|folder|project)\s+(?:جديد\s+|new\s+)?([^\s«»\"'،,؛;]+)", prompt, re.I))
            if m:
                name, raw_used = _unq(m.group(1)), m.group(1)
        if not name or name.lower() in _CREATE_GENERIC:
            return Analysis(question="ما اسم المجلد أو المشروع الذي تريد إنشاءه؟ مثال: «أنشئ مجلد Reports»")
        # لا نُسقط جزءاً من الطلب بصمت: أي كلمات غير مفهومة بعد الاسم ← سؤال
        left = norm(prompt.replace(raw_used, " § ", 1))
        # «مشروع» تُحدِّد نوع الإنشاء من فعل الطلب وموضوعه فقط، لا من كلمة داخل الاسم نفسه (مجلد «مشروع التخرج» أو project_files مجلد عادي)
        is_project = bool(re.search(r"(?<!\w)(?:ال)?(?:مشروع|هيكل|project)(?!\w)", left))
        for tok in re.findall(r"[^\W_]+", left):
            if tok in ("§", "و") or tok in _CREATE_FILLER or (tok.startswith("و") and tok[1:] in _CREATE_FILLER):
                continue
            return Analysis(question=f"لم أفهم «{tok}» في طلب الإنشاء. إن كان اسم المجلد من عدة كلمات فاكتبه بين علامتي تنصيص: «أنشئ مجلد «صور قديمة»».")
        safe = _safe_name(name)
        if not safe or (safe.rsplit(".", 1)[-1].lower() in _KNOWN_EXTS and "." in safe):
            return Analysis(question="اسم المجلد غير صالح (بلا مسارات ولا رموز ممنوعة ولا اسم يشبه ملفاً). اكتب اسماً عادياً.")
        if any(not d and "/" not in r and norm(r) == norm(safe) for r, d in ents.items()):
            return Analysis(question=f"يوجد ملف باسم «{safe}» فلا أنشئ مجلداً بالاسم نفسه. اختر اسماً آخر.")
        if is_project:
            return Analysis([
                _step(1, "create_dir", f"{safe}/src", "وجود المجلد"),
                _step(2, "create_dir", f"{safe}/docs", "وجود المجلد"),
                _step(3, "create_dir", f"{safe}/tests", "وجود المجلد"),
            ])
        return Analysis([_step(1, "create_dir", safe, "وجود المجلد")])

    def _inspect(self, prompt: str, ents: Ents) -> Analysis:
        res = self._resolve(prompt, ents, names_only=True)
        if res.ask:
            return Analysis(question=res.ask)
        # كلمة باقية لا نفهمها (مجلد بتهجئة خاطئة مثل «Docz») ← «غير موجود»، لا فحص الجذر بدلاً عنه
        t = norm(_QUOTE_CHARS.sub(" ", prompt))
        for it in sorted(res.items, key=len, reverse=True):
            for nm in {norm(it), norm(it.split("/")[-1])}:
                t = _mention_re(nm).sub(" § ", t)
        tok = _unknown_token(W_INSPECT.sub(" ", t), _INSPECT_FILL)
        if tok:
            return Analysis(question=f"لم أجد «{tok}» في مجلد العمل. اكتب اسمه كما يظهر، أو اطلب «افحص» وحدها لفحص مجلد العمل كله.")
        if not res.items:
            return Analysis([_step(1, "inspect", ".", "العنصر موجود فعلاً")])
        return Analysis([_step(i, "inspect", t, "العنصر موجود فعلاً") for i, t in enumerate(res.items[:MAX_AUTO_STEPS], 1)],
                        note=_trunc_note(min(len(res.items), MAX_AUTO_STEPS), max(0, len(res.items) - MAX_AUTO_STEPS)))


class RuleFirstProvider(ModelProvider):
    """قواعد أولاً: الطلب المعروف يُخطَّط محلياً بلا كلفة، وغيره يُحال للنموذج الحقيقي."""

    def __init__(self, fallback: ModelProvider, rules: Optional[RuleProvider] = None):
        self.fallback = fallback
        self.rules = rules or RuleProvider()
        self.rule_hits = 0
        self.fallback_calls = 0

    def name(self) -> str:
        return f"RuleFirst({self.fallback.name()})"

    def bind_workspace(self, root) -> None:
        self.rules.bind_workspace(root)

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        ctx = parse_context(system_prompt)
        a = self.rules.analyze(prompt, ctx)
        # القواعد فهمت (خطة) أو فهمت ورفضت/سألت عمداً (التباس، استثناء، شرط، تعدد عمليات…): لا يُحال السؤال لنموذج قد يخمّن
        # خطة أوسع مما طُلب. الإحالة للنموذج فقط حين «لم أفهم الطلب» أصلاً.
        if a.steps or a.note or (a.question and a.question != HELP_QUESTION):
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
