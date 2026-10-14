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
import os
import re
import datetime
import unicodedata
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .base import ModelProvider, CONTEXT_MARKER
from ..safety import EmergencyStopTriggered
from ..tools.files import _bad_component, check_not_big_cloud_file, CloudFileNotLocalError, _is_link

_DUP_SKIP_DIRS = ("نسخة_احتياطية", "نسخ_احتياطية", "مكررات", "backup", "backups")   # مجلدات لا تدخل مقارنة المكررات (نسخ احتياطية متعمدة / ما نُقل سابقاً)
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
_SINGULAR_NOUNS = ("صوره", "مستند", "جدول", "عرض", "مضغوط", "برنامج", "فاتوره", "ايصال")  # مفردات تدل على نوع لكنها قد تعني ملفاً واحداً
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
           "تنسخ", "copy", "backup", "نسخه", "احتياطيه", "اعمل", "اعملي", "سوي", "عمل", "كلها", "كله", "جميعها", "all", "انشئ", "create",
           "make", "خذ", "take", "نظف", "نظفها", "تنظيف", "clean"}
_CREATE_GENERIC = {"جديد", "جديده", "جديدا", "باسم", "بإسم", "بأسم", "تجريبي", "تجريبيا", "new", "named", "called", "name", "a", "an", "the"}
_CREATE_FILLER = {"انشئ", "انشاء", "اعمل", "جهز", "create", "make", "مجلد", "مجلدا", "مشروع", "مشروعا", "هيكل", "folder", "project",
                  "جديد", "جديده", "جديدا", "new", "باسم", "بإسم", "بأسم", "اسمه", "اسمها", "named", "called", "name", "a", "an",
                  "the", "لي", "for", "me", "لو", "سمحت", "فضلك", "please", "ممكن", "هل", "can", "you", "تجريبي", "تجريبيا", "ال",
                  "الان", "now", "ابغي", "اريد", "ابي", "i", "want", "تنشئ", "تعمل", "تجهز", "هيكلا", "داخل", "في", "in", "العمل", "مجلد"}
_ORGANIZE_FILLER = {"رتب", "ترتيب", "نظم", "تنظيم", "صنف", "فرز", "organize", "sort", "الملفات", "ملفات", "ملف", "file", "files",
                    "حسب", "بحسب", "according", "لو", "سمحت", "فضلك", "please", "ممكن", "هل", "can", "you", "الان", "now", "كل",
                    "جميع", "all", "my", "لي", "الكل", "تلقائيا", "اوتوماتيك", "تنظم", "ترتب", "the", "في", "المجلد", "مجلد",
                    "العمل", "الحالي", "هذا", "ال", "ملفاتي", "ملفاتك", "ملفاتنا", "ملفات", "الاشياء", "رتبهم", "رتبها", "نظمها", "نظمهم",
                    "الرجاء", "رجاء", "مجلدات", "بمجلدات", "ممكنه", "سطح", "المكتب", "مكتبي", "مكتب", "بشكل", "حسبما", "ترتبها", "ترتبهم"}
_QUAL_AS_NAME = {"old", "new", "older", "newer", "big", "small", "large", "empty", "قديم", "جديد", "كبير", "صغير"}
_GENERIC_DEST = {"واحد", "جديد", "جديده", "خاص", "منفصل", "اخر", "اخري", "new", "one", "single", "separate", "other", "another", "a",
                 "هنا", "هناك", "فوق", "تحت", "مجلد", "المجلد", "folder", "هذا", "هذه", "الخارج", "خارج", "الاعلي", "اعلي", "الاب",
                 "الاصل", "parent", "الاول", "الثاني", "الثالث", "الاخير", "التالي", "السابق", "الاخر", "الاخري"}
_ROOT_TOKENS = {"العمل", "الحالي", "الرئيسي", "الرئيسيه", "هذا", "هذه", "الجذر", "المجلد"}
_ROOT_DEST = {"العمل", "الحالي", "الرئيسي", "الرئيسيه", "الجذر", "root", "workspace"}  # وجهة = جذر مجلد العمل نفسه (لا مجلد بهذا الاسم)
_DUP_FILL = (_FILLER - {"backup", "الي", "to", "into", "في", "داخل", "من", "in", "from", "inside", "احتياطيه"}) | {
    "النسخ", "الزائده", "ال", "لي", "ابحث", "عن", "اعرض", "اظهر", "توجد", "يوجد", "عندي", "كم", "ما", "هي", "هو", "التكرار", "تكرار", "find",
    "search", "show", "list", "ملفاتي", "ملفاتك", "عندك", "عدد"}
_DUP_HERE = re.compile(r"(?<!\w)(?:في|داخل|من)\s+(?:هذا\s+)?(?:ال)?(?:مجلد\s+)?(?:العمل|الحالي|الرئيسي|المجلد)(?!\w)")
_RENAME_FILL = (_FILLER | _ORGANIZE_FILLER | {"اضف", "ضيف", "اضافه", "زد", "add", "اعد", "تسميه", "غير", "اسم", "اسماء", "الاسماء", "rename",
                                              "بادئه", "البادئه", "prefix", "with", "for", "of", "by", "using", "ل", "ب", "بدايه", "البدايه", "اليوم",
                                              "today", "start", "beginning"})
_INSPECT_FILL = (_FILLER | _ORGANIZE_FILLER | {"ما", "هو", "هي", "ماذا", "يوجد", "محتويات", "محتوي", "العمل", "الحالي", "الرئيسي",
                                               "الجذر", "workspace", "root", "is", "what", "whats", "a", "of", "ماهي", "ماهو"})
_CREATE_STOP = {"فيه", "فيها", "داخله", "بداخله", "بداخلها", "معه", "معها", "ثم", "بعدين", "بعدها", "تحته", "عليه"}
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
W_DELETE = _words("احذف", "حذف", "امسح", "مسح", "ازل", "ازاله", "شيل", "تخلص من", "تحذف", "تمسح", "نظف", "نظفها", "تنظيف", "delete", "remove",
                  "erase", "clean")
W_MOVE = _words("انقل", "نقل", "حرك", "تنقل", "تحرك", "move")
W_PUT = _words("ضع", "حط", "اجمع", "جمع", "put")
W_COPY = _words("انسخ", "نسخ", "تنسخ", "copy")
W_BACKUP = re.compile(r"(?<!\w)(?:نسخه\s+احتياطيه|نسخ\s+احتياطي|backup)(?!\w)")   # اسم/وجهة لا فعل نسخ
W_RENAME = _words("اعد تسميه", "غير اسم", "تسميه", "rename")
W_ADD_PREFIX = re.compile(r"(?<!\w)(?:اضف|ضيف|اضافه|زد|add)\s+(?:ال)?(?:بادئه|prefix|تاريخ|date)(?!\w)")
W_PREFIX = _words("بادئه", "prefix", "بدايه", "اضافه", prefix="(?:ب)?(?:ال)?")
W_ORGANIZE = _words("رتب", "رتبها", "رتبهم", "ترتب", "ترتبها", "ترتبهم", "ترتيب", "نظم", "نظمها", "نظمهم", "تنظيم", "صنف", "فرز", "organize", "sort")
W_CREATE = _words("انشئ", "انشاء", "اعمل", "جهز", "create", "make")
W_CREATE_OBJ = _words("مجلد", "مشروع", "هيكل", "folder", "project", prefix="(?:ال)?")
W_INSPECT = _words("افحص", "اعرض", "فحص", "تحقق", "inspect", "ماذا يوجد", "ماذا في", "ما محتوي", "ما محتويات", "show", "list",
                   "what is in", "whats in", "what's in", "ابحث عن", "ابحث", "بحث عن", "هل توجد", "هل يوجد", "هل عندي", "find", "search")
W_UPDATE = _words("حدث", "تحديث", "ترقيه", "winget", "upgrade", "update")
W_WHOAMI = re.compile(r"(?<!\w)(?:whoami|من انا|اسم المستخدم|(?:ال)?هويه)(?!\w)")
W_DUP = re.compile(r"(?<!\w)[وف]?(?:ال)?(?:مكرر\w*|متكرر\w*|تكرار|duplicates?)(?!\w)")
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


_PREFIX_RES = (
    re.compile(r"(?:بادئه|بادئة|prefix)\s*[:：]?\s*[«\"“](.+?)[»\"”]"),
    re.compile(r"(?:بادئه|بادئة|prefix)\s*[:：]?\s*([^\s«»\"'،,]+)"),
    re.compile(r"(?:بإضافة|باضافة|بإضافه|اضافة|إضافة)\s+[«\"']?([^\s«»\"'،,]+)[»\"']?\s+(?:في|الى|إلى|ل)?\s*(?:بداية|اول|أول)"),
)


def _prefix_match(prompt: str):
    """مطابقة عبارة البادئة في الطلب (المجموعة 1 = نص البادئة) أو None."""
    for rx in _PREFIX_RES:
        m = rx.search(prompt)
        if m:
            return m
    return None


def _orig_word(tok: str, *texts: str) -> str:
    """الكلمة كما كتبها المستخدم (لا المطبَّعة) لعرضها في رسائل «لم أفهم»."""
    for t in texts:
        for w in re.findall(r"[^\W_]+", t or ""):
            if norm(w) == tok:
                return w
        for w in re.findall(r"[^\s«»\"'،,؛;:!؟?()]+", t or ""):
            if tok in norm(w):
                return w
    return tok


_TEMP_EXTS = {"tmp", "temp", "crdownload"}   # .bak/.part قد تكون نسخاً احتياطية يدوية أو تنزيلات ناقصة مهمة: لا تُعدّ مؤقتة


def _is_temp(base: str) -> bool:
    b = base.lower()
    return b.startswith("~$") or (("." in b) and b.rsplit(".", 1)[-1] in _TEMP_EXTS)


# فئات بالمعنى لا بالامتداد وحده: (اسم، كلمات مطبَّعة، دالة اختيار على اسم الملف)
_KW_GROUPS = (
    ("المؤقتة", ("مؤقت", "مؤقته", "مؤقتات", "temp", "temporary"), _is_temp),
    ("الفواتير", ("فواتير", "invoice", "invoices"), lambda b: any(k in norm(b) for k in ("فاتور", "فواتير", "invoice"))),
    ("الإيصالات", ("ايصالات", "receipt", "receipts"), lambda b: any(k in norm(b) for k in ("ايصال", "receipt"))),
    ("الإكسل", ("اكسل", "excel"), lambda b: b.lower().rsplit(".", 1)[-1] in ("xls", "xlsx", "xlsm") and "." in b),
    ("الوورد", ("وورد", "word"), lambda b: b.lower().rsplit(".", 1)[-1] in ("doc", "docx", "docm") and "." in b),
)
_KW_RES = [(name, re.compile(r"(?<![\w])(?:و)?(?:ال)?(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w])"), pred)
           for name, words, pred in _KW_GROUPS]

_FILE_KW_RE = re.compile(r"(?<![\w])(?:ال)?(?:ملف|file)\s+(?:ال)?(?:" + "|".join(re.escape(w) for _, ws, _ in _KW_GROUPS for w in ws) + r")(?![\w])")

_FULL_SCAN_LIMIT = 5000


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


def _join_notes(*notes) -> Optional[str]:
    return " ".join(n for n in notes if n) or None


def _skip_notes(clash: List[str], locks: List[str]) -> Optional[str]:
    """ملاحظة بما تُخطي: ملفات اسمها موجود أصلاً في الوجهة، وملفات القفل ~$ (يفتحها برنامج آخر)."""
    parts = []
    if clash:
        parts.append(f"تُخطي {len(clash)} ملفاً لأن في وجهته ملفاً بالاسم نفسه: " + "، ".join(clash[:8]) + (" …" if len(clash) > 8 else "") + ".")
    if locks:
        parts.append(f"تُركت {len(locks)} من ملفات القفل المؤقتة (~$) كما هي لأن برنامجاً قد يفتحها: " + "، ".join(locks[:8]) + (" …" if len(locks) > 8 else "") + ".")
    return " ".join(parts) or None


INFO_PREFIX = "لا شيء للتنفيذ: "   # نتيجة عادية (لا خطة لأن لا شيء يلزم)، لا خطأ: يعرضها المتحكم كمعلومة


def _info(text: str) -> "Analysis":
    return Analysis(note=INFO_PREFIX + text)


class Analysis:
    """slot: نوع الخانة الناقصة حين يكون السؤال طلب توضيح (dest/new_name/folder_name/prefix/organize_mode/source)."""

    def __init__(self, steps=None, question=None, note=None, slot=None):
        self.steps, self.question, self.note, self.slot = steps, question, note, slot


_ANSWER_VERBS = (W_DELETE, W_MOVE, W_PUT, W_COPY, W_RENAME, W_ORGANIZE, W_CREATE, W_INSPECT)


def _quote_name(ans: str, always: bool = False) -> str:
    if _QUOTE_CHARS.search(ans):
        return ans
    return f"«{ans}»" if always or re.search(r"\s", ans) or not re.fullmatch(r"[\w.\-]+", ans) else ans


_ANS_SCOPE_RE = re.compile(r"(?<!\w)(?:في|داخل|من|in|inside|from)(?!\w)")
_SLOT_PREFIX = re.compile(r"^(?:(?:ال)?(?:مجلد|folder|directory|dir)|في|داخل|الى|إلى|الي|to|into|in|inside)\s+", re.I)


def _strip_slot_words(ans: str, slot: str) -> str:
    """ينزع كلمات الربط الطبيعية (مجلد/في/داخل/folder…) من جواب الوجهة أو اسم المجلد ليبقى الاسم الحقيقي؛ «مجلد جديد» يبقى حرفياً."""
    out = ans
    for _ in range(3):
        m = _SLOT_PREFIX.match(norm(out))
        if not m:
            break
        rest = out[m.end():].strip()
        if not rest or norm(rest) in ("جديد", "جديده", "new"):
            break
        out = rest
    if slot == "folder_name":
        m = re.match(r"^(?:باسم|اسمه|اسمها|name|named|called)\s+(.+)$", norm(out), re.I)
        if m:
            out = out[len(out) - len(m.group(1)):]
    return out.strip()


def merge_clarification(request: str, slot: Optional[str], answer: str) -> Optional[str]:
    """يحوّل جواباً عارياً على سؤال توضيح (اسم مجلد، اسم جديد، معيار ترتيب…) إلى جملة صريحة تُعاد تحليلها بدل لصقه نصاً حراً.
    يعيد None إن لم يكن الجواب عارياً (جملة كاملة بفعل/علامة وجهة) أو الخانة مجهولة، فيبقى السلوك القديم (لصق الجواب بالطلب)."""
    ans = re.sub(r"\s+", " ", strip_format_chars(answer or "")).strip()
    req = strip_format_chars(request or "").strip()
    if not slot or not ans or not req:
        return None
    n = norm(ans)
    if _DEST_TO.search(_dest_mask(ans)) or any(w.search(n) for w in _ANSWER_VERBS):
        return None
    last_marker = None
    for last_marker in _DEST_TO.finditer(_dest_mask(req)):
        pass
    if slot in ("dest", "folder_name"):   # «مجلد Docs» / «في Docs» / «folder Docs»: الاسم الحقيقي بلا كلمات الربط
        ans = _strip_slot_words(ans, slot)
        if not ans:
            return None
    if slot in ("dest", "new_name"):
        q = _quote_name(ans, slot == "new_name")
        if last_marker is not None:   # الوجهة/الاسم الجديد كان ناقصاً أو غير صالح: يُستبدل ما بعد العلامة بالجواب (لا يُلصق بعده)
            return f"{req[:last_marker.end()]} {q}"
        return f"{req} إلى {q}"
    if slot == "folder_name":
        return f"{req} باسم {_quote_name(ans, True)}"
    if slot == "prefix":
        pref = ans[len("بادئة"):].strip() if n.startswith(("بادئه ", "بادئة ")) else ans
        if re.search(r"(?:بادئه|بادئة|prefix)\s*$", norm(req)):
            return f"{req} {_quote_name(pref, True)}"
        return f"{req} بإضافة البادئة {_quote_name(pref, True)}"
    if slot == "organize_mode":
        nreq = re.sub(r"(?<!\w)(?:من\s+فضلك|please)(?!\w)", " ", norm(req))
        head = re.split(r"(?<!\w)(?:حسب|بحسب|by|according)(?!\w)", W_ORGANIZE.sub(" ", nreq))[0]
        if (_FILELIKE_I.search(req) or _QUOTE_RE.search(req) or _ANS_SCOPE_RE.search(nreq)
                or any(tok not in _FILLER and tok not in ("العمل", "عمل", "لي", "ال") for tok in re.findall(r"[^\W_]+", head))):
            return None   # الطلب الأصلي يحدد اسماً أو نطاقاً: لا نُسقطه ونرتّب الجذر كله؛ يُلصق الجواب ويُعاد التحليل (فيسأل عند الالتباس)
        mode = re.sub(r"^(?:حسب|بحسب|by)\s+", "", n)
        mode = ans[len(ans) - len(mode):] if len(mode) <= len(ans) else ans
        return f"رتب الملفات حسب {mode}"
    if slot == "source":
        keep: List[str] = []

        def hold(mm):
            keep.append(mm.group(0))
            return f"\ue000{len(keep) - 1}\ue001"
        held = _FILELIKE_I.sub(hold, _QUOTE_RE.sub(hold, req))   # الأجزاء المقتبسة وأسماء الملفات لا تُمسّ بإزالة كلمة «ملفات»
        held = re.sub(r"\s+", " ", re.sub(r"(?<!\w)(?:كل\s+|جميع\s+)?(?:ال)?(?:ملفات|ملف|files?)(?!\w)", " ", held)).strip()   # كلمة «الملفات» العامة لا تبقى مع الاسم
        req = re.sub("\ue000(\\d+)\ue001", lambda mm: keep[int(mm.group(1))], held)
        m = None
        for m in _DEST_TO.finditer(_dest_mask(req)):
            pass
        if m is None:   # «ضع/اجعل … في X»: لا علامة إلى، فالمصدر يسبق علامة «في/داخل» الأخيرة
            for m in _DEST_IN.finditer(_dest_mask(req)):
                pass
        if m is not None:
            return f"{req[:m.start()].rstrip()} {ans} {req[m.start():]}"
        return f"{req} {ans}"
    return None


class Resolved:
    """نتيجة تحديد العناصر المقصودة في الطلب: القائمة + نوع المعيار (name/cat/ext) أو سؤال عند الالتباس."""

    def __init__(self, items=None, kind="", ask=None, kw=None, note=None):
        self.items: List[str] = items or []
        self.kind = kind
        self.ask = ask
        self.kw: List[str] = kw or []     # أسماء الفئات بالمعنى المطابقة (مثل «المؤقتة»)
        self.note: Optional[str] = note   # ملاحظة تخطٍّ (ملفات القفل ~$) تُضاف لملاحظة الخطة


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
        self.cancel_check: Optional[Callable[[], bool]] = None   # يضبطه المتحكم: عند الإيقاف يتوقف تجزئة المكررات فوراً

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
            return json.dumps({"questions": qs, "slots": [a.slot] if qs else []}, ensure_ascii=False)
        if a.steps:
            out = {"steps": a.steps}
            if a.note:
                out["note"] = a.note   # مثل «اقتُصرت الخطة على 48 عنصراً»: تصل للمستخدم مع الخطة
            return json.dumps(out, ensure_ascii=False)
        return json.dumps({"steps": [], "note": a.note or a.question or HELP_QUESTION}, ensure_ascii=False)

    def understands(self, prompt: str, context: List[str]) -> bool:
        a = self.analyze(prompt, context)
        return bool(a.steps)

    # ---------- مسح هيكل مجلد العمل كاملاً للقواعد ----------
    def _full_ents(self) -> Optional[Ents]:
        """مسح مستويين من مجلد العمل (كما يرى المخطط) بحد أعلى كبير بدل 60 عنصراً. partial=True فقط إن تجاوز الحد فعلاً."""
        root = self.workspace
        if root is None:
            return None
        lines: List[str] = []
        truncated = False

        def ok(entry: "os.DirEntry", rel: str) -> bool:
            if _is_link(Path(entry.path)):
                return False
            if rel != "".join(ch for ch in rel if ch.isprintable()) or len(rel) > 120 or rel != rel.strip() or "\n" in rel:
                return False
            return True

        try:
            tops = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            return None
        for e in tops:
            if e.name == ".trash" or e.name.startswith(".undo_journal") or not ok(e, e.name):
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            if len(lines) >= _FULL_SCAN_LIMIT:
                truncated = True   # هناك عنصر آخر لا يتسع: الاقتطاع حقيقي (5000 بالضبط بلا زيادة ليس اقتطاعاً)
                break
            lines.append(e.name + ("/" if is_dir else ""))
            if is_dir:
                try:
                    kids = sorted(os.scandir(e.path), key=lambda c: c.name)
                except OSError:
                    continue
                for c in kids:
                    rel = f"{e.name}/{c.name}"
                    if not ok(c, rel):
                        continue
                    if len(lines) >= _FULL_SCAN_LIMIT:
                        truncated = True
                        break
                    lines.append(rel + ("/" if c.is_dir(follow_symlinks=False) else ""))
            if truncated:
                break
        if truncated:
            lines.append("… (القائمة مقتطعة)")
        return _entries(lines)

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
        if ents.partial and self.workspace is not None:
            ents = self._full_ents() or ents   # قائمة النموذج مقتطعة؛ القواعد تمسح هيكل المجلد كاملاً (حد كبير) فلا تتعطل عند >60 عنصراً
        top_files = [r for r, d in ents.items() if not d and "/" not in r]
        top_dirs = [r for r, d in ents.items() if d and "/" not in r]
        pm = _prefix_match(prompt)
        mprompt = prompt[:pm.start(1)] + " § " + prompt[pm.end(1):] if pm else prompt   # البادئة (مثل «جديد_») ليست كلمة مؤهِّلة ولا نفياً
        text = _DUP_COPY_PHRASE.sub(" مكررات ", self._mask(mprompt, ents))
        text = re.sub(r"(?<!\w)من\s+فضلك(?!\w)", " ", text)

        intents = set()
        if W_DELETE.search(text):
            intents.add("delete")
        if W_MOVE.search(text):
            intents.add("move")
        if W_COPY.search(text):
            intents.add("copy")
        if W_BACKUP.search(text) and not intents & {"delete", "move", "copy"}:
            intents.add("backup")   # «اعمل نسخة احتياطية من X» / «backup X»: نسخ X بجانبه باسم X_backup
        if (W_RENAME.search(text) and W_PREFIX.search(text)) or W_ADD_PREFIX.search(text):
            intents.add("rename")
        elif W_RENAME.search(text):
            intents.add("rename1")
        if W_ORGANIZE.search(text):
            intents.add("organize")
        if W_CREATE.search(text) and W_CREATE_OBJ.search(self._mask(prompt, ents, files_only=True)) and "backup" not in intents:
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
            others = intents - {"dup", "delete", "move", "inspect"}
            if others or {"delete", "move"} <= intents or ("inspect" in intents and intents & {"delete", "move"}):
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
        question_form = bool(re.match(r"\s*(?:هل|do|does|should|can|could|is|are)(?!\w)", norm(prompt), re.I)) or prompt.rstrip().endswith(("؟", "?"))
        if "dup" in intents and (not intents & {"delete", "move"} or question_form):   # بحث/عرض/سؤال عن المكررات: تقرير قراءة فقط بلا حذف ولا نقل
            q = self._dup_extra_ask(prompt, text)
            return Analysis(question=q, slot="source") if q else self._duplicates_report(ents)
        if "create" in intents:
            return self._create(prompt, ents)
        if "backup" in intents:
            return self._backup(prompt, ents)
        if "inspect" in intents:
            return self._inspect(prompt, ents)

        # العمليات التي تغيّر الملفات: لا نخمّن عند الاستثناء أو الشرط
        stripped = re.sub(r"(?<!\w)غير\s+اسم", " ", text)
        if W_NEG.search(stripped):
            return Analysis(question=EXCEPT_QUESTION)
        if W_QUAL.search(stripped):
            return Analysis(question=QUALIFIER_QUESTION)

        if "dup" in intents:
            q = self._dup_extra_ask(prompt, text)
            if q:
                return Analysis(question=q)  # نوع/اسم/نطاق/وجهة مع «المكررات»: لا نتجاهله بصمت
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
            by_type = re.search(r"(?<!\w)(?:ال)?(?:نوع(?:ها|ه|هم)?|type)(?!\w)", text)
            left = re.sub(r"(?<!\w)(?:ب)?(?:ال)?(?:تاريخ|شهر|سنه|نوع(?:ها|ه|هم)?)(?!\w)|(?<!\w)(?:date|month|year|type|by)(?!\w)", " ", text)
            for tok in re.findall(r"[^\W_]+|§", left):
                if tok in _ORGANIZE_FILLER or (tok.startswith("و") and tok[1:] in _ORGANIZE_FILLER):
                    continue
                return Analysis(question=("الترتيب يشمل كل ملفات مجلد العمل فقط، وبمعيار واحد: النوع أو الشهر أو السنة. "
                                          "اكتب مثلاً «رتّب الملفات حسب النوع»."), slot="organize_mode")
            if by_date and by_type:
                return Analysis(question="اختر معياراً واحداً للترتيب: النوع أو التاريخ.", slot="organize_mode")
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

    def _resolve(self, head: str, ents: Ents, names_only: bool = False, inspecting: bool = False) -> Resolved:
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
            if inspecting:
                return None   # «اعرض محتويات Docs» = فحص المجلد نفسه (والتقرير يسرد ما بداخله)
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
        groups: list = []
        kw_names: List[str] = []
        if not names_only:
            sg = _SINGULAR_RE.search(work) or _FILE_CAT_RE.search(work) or _FILE_KW_RE.search(work)
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
            for gname, rx, pred in _KW_RES:   # مؤقتة / فواتير / إيصالات / إكسل / وورد: فئات بالمعنى
                if rx.search(work):
                    groups.append(pred)
                    kw_names.append(gname)
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
                return Resolved(ask=f"لم أفهم «{_orig_word(tok, head)}» في الطلب. سمِّ الملفات بأسمائها كما تظهر، "
                                    f"أو اطلب نوعاً (مثل «الصور» أو «ملفات pdf» أو «الملفات المؤقتة»).")
        if names and (cats or exts or groups):
            return Resolved(ask="حدّد بالأسماء أو بالنوع، لا بالاثنين معاً في طلب واحد.")
        if len(names) > 1 and any(ents.get(n) and norm(n.split("/")[-1]) in _QUAL_AS_NAME for n in names):
            return Resolved(ask="اسم أحد المجلدات (مثل Old أو New) يشبه وصفاً للملفات، فلا أعرف إن كنت تقصد المجلد أم ملفات «قديمة/جديدة». "
                                "اكتب «مجلد <اسم>» بالتنصيص أو سمِّ الملفات وحدها.")
        if names:
            kept = [n for n in dict.fromkeys(names) if not any(n.startswith(o + "/") for o in names if o != n)]
            return Resolved(kept, "name")
        if cats or exts or groups:
            sel = [r for r in pool if (EXT_TO_CAT.get(r.rsplit(".", 1)[-1].lower() if "." in r.split("/")[-1] else "") in cats)
                   or (r.rsplit(".", 1)[-1].lower() in exts and "." in r.split("/")[-1])
                   or any(pred(r.split("/")[-1]) for pred in groups)]
            # ملفات القفل ~$cv.docx يفتحها الأوفيس: لا تُختار ضمن نوع/امتداد (إلا حين تُطلب «المؤقتة» صراحة)
            locks = [r for r in sel if r.split("/")[-1].startswith("~$") and _is_temp not in groups]
            sel = [r for r in sel if r not in locks]
            return Resolved(sorted(dict.fromkeys(sel)), "cat" if (cats or groups) and not exts else "ext", kw=kw_names,
                            note=_skip_notes([], sorted(locks)))
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
        all_files = [r for r, d in ents.items() if not d and "/" not in r and _visible(r)]
        locks = [f for f in all_files if f.startswith("~$")]   # ملفات قفل برامج الأوفيس: لا تُنقل
        files = [f for f in all_files if f not in locks]
        if not files:
            if ents.partial:
                return Analysis(question=PARTIAL_QUESTION)   # لا نقول «لا شيء» والقائمة التي أراها ناقصة
            return _info("لا توجد ملفات مبعثرة في مجلد العمل لترتيبها." + (" " + _skip_notes([], locks) if locks else ""))
        lower_files = {f.casefold() for f in files}
        if any(c.casefold() in lower_files for c in list(CATEGORIES) + [OTHER]):
            return Analysis(question="يوجد ملف باسم أحد مجلدات الترتيب (مثل «صور» أو «أخرى»). غيّر اسمه أولاً.")
        existing = {r.casefold() for r in ents}
        moves, cats, clash = [], [], []
        for f in sorted(files, key=str.lower):
            ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
            cat = EXT_TO_CAT.get(ext, OTHER)
            if f"{cat}/{f}".casefold() in existing:
                clash.append(f)   # الوجهة موجودة أصلاً: نتركه (ونذكره) بدل أن تفشل الخطة في منتصفها أو نكتب فوق شيء
                continue
            existing.add(f"{cat}/{f}".casefold())   # ملفان يختلفان بحالة الأحرف فقط يتصادمان في الوجهة (ويندوز)
            moves.append((f, cat))
        dropped = max(0, len(moves) - MAX_AUTO_STEPS)
        moves = moves[:MAX_AUTO_STEPS]
        while True:
            cats = list(dict.fromkeys(c for _, c in moves))
            if len(cats) + len(moves) <= MAX_AUTO_STEPS:
                break
            moves.pop()
            dropped += 1
        extra = _skip_notes(clash, locks)
        if not moves:
            if ents.partial:
                return Analysis(question=PARTIAL_QUESTION)
            return _info("كل الملفات مرتّبة أو وجهاتها موجودة أصلاً." + (" " + extra if extra else ""))
        steps, i = [], 1
        for c in cats:
            steps.append(_step(i, "create_dir", c, "وجود المجلد")); i += 1
        for f, c in moves:
            steps.append(_step(i, "move", f, "المصدر اختفى والوجهة مطابقة", dest=f"{c}/{f}")); i += 1
        return Analysis(steps, note=_join_notes(_trunc_note(len(moves), dropped, ents.partial), extra))

    def _rename(self, prompt: str, ents: Ents) -> Analysis:
        """إعادة تسمية كل ملفات مجلد (أو مجلد العمل) بإضافة بادئة. لا يدعم اختيار ملفات بعينها: يسأل بدل أن يعمّم.
        أي كلمة باقية لا نفهمها (مجلد بتهجئة خاطئة، ملف غير موجود، «للملف الأول»…) ← سؤال، لا رجوع صامت لكل ملفات الجذر."""
        dirs = sorted((r for r, d in ents.items() if d), key=len, reverse=True)
        m = _prefix_match(prompt)
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
            return Analysis(question="ما البادئة التي تريد إضافتها لأسماء الملفات؟ مثال: «بادئة 2026_»", slot="prefix")
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
            return Analysis(question=(f"لم أفهم «{_orig_word(tok, prompt)}» في الطلب، فلا أطبّق البادئة على كل ملفات الجذر بالتخمين. "
                                      "اذكر اسم مجلد موجود كما يظهر، أو اكتب «أضف بادئة x_» وحدها لملفات مجلد العمل."))
        if folder:
            files = [e for e, d in ents.items() if not d and e.rsplit("/", 1)[0] == folder and "/" in e]
        else:
            files = [e for e, d in ents.items() if "/" not in e and not d]
        files = [f for f in files if _visible(f) and not f.split("/")[-1].startswith(prefix)]
        locks = [f for f in files if f.split("/")[-1].startswith("~$")]   # ملفات قفل الأوفيس لا تُعاد تسميتها
        files = [f for f in files if f not in locks]
        if not files:
            if ents.partial:
                return Analysis(question=PARTIAL_QUESTION)
            return _info("لا توجد ملفات مناسبة لإعادة التسمية" + (f" داخل {folder}." if folder else ".") + (" " + _skip_notes([], locks) if locks else ""))
        dropped = max(0, len(files) - MAX_AUTO_STEPS)
        files = sorted(files, key=str.lower)[:MAX_AUTO_STEPS]   # الباقي يكمله التشغيل التالي (الملفات التي بدأت بالبادئة تُستثنى)
        existing = {r.casefold() for r in ents}
        for f in files:
            new = (f.rsplit("/", 1)[0] + "/" if "/" in f else "") + prefix + f.split("/")[-1]
            if new.casefold() in existing:
                return Analysis(question=f"الاسم الجديد «{new}» موجود أصلاً. غيّر البادئة أو أعد تسمية الموجود أولاً.")
        steps = [_step(i, "rename", f, "الاسم الجديد موجود والمحتوى مطابق", new_name=prefix + f.split("/")[-1])
                 for i, f in enumerate(sorted(files), 1)]
        return Analysis(steps, note=_join_notes(_trunc_note(len(steps), dropped, ents.partial), _skip_notes([], locks)))

    def _rename_one(self, prompt: str, ents: Ents) -> Analysis:
        """«غير اسم a.txt إلى b.txt»: إعادة تسمية عنصر واحد باسم صريح (يبقى في مجلده)."""
        ms = list(_DEST_TO.finditer(prompt)) or list(re.finditer(r"(?<!\w)ب(?=\s*[A-Za-z0-9«\"“])", prompt))
        if not ms:
            if re.search(r"(?<!\w)(?:ال)?ملفات(?!\w)|(?<!\w)files(?!\w)", norm(prompt)):
                return Analysis(question="إعادة تسمية عدة ملفات تتم بإضافة بادئة لأسمائها. ما البادئة؟ مثال: «2026_»", slot="prefix")
            return Analysis(question="ما الاسم الجديد؟ مثال: «غير اسم a.txt إلى b.txt»", slot="new_name")
        m = ms[-1]
        head, tail = prompt[:m.start()], prompt[m.end():].strip()
        q = re.match(r"^[«\"“'](.+?)[»\"”']", tail)
        if q:
            new, rest = q.group(1).strip(), tail[q.end():]
        else:
            t = re.match(r"^([^\s«»\"'،,؛;]+)", tail)
            new, rest = (_unq(t.group(1)), tail[t.end():]) if t else ("", "")
        if not new:
            return Analysis(question="ما الاسم الجديد؟ مثال: «غير اسم a.txt إلى b.txt»", slot="new_name")
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

    @staticmethod
    def _ask(question: str) -> Analysis:
        """سؤال القواعد مع نوع الخانة الناقصة إن عُرف (ليحوّل المتحكم الجواب العاري إلى جملة صريحة)."""
        slot = "dest" if (question.startswith(("ما اسم المجلد الذي تريد الوجهة", "إلى أي مجلد", "اسم المجلد يبدو من أكثر"))
                          or any(k in question for k in ("ليس اسم مجلد", "يبدو اسم ملف لا مجلد", "فلا أستطيع إنشاء مجلد بالاسم"))) else None
        return Analysis(question=question, slot=slot)

    def _copy(self, prompt: str, ents: Ents, top_dirs: List[str], top_files: List[str]) -> Analysis:
        dest_dir, ask, head = self._parse_dest(prompt, ents, top_dirs, top_files, allow_in=False)
        if ask:
            return self._ask(ask)
        res = self._resolve(head, ents)
        if res.ask:
            return Analysis(question=res.ask)
        if ents.partial and res.kind in ("cat", "ext"):
            return Analysis(question=PARTIAL_QUESTION)
        if not res.items:
            return Analysis(question="ما اسم الملف أو المجلد الذي تريد نسخه؟ اكتبه كما يظهر في مجلد العمل.", slot="source")
        if len(res.items) > MAX_AUTO_STEPS - 1:
            return Analysis(question=f"عدد العناصر كبير ({len(res.items)}). حدّد مجموعة أصغر.")
        if not dest_dir:
            dest_dir = "نسخة_احتياطية"
            if any(f.casefold() == dest_dir.casefold() for f in top_files):
                return Analysis(question="يوجد ملف باسم «نسخة_احتياطية» فلا أنشئ مجلداً بالاسم نفسه. اكتب مجلد الوجهة: «انسخ <اسم> إلى <مجلد>».")
        return self._with_note(self._transfer("copy", res.items, dest_dir, ents, top_dirs, "الوجهة مطابقة للمصدر والمصدر سليم"), res.note)

    @staticmethod
    def _with_note(a: Analysis, note: Optional[str]) -> Analysis:
        if note and (a.steps or (a.note and a.note.startswith(INFO_PREFIX))):
            a.note = _join_notes(a.note, note)
        return a

    def _move(self, prompt: str, ents: Ents, top_dirs: List[str], top_files: List[str]) -> Analysis:
        dest_dir, ask, head = self._parse_dest(prompt, ents, top_dirs, top_files, allow_in=True)
        if ask:
            return self._ask(ask)
        if not dest_dir:
            return Analysis(question="إلى أي مجلد تريد النقل؟ مثال: «انقل ملفات pdf إلى مستندات»", slot="dest")
        res = self._resolve(head, ents)
        if res.ask:
            return Analysis(question=res.ask)
        if ents.partial and res.kind in ("cat", "ext"):
            return Analysis(question=PARTIAL_QUESTION)
        if not res.items:
            return Analysis(question="ما الذي تريد نقله؟ اكتب اسم الملف/المجلد أو نوعه (مثل pdf أو الصور).", slot="source")
        if len(res.items) > MAX_AUTO_STEPS - 1:
            return Analysis(question=f"عدد العناصر كبير ({len(res.items)}). حدّد مجموعة أصغر.")
        return self._with_note(self._transfer("move", res.items, dest_dir, ents, top_dirs, "المصدر اختفى والوجهة مطابقة"), res.note)

    @staticmethod
    def _unique_name(base: str, taken: set) -> str:
        """اسم فريد بصيغة «name (1).ext» (نفس صيغة حماية FileManager) لا يصطدم بما في taken (casefold)."""
        stem, suffix = (base.rsplit(".", 1)[0], "." + base.rsplit(".", 1)[1]) if "." in base[1:] else (base, "")
        n = 1
        while True:
            cand = f"{stem} ({n}){suffix}"
            if cand.casefold() not in taken:
                return cand
            n += 1

    def _same_content(self, a: str, b: str) -> bool:
        """ملفان متطابقا المحتوى (الحجم ثم SHA-256)؟ يعيد False عند أي شك (مجلد، غير متاح، كبير جداً)."""
        sa, sb = self._stat(a), self._stat(b)
        if not sa or not sb or sa[1].st_size != sb[1].st_size or sa[1].st_size > self.MAX_HASH_BYTES:
            return False
        try:
            check_not_big_cloud_file(sa[0])
            check_not_big_cloud_file(sb[0])
            hs = []
            for fp in (sa[0], sb[0]):
                h = hashlib.sha256()
                with open(fp, "rb") as fh:
                    while chunk := fh.read(1 << 20):
                        h.update(chunk)
                hs.append(h.digest())
            return hs[0] == hs[1]
        except (OSError, CloudFileNotLocalError):
            return False

    def _identical_copy(self, src: str, parent: str, base: str, ents: Ents) -> Optional[str]:
        """نسخة مطابقة المحتوى لـ src موجودة في parent باسم base أو «base (n)»؟ تعيد اسمها أو None (المصدر والوجهة ملفان فقط)."""
        if ents.get(src, True):
            return None
        stem, suffix = (base.rsplit(".", 1)[0], "." + base.rsplit(".", 1)[1]) if "." in base[1:] else (base, "")
        pat = re.compile(re.escape(stem.casefold()) + r"(?: \(\d+\))?" + re.escape(suffix.casefold()) + r"$")
        pre = (parent + "/") if parent else ""
        for r, is_dir in ents.items():
            if is_dir or not r.casefold().startswith(pre.casefold()) or "/" in r[len(pre):]:
                continue
            if pat.fullmatch(r[len(pre):].casefold()) and self._same_content(src, r):
                return r
        return None

    def _transfer(self, action: str, sources: List[str], dest_dir: str, ents: Ents,
                  top_dirs: List[str], criteria: str) -> Analysis:
        existing = {r.casefold() for r in ents}
        if dest_dir == ".":  # جذر مجلد العمل: العنصر يوضع باسمه في الجذر
            nested = [s for s in sources if "/" in s]
            if not nested:
                return _info("العناصر المطلوبة موجودة أصلاً في جذر مجلد العمل.")
            if len(nested) != len(sources) and action == "copy":
                return Analysis(question="بعض العناصر في الجذر أصلاً فلا يمكن نسخها إليه. سمِّ العناصر التي داخل المجلدات فقط.")
            bases = [s.split("/")[-1].casefold() for s in nested]
            if len(set(bases)) != len(bases):
                return Analysis(question="في الاختيار عنصران بالاسم نفسه من مجلدين مختلفين فيتعارضان في الجذر. اطلب أحدهما.")
            taken, notes, steps, same = set(existing), [], [], []
            for src in sorted(nested):
                base = src.split("/")[-1]
                new = base
                if base.casefold() in taken:
                    twin = self._identical_copy(src, "", base, ents) if action == "copy" else None
                    if twin:
                        same.append(f"«{src}» (نسخة مطابقة موجودة: «{twin}»)")
                        continue
                    new = self._unique_name(base, taken)
                    notes.append(f"«{base}» موجود في الجذر أصلاً فسيُحفظ باسم «{new}»")
                taken.add(new.casefold())
                steps.append(_step(len(steps) + 1, action, src, criteria, dest=new))
            if len(nested) != len(sources):
                notes.append("العناصر الموجودة في الجذر أصلاً تُركت كما هي")
            if same:
                if not steps:
                    return _info("نسخة مطابقة المحتوى موجودة أصلاً فلم أنسخ: " + "، ".join(same[:8]) + ".")
                notes.append("تُخطي نسخ ما له نسخة مطابقة موجودة: " + "، ".join(same[:8]))
            return Analysis(steps, note=("؛ ".join(notes) + ".") if notes else None)
        sources = [s for s in sources if s.casefold() != dest_dir.casefold()]
        if action == "move":
            sources = [s for s in sources if s.casefold() != f"{dest_dir}/{s.split('/')[-1]}".casefold()]
            if not sources:
                return _info(f"العناصر المطلوبة موجودة أصلاً داخل {dest_dir}.")
        if not sources:
            return Analysis(question="لا يمكن نسخ المجلد إلى نفسه. اختر وجهة أخرى.")
        bases = [s.split("/")[-1].casefold() for s in sources]
        if len(set(bases)) != len(bases):
            return Analysis(question="في الاختيار عنصران بالاسم نفسه من مجلدين مختلفين فيتعارضان في الوجهة. اطلب أحدهما.")
        steps, i = [], 1
        if dest_dir.casefold() not in {d.casefold() for d in top_dirs}:
            steps.append(_step(i, "create_dir", dest_dir, "وجود المجلد")); i += 1
        taken, notes, same = set(existing), [], []
        for src in sorted(sources):
            base = src.split("/")[-1]
            new = base
            if f"{dest_dir}/{base}".casefold() in taken:   # الوجهة فيها الاسم أصلاً: اسم فريد «(1)» صريح بدل سؤال لا يُجاب عنه
                twin = self._identical_copy(src, dest_dir, base, ents) if action == "copy" else None
                if twin:   # نسخة مطابقة المحتوى موجودة: لا نكدّس «x (1)…x (9)» متطابقة
                    same.append(f"«{src}» (نسخة مطابقة موجودة: «{twin}»)")
                    continue
                new = self._unique_name(base, {t[len(dest_dir) + 1:] for t in taken if t.startswith(dest_dir.casefold() + "/")})
                notes.append(f"«{base}» موجود في {dest_dir} أصلاً فسيُحفظ باسم «{new}» (لا شيء يُستبدل)")
            taken.add(f"{dest_dir}/{new}".casefold())
            steps.append(_step(i, action, src, criteria, dest=f"{dest_dir}/{new}")); i += 1
        if same:
            if not any(st["action_type"] == action for st in steps):
                return _info("نسخة مطابقة المحتوى موجودة أصلاً في " + dest_dir + " فلم أنسخ: " + "، ".join(same[:8]) + ".")
            notes.append("تُخطي نسخ ما له نسخة مطابقة موجودة: " + "، ".join(same[:8]))
        return Analysis(steps, note=("؛ ".join(notes[:6]) + (" …" if len(notes) > 6 else "") + ".") if notes else None)

    def _backup(self, prompt: str, ents: Ents) -> Analysis:
        """«اعمل نسخة احتياطية من X»: نسخ X إلى مجلد «نسخة_احتياطية» (يُنشأ عند الحاجة) كخطة واحدة؛ وإن وُجد الاسم هناك يُحفظ بـ «(1)»."""
        head = re.sub(r"(?:نسخ[ةه]\s+احتياطي[ةه]?|backup)\s+(?:(?:من|of)\s+)?", " ", prompt, flags=re.I)   # «من» هنا ليست نطاقاً
        res = self._resolve(head, ents)
        if res.ask:
            return Analysis(question=res.ask)
        if ents.partial and res.kind in ("cat", "ext"):
            return Analysis(question=PARTIAL_QUESTION)
        if not res.items:
            return Analysis(question="نسخة احتياطية من ماذا؟ اكتب اسم الملف أو المجلد كما يظهر، مثل «اعمل نسخة احتياطية من Docs».", slot="source")
        if len(res.items) > MAX_AUTO_STEPS:
            return Analysis(question=f"عدد العناصر كبير ({len(res.items)}). حدّد مجموعة أصغر.")
        dest_dir = "نسخة_احتياطية"
        if any(not d and "/" not in r and r.casefold() == dest_dir.casefold() for r, d in ents.items()):
            return Analysis(question="يوجد ملف باسم «نسخة_احتياطية» فلا أنشئ مجلداً بالاسم نفسه. اكتب مجلد الوجهة: «انسخ <اسم> إلى <مجلد>».")
        top_dirs = [r for r, d in ents.items() if d and "/" not in r]
        return self._with_note(self._transfer("copy", res.items, dest_dir, ents, top_dirs, "الوجهة مطابقة للمصدر والمصدر سليم"), res.note)

    def _dup_extra_ask(self, prompt: str, text: str) -> Optional[str]:
        """سؤال إن جاء مع «المكررات» نوع/اسم/نطاق/وجهة (لا نتجاهله بصمت)، وإلا None."""
        left = _DUP_HERE.sub(" ", re.sub(r"(?<!\w)تخلص\s+من(?!\w)", " ", W_DUP.sub(" ", W_INSPECT.sub(" ", text))))
        if _DEST_TO.search(_dest_mask(prompt)) or _unknown_token(left, _DUP_FILL, mask_unknown=True):
            return DUP_EXTRA_QUESTION
        return None

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
        all_files = [r for r, d in ents.items() if not d and "/" not in r and _visible(r)]
        locks = [f for f in all_files if f.startswith("~$")]
        files = [f for f in all_files if f not in locks]
        existing = {r.casefold() for r in ents}
        moves, folders, clash = [], [], []
        names_cf = {x.casefold() for x in files}
        for f in sorted(files, key=str.lower):
            st = self._stat(f)
            if not st:
                continue
            d = datetime.date.fromtimestamp(st[1].st_mtime)
            key = f"{d.year}" if by_year else f"{d.year}-{d.month:02d}"
            if key.casefold() in names_cf:
                clash.append(f)
                continue
            if f"{key}/{f}".casefold() in existing:
                clash.append(f)
                continue
            if key not in folders:
                folders.append(key)
            moves.append((f, key))
        n_before = len(moves)
        moves = moves[: max(0, MAX_AUTO_STEPS - len(folders))]
        dropped = n_before - len(moves)
        folders = [k for k in folders if any(m[1] == k for m in moves)]
        extra = _skip_notes(clash, locks)
        if not moves:
            if ents.partial:
                return Analysis(question=PARTIAL_QUESTION)
            return _info("لا توجد ملفات لترتيبها حسب التاريخ في مجلد العمل." + (" " + extra if extra else ""))
        steps, i = [], 1
        for k in folders:
            steps.append(_step(i, "create_dir", k, "وجود المجلد")); i += 1
        for f, k in moves:
            steps.append(_step(i, "move", f, "المصدر اختفى والوجهة مطابقة", dest=f"{k}/{f}")); i += 1
        return Analysis(steps, note=_join_notes(_trunc_note(len(moves), dropped, ents.partial), extra))

    def _dup_groups(self, ents: Ents) -> Optional[List[List[str]]]:
        """مجموعات الملفات المتطابقة المحتوى (حجم ثم SHA-256) في العمق الذي يمسحه المسح الكامل. كل مجموعة مرتبة: الأصل أولاً."""
        if self.workspace is None:
            return None
        import time as _t
        ckey = tuple(ents.items())
        hit = getattr(self, "_dup_cache", None)
        if hit and hit[0] == ckey and _t.monotonic() - hit[1] < 3.0:
            return hit[2]   # نفس الطلب يمرّ على التحليل أكثر من مرة (فهم/توضيح/تخطيط): مسح واحد يكفي
        files = [r for r, d in ents.items() if not d and not any(part.startswith(".") for part in r.split("/"))
                 and r.split("/")[-1].lower() not in SYSTEM_FILES and not r.split("/")[-1].startswith("~$")
                 and not ("/" in r and r.split("/")[0].casefold() in _DUP_SKIP_DIRS)]   # النسخ الاحتياطية وما نُقل لـ«مكررات» ليس «مكرراً» يُحذف
        by_size: Dict[int, List[Tuple[str, Path]]] = {}
        for f in files:
            st = self._stat(f)
            if st and 0 < st[1].st_size <= self.MAX_HASH_BYTES:
                by_size.setdefault(st[1].st_size, []).append((f, st[0]))
        groups: List[List[str]] = []
        for group in by_size.values():
            if len(group) < 2:
                continue
            by_hash: Dict[str, List[str]] = {}
            for rel, fp in group:
                if self.cancel_check is not None and self.cancel_check():
                    raise EmergencyStopTriggered("أُوقف البحث عن المكررات.")
                h = hashlib.sha256()
                try:
                    check_not_big_cloud_file(fp)  # لا نُنزّل ملفات OneDrive الكبيرة لمجرد البحث عن مكررات
                except CloudFileNotLocalError:
                    continue
                try:
                    with open(fp, "rb") as fh:
                        while chunk := fh.read(1 << 20):
                            h.update(chunk)
                            if self.cancel_check is not None and self.cancel_check():
                                raise EmergencyStopTriggered("أُوقف البحث عن المكررات.")
                except OSError:
                    continue
                by_hash.setdefault(h.hexdigest(), []).append(rel)
            for same in by_hash.values():
                if len(same) > 1:
                    same.sort(key=lambda r: (r.count("/"), len(r), r.lower()))
                    groups.append(same)
        groups.sort(key=lambda g: g[0].lower())
        self._dup_cache = (ckey, _t.monotonic(), groups)
        return groups

    def _duplicates(self, delete: bool, ents: Ents) -> Analysis:
        """يكشف الملفات المكررة بمحتواها ويُبقي نسخة واحدة (الأقصر مساراً ثم الأقل عمقاً أصلاً)؛
        يحذف المكررات (بموافقة) أو ينقلها لمجلد «مكررات». الملفات المخفية وملفات النظام والفارغة تُتجاهل."""
        groups = self._dup_groups(ents)
        if groups is None:
            return Analysis(note="كشف المكررات يحتاج ربط المساعد بمجلد العمل.")
        dups = sorted((d for g in groups for d in g[1:]), key=str.lower)
        if not dups:
            return _info("لم أجد ملفات مكررة المحتوى في مجلد العمل.")
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
        return Analysis(steps, note=_trunc_note(len(dups), 0, ents.partial))

    def _duplicates_report(self, ents: Ents) -> Analysis:
        """بحث/عرض المكررات: تقرير قراءة فقط (فحص كل ملف في المجموعات) والملاحظة تسرد المجموعات؛ لا حذف ولا نقل."""
        groups = self._dup_groups(ents)
        if groups is None:
            return Analysis(note="كشف المكررات يحتاج ربط المساعد بمجلد العمل.")
        if not groups:
            return _info("لم أجد ملفات مكررة المحتوى" + (" في الجزء الذي مسحته (القائمة كبيرة)." if ents.partial else " في مجلد العمل."))
        shown = []
        for g in groups[:12]:
            shown.append(" = ".join(g[:4]) + (f" (+{len(g) - 4})" if len(g) > 4 else ""))
        more = f" … و{len(groups) - 12} مجموعة أخرى" if len(groups) > 12 else ""
        n_dup = sum(len(g) - 1 for g in groups)
        note = f"وجدت {len(groups)} مجموعة مكررة ({n_dup} نسخة زائدة)؛ لم يُحذف أو يُنقل شيء: " + "؛ ".join(shown) + more
        if ents.partial:
            note += ". المسح جزئي لأن مجلد العمل كبير، فقد توجد مكررات أخرى لم أفحصها"
        members = [m for g in groups for m in g][:MAX_AUTO_STEPS]
        return Analysis([_step(i, "inspect", m, "العنصر موجود فعلاً") for i, m in enumerate(members, 1)],
                        note=_join_notes(note, _trunc_note(len(members), max(0, sum(len(g) for g in groups) - len(members)))))

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
            return Analysis(question="ما الملف أو المجلد الذي تريد حذفه؟ اكتب اسمه كما يظهر في مجلد العمل.", slot="source")
        if len(res.items) > MAX_AUTO_STEPS:
            return Analysis(question=f"عدد العناصر كبير ({len(res.items)}). حدّد مجموعة أصغر للحذف.")
        steps = [_step(i, "delete", t, "العنصر اختفى من مكانه وصار في السلة بنفس المحتوى")
                 for i, t in enumerate(sorted(res.items), 1)]
        note = res.note
        if "المؤقتة" in res.kw:   # «المؤقتة» تقدير بالمعنى: تُعرض أسماء ما سيُحذف قبل الموافقة
            names = sorted(res.items)
            note = _join_notes("ستُحذف هذه الملفات المؤقتة (كل حذف يطلب موافقتك ويمكن التراجع عنه): " + "، ".join(names[:30])
                               + (f" … و{len(names) - 30} أخرى" if len(names) > 30 else "") + ".", note)
        return Analysis(steps, note=note)

    def _create(self, prompt: str, ents: Ents) -> Analysis:
        name = None
        q = _QUOTE_RE.search(prompt)
        raw_used = ""
        if q:
            name, raw_used = q.group(1).strip(), q.group(0)
        else:
            m = (re.search(r"(?<![\w])(?:باسم|بإسم|بأسم|اسمه|اسمها|named|called|name)\s*[:：]?\s*", prompt, re.I)
                 or re.search(r"(?:مشروع|مجلد|هيكل مشروع|folder|project)\s+(?:جديد\s+|new\s+)?", prompt, re.I))
            if m:
                # الاسم غير المنصَّص يمتد على كل الكلمات التالية حتى أول كلمة حشو/وجهة (مثل «فواتير 2025»)
                toks = []
                for t in re.finditer(r"[^\s«»\"'،,؛;]+", prompt[m.end():]):
                    nt = norm(t.group(0))
                    if (nt in _CREATE_FILLER or nt == "و" or nt.strip(".!؟?") in _CREATE_FILLER or nt in _CREATE_STOP
                            or _FILELIKE_I.search(t.group(0)) or "/" in t.group(0)
                            or any(w.search(nt) for w in (W_DELETE, W_MOVE, W_COPY, W_PUT, W_CREATE, W_THEN))):
                        break
                    toks.append(t)
                if toks:
                    seg = prompt[m.end():][toks[0].start():toks[-1].end()]
                    name, raw_used = _unq(seg), seg
        if not name or name.lower() in _CREATE_GENERIC:
            return Analysis(question="ما اسم المجلد أو المشروع الذي تريد إنشاءه؟ مثال: «أنشئ مجلد Reports»", slot="folder_name")
        # لا نُسقط جزءاً من الطلب بصمت: أي كلمات غير مفهومة بعد الاسم ← سؤال
        left = norm(prompt.replace(raw_used, " § ", 1))
        # «مشروع» تُحدِّد نوع الإنشاء من فعل الطلب وموضوعه فقط، لا من كلمة داخل الاسم نفسه (مجلد «مشروع التخرج» أو project_files مجلد عادي)
        is_project = bool(re.search(r"(?<!\w)(?:ال)?(?:مشروع|هيكل|project)(?!\w)", left))
        for tok in re.findall(r"[^\W_]+", left):
            if tok in ("§", "و") or tok in _CREATE_FILLER or (tok.startswith("و") and tok[1:] in _CREATE_FILLER):
                continue
            return Analysis(question=f"لم أفهم «{_orig_word(tok, prompt)}» في طلب الإنشاء. إن كان اسم المجلد من عدة كلمات فاكتبه بين علامتي تنصيص: «أنشئ مجلد «صور قديمة»».")
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
        res = self._resolve(prompt, ents, names_only=True, inspecting=True)
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
