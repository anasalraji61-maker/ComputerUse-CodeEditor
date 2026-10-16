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
from ..arabic import n_units as _n, UNITS as _UNITS   # العدد مع المعدود بالإعراب الصحيح (مشترك بين الوحدات)
from ..safety import EmergencyStopTriggered
from ..tools.files import (_bad_component, check_not_big_cloud_file, CloudFileNotLocalError, _is_link, name_problem,
                           name_len, MAX_NAME_UNITS, MAX_PATH_CHARS, IS_WINDOWS)

_DUP_SKIP_DIRS = ("نسخة_احتياطية", "نسخ_احتياطية", "مكررات", "backup", "backups")   # مجلدات لا تدخل مقارنة المكررات (نسخ احتياطية متعمدة / ما نُقل سابقاً)
MAX_AUTO_STEPS = 400  # = MAX_PLAN_STEPS في planner.py (خطوات الإنشاء تُحسب ضمن الحد في الترتيب/التاريخ؛ والنقل/النسخ يتركان مقعداً لإنشاء المجلد)
MAX_AUTO_DELETE_STEPS = 48  # خطط الحذف تطلب موافقة لكل خطوة: سقفها يبقى صغيراً كي لا تغرق الواجهة بنوافذ الموافقة

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
BACKUP_MULTI_QUESTION = ("الطلب يجمع نسخة احتياطية وعملية أخرى، ولن أنفّذ إحداهما وأهمل الأخرى بصمت. اطلب النسخة الاحتياطية أولاً "
                         "(مثل «اعمل نسخة احتياطية من Docs») ثم اطلب العملية الأخرى بعد انتهائها.")
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
_QUOTE_RE = re.compile(r"(?:[«\"“]|(?<!\w)['‘])(.+?)(?:[»\"”]|['’](?!\w))")
_QUOTE_CHARS = re.compile(r"[«»\"“”‘’]")
_POLITE = {"لو", "سمحت", "من", "فضلك", "الان", "الآن", "please", "now", "فورا", "فوراً", "الي", "لي"}
_STOP_AFTER_FROM = {"نوع", "انواع", "هذا", "هذه", "هذي", "ذلك", "الان", "اليوم", "امس", "بين", "فضلك", "سمحت"}
_FILLER = {"ملف", "ملفات", "الملف", "الملفات", "file", "files", "مجلد", "المجلد", "folder", "folders", "the", "this", "these",
           "كل", "جميع", "كافه", "نوع", "انواع", "من", "في", "داخل", "الي", "to", "into", "in", "from", "inside", "لو", "سمحت",
           "فضلك", "الان", "please", "now", "هذا", "هذه", "على", "عن", "ممكن", "هل", "can", "you", "could", "would", "ابغي",
           "اريد", "ابي", "i", "want", "احذف", "حذف", "امسح", "مسح", "ازل", "ازاله", "شيل", "تخلص", "تحذف", "تمسح", "delete",
           "remove", "erase", "انقل", "نقل", "حرك", "تنقل", "تحرك", "move", "ضع", "حط", "اجمع", "جمع", "put", "انسخ", "نسخ",
           "تنسخ", "copy", "backup", "نسخه", "احتياطيه", "اعمل", "اعملي", "سوي", "عمل", "كلها", "كله", "جميعها", "all", "ملفاتي", "ملفاتك", "ملفاتنا", "انشئ", "create",
           "make", "خذ", "take", "نظف", "نظفها", "تنظيف", "clean"}
_EXEC_EXTS = {"exe", "bat", "cmd", "com", "scr", "msi", "ps1", "vbs", "vbe", "wsf", "hta", "lnk", "reg", "jar", "dll", "sys", "pif", "cpl", "msc"}
_CREATE_GENERIC = {"جديد", "جديده", "جديدا", "باسم", "بإسم", "بأسم", "تجريبي", "تجريبيا", "new", "named", "called", "name", "a", "an", "the"}
_CREATE_FILLER = {"يسمي", "يسمى", "مسمي", "مسمى", "المسمي", "المسمى", "بعنوان", "عنوانه", "titled", "الي", "اللي", "الذي", "حقي", "سوي", "سمي", "انشئ", "انشاء", "اعمل", "جهز", "create", "make", "مجلد", "مجلدا", "مشروع", "مشروعا", "هيكل", "folder", "project",
                  "جديد", "جديده", "جديدا", "new", "باسم", "بإسم", "بأسم", "اسمه", "اسمها", "named", "called", "name", "a", "an",
                  "the", "لي", "for", "me", "فرعي", "فرعيا", "لو", "سمحت", "فضلك", "please", "ممكن", "هل", "can", "you", "تجريبي", "تجريبيا", "ال",
                  "الان", "now", "ابغي", "اريد", "ابي", "i", "want", "تنشئ", "تعمل", "تجهز", "هيكلا", "داخل", "في", "in", "العمل", "مجلد",
                  "على", "علي", "عند", "ضمن", "سطح", "المكتب", "مكتب", "مكتبي", "desktop", "on", "at", "here", "هنا", "الحالي", "الرئيسي", "workspace", "جهازي"}   # كلمات موقع: المجلد يُنشأ في مجلد العمل
_ORGANIZE_FILLER = {"رتب", "ترتيب", "نظم", "تنظيم", "صنف", "فرز", "organize", "sort", "الملفات", "ملفات", "ملف", "file", "files",
                    "حسب", "بحسب", "according", "لو", "سمحت", "فضلك", "please", "ممكن", "هل", "can", "you", "الان", "now", "كل",
                    "جميع", "all", "my", "لي", "الكل", "تلقائيا", "اوتوماتيك", "تنظم", "ترتب", "the", "في", "المجلد", "مجلد",
                    "العمل", "الحالي", "هذا", "ال", "ملفاتي", "ملفاتك", "ملفاتنا", "ملفات", "الاشياء", "رتبهم", "رتبها", "نظمها", "نظمهم",
                    "الرجاء", "رجاء", "مجلدات", "بمجلدات", "ممكنه", "ابي", "اريد", "ابغي", "عايز", "شيء", "شي", "كلشي", "سطح", "المكتب", "مكتبي", "مكتب", "بشكل", "حسبما", "ترتبها", "ترتبهم"}
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
_AGAIN_WORDS = {"و", "مره", "اخري", "اخر", "ايضا", "كمان", "تاني", "ثانيه", "مجددا", "ثم", "بعدها"}   # «مرة أخرى»: ليست عملية ثانية
_INSPECT_FILL = (_FILLER | _ORGANIZE_FILLER | {"ما", "هو", "هي", "ماذا", "يوجد", "محتويات", "محتوي", "العمل", "الحالي", "الرئيسي",
                                               "الجذر", "workspace", "root", "is", "what", "whats", "a", "of", "ماهي", "ماهو", "المجلدات", "مجلدات", "المحتويات", "اللي", "الي", "شيء", "me", "ماذا"})
_CREATE_STOP = {"على", "علي", "عند", "ضمن", "on", "at", "فيه", "فيها", "داخله", "بداخله", "بداخلها", "معه", "معها", "with", "مع", "containing", "contains", "includes", "يحتوي", "تحتوي", "بي", "بيه", "and", "ثم", "بعدين", "بعدها", "تحته", "عليه"}
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


_POLITE_RE = re.compile(r"\s*(?<![\w.])(?:من\s+فضلك|لو\s+سمحت|لو\s+تكرمت|الرجاء|رجاء|يا\s+مساعد|هل\s+(?:يمكنك|تستطيع|بامكانك|بإمكانك)|"
                        r"(?:can|could|would)\s+you|please|لي|"
                        r"يا\s*ريت|ياريت|شكرا(?:\s+لك)?|شكراً|يعطيك\s+العافي[ةه]|الله\s+يعافيك|السلام\s+عليكم|مرحبا|مرحباً|اهلا|أهلا|هلا|عيني|تكفى|تكفي|وش\s+رايك|ينفع|فيك|ارجو|أرجو|تلقائيا|تلقائياً|"
                        r"يا\s+(?:اخوي|أخوي|زلمة|هندسة|رجال|شباب|صديقي|عزيزي|مساعد)|"
                        r"هس[ةهاع]|الحين|دحين|هلق|هلأ|ود[يى](?!\s+[^\s«»\"]+\.[A-Za-z0-9]{1,6}(?![\w]))|اني|آني|بغيت|بد[يى]|عايز|عاوز|حابب|حاب|عيوني|حبيبي|اخوي|خوش|ان\s*شاء\s*الله|"   # لهجات: «الآن»، «أريد»، مجاملات
                        r"يا\s+(?:غالي|طيب|حبيب|اخي|أخي|استاذ|أستاذ|باشا|بطل)|ويا[يه]|معاي)(?!\w|\.\w)\s*", re.I)
_ARROW_RE = re.compile(r"\S*(?:-+>|=+>|[→⟶⇒➜➔])\S*")
_GLUE_WORDS = ("انسخ", "انقل", "احذف", "امسح", "رتب", "اضف", "أضف", "حدث", "حدّث", "ملفات", "الملفات", "مجلد", "المجلد", "مشروع", "بادئة", "البادئة",
               "باسم", "إلى", "الى", "الي", "إلي", "داخل", "copy", "move", "delete", "to", "into")
_GLUE_RE = re.compile(r"(?<![\w.\-/\\])(?P<w>" + "|".join(sorted((re.escape(w) for w in _GLUE_WORDS), key=len, reverse=True)) + r")(?P<r>[A-Za-z0-9][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]+)*)(?![\w.\-/\\])")
_QAM_RE = re.compile(r"\s*(?<![\w.])قم\s+ب(?=[\w])")
_AL_EXT_RE = re.compile(r"(?<![\w.])ال([A-Za-z0-9]{2,5})(?![\w.])")
_TYPO_RE = re.compile(r"(?<![\w.])[\u0621-\u064a]{5,9}(?![\w.])")
_TYPO_TARGETS = ("الملفات", "المجلدات")


def _typo_fix(m, names: set) -> str:
    """خطأ إملائي بحرف واحد في كلمتين شائعتين جداً (الملفات/المجلدات) ← الصواب؛ ما كان اسماً موجوداً لا يُمسّ."""
    w = m.group(0)
    nw = norm(w)
    if nw in names or nw in _TYPO_TARGETS or len(nw) < 6:
        return w
    for t in _TYPO_TARGETS:
        if _ed1(nw, t):
            return t
    return w


_VAV_DIR_RE = re.compile(r"(?<![\w.])و([A-Za-z][A-Za-z0-9_\-]*)(?![\w.])")
_SLASH_END_RE = re.compile(r"(?<=[\w])/+(?=\s|$|[،,؛;.؟?!])")
_GLUE_L_RE = re.compile(r"(?<![\w.])ل[\u0621-\u064a]{3,}(?![\w.])")
_VAV_FILE_RE = re.compile(r"(?<![\w.])و([A-Za-z0-9_][^\s«»\"“”'،,]*\.[A-Za-z0-9]{1,6})(?![\w])")


# ---- لهجات عراقية/خليجية/شامية شائعة ← فصحى قبل التحليل (فتنتج الخطة نفسها، ويبقى مبدأ «اسأل لا تخمّن» كما هو) ----
def _ar(p: str) -> str:
    """نمط يتسامح مع بدائل الحروف العربية (همزة الألف، ي/ى، ه/ة)."""
    m = {"ا": "[اأإآ]", "ي": "[يى]", "ى": "[يى]", "ه": "[هة]", "ة": "[هة]"}
    return "".join(m.get(c, c) for c in p)


_DLB, _DRB = r"(?<![\w.\\/\-])", r"(?![\w/\\\-]|\.\w)"
_D_VERBS = {"احذف": "احذف", "امسح": "امسح", "انقل": "انقل", "انسخ": "انسخ", "رتب": "رتب", "ارتب": "رتب", "نظف": "نظف", "شيل": "شيل",
            "حط": "حط", "ضع": "ضع", "غير": "غير", "ضيف": "ضيف", "اضف": "اضف", "اعرض": "اعرض", "افحص": "افحص",
            "ابحث": "ابحث", "اعمل": "اعمل", "انشئ": "انشئ", "جهز": "جهز", "نظم": "نظم", "صنف": "صنف", "حرك": "حرك", "اجمع": "اجمع",
            "شوف": "اعرض", "وري": "اعرض", "ورني": "اعرض",
            "وزع": "رتب", "اشطب": "احذف", "امحي": "امسح", "طير": "احذف", "ارمي": "احذف", "تشيل": "شيل", "احدف": "احذف", "احزف": "احذف", "احذفف": "احذف",
            "اححذف": "احذف", "امسخ": "امسح", "انسح": "انسخ", "انصخ": "انسخ", "انقن": "انقل", "رتبب": "رتب", "غيير": "غير", "اعرد": "اعرض",
            "اكريت": "انشئ", "انشي": "انشئ", "انشيء": "انشئ", "انشا": "انشئ", "اظهر": "اعرض", "استعرض": "اعرض", "اشوف": "اعرض"}
_D_VERB_RE = re.compile(_DLB + r"(?P<p>[وف])?(?P<v>" + "|".join(sorted((_ar(v) for v in _D_VERBS), key=len, reverse=True)) + r")(?:لي|ي|ني|نى)?" + _DRB)
_D_NOUN_RES = (
    (re.compile(_DLB + r"(?P<p>(?:[وفبل]|وال|بال|فال|لل)?(?:ال)?)فا[يى]ل(?P<s>ات|ين|ان|اتي)?" + _DRB), lambda m: m.group("p") + "ملف" + (m.group("s") or "")),
    (re.compile(_DLB + r"(?P<p>(?:[وفبل]|وال|بال|فال|لل)?(?:ال)?)ف[وؤ]?لدر(?P<s>ات|ين|ان)?" + _DRB), lambda m: m.group("p") + "مجلد" + (m.group("s") or "")),
)
_D_SIMPLE = (   # (نمط، بديل): كلمات أو عبارات لهجية بمعادل فصيح مباشر
    (re.compile(_DLB + _ar(r"(?:[وف])?مالت(?:ي|نا|ك|ه|ها)|مالنا") + _DRB), " "),            # «الصور مالتي» = «الصور»
    (re.compile(_DLB + r"(?P<p>[وف])?كل\s*ش[يى]" + _DRB), lambda m: (m.group("p") or "") + "كل شيء"),
    (re.compile(_DLB + r"(?P<p>[وف])?فيه\s+(?:ايه|إيه|شنو|شو)\s+(?:(?:في|ب)\s+){1,2}(?=\S)"), lambda m: (m.group("p") or "") + "ماذا في "),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:شنو|شو|ايش|إيش|وش|ويش|شكو|ايه|إيه|ما\s+الذي|ماذا)\s+(?:(?:ال)?(?:ملفات|مجلدات)\s+)?(?:(?:اللي|الي|الذي|التي)\s+)?(?:(?:في|ب)\s+){1,2}(?=\S)"),
     lambda m: (m.group("p") or "") + "ماذا في "),
    (re.compile(_DLB + r"(?P<p>[وف])?عندي\s+كام\s+(?:ملف|مجلد)" + _DRB), lambda m: (m.group("p") or "") + "ماذا يوجد"),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:كم|كام)\s+(?:عدد\s+)?(?:ال)?(?:ملف\w*|مجلد\w*)(?:\s+عندي)?" + _DRB), lambda m: (m.group("p") or "") + "ماذا يوجد"),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:اطلعني|طلعني)\s+(?:على|علي)\s+(?=\S)"), lambda m: (m.group("p") or "") + "اعرض "),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:لاقي|لاقيلي|لاقي\s+لي)\s+(?=\S)"), lambda m: (m.group("p") or "") + "ابحث عن "),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:شنو|شو|وش|ويش|ايش|ايه|إيه|ما\s+هي|ماهي)\s+(?:(?:ال)?(?:ملفات|مجلدات)\s+)(?:(?:اللي|الي|الذي|التي)\s+)?(?:ال)?(?:موجود\w*|عندي|هنا|يوجد)" + _DRB),
     lambda m: (m.group("p") or "") + "ماذا يوجد"),
    (re.compile(_DLB + r"(?:(?:ال)?ملف\s+)?(?:(?:اللي|الذي|الي|التي)\s+)?(?:اسمه|إسمه|المسمى|مسمى)\s+(?=[^\s«»\"]+\.[A-Za-z0-9]{1,6}(?![\w]))"), ""),
    (re.compile(_DLB + r"(?:ال)?موجود[ةه]?\s+(?:هنا|بالمجلد|عندي)" + _DRB), ""),
    (re.compile(_DLB + r"خل[يى]\s+(?=ت(?:مسح|شيل|حذف|نقل|نسخ)\w*)"), ""),
    (re.compile(_DLB + r"(?:نحو|باتجاه|بإتجاه|بأتجاه)(?=\s)"), "الى"),
    (re.compile(_DLB + r"(?:عالفولدر|عالمجلد|عالفلدر)(?=\s)"), "الى المجلد"),
    (re.compile(_DLB + r"(?:مجذ|مجلذ|مجل)(?=\s)"), "مجلد"),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:شنو|شو|وش|ويش|ايش|ايه|إيه|شكو)\s+(?:(?:ال)?(?:ملفات|مجلدات)\s+)?(?:(?:اللي|الي)\s+)?(?:ال)?(?:موجود\w*|عندي|هنا|يوجد|بالمجلد|بمجلد\w*|بالفولدر|فيه)" + _DRB),
     lambda m: (m.group("p") or "") + "ماذا يوجد"),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:شنو|شو|ايش|إيش|وش|ويش|شكو)\s+(?:(?:ال)?(?:ملفات|مجلدات)\s+)?(?:ال)?(?:موجود\w*|عندي|هنا|يوجد|بالمجلد|بمجلد\w*|بالفولدر|فيه)" + _DRB),
     lambda m: (m.group("p") or "") + "ماذا يوجد"),
    (re.compile(_DLB + r"شكو(?:\s+(?:هنا|بالمجلد|بمجلد\w*|عندي))?" + _DRB), "ماذا يوجد"),
    (re.compile(_DLB + r"(?P<p>[وف])?" + _ar("اكو") + r"\s+(?=\S)"), lambda m: (m.group("p") or "") + "هل يوجد "),
    (re.compile(_DLB + r"(?P<p>[وف])?وين\s+(?=\S)"), lambda m: (m.group("p") or "") + "ابحث عن "),
    (re.compile(_DLB + r"(?P<p>[وف])?دور(?:لي|ي)?\s+(?:على|عن|ع)\s+"), lambda m: (m.group("p") or "") + "ابحث عن "),
    (re.compile(_DLB + r"(?:جوا|جوه|جوّا|بجوه|بداخل)(?=\s)"), "داخل"),
    (re.compile(_DLB + r"(?P<p>[وف])?سم[يى]?\s+(?:(?:ال)?ملف\s+)?(?P<x>[^\s«»\"]+)\s+(?:(?:باسم|بإسم|الى|إلى|الي|ب)(?:\s+|$)|(?=[^\s«»\"]+\.[A-Za-z0-9]{1,6}(?![\w])))"), lambda m: (m.group("p") or "") + "غير اسم " + m.group("x") + " الى "),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:عدل|تغيير|تغير|اغير|تغيّر)(?:لي)?\s+(?=(?:ال)?اسم)"), lambda m: (m.group("p") or "") + "غير "),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:غير|غر|غيير)\s+اصم" + _DRB), lambda m: (m.group("p") or "") + "غير اسم"),
    (re.compile(_DLB + r"change\s+name\s+of\s+"), "غير اسم "),
    (re.compile(_DLB + r"(?P<f>[^\s«»\"'،,؛;]+\.[A-Za-z0-9]{1,6})\s+عن\s+(?=\S)"), lambda m: m.group("f") + " من "),   # «شيل a.txt عن Docs» = من
    (re.compile(_DLB + r"(?P<n>(?:ال)?(?:ملف|اغنيه|اغنية|صوره|صورة|فيديو|مستند)(?:\s+(?:ال)?مؤقت[ةه]?)?)\s+(?P<f>[^\s«»\"]+\.[A-Za-z0-9]{1,6})(?![\w])"),
     lambda m: m.group("f")),   # (guard في _dialect يترك الاسم الموجود كاملاً مثل «صورة العائلة.jpg»)
    (re.compile(_DLB + r"(?:اللي|الذي|التي)\s+(?:ب|في|داخل)\s+(?=\S)"), "في "),
    (re.compile(_DLB + r"(?:اللي|الذي|التي)\s+(?=(?:في|ب|داخل)\s)"), ""),
    (re.compile(_DLB + r"(?P<m>ملفات\s+)(?:ال)?نوع\s+(?=[A-Za-z0-9]{2,5}(?![\w.]))"), lambda m: m.group("m")),
    (re.compile(_DLB + r"(?:غر|غيّر|غيير)\s+(?=اسم)"), "غير "),
    (re.compile(_DLB + r"احدث\s+(?=[A-Za-z0-9]+\.[A-Za-z0-9.]+)"), "حدث "),
    (re.compile(_DLB + r"ضب(?=\s+(?:ال)?ملفات)"), "رتب"),
    (re.compile(_DLB + r"على\s+(?=(?:ال)?(?:مجلدات|فولدرات)\s+حسب)"), ""),
    (re.compile(_DLB + r"للمجلد(?=\s)"), "الى المجلد"),
    (re.compile(_DLB + r"لمجلد(?=\s)"), "الى مجلد"),
    (re.compile(_DLB + r"بدل(?:لي|ي)?\s+(?=" + _ar("اسم") + r")"), "غير "),
    (re.compile(_DLB + r"زيد(?:لي|ي)?\s+(?=(?:ال)?" + _ar("بادئه") + r")"), "زد "),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:[اتن]سو[يى])(?:\s*لي)?\s+"), lambda m: (m.group("p") or "") + "اعمل "),
    (re.compile(_DLB + r"(?P<p>[وف])?(?:ت[كگ]در(?:ون)?|تقدر(?:ون)?)(?=\s)"), lambda m: (m.group("p") or "") + "ممكن"),
)
_D_LEAD = re.compile(r"^(?P<pre>\s*(?:(?:اريد|أريد|ابي|ابغي|ابغى|ممكن|اني|آني|انا)\s+)*)(?P<w>س[ً-ْ]*و[ً-ْ]*[يى][ً-ْ]*|خ[ً-ْ]*ل[ً-ْ]*[يى][ً-ْ]*)(?P<l>\s*لي|لي)?(?P<sp>\s+)")
_D_MAKE_OBJ = re.compile(r"(?:(?:ثلاث|ثلاثه|اثنين|اربع|اربعه|خمس|خمسه|\d+)\s+)?(?:ال)?(?:مجلد|فولدر|فلدر|folder|نسخ|مشروع|backup|هيكل)", re.I)


def _dialect_lead(prompt: str, names: set) -> str:
    """«سوي مجلد X» / «خلي X في Y» في أول الطلب فقط (وسط الجملة «سوى» = ما عدا، فلا نمسّها): سوي ← اعمل، خلي ← ضع إن وُجدت وجهة."""
    m = _D_LEAD.match(prompt)
    if not m or norm(m.group("w")) in names:
        return prompt
    rest = prompt[m.end():]
    w = norm(m.group("w"))
    if w == "سوي":
        if not _D_MAKE_OBJ.match(norm(rest)):
            return prompt
        new = "اعمل"
    else:
        masked = _QUOTE_RE.sub(lambda q: "§" * len(q.group(0)), rest)
        if not (_DEST_TO.search(masked) or _DEST_IN.search(masked)):
            return prompt   # «خلي X» بلا وجهة: قد تعني «أبقِ X» (استثناء) فتبقى كلمة نفي تُسأل
        new = "ضع"
    return m.group("pre") + new + m.group("sp") + rest


def _dialect(seg: str, names: set) -> str:
    """يبدّل كلمات اللهجة بمعادلها الفصيح في جزء غير مقتبس؛ ما كان اسماً موجوداً في مجلد العمل لا يُمسّ."""
    def guard(fn):
        def inner(m):
            t = norm(m.group(0)).strip()
            forms = {t, t[1:] if t[:1] in "وفبل" else t}
            forms |= {f[2:] for f in forms if f.startswith("ال")}
            return m.group(0) if forms & names else fn(m)
        return inner

    def verb(m):
        return (m.group("p") or "") + _D_VERBS[norm(m.group("v"))]
    seg = _D_VERB_RE.sub(guard(verb), seg)
    for rx, rep in _D_NOUN_RES:
        seg = rx.sub(guard(rep), seg)
    for rx, rep in _D_SIMPLE:
        seg = rx.sub(guard(rep if callable(rep) else (lambda m, r=rep: r)), seg)
    return seg


_PH_PRE = r"^(?P<pre>\s*(?:(?:اريد|أريد|ابي|ابغي|ابغى|ابغا|عايز|عاوز|بدي|ممكن|حابب|ابا|i\s+want(?:\s+to)?)\s+)*)"
_PH_BK = re.compile(_PH_PRE + r"(?:(?P<v>اعمل|اعملي|عمول|سوي|خذ|خد|احفظ|احتفظ\s+ب|هات|take|make|create|do|save|انشئ|أنشئ|انشاء|إنشاء|احتفظ)(?:\s+لي)?\s+)?"
                    r"(?P<n>(?:ب)?(?:(?:ال)?نسخ[ةه]\s+(?:ال)?احتياطي[ةه]|(?:ال)?نسخ[ةه]|باك\s*[أا]ب|باكاب|(?:a\s+)?backup(?:\s+copy)?))"
                    r"(?:\s+(?:of|من|عن|ل)|\s+الى(?=\s+(?:ال)?مجلد\s))?\s+(?P<rest>\S.*)$", re.I)
_PH_DEST = re.compile(r"(?<!\w)(?:الى|إلى|الي|إلي|في|داخل|to|into|inside|in)(?!\w)", re.I)
_PH_PLAIN_VERBS = {"خذ", "خد", "احفظ", "احتفظ", "هات", "take", "save", "عمول"}
_PH_FILELIKE = r"[^\s«»\"]+\.[A-Za-z0-9]{1,6}"
_PH_MOVEV = re.compile(_PH_PRE + r"(?P<v>ودي|وديه|ودّي|زيح|زيحه|نقلي|هات)\s+(?P<rest>\S.*)$")
_PH_REORDER = re.compile(r"^(?P<v>ودي|وديه|انقل|انسخ|حط|ضع|حرك|move|copy|put)\s+(?P<m>الى|إلى|الي|في|to|into)\s+(?P<d>[^\s«»\"]+)\s+(?P<f>" + _PH_FILELIKE + r")$", re.I)
_PH_REORDER2 = re.compile(r"^(?P<m>الى|إلى|الي|في|to|into)\s+(?P<d>[^\s«»\"]+)\s+(?P<v>انقل|انسخ|حط|ضع|حرك|move|copy|put|ودي|وديه)\s+(?P<f>" + _PH_FILELIKE + r")$", re.I)
_PH_MULTI = re.compile(r"^(?:انشئ|اعمل|جهز)\s+(?:(?P<cnt>ثلاث|ثلاثه|اثنين|اربع|اربعه|خمس|خمسه|\d+)\s+)?(?:ال)?مجلدات\s+(?P<names>\S.*)$")
_COUNT_WORDS = {"اثنين": 2, "ثلاث": 3, "ثلاثه": 3, "اربع": 4, "اربعه": 4, "خمس": 5, "خمسه": 5}
_PH_CREATE = re.compile(_PH_PRE + r"(?:(?P<v>عمول|اكريت|ضيف|اضف|ضف|تعمللي|اعمللي|add|new)\s+)?(?P<obj>(?:ال)?(?:مجلد|مشروع)ا?|folder)\s+(?P<rest>\S.*)$", re.I)
_PH_CREATE3 = re.compile(r"^(?:ال)?مجلد\s+(?:جديد[اة]?\s+)?(?:اسمه|باسم|بإسم|يسمى|بعنوان)\s+\S.*$")
_PH_CREATE2 = re.compile(_PH_PRE + r"(?:ت)?(?:عمل|اعمل)\s*لي\s+(?P<obj>(?:ال)?(?:مجلد|مشروع)|folder)\s+(?P<rest>\S.*)$", re.I)
_PH_WANT = re.compile(r"(?:اريد|أريد|ابي|ابغي|ابغى|ابغا|عايز|عاوز|بدي|حابب|ابا)\s")
_PH_SHELL = re.compile(r"^(?P<v>cp|mv|ren|rename|mkdir|ls|dir|check)(?:\s+(?P<rest>\S.*))?$", re.I)
_PH_ASK_FILE = re.compile(r"^(?:هل\s+|هو\s+)?(?P<f>" + _PH_FILELIKE + r")\s+موجود[ةه]?\s*[؟?]?$")
_PH_HAVE_FILE = re.compile(r"^(?:هل\s+)?عندي\s+(?:ملف\s+)?(?P<f>" + _PH_FILELIKE + r")\s*[؟?]?$")
_PH_DEST_PREP = re.compile(r"(?<![\w.])(?:عند|ل|ب)\s+(?P<n>[^\s،,؛;]+)")
_PH_MOVECOPY = re.compile(r"(?<![\w.])(?:انقل|انسخ|ضع|حط|حرك|اجمع|غير\s+اسم|سمي|copy|move|put|rename)(?![\w])", re.I)


def _dialect_phrases(t: str, names: set) -> str:
    """صيغ لهجية/مختصرة تتجاوز كلمة واحدة (نسخة احتياطية، ودّي، cp/mv، فولدر جديد…) ← صيغة قانونية، على طلب بلا اقتباس يخصّها.
    لا تمسّ ما كان اسماً موجوداً؛ وما لا يطابق بدقة يبقى كما هو فيُسأل المستخدم."""
    if "«" in t or "\"" in t:
        return t
    st = t.strip()
    m = _PH_SHELL.match(st)
    if m and norm(m.group(0)) not in names:
        v, rest = m.group("v").lower(), (m.group("rest") or "").strip()
        toks = rest.split()
        if v in ("ls", "dir"):
            return "اعرض " + (rest if rest else "الملفات")
        if v == "check":
            return "افحص " + rest if rest else t
        if v == "mkdir":
            return "انشئ مجلد " + rest if rest else t
        if v in ("ren", "rename") and len(toks) == 2 and re.fullmatch(_PH_FILELIKE, toks[1]) and re.fullmatch(_PH_FILELIKE, toks[0]):
            return f"rename {toks[0]} to {toks[1]}"
        if v in ("cp", "mv"):
            w = "copy" if v == "cp" else "move"
            if len(toks) == 2 and not re.search(r"\.[A-Za-z0-9]{1,6}$", toks[1]) and not _PH_DEST.search(rest):
                return f"{w} {toks[0]} to {toks[1]}"
            return f"{w} {rest}" if rest else t
        return t
    if re.fullmatch(r"(?:منو|مين)(?:\s+(?:انا|اني|آني))?|(?:انا|اني|آني)\s+(?:مين|منو)", norm(st)):
        return "من انا"
    m = _PH_ASK_FILE.match(st) or _PH_HAVE_FILE.match(st)
    if m:
        return "ابحث عن " + m.group("f")
    m = _PH_BK.match(st)
    if m:
        n, v = norm(m.group("n")), norm(m.group("v") or "")
        bk_like = "احتياطي" in n or "باك" in n or "backup" in n
        rest = re.sub(r"^the\s+", "", m.group("rest").strip(), flags=re.I)
        mm = re.match(r"^(?:ال)?مجلد\s+(\S+)$", rest)
        if mm and norm(mm.group(1)) in names:
            rest = mm.group(1)
        rest = re.sub(r"\s+(?:folder|مجلد)$", "", rest, flags=re.I).strip()
        has_dest = bool(_PH_DEST.search(rest))
        if rest and (bk_like or v in {norm(x) for x in _PH_PLAIN_VERBS}) and norm(st) not in names and not re.match(r"^(?:ال)?(?:ملفات|كل|جميع|all)\b", rest):
            if has_dest:
                return m.group("pre") + "انسخ " + rest if not bk_like else t
            if not v or bk_like or v in {norm(x) for x in _PH_PLAIN_VERBS}:
                return m.group("pre") + "اعمل نسخة احتياطية من " + rest
        return t
    m = _PH_REORDER.match(st) or _PH_REORDER2.match(st)
    if m and not re.search(_PH_FILELIKE + "$", m.group("d")):
        v = norm(m.group("v"))
        verb = "انقل" if v in ("ودي", "وديه", "move") else "انسخ" if v in ("copy",) else v
        return f"{verb} {m.group('f')} {m.group('m')} {m.group('d')}"
    m = _PH_MULTI.match(norm(st)) and _PH_MULTI.match(st)
    if m:
        parts = [x for x in re.split(r"\s+و\s*|\s*[،,]\s*|\s+and\s+", m.group("names").strip()) if x]
        want = m.group("cnt")
        if len(parts) >= 2 and all(re.fullmatch(r"[\w\-؀-ۿ]+", x) for x in parts) and (
                not want or (_COUNT_WORDS.get(norm(want)) or (int(want) if want.isdigit() else -1)) == len(parts)):
            return "انشئ " + " و".join("مجلد " + x for x in parts)
    m = _PH_MOVEV.match(st)
    if m and (_PH_DEST.search(m.group("rest")) or _PH_DEST_PREP.search(m.group("rest")) or re.search(r"(?<!\w)(?:الى|إلى)", m.group("rest"))):
        return m.group("pre") + "انقل " + m.group("rest")
    if _PH_CREATE3.match(st):
        return "انشئ " + st
    m = _PH_CREATE2.match(st)
    if m:
        return "انشئ " + m.group("obj") + " " + m.group("rest")
    m = _PH_CREATE.match(st)
    if m:
        v = (m.groupdict().get("v") or "")
        if v or _PH_WANT.search(m.group("pre") + " "):
            return "انشئ " + m.group("obj") + " " + m.group("rest")
    return t


def _outside_quotes(text: str, fn) -> str:
    """يطبّق fn على الأجزاء غير المقتبسة فقط (المقتبس حرفي يُبقى كما كتبه المستخدم)."""
    out, pos = [], 0
    for m in _QUOTE_RE.finditer(text):
        out.append(fn(text[pos:m.start()]))
        g = m.group(0)
        out.append(f"«{m.group(1)}»" if g[0] in "'‘" else g)   # الاقتباس المفرد يُوحَّد إلى «» فيعامل كالمزدوج في كل مكان
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


def glue_l(m, names: set) -> str:
    """«للمشاريع» / «لمشاريع» ← «الى المشاريع» حين يكون ما بعد اللام اسماً موجوداً فعلاً (لا نفصل لاماً من كلمة عادية)."""
    t = m.group(0)
    if norm(t) in names:
        return t
    cands = ["ال" + t[2:]] if t.startswith("لل") else []
    cands.append(t[1:])
    for c in cands:
        if norm(c) in names:
            return "الى " + c
    return t


def _clean_prompt(prompt: str, names: set) -> str:
    """تطبيع واحد للضجيج الشكلي قبل أي تحليل: التشكيل والتطويل، عبارات المجاملة، «قم بـ»، واو الوصل الملتصقة باسم ملف.
    الأجزاء المقتبسة والأسماء الموجودة فعلاً (names: مطبَّعة) لا تُمسّ، فلا يختفي مجلد اسمه «لي» أو «رجاء» مثلاً."""
    def polite(m):
        w = norm(m.group(0)).split()
        if len(w) == 1 and w[0] in names or w[-1] in names:
            return m.group(0)
        return " "

    def vav(m):
        return m.group(0) if norm(m.group(0)) in names else "و " + m.group(1)

    def arrow2(m):
        t = m.group(0)
        if norm(t) in names:
            return t
        return re.sub(r"\s*(?:-+>|=+>|[→⟶⇒➜➔])\s*", " إلى ", t)

    def glue(m):
        # «المجلدBackup» / «إلىB» / «انسخWork»: كلمة وظيفية ملتصقة باسم لاتيني ← تُفصل (ما لم يكن الرمز كله اسماً موجوداً)
        r = m.group("r")
        if norm(m.group(0)) in names or (m.group("w").isascii() and not (r[0].isupper() or r[0].isdigit())):
            return m.group(0)   # «copyy»/«too»: خطأ إملائي لا كلمة ملتصقة باسم (الاسم الملتصق يبدأ بحرف كبير أو رقم)
        return m.group("w") + " " + r

    def fix(seg):
        seg = _MARK_OR_FILE.sub(lambda m: m.group(1) or "", seg)   # التشكيل يُحذف إلا داخل ما يشبه اسم ملف (الاسم الجديد يبقى كما كُتب)
        seg = _ARROW_RE.sub(arrow2, seg)
        seg = _GLUE_RE.sub(glue, seg)
        seg = _POLITE_RE.sub(polite, seg)
        seg = _QAM_RE.sub(" ", seg)
        seg = _VAV_FILE_RE.sub(vav, seg)
        seg = _VAV_DIR_RE.sub(lambda m: "و " + m.group(1) if norm(m.group(1)) in names and norm(m.group(0)) not in names else m.group(0), seg)
        seg = _SLASH_END_RE.sub("", seg)
        seg = _AL_EXT_RE.sub(lambda m: m.group(1) if m.group(1).lower() in _KNOWN_EXTS and norm(m.group(0)) not in names else m.group(0), seg)
        seg = _TYPO_RE.sub(lambda m: _typo_fix(m, names), seg)
        seg = _GLUE_L_RE.sub(lambda m: glue_l(m, names), seg)
        return _dialect(seg, names)
    out = _outside_quotes(_dialect_lead(prompt, names), fix).strip()
    out = _dialect_phrases(out, names)
    mf = re.search(r"(?<![\w.])([^\s«»\"]+)\s+(?:folder|directory)\s*[.!؟?]*$", out, re.I)
    if mf and norm(mf.group(1)) in names:
        out = out[:mf.start()] + mf.group(1)
    if _PH_MOVECOPY.search(out) and "«" not in out:
        def dest_prep(m):
            n = m.group("n").rstrip("/")
            return "الى " + m.group("n") if (norm(n) in names or re.match(r"[A-Za-z0-9]", n)) else m.group(0)
        out = _PH_DEST_PREP.sub(dest_prep, out)
    return out


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


def _bad_component_n(name: str) -> bool:
    return _bad_component(name) or _bad_component(name.translate(_DIGITS))


def _safe_name(name: str) -> Optional[str]:
    """اسم عنصر صالح أو None. لا نحذف النقاط من أول الاسم أو آخره بصمت (فيتحول «.config» إلى config)؛ يُرفض فيُسأل المستخدم."""
    name = name.strip().strip("«»\"'،,؛:").strip()
    if not name or name in (".", "..") or name.startswith(".") or re.search(r'[\\/:*?"<>|]', name) or _bad_component_n(name):
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
# «اعمل/خذ نسخة احتياطية» فعل نسخ احتياطي (وليس اسم مجلد وجهة): مع أي عملية أخرى في الطلب نفسه يُسأل المستخدم بدل إسقاط إحداهما بصمت
_BACKUP_VERB = re.compile(r"(?<!\w)[وف]?(?:اعمل|اعملي|خذ|اصنع|سوي|انشئ|جهز|اريد|create|make|take|do)\s+(?:لي\s+)?(?:(?:ال)?نسخه\s+احتياطيه|نسخ\s+احتياطي|backups?)(?!\w)")
W_BACKUP = re.compile(r"(?<!\w)(?:نسخه\s+احتياطيه|نسخ\s+احتياطي|backup)(?!\w)")   # اسم/وجهة لا فعل نسخ
W_RENAME = _words("اعد تسميه", "غير اسم", "تسميه", "rename")
W_ADD_PREFIX = re.compile(r"(?<!\w)[وف]?(?:اضف|ضيف|اضافه|زد|add)\s+(?:ال)?(?:بادئه|prefix|تاريخ|date)(?!\w)")
W_PREFIX = _words("بادئه", "prefix", "بدايه", "اضافه", prefix="(?:ب)?(?:ال)?")
W_ORGANIZE = _words("رتب", "رتبها", "رتبهم", "ترتب", "ترتبها", "ترتبهم", "ترتيب", "نظم", "نظمها", "نظمهم", "تنظيم", "صنف", "فرز", "organize", "sort")
W_CREATE = _words("انشئ", "انشاء", "اعمل", "جهز", "create", "make")
W_CREATE_OBJ = re.compile(r"(?<!\w)[وف]?(?:ال)?(?:مجلد|مشروع|هيكل|folder|project)ا?(?!\w)")
W_INSPECT = _words("افحص", "اعرض", "فحص", "تحقق", "inspect", "ماذا يوجد", "ماذا في", "ما محتوي", "ما محتويات", "show", "list",
                   "what is in", "whats in", "what's in", "ابحث عن", "ابحث", "بحث عن", "هل توجد", "هل يوجد", "هل عندي", "find", "search")
W_UPDATE = _words("حدث", "تحديث", "ترقيه", "winget", "upgrade", "update")
W_WHOAMI = re.compile(r"(?<!\w)[وف]?(?:whoami|من انا|اسم المستخدم|(?:ال)?هويه)(?!\w)")
W_DUP = re.compile(r"(?<!\w)[وف]?(?:ال)?(?:مكرر\w*|متكرر\w*|تكرار|duplicates?)(?!\w)")
_DUP_COPY_PHRASE = re.compile(r"(?<!\w)نسخ(?:ه|ات)?\s+(?:ال)?مكرر\w*")
W_THEN = _words("ثم", "بعدين", "بعدها", "بعد ذلك", "then", "afterwards", "and then")
W_NEG = _words("عدا", "ماعدا", "الا", "غير", "ليس", "ليست", "ليسوا", "لا", "بدون", "دون", "باستثناء", "استثناء", "سوي", "خلا",
               "لكن", "ولكن", "احتفظ", "ابق", "ابقي", "اترك", "keep", "besides", "other than", "apart from", "except", "not",
               "dont", "don't", "without", "excluding", "exclude", "but", "only", "just", "خلي", "خليه", "خليها", "خليهم", "مو", "مب", "موب", "مش", "بس",
               prefix="[و]?")
_QUAL_WORDS = ("قديم", "كبير", "صغير", "جديد", "اكبر", "اصغر", "اقدم", "احدث", "تنتهي", "ينتهي", "تبدا", "يبدا", "تحتوي",
               "يحتوي", "تتجاوز", "اكثر", "اقل", "فارغ", "قبل", "بعد", "older", "newer", "larger", "smaller", "bigger",
               "empty", "oldest", "newest", "biggest", "smallest")
W_QUAL = re.compile(r"(?<!\w)(?:ال)?(?:" + "|".join(_QUAL_WORDS) + r")\w*(?!\w)")
W_ALL = _words("الكل", "كل شيء", "كل شي", "جميع", "كافه", "everything", "all", prefix="")
W_CONTENTS = _words("محتويات", "محتوي", "contents", prefix="(?:ال)?")
_COPY_OF_RE = re.compile(r"(?<!\w)(?:ال)?نسخ[ةه](?!\w)|(?<!\w)cop(?:y|ies)\s+of(?!\w)", re.I)
_DEST_TO = re.compile(r"(?<!\w)(?:[إأآا]ل[ىي]|to|into)(?!\w)", re.I)
_DEST_IN = re.compile(r"(?<!\w)(?:ف[يى]|داخل|in|inside)(?!\w)", re.I)
_SCOPE_RE = re.compile(r"(?<!\w)(?:من|ف[يى]|داخل|from|in|inside)\s+(?:(?:ال)?مجلد\s+|(?:the\s+)?folder\s+)?")
_PKG_RE = re.compile(r"(?<![\w./\\-])[A-Za-z0-9][A-Za-z0-9+_-]*\.[A-Za-z0-9+_-]*[A-Za-z][A-Za-z0-9+_.-]*(?![\w+])")
_FILELIKE = re.compile(r"(?<![\w§])[^\s§]*[\w؀-ۿ]\.[a-z0-9]{1,6}(?![\w])")
_PATHLIKE = re.compile(r"[^\s§]*[/\\][^\s§]*")
_FILELIKE_I = re.compile(_FILELIKE.pattern, re.I)
_MARK_OR_FILE = re.compile("(" + _FILELIKE.pattern + ")|" + _DIACRITICS.pattern, re.I)


_PREFIX_RES = (
    re.compile(r"(?:بادئه|بادئة|prefix)\s*[:：]?\s*[«\"“](.+?)[»\"”]"),
    re.compile(r"(?:بادئه|بادئة|prefix)\s*[:：]?\s*([^\s«»\"'،,]+)"),
    re.compile(r"(?:بإضافة|باضافة|بإضافه|اضافة|إضافة)\s+[«\"']?([^\s«»\"'،,]+)[»\"']?\s+(?:في|الى|إلى|ل)?\s*(?:بداية|اول|أول)"),
)


_STRIP_AFFIX_RE = re.compile(
    r"(?<!\w)(?:ب|و|ف)?(?:ال)?(?:احذف|حذف|ازاله|ازال|ازل|ازيل|شيل|امسح|مسح|نزع|انزع|remove|removing|delete|deleting|strip|drop)\s+"
    r"(?:(?:the|ال)\s*)?(?:ال)?(بادئه|لاحقه|prefix|suffix)(?!\w)")
_AFFIX_STOP = {"من", "في", "عن", "for", "from", "of", "the", "كل", "جميع", "الملفات", "ملفات", "اسماء", "الاسماء", "اسم", "files", "file",
               "ملف", "all", "to", "الي", "الى", "داخل", "in", "ثم", "و", "ب", "ل", "مجلد", "المجلد", "folder"}


def _strip_affix_parse(prompt: str):
    """«احذف/أزل البادئة old_» / «بإزالة اللاحقة _bak»: يعيد (النوع prefix|suffix، النص أو None، بداية العبارة، نهايتها) أو None."""
    n = norm(_QUOTE_RE.sub(lambda m: " " * len(m.group(0)), prompt))
    sm = _STRIP_AFFIX_RE.search(n)
    if not sm:
        return None
    kind = "suffix" if sm.group(1) in ("لاحقه", "suffix") else "prefix"
    am = re.match(r"\s*[:：]?\s*(?:[«\"“'‘](.+?)[»\"”'’]|([^\s«»\"'،,]+))", prompt[sm.end():])
    if not am:
        return kind, None, sm.start(), sm.end()
    if am.group(1) is not None:
        return kind, am.group(1), sm.start(), sm.end() + am.end()
    tok = re.sub(r"[!؟?]+$", "", am.group(2))
    if not tok or norm(tok) in _AFFIX_STOP:
        return kind, None, sm.start(), sm.end()
    return kind, tok, sm.start(), sm.end() + am.end()


_PREFIX_NOT_VALUE = {norm(w) for w in (
    "لملفات", "للملفات", "ملفات", "الملفات", "لكل", "كل", "لجميع", "جميع", "على", "علي", "الى", "إلى", "الي", "في", "ل", "لـ", "ب", "بـ", "من", "عن", "مع", "و",
    "لاسماء", "لأسماء", "للاسماء", "للأسماء", "اسماء", "أسماء", "لملف", "لمجلد", "للمجلد", "مجلد", "المجلد", "جديدة", "جديده", "جديد", "الجديدة", "لهذه", "لهذا",
    "to", "for", "all", "files", "file", "on", "in", "of", "with", "the", "every", "each", "names", "filenames")}


def _prefix_match(prompt: str):
    """مطابقة عبارة البادئة في الطلب (المجموعة 1 = نص البادئة) أو None. كلمة وظيفية بعد «بادئة» (لملفات/على/to…) ليست قيمة بادئة: تُسأل."""
    for i, rx in enumerate(_PREFIX_RES):
        m = rx.search(prompt)
        if m and not (i == 1 and norm(m.group(1)) in _PREFIX_NOT_VALUE):
            return m
    return None


def _prefix_text(m) -> str:
    """نص البادئة من المطابقة: غير المنصَّصة تفقد علامات ! ؟ ? الطرفية (علامة الجملة لا من الاسم)؛ والمنصَّصة حرفية."""
    t = m.group(1)
    before = m.string[max(0, m.start(1) - 1):m.start(1)]
    return t if before in ("«", '"', "“", "'", "‘") else re.sub(r"[!؟?]+$", "", t)


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


_EN_FUNCTION_WORDS = {"a", "an", "the", "of", "to", "in", "on", "and", "or", "for", "my", "all", "me", "it", "is"}
_TEMP_EXTS = {"tmp", "temp", "crdownload"}   # .bak/.part قد تكون نسخاً احتياطية يدوية أو تنزيلات ناقصة مهمة: لا تُعدّ مؤقتة


def _is_temp(base: str) -> bool:
    b = base.lower()
    return b.startswith("~$") or (("." in b) and b.rsplit(".", 1)[-1] in _TEMP_EXTS)


# فئات بالمعنى لا بالامتداد وحده: (اسم، كلمات مطبَّعة، دالة اختيار على اسم الملف)
_KW_GROUPS = (
    ("المؤقتة", ("مؤقت", "مؤقته", "مؤقتات", "temp", "temporary", "تمب"), _is_temp),
    ("الفواتير", ("فواتير", "invoice", "invoices"), lambda b: any(k in norm(b) for k in ("فاتور", "فواتير", "invoice"))),
    ("الإيصالات", ("ايصالات", "receipt", "receipts"), lambda b: any(k in norm(b) for k in ("ايصال", "receipt"))),
    ("الإكسل", ("اكسل", "excel"), lambda b: b.lower().rsplit(".", 1)[-1] in ("xls", "xlsx", "xlsm") and "." in b),
    ("الوورد", ("وورد", "word"), lambda b: b.lower().rsplit(".", 1)[-1] in ("doc", "docx", "docm") and "." in b),
)
_KW_RES = [(name, re.compile(r"(?<![\w.])(?:و)?(?:ال)?(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w])"), pred)
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


_DATE_MIN, _DATE_MAX = datetime.date(1601, 1, 1), datetime.date(9999, 12, 31)


def _mtime_date(ts: float) -> Tuple[datetime.date, bool]:
    """تاريخ آخر تعديل من الطابع الزمني دون أن يفشل: قبل 1970 أو خارج النطاق (ويندوز يرمي OSError لـ fromtimestamp) يُحسب من
    1970-01-01 + فرق، ويُقيَّد بين 1601 و9999؛ NaN/لانهاية ← 1970-01-01. الثاني في الناتج: هل قُيِّد/عُدِّل التاريخ (غير اعتيادي)؟"""
    try:
        if ts != ts or ts in (float("inf"), float("-inf")):
            return datetime.date(1970, 1, 1), True
        d = datetime.date.fromtimestamp(ts)
        return d, d < datetime.date(1970, 1, 1)
    except (OverflowError, OSError, ValueError):
        pass
    try:
        d = (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=ts)).date()
    except (OverflowError, ValueError):
        d = _DATE_MAX if ts > 0 else _DATE_MIN
    return min(max(d, _DATE_MIN), _DATE_MAX), True


_ALL_CAT_EXTS = {e for exts in CATEGORIES.values() for e in exts}


def _not_found(tok: str, ents: "Ents", extra: str = "") -> str:
    """«لم أجد X»؛ وإن كانت قائمة مجلد العمل نفسها مقتطعة (أكثر من حد المسح) نقول ذلك بدل أن يظن المستخدم أن الملف غير موجود."""
    msg = f"لم أجد «{tok}» في مجلد العمل. اكتب اسمه كما يظهر{extra}."
    near = _near_dir(tok, [r.split("/")[-1] for r, d in ents.items() if d]) if hasattr(ents, "items") else None
    if near:
        msg += f" هل تقصد المجلد «{near}»؟"
    if getattr(ents, "partial", False):
        msg += " (قائمة مجلد العمل كبيرة ومقتطعة، فقد يكون موجوداً ولم يظهر لي.)"
    return msg


def _trunc_note(done: int, dropped: int, partial: bool = False):
    """تنبيه عند اقتطاع الخطة بسبب حد الخطوات، أو لأن قائمة مجلد العمل نفسها مقتطعة، حتى لا يظن المستخدم أن كل شيء شمله التنفيذ."""
    parts = []
    if dropped > 0:
        parts.append(f"اقتُصرت هذه الخطة على {_n(done, 'عنصر')} بسبب حد الخطوات؛ بقي {_n(dropped, 'عنصر')}. أعد الطلب بعد التنفيذ لإكمال الباقي.")
    if partial:
        parts.append(f"قائمة مجلد العمل مقتطعة، فشملت هذه الخطة الملفات الظاهرة لي فقط ({_n(done, 'عنصر')}). أعد الطلب بعد التنفيذ لإكمال ما لم يظهر.")
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
        parts.append(f"تُخطي {len(clash)} من الملفات لأن في وجهتها ملفاً بالاسم نفسه: " + "، ".join(clash[:8]) + (" …" if len(clash) > 8 else "") + ".")
    if locks:
        parts.append(f"تُركت ملفات القفل المؤقتة (~$) كما هي ({_n(len(locks), 'ملف')}) لأن برنامجاً قد يفتحها: " + "، ".join(locks[:8]) + (" …" if len(locks) > 8 else "") + ".")
    return " ".join(parts) or None


def _skip_dup_note(names: List[str]) -> Optional[str]:
    if not names:
        return None
    return (f"تُركت الملفات ذات اللاحقة «(رقم)» ({_n(len(names), 'ملف')}) لأنها قد تكون نسخاً لملفات تخصك: " + "، ".join(names[:8])
            + (" …" if len(names) > 8 else "") + ". سمِّها بأسمائها إن أردتها.")


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


_ANS_LEAD = {"لا", "بل", "نعم", "اقصد", "قصدي", "اريد", "ابي", "ابغي", "ممكن", "ثم", "و", "اكتب", "قل", "يعني", "اذن", "طيب", "خلاص"}
_ANS_VERB_RES = _ANSWER_VERBS + (W_UPDATE, W_ORGANIZE)


def _flex(name: str) -> str:
    """نمط لاسم يتسامح مع بدائل الحروف العربية (همزات، ي/ى، ه/ة) ويطابق حالة الأحرف بلا تمييز."""
    cls = {"ا": "[اأإآ]", "أ": "[اأإآ]", "إ": "[اأإآ]", "آ": "[اأإآ]", "ي": "[يى]", "ى": "[يى]", "ه": "[هة]", "ة": "[هة]"}
    return "".join(cls.get(c, re.escape(c)) for c in name)


def _answer_is_request(ans: str) -> bool:
    """جواب سؤال التوضيح الذي هو في الحقيقة طلب كامل جديد (يبدأ بفعل عملية ومعه ما يكمله): «رتّب الملفات حسب النوع»، «احذف a.txt»…"""
    n = norm(_clean_prompt(strip_format_chars(ans), set()).strip("«»\"'“”"))
    toks = [t for t in re.split(r"\s+", n) if t]
    while toks and toks[0] in _ANS_LEAD:
        toks.pop(0)
    if len(toks) < 2:
        return False
    first = toks[0].strip("«»\"'“”،,:؛")
    if "/" in first or "." in first:
        return False   # «نقل.txt» اسم ملف لا فعل
    return any(w.fullmatch(first) for w in _ANS_VERB_RES)


def merge_clarification(request: str, slot: Optional[str], answer: str) -> Optional[str]:
    """يحوّل جواباً عارياً على سؤال توضيح (اسم مجلد، اسم جديد، معيار ترتيب…) إلى جملة صريحة تُعاد تحليلها بدل لصقه نصاً حراً.
    جواب هو طلب كامل جديد (يبدأ بفعل: «احذف a.txt»، «رتّب الملفات حسب النوع») يُعاد كما هو فيُعامل طلباً جديداً لا توضيحاً (حتى بلا خانة).
    يعيد None إن لم يكن الجواب عارياً (جملة فيها فعل/علامة وجهة) أو الخانة مجهولة، فيبقى السلوك القديم (لصق الجواب بالطلب)."""
    ans = re.sub(r"\s+", " ", strip_format_chars(answer or "")).strip()
    req = strip_format_chars(request or "").strip()
    if ans and _answer_is_request(ans):
        out = ans.strip("«»\"“”").strip()
        while True:   # «بل احذف x» / «اقصد انقل y»: كلمات التمهيد لا تدخل الطلب الجديد
            w = out.split(" ", 1)
            if len(w) == 2 and norm(w[0]) in _ANS_LEAD:
                out = w[1].strip()
            else:
                return out
    if not slot or not ans or not req:
        return None
    n = norm(ans)
    if slot == "path":   # جواب «اكتب المسار الكامل»: يحلّ محل الاسم الملتبس في الطلب الأصلي
        pth = re.sub(r"^\./", "", ans.replace("\\", "/").strip("«»\"'“” ")).strip("/")
        if "/" not in pth or any(c in pth for c in '<>|?*:"') or ".." in pth.split("/"):
            return None
        rx = re.compile(r"(?<![\w./\\\-])" + _flex(pth.rsplit("/", 1)[-1]) + r"(?![\w/\\\-]|\.\w)", re.I)
        m = rx.search(_QUOTE_RE.sub(lambda q: "§" * len(q.group(0)), req))
        if not m:
            return None
        return f"{req[:m.start()]}{_quote_name(pth)}{req[m.end():]}"
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
    if slot in ("strip_prefix", "strip_suffix"):
        info = _strip_affix_parse(req)
        if info and info[1] is None:
            n = norm(req)
            sm = _STRIP_AFFIX_RE.search(n)
            return f"{req[:sm.end()]} {_quote_name(ans, True)}{req[sm.end():]}"
        return None
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
    root_complete = False        # مستوى الجذر ظاهر كاملاً (قد يكون المقتطع أبناء المجلدات فقط): لا يُسأل عن «كل ملفات الجذر» حينها
    skipped: tuple = ()          # [(مسار، سبب)] عناصر تجاوزها المسح لأن اسمها غير صالح للتعامل الآمن (محارف اتجاه/غير مرئية، طول زائد…)
    skipped_line: str = ""       # سطر «… (N عنصراً باسم غير صالح … لم يُعرض: …)» من قائمة المنفّذ إن وُجد


def _entries(context: List[str]) -> Ents:
    out = Ents()
    for e in context:
        if e.startswith("…"):
            out.partial = True
            if "باسم غير صالح" in e:
                out.skipped_line = e
            continue
        rel = e.rstrip("/")
        if rel and "\n" not in rel:
            out[rel] = e.endswith("/")
    return out


PARTIAL_QUESTION = ("مجلد العمل كبير والقائمة التي أراها مقتطعة، فلا أعمّم على «كل الملفات» حتى لا أفوّت شيئاً أو أنفّذ ناقصاً. "
                    "سمِّ الملفات بأسمائها أو اختر مجلد عمل أصغر.")


_GENERIC_NOUN_NAMES = {"ملفات", "الملفات", "ملف", "الملف", "files", "file", "folder", "folders", "مجلد", "المجلد"}
_FUNC_NAME_RES = (re.compile(r"(?<!\w)(?:كل|جميع|الكل|all|everything)(?!\w)"), W_NEG, W_THEN, W_DELETE, W_MOVE, W_COPY, W_PUT, W_DUP, _DEST_TO, _DEST_IN, re.compile(r"(?<!\w)(?:من|from|عن)(?!\w)"))
_NAME_LEAD = {"مجلد", "المجلد", "folder", "ملف", "الملف", "file", "الي", "to", "into", "في", "داخل", "in", "inside", "من", "from", "§"}


def _function_name_clash(prompt: str, ents: "Ents") -> Optional[str]:
    """اسم عنصر موجود هو أيضاً كلمة استثناء/حرف/فعل (مجلد «بدون» أو «من») ظهر مجرداً في الطلب ← سؤال؛ لا نحجبه فنفوّت الاستثناء
    ولا نعدّه هدفاً. يُستثنى ما سبقته كلمة «مجلد/إلى/من/في…» (استعمال صريح كاسم) أو ما كان بين علامتي تنصيص."""
    t = norm(_QUOTE_RE.sub(" § ", prompt))
    t = re.sub(r"(?<!\w)تخلص\s+من(?!\w)", " تخلص ", t)   # «من» هنا جزء من الفعل (احذف) لا علامة اسم صريح
    all_names = {norm(r.split("/")[-1]) for r in ents}
    for base in sorted({r.split("/")[-1] for r in ents}, key=len, reverse=True):
        nn = norm(base)
        generic = nn in _GENERIC_NOUN_NAMES
        if not nn or " " in nn or "." in nn or not (generic or any(rx.search(nn) for rx in _FUNC_NAME_RES)):
            continue
        for m in _mention_re(nn).finditer(t):
            prev = re.findall(r"[^\W_]+|§", t[:m.start()])
            if prev and prev[-1] in _NAME_LEAD:
                continue
            if generic and prev and prev[-1] in ("كل", "جميع", "all"):
                return (f"«{base}» اسم عنصر موجود في مجلد العمل وهو أيضاً كلمة عامة («{prev[-1]} {base}»)، فلا أعرف أيّهما تقصد. "
                        f"إن قصدت الاسم فاكتبه بين علامتي تنصيص «{base}»، وإلا أعد صياغة الطلب.")
            if generic:
                nxt = re.match(r"\s*([^\s]+)", t[m.end():])
                if not nxt or not (nxt.group(1) == "§" or re.search(r"[\w.]\.[a-z0-9]{1,6}$|/", nxt.group(1)) or nxt.group(1).strip("،,.") in all_names):
                    continue   # «ملفات»/files كلمة عامة: التباس فقط إن تلاها اسم ملف أو نص منصَّص (ملفات a.txt)
            return (f"«{base}» اسم عنصر موجود في مجلد العمل وهو أيضاً كلمة لها معنى في الطلب (استثناء أو حرف أو فعل)، فلا أعرف أيّهما تقصد. "
                    f"إن قصدت الاسم فاكتبه بين علامتي تنصيص «{base}»، وإلا أعد صياغة الطلب.")
    return None


_START_RE = re.compile(r"\s*(?:ابدا|ابدي|start|run|go|نفذ|نفذي|شغل|ok|okay|تمام|موافق|yes|نعم)\s*[.!؟?]*\s*$", re.I)
_Q_START = re.compile(r"\s*(?:هل|ماذا\s+لو|ماذا\s+(?:يحدث|سيحدث)\s+(?:لو|اذا|إذا)|what\s+if|should\s+i|do\s+i|is\s+it|will\s+it)(?!\w)", re.I)
_POLITE_OPEN = re.compile(r"\s*(?:ممكن|ت[كگ]در(?:ون)?|تقدر(?:ون)?|هل\s+(?:يمكنك|تستطيع|بامكانك|تقدر)|(?:can|could|would)\s+you|please|لو\s+(?:سمحت|تكرمت)|من\s+فضلك|رجاء|الرجاء)(?!\w)", re.I)   # طلب مهذب لا سؤال
_CAN_YOU = re.compile(r"\s*هل\s+(?:يمكنك|تستطيع|بامكانك|تقدر)(?!\w)")


def _is_question(prompt: str) -> bool:
    """الطلب سؤال لا أمر: ينتهي بـ ؟/? أو يبدأ بـ هل/ماذا لو… («هل يمكنك حذف x» بلا علامة استفهام طلب مهذب لا سؤال)."""
    head = re.split(r"\s*\n?توضيحات المستخدم:", strip_format_chars(prompt))[0].strip()
    n = norm(head)
    if _POLITE_OPEN.match(n) or re.match(r"\s*ينفع\s+ا[^\sأإآ]", head):   # «ينفع امسح x؟» طلب مهذب (بصيغة الأمر)، أما «ينفع أمسح» فسؤال
        return False
    if head.endswith(("؟", "?")):
        return True
    return bool(_Q_START.match(n)) and not _CAN_YOU.match(n)


_PAST_TO_IMP = {"حذفت": "احذف", "مسحت": "امسح", "نقلت": "انقل", "نسخت": "انسخ", "حذفنا": "احذف", "نقلنا": "انقل", "نسخنا": "انسخ", "ازلت": "ازل"}
_PAST_VERBS = re.compile(r"(?<!\w)(" + "|".join(_PAST_TO_IMP) + r")(?!\w)")
_Q_VERBS = {"delete": "سيُحذف", "move": "سيُنقل", "copy": "سيُنسخ", "rename": "ستُعاد تسمية", "create_dir": "سيُنشأ المجلد",
            "winget_upgrade": "ستُحدَّث الحزمة"}


def _question_reply(a: "Analysis") -> "Analysis":
    """جواب قصير على سؤال صيغته أمر: ما الذي سيتأثر لو طُلب فعلاً، دون خطة تُنفَّذ."""
    by: Dict[str, List[str]] = {}
    for st in a.steps or []:
        by.setdefault(st.get("action_type", ""), []).append(st.get("target", ""))
    parts = []
    for act, names in by.items():
        if act not in _Q_VERBS:
            continue
        shown = "، ".join(names[:5]) + (f" و{_n(len(names) - 5, 'عنصر')} غيرها" if len(names) > 5 else "")
        parts.append(f"{_Q_VERBS[act]} {shown}")
    return _info("هذا سؤال لا أمر فلم أنفّذ شيئاً. لو نفّذتُه: " + "؛ ".join(parts) + ". اكتب الأمر صريحاً (مثل «احذف …») لتنفيذه.")


# ---- طلبات خارج نطاق المساعد (كلمات المرور، المال، الأمان، تثبيت البرامج): جواب معلوماتي صريح لا «لم أفهم» ----
_F_PW = re.compile(
    r"(?<!\w)(?:و)?[انيت]?(?:غير|بدل|عدل|ضبط|اضبط|حدث|ارسل|اعطني|اعطيني|اكشف|اظهر|اعرض|وري|ورني|ارني|شوف|هات|طلع|استرجع|استعيد|"
    r"reset|change|set|update|tell|reveal|show|display|get|give|recover)\w*\s+(?:(?:my|the|your|our|me|all)\s+|لي\s+|لى\s+|كل\s+)?(?:ال)?"
    r"(?:كلم[هات]+\s+(?:ال)?(?:مرور|سر)|باس\s*(?:ورد|وورد|وارد)\w*|passwords?|passcode|رقم\s+(?:ال)?سري|رمز\s+(?:ال)?(?:مرور|دخول|سري)|pin\s+code)(?!\w)"
    r"|(?<!\w)(?:نسيت|نسيته|نسينا|ضاع|ضيعت|forgot|forgotten|lost)\w*\s+(?:(?:my|the|your)\s+)?(?:ال)?(?:كلم[هات]+\s+(?:ال)?(?:مرور|سر)|باس\s*(?:ورد|وورد|وارد)\w*|passwords?|رقم\s+(?:ال)?سري|رمز\s+(?:ال)?(?:مرور|دخول|سري))(?!\w)")
_F_MONEY_VERB = re.compile(r"(?<!\w)(?:و)?(?:ادفع|سدد|اشتر|اشتري|اشتريلي|اشترلي|ادفعلي|سددلي|pay|buy|purchase|checkout)(?!\w)")
_F_SEND = re.compile(r"(?<!\w)(?:و)?(?:حول|حولي|حولل[يى]|ارسل|ارسلي|ابعث|ابعثلي|ابعت|ابعتلي|دز|دزل[يى]|transfer|send|wire)(?!\w)")
_F_MONEY_OBJ = re.compile(
    r"\d[\d.,]*\s*(?:\$|دولار\w*|ريال\w*|دينار\w*|درهم\w*|يورو|جنيه\w*|ليره|الف|usd|eur|dollars?|euros?)|\$\s*\d"
    r"|(?<!\w)(?:ال)?(?:مبلغ|فلوس|نقود|رصيد|money|funds|cash|payment)(?!\w)|(?<!\w)(?:ال)?(?:دولار|دينار|ريال|usd)(?!\w)")
_F_SECURITY = re.compile(
    r"(?<!\w)(?:اعدادات|اعداد|settings?)\s+(?:ال)?(?:امان|حمايه|security|privacy|خصوصيه|جدار)|(?<!\w)(?:ال)?جدار\s+(?:ال)?ناري(?!\w)"
    r"|(?<!\w)(?:firewall|antivirus|defender|bitlocker|uac|regedit|registry)(?!\w)|(?<!\w)(?:ال)?ف[اي]ير\s*و+ل(?!\w)|(?<!\w)(?:ال)?مضاد\s+(?:ال)?فيروسات(?!\w)"
    r"|(?<!\w)(?:ال)?ريجستري(?!\w)|(?<!\w)(?:ال)?سجل\s+(?:ال)?نظام(?!\w)"
    r"|(?<!\w)(?:ال)?(?:انتي|anti)\s*-?\s*(?:ف[اي]+ر[وؤ]*س\w*|virus)(?!\w)|(?<!\w)(?:ويندوز|windows)\s+(?:ديفندر|defender)(?!\w)|(?<!\w)(?:ال)?ديفندر(?!\w)"
    r"|(?<!\w)(?:و)?(?:اطف\w*|اطفئ\w*|عطل\w*|وقف\w*|اوقف\w*|سكر\w*|اغلق\w*|الغ\w*|ايقاف|تعطيل|disable|turn\s+off|stop|kill)\s+(?:لي\s+)?(?:ال)?(?:حمايه|جدار|امان|فاير)")
_F_INSTALL = re.compile(r"(?<!\w)(?:و)?(?:ثبت|ثبتلي|نصب|نصبلي|install|installing)(?!\w)|(?<!\w)(?:نزل|نزلي|حمل|download)\s+(?:لي\s+)?(?:ال)?(?:برنامج|تطبيق|program|app|application|software)(?!\w)")
_F_UNINSTALL = re.compile(
    r"(?<!\w)(?:uninstall|الغ[اءي]*\s*تثبيت|ازاله\s+تثبيت|ازل\s+تثبيت|ازيل\s+تثبيت|فك\s+تثبيت)(?!\w)"
    r"|(?<!\w)(?:و)?(?:ازل|ازاله|ازيل|احذف|شيل|امسح|remove|delete|uninstall)(?:لي)?\s+(?:لي\s+)?(?:ال)?(?:برنامج|تطبيق|program|app|application|software)(?!\w)\s+[^\s§]")
_F_DOWNLOAD = re.compile(
    r"(?<!\w)(?:و)?(?:نزل|نزلي|نزلل[يى]|حمل|حملي|حملل[يى]|download)\s+(?:لي\s+)?(?:ال)?"
    r"(?:كروم|chrome|فايرفوكس|firefox|فوتوشوب|photoshop|واتساب|واتس\s*اب|whatsapp|تيليجرام|telegram|زوم|zoom|سكايب|skype|ديسكورد|discord|ستيم|steam|"
    r"vlc|اوفيس|office|اكسل|excel|وورد|word|باورنت|باور\s*بوينت|powerpoint|اندرويد|ايتونز|itunes|ادوبي|adobe|اوبرا|opera|ايدج|edge|فيسبوك|facebook|انستغرام|instagram|"
    r"تيك\s*توك|tiktok|سناب|snapchat|يوتيوب|youtube|نتفلكس|netflix|سبوتيفاي|spotify|بايثون|python|جافا|java|نود|node|دوكر|docker|vscode|فيجوال\s*ستوديو|visual\s*studio|"
    r"العاب|لعبه|games?|فيديو|فيديوهات|اغاني|اغنيه|افلام|فيلم|مسلسل|تورنت|torrent|كراك|crack)(?!\w)")
_F_DL_MSG = "تنزيل البرامج والتطبيقات والوسائط من الإنترنت غير مدعوم؛ نزّلها بنفسك من موقعها الرسمي أو من متجر ويندوز."
_F_MSG = "هذا النوع من العمليات لا أنفّذه؛ اعمله بنفسك ({})."
_F_INSTALL_MSG = "تثبيت/إزالة البرامج غير مدعومة بعد؛ المدعوم تحديث برنامج (مثال: «حدّث برنامج Git.Git»). ثبّته أو أزله بنفسك من إعدادات ويندوز."


# ---- طلب التراجع/الاسترجاع كتابةً: التراجع يتم بزر «تراجع» في النافذة (سجل العمليات)، لا بخطة جديدة؛ نجيب بإرشاد بدل «لم أفهم» ----
_UNDO_V = r"(?:ارجع|رجع|رجعني|رد|ردد|استرجع|استعد|استعيد|اعد|restore|recover|bring\s+back)"
_UNDO_RES = (
    re.compile(r"(?<!\w)(?:تراجع|ترجع|اتراجع|undo|rollback|revert)(?!\w)"),
    re.compile(r"(?<!\w)" + _UNDO_V + r"\w*\s+(?:\S+\s+){0,2}?(?:اللي|الي|يلي|الذي|التي)\s+(?:حذفت|مسحت|شلت|اشطبت|سويت|عملت|نقلت|نسخت|حذفنا|مسحنا)"),
    re.compile(r"(?<!\w)(?:استرجع|استعد|استعيد|restore|recover)\w*\s+(?:.*?\s)?(?:ال)?(?:محذوف\w*|deleted|سله\s+(?:ال)?(?:محذوفات|مهملات)|ما\s+حذفت\w*)(?!\w)"),
    re.compile(r"(?<!\w)(?:ارجع|رجعني|رجعها|رجعه|رجع)\w*\s+(?:زي|مثل|لشو|كما|الي|الى|ل)\s*(?:ما\s+)?(?:كان\w*|شو\s+كان)"),
    re.compile(r"(?<!\w)(?:الغ|الغي|الغاء)\w*\s+(?:(?:ال)?(?:اخر|اخير)\w*\s+(?:ال)?عملي\w*|(?:ال)?عملي\w*\s+(?:ال)?(?:اخر|اخير)\w*|(?:اللي|الي)\s+(?:سويت|عملت)\w*)"),
    re.compile(r"(?<!\w)اعكس\s+(?:ال)?عملي"),
    re.compile(r"(?<!\w)سله\s+(?:ال)?(?:محذوفات|مهملات)|(?<!\w)recycle\s+bin|(?<!\w)trash\s+bin"),
    re.compile(r"(?<!\w)(?:اعد|رد|ارجع)\w*\s+(?:ال)?(?:ملفات|مجلدات)\s+(?:(?:الي|الى|ل)\s+)?مكان\w*"),
    re.compile(r"(?<!\w)(?:استرجع|استعد|استعيد|restore|recover)\w*\s+[§¤](?!\w)"),
)
_UNDO_MSG = ("التراجع لا يتم بأمر نصي: اضغط زر «تراجع» في النافذة ليرجع آخر مهمة نُفّذت (وبالضغط مرة أخرى يرجع التي قبلها). "
             "المحذوفات تُنقل إلى مجلد .trash ولا تُمسح نهائياً. لم أنفّذ أي شيء الآن.")


def is_undo_request(prompt: str) -> bool:
    """هل الطلب النصي طلب تراجع/استرجاع (لا خطة جديدة)؟ للمتحكم: يوجّهه إلى مسار التراجع بدل المخطِّط."""
    t = _FILELIKE_I.sub(" § ", norm(_QUOTE_RE.sub(" ¤ ", strip_format_chars(prompt or ""))))
    return any(rx.search(t) for rx in _UNDO_RES)


def _forbidden(t: str) -> Optional["Analysis"]:
    """t: الطلب مطبَّعاً وقد حُجب فيه المقتبس (¤) وأسماء العناصر الموجودة وما يشبه اسم ملف (§).
    يعيد جواباً معلوماتياً (بلا خطة) لطلب من نوع لا ينفّذه المساعد، أو None."""
    t = re.sub(r"(?<!\w)(?:ملف\w*|files?|مجلد\w*|folders?|باسم|named?|اسم|extension|امتداد)\s+(?:ثبت|نصب|install\w*)", " § ", t)   # «ملفات install» اسم لا فعل
    if _F_PW.search(t):
        return _info(_F_MSG.format("تغيير كلمات المرور يتم من إعدادات الحساب أو الخدمة نفسها، ولا يجوز أن تمرّ عبر مساعد ملفات"))
    if _F_MONEY_VERB.search(t) or (_F_SEND.search(t) and _F_MONEY_OBJ.search(t)):
        return _info(_F_MSG.format("الدفع والشراء والتحويلات المالية عبر تطبيق البنك أو المتجر"))
    if _F_SECURITY.search(t):
        return _info(_F_MSG.format("إعدادات الأمان والجدار الناري ومضاد الفيروسات من «أمن ويندوز» في الإعدادات"))
    if _F_DOWNLOAD.search(t):
        return _info(_F_DL_MSG)
    t2 = re.sub(r"(?<!\w)(?:ثبت|ثبتلي|نصب|install)\w*\s+(?:ال)?(?:اخر|احدث|latest|newest)\s+(?:تحديث|تحديثات|update|updates)(?!\w)", " تحديث ", t)   # «ثبّت آخر تحديث للبرنامج X» = تحديث لا تثبيت جديد
    if _F_INSTALL.search(t2) or _F_UNINSTALL.search(t2):
        return _info(_F_INSTALL_MSG)
    return None


# ---- طبقة «الصيغ» الاحتياطية: طلب مألوف بكلمات أخرى (لهجة، ترتيب مختلف، حشو، خطأ إملائي بحرف) يُعاد إلى الصيغة القانونية ثم يمرّ على المحلل نفسه ----
# لا تعمل إلا إذا لم يفهم المحلل الأصلي شيئاً (لا خطة ولا معلومة). وكل كلمة غير معروفة (اسم لا نعرفه، استثناء، شرط، «ثم»…) تُلغي إعادة الصياغة
# فيبقى الجواب الأصلي (سؤال/لم أفهم). ما كان محظوراً (كلمات مرور/مال/أمان/تثبيت) أُجيب قبل الوصول إلى هنا.
def _fs(s: str) -> frozenset:
    return frozenset(norm(w) for w in s.split())


def _lev(a: str, b: str) -> int:
    """مسافة ليفنشتاين مع تبديل الحرفين المتجاورين (Damerau)."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def _near_dir(name: str, dirs: List[str]) -> Optional[str]:
    """مجلد موجود يشبه اسماً مكتوباً بخطأ إملائي («Dcos»/«Fotos»/«Docss»/«Backup» ← «Bakup»)، أو None."""
    nn = norm(name)
    best = None
    for d in dirs:
        nd = norm(d)
        if nd == nn or ((nn in nd or nd in nn) and abs(len(nn) - len(nd)) >= 2) or re.sub(r"\d", "", nn) == re.sub(r"\d", "", nd):
            continue   # اسم يحتوي الموجود أو يختلف برقم فقط («D_backup»، «Report2») اسم جديد مقصود لا خطأ إملائي
        lim = 2 if min(len(nn), len(nd)) >= 5 else 1 if min(len(nn), len(nd)) >= 3 else 0
        if lim and _lev(nn, nd) <= lim and (best is None or _lev(nn, nd) < _lev(nn, norm(best))):
            best = d
    return best


def _ed1(a: str, b: str) -> bool:
    """مسافة تحرير ≤ 1 (حرف زائد/ناقص/مختلف أو حرفان متبادلان)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        d = [i for i in range(la) if a[i] != b[i]]
        return len(d) == 1 or (len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])
    if la < lb:
        a, b = b, a
    return any(a[:i] + a[i + 1:] == b for i in range(len(a)))


_FR_FILL = _fs("ارجو يرجى الرجاء رجاء رجاءا ممكن يمكنك تستطيع تقدر تكدر اريد اريدك نريد ارغب احتاج احب لازم يجب ينبغي يلزم خل خلي هسه الان حاليا "
               "وياك شوف يابه يمعود يالله يا لو سمحت تكرم بسرعه هذا هذه هذي هاي هاذا شنو رايك قم قومي "
               "please kindly can could would you i we want need like the a an my me our for just now so these this those that")
_FR_SORTV = _fs("رتب ارتب ربت نظم صنف افرز فرز ترتيب تنظيم تصنيف جمع sort sorting sorted organize organise organizing organized arrange arranged categorize categorise group order tidy رتبب رتتب مرتبه مرتب شرتب")
_FR_PUTV = _fs("ضع حط اجعل put place")
_FR_CRIT_T = _fs("نوع انواع امتداد امتدادات type types extension extensions kind kinds نوغ نو نواع")
_FR_CRIT_D = {"تاريخ": "التاريخ", "date": "التاريخ", "شهر": "الشهر", "month": "الشهر", "سنه": "السنة", "year": "السنة"}
_FR_SORT_OK = _fs("ملفات ملف فايلات فايل files file filenames مبعثره متشابهه مختلفه محتويات contents حسب بحسب وفق وفقا طبقا حصب هسب according based on by "
                  "في داخل الي الى ب ل to into in folders folder مجلد مجلدات من of كل جميع كافه all everything every each منفصله خاص خاصه به بها own its their "
                  "تلقائيا automatically and و بشكل سوي اسوي سويلي دير اعمل make up اكو يوجد هناك عندي عندنا there are is have has بصوره")
_FR_LIGHTV = _fs("سوي اسوي سويلي دير اعمل make")
_FR_FOLD_OK = _fs("حسب بحسب وفق وفقا طبقا by according based خاص خاصه منفصل منفصله own its separate separately الحالي هذا هذه تلقائيا automatically بشكل من ل في ب and و")
_FR_DOWNLOADS = _fs("التنزيلات تنزيلات downloads download التحميلات")
_FR_FOLD = _fs("مجلد مجلدات folder folders directory dir دليل")
_FR_CONN = _fs("الي الى to into in inside within على علي ل ب في داخل يم نحو onto towards too ot tto الة لي وضعه وضعها حطه حطها")
_FR_COPYV = _fs("انسخ نسخ ننسخ ينسخ تنسخ نسخه كبي كوبي كرر copy copies cp duplicate clone replicate")
_FR_COPY_OK = _fs("make take put create give place it of over عمل اكو من مع with احتياطيه اعمل سوي ضع حط نزل and و كامل بالكامل whole entire full all كل جميع back up")
_FR_LIGHT_VERBS = _fs("make take put create give place اعمل سوي ضع حط نزل عمل")
_FR_VERIFYV = _fs("تحقق التحقق تاكد التاكد تاكيد verify check confirm validate")
_FR_VERIFYO = _fs("بصمه البصمه الهاش هاش hash checksum سلامه integrity صحه صحة")
_FR_PFXW = _fs("بادئه بادئ prefix بدايه")
_FR_ADDV = _fs("اضف ضف ضيف ضيفلي اضيف اضافه اعاده زد الحق الحاق حط ضع add adding prepend append put give rename renaming اعد تسميه تسمي سمي غير تغيير تغير "
               "عدل عدلي سم ريناميلي ريناك بادئه ابدا ابدء تبدا تبدي يبدا start begin starts begins بدايه")
_FR_POSW = _fs("قبل بدايه اولها باول لاول بالاول front beginning start before begin ابدا ابدء تبدا تبدي يبدا starts begins")
_FR_PFX_OK = _fs("ملفات ملف اسماء اسم فايلات فايل files file names name filenames filename كل جميع كافه all every each دفعه batch bulk في ب ل الى الي على داخل "
                 "in to of at on by too with مع as inside سوي اسوي سويلي دير اعمل make مجلد مجلدات folder الموجوده موجود بمجلد النص text and و بالبدايه")
_FR_PROJ_STRONG = _fs("مشروع مشاريع مشرع بروجكت project projects projcet prject projet")
_FR_CREV = _fs("انشئ انشا انشي انشاء انشىء سوي اسوي سويلي اعمل جهز جهزلي ابن ابني ابنيلي بناء كون تكوين هيئ تهيئه ابدا ابدء create creat crate creaet make build "
               "generate scaffold init initialize initialise set setup start new دير")
_FR_PROJ_OK = _fs("جديد جديدا جديده new باسم اسمه اسمها بأسم named nammed called name اسم فيه فيها بي بيه مع with containing and و في in على for ل structure "
                  "skeleton هيكل بنيه مجلدات folders folder مجلد up of بروجكت")
_FR_SUBS = frozenset({"src", "docs", "tests"})
_FR_UPDV = _fs("حدث حدثلي حدثي تحديث حدت حذث حدس حدتث تحدث يتحدث ترقيه ارقي رق ارق upgrade update refresh winget upgrades updates updating")
_FR_UPD_OK = _fs("برنامج تطبيق حزمه package packages program application app apps باستخدام بواسطه عبر using with via through الي الى to اخر احدث latest newest "
                 "اصدار نسخه release version وتاكد تاكد تاكيد confirm make sure is up date متوفره available run for ب ل ويندوز windows من and و")
_FR_GLUED = re.compile(r"^(update|upgrade|winget|حدث|حدّث|حدثلي|برنامج|البرنامج|التطبيق|تطبيق|الحزمة|حزمة|الحزمه|بالـ|ب|بالwinget)"
                       r"([A-Za-z0-9][A-Za-z0-9+_\-]*(?:\.[A-Za-z0-9+_\-]+)+)$", re.I)
_FR_PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_\-]*(?:\.[A-Za-z0-9+_\-]+)+$")
_FR_AR = re.compile(r"[؀-ۿ]")


def _fr_variants(n: str) -> List[str]:
    out = [n]
    if _FR_AR.search(n):
        for b in list(out):
            if len(b) > 2 and b[0] in "وف":
                out.append(b[1:])
        for b in list(out):
            for p in ("هال", "بال", "لل", "ال", "ب", "ل", "ك", "ت", "ي", "ن"):
                if len(b) > len(p) + 1 and b.startswith(p):
                    out.append(b[len(p):])
        for b in list(out):
            for s in ("لي", "ني", "ها", "هم", "ه", "ك"):
                if len(b) > len(s) + 2 and b.endswith(s):
                    out.append(b[:-len(s)])
    return out


def _fr_is(n: str, voc, fuzzy: bool = False) -> bool:
    vs = _fr_variants(n)
    if any(v in voc for v in vs):
        return True
    if fuzzy:
        for v in vs:
            if len(v) >= (5 if _FR_AR.search(v) else 3) and not v.isdigit():   # الجذور العربية القصيرة لا تُصحَّح تقريبياً («محدّثة» ليست «تحدث»)
                for w in voc:
                    if len(w) >= 4 and v != w and _ed1(v, w):
                        return True
    return False


def _fr_named(n: str, names: set) -> bool:
    return any(v in names for v in _fr_variants(n))


def _fr_tokens(clean: str, names: set = frozenset()) -> List[Tuple[str, str]]:
    qs: List[str] = []

    def hold(m):
        qs.append("«" + m.group(1) + "»")
        return f" ¤{len(qs) - 1} "
    out: List[Tuple[str, str]] = []
    for w in _QUOTE_RE.sub(hold, clean).split():
        w = re.sub(r"\.+$", "", w.strip("؟?!،,؛;:"))   # الأقواس تبقى: «(old)_» بادئة
        if not w or not re.search(r"\w", w):
            continue
        if w[0] == "¤" and w[1:].isdigit():
            out.append((qs[int(w[1:])], "¤"))
            continue
        if norm(w) not in names:
            b = re.match(r"^([بلك])ـ?(?=[A-Za-z0-9])(.+)$", w)
            if b:
                out += [(b.group(1), b.group(1)), (b.group(2), norm(b.group(2)))]
                continue
            h = re.match(r"^([\u0621-\u064a]{3,}?)(حسب)$", w)
            if h and norm(h.group(1)) in ("الملفات", "ملفات", "الفايلات"):
                out += [(h.group(1), norm(h.group(1))), ("حسب", "حسب")]
                continue
        g = _FR_GLUED.match(w)
        if g:
            out += [(g.group(1), norm(g.group(1))), (g.group(2), norm(g.group(2)))]
            continue
        out.append((w, norm(w)))
    return out


def _fr_sort(toks, names, raw=()):
    kinds, crit, implicit, dl = set(), set(), False, False
    ns = [n for _o, n in toks]
    for i, (o, n) in enumerate(toks):
        if n == "¤" or _fr_named(n, names) and not _fr_is(n, _FR_DOWNLOADS):
            return None
        if _fr_is(n, _FR_DOWNLOADS):
            dl = True
        elif _fr_is(n, _FR_SORTV, True):
            kinds.add("v")
        elif _fr_is(n, _FR_PUTV):
            kinds.add("put")
        elif _fr_is(n, _FR_CRIT_T, True):
            crit.add("النوع")
            implicit = implicit or any(x in ("كل", "each", "every") for x in ns)
        elif any(v in _FR_CRIT_D for v in _fr_variants(n)):
            crit.add(next(_FR_CRIT_D[v] for v in _fr_variants(n) if v in _FR_CRIT_D))
        elif _fr_is(n, _FR_SORT_OK, True):
            pass
        else:
            return None
    for i, (o, n) in enumerate(raw):   # «رتب مجلد my files» / «مجلد X»: مجلد مذكور بالاسم ← ليس مجلد العمل نفسه
        if n in ("مجلد", "المجلد", "folder", "directory", "dir") and i + 1 < len(raw) and not (
                _fr_is(raw[i + 1][1], _FR_FOLD_OK, True) or _fr_is(raw[i + 1][1], _FR_DOWNLOADS)):
            return None
    fold = any(_fr_is(n, _FR_FOLD) for n in ns)
    by = any(n in ("حسب", "بحسب", "by", "وفق", "according", "based") for n in ns)
    if len(crit) != 1 or not ("v" in kinds or (implicit and fold) or (fold and by and any(_fr_is(n, _FR_LIGHTV) for n in ns))):
        return None
    return "رتب الملفات حسب " + next(iter(crit)), ("أرتّب مجلد العمل الحالي نفسه (لا مجلداً آخر اسمه «التنزيلات»)." if dl else None)


def _fr_copy(toks, names, raw=()):
    toks = list(toks)
    for i in range(len(toks) - 1):   # «back up X to Y» ← نسخ
        if toks[i][1] == "back" and toks[i + 1][1] == "up":
            toks[i:i + 2] = [("copy", "copy"), ("", "")]
            break
    toks = [t for t in toks if t[1]]
    verb, verify, ops, conns, nverbs, nlight = False, False, [], [], 0, 0
    for i, (o, n) in enumerate(toks):
        if n == "¤" or _fr_named(n, names):
            ops.append(i)
        elif _fr_is(n, _FR_COPYV, True):
            verb = True
            nverbs += 0 if verify else 1
        elif _fr_is(n, _FR_VERIFYV):
            verify = True
        elif _fr_is(n, _FR_VERIFYO):
            verify = True
        elif n == "then" and i + 1 < len(toks) and _fr_is(toks[i + 1][1], _FR_VERIFYV):
            pass
        elif _fr_is(n, _FR_CONN, True) or (n[:1] in "بل" and _fr_is(n[1:], _FR_FOLD)) or (n[:3] in ("بال", "لل") and _fr_is(n[3:], _FR_FOLD)):
            conns.append(i)
        elif _fr_is(n, _FR_FOLD) or _fr_is(n, _FR_COPY_OK):
            nlight += 1 if _fr_is(n, _FR_LIGHT_VERBS) else 0
        else:
            ops.append(i)
    if not verb or nverbs != 1 or nlight > 1 or len(ops) != 2 or not any(ops[0] < c < ops[1] for c in conns):
        return None   # فعلان (مثل «ضع x في y واعمل نسخة احتياطية») = عمليتان لا نسخ واحد
    def folder(i):   # «انسخ مجلد pdf» لا «انسخ pdf» (ملفات النوع): لا نُسقط كلمة «مجلد»
        return "المجلد " if any(0 <= j < len(toks) and (_fr_is(toks[j][1], _FR_FOLD) or (j in conns and toks[j][1][1:2] and _fr_is(toks[j][1][1:], _FR_FOLD)))
                                 for j in (i - 1, i + 1)) else ""
    return f"انسخ {folder(ops[0])}{toks[ops[0]][0]} إلى {folder(ops[1])}{toks[ops[1]][0]}", ("يتحقق المنفّذ من محتوى النسخ بعد كل عملية نسخ." if verify else None)


def _fr_prefix(toks, names, raw=()):
    us = [i for i, (o, n) in enumerate(toks) if n != "¤" and re.search(r"[_\-]$", o)]
    pw = [i for i, (o, n) in enumerate(toks) if _fr_is(n, _FR_PFXW, True)]
    if len(us) == 1:
        pi = us[0]
    elif not us and pw and pw[-1] + 1 < len(toks) and not _fr_is(toks[pw[-1] + 1][1], _FR_PFX_OK):
        pi = pw[-1] + 1
    else:
        return None
    has_add, has_pos, ops = False, bool(pw) or len(us) == 1, []   # «IMG_» بحد ذاته (ينتهي بـ _ أو -) يدل على بادئة
    for i, (o, n) in enumerate(toks):
        if i == pi:
            continue
        if n == "¤" or _fr_named(n, names):
            ops.append(i)
        elif _fr_is(n, _FR_PFXW, True):
            has_pos = True
            has_add = has_add or (n in ("prefix", "prefixing") and i != pi - 1)   # «prefix all files…» فعل لا اسم («prefix IMG_» اسم)
        elif _fr_is(n, _FR_POSW) or (n in ("اول", "الاول") and i and toks[i - 1][1] in ("في", "ب", "ل", "at", "in")):   # «في أول الاسم» لا «الأول» وحدها
            has_pos = True
            has_add = has_add or _fr_is(n, _FR_ADDV)
        elif _fr_is(n, _FR_ADDV, True):
            has_add = True
        elif _fr_is(n, _FR_PFX_OK, True):
            pass
        else:
            ops.append(i)
    if not (has_add and has_pos) or len(ops) != 1:
        return None
    return f"أضف البادئة {toks[pi][0]} إلى ملفات {toks[ops[0]][0]}", None


def _fr_project(toks, names, raw=()):
    proj = cre = False
    subs, ops = set(), []
    for i, (o, n) in enumerate(raw[:-2]):   # «مجلد مشروع X»: مشروع X أم مجلد اسمه «مشروع X»؟ يُسأل
        if n in ("مجلد", "folder") and raw[i + 1][1] in ("مشروع", "project") and raw[i + 2][1] not in ("باسم", "اسمه", "اسمها", "named", "nammed", "called", "name", "بأسم", "بإسم"):
            return None
    strong = [i for i, (o, n) in enumerate(raw) if _fr_is(n, _FR_PROJ_STRONG)]
    if len(strong) > 1 or any(i and raw[i - 1][1] in ("باسم", "اسمه", "اسمها", "named", "nammed", "called", "name", "بأسم", "بإسم") for i in strong):
        return None   # «مشروع مشروع التخرج» / «باسم مشروع التخرج»: كلمة «مشروع» الثانية جزء من الاسم لا نوع الإنشاء
    wants = any(n in ("جديد", "جديدا", "جديده", "new", "اريد", "اريدك", "نريد", "ارغب", "احتاج", "want", "need") for _o, n in raw)
    for o, n in toks:
        if n == "¤" or _fr_named(n, names):
            ops.append(o)
        elif _fr_is(n, _FR_PROJ_STRONG, True):
            proj = True
        elif n.lower() in _FR_SUBS or (n[:1] == "و" and n[1:].lower() in _FR_SUBS):
            subs.add(n.lstrip("و").lower() if n.lstrip("و").lower() in _FR_SUBS else n.lower())
        elif _fr_is(n, _FR_CREV, True):
            cre = True
        elif _fr_is(n, _FR_PROJ_OK):
            pass
        else:
            ops.append(o)
    if not proj or not (cre or wants) or len(ops) != 1 or (subs and subs != _FR_SUBS):
        return None
    return f"أنشئ مشروع {ops[0]}", None


def _fr_update(toks, names, raw=()):
    pk, has_u, ops = [], False, []
    for o, n in toks:
        if _FR_PKG.match(o) and not _fr_named(n, names) and o.rsplit(".", 1)[-1].lower() not in _KNOWN_EXTS and re.search(r"[A-Za-z]", o.rsplit(".", 1)[-1]):
            pk.append(o)
        elif n == "¤" or _fr_named(n, names):
            ops.append(o)
        elif _fr_is(n, _FR_UPDV, True):
            has_u = True
        elif _fr_is(n, _FR_UPD_OK, True):
            pass
        else:
            ops.append(o)
    ns = [n for _o, n in toks]
    has_u = has_u or ("up" in ns and "date" in ns and "sure" in ns)   # «make sure X is up to date»
    if not has_u or len(pk) > 1 or (pk and ops):
        return None
    if pk:
        return f"حدث {pk[0]}", None
    if not _fr_is(toks[0][1], _FR_UPDV, True) or len(ops) > 3:
        return None   # بلا معرّف حزمة: لا بد أن يكون الفعل أول الطلب (تفادي جمل خبرية فيها «تحديث/حدث»)
    return ("حدث برنامج " + " ".join(ops)).strip(), None


# أسئلة تعني «لم أفهم الصياغة» فتجوز إعادة المحاولة بصيغة قانونية؛ أسئلة الأمان الأخرى (مجلد كبير مقتطع، ملف بعينه…) تبقى كما هي
_FR_RETRY_ASKS = ("لم أفهم", "لم أجد", "اسم المجلد يبدو", "الترتيب يشمل", "إعادة تسمية عدة ملفات تتم بإضافة بادئة", "ما معرّف الحزمة",
                  "ما اسم المجلد أو المشروع", "ما البادئة", "الاسم الجديد يبدو", "الطلب فيه استثناء أو نفي", "الطلب فيه شرط",
                  "إعادة التسمية بالبادئة تُطبَّق", "الطلب يحتوي أكثر من عملية", "الطلب يجمع نسخة احتياطية", "المكررات تُكشف")   # الأخيران: قد تكون «ضع نسخة» التباساً لا عمليتين؛ الصيغ نفسها ترفض ما فيه فعلان
_FR_FRAMES = (_fr_update, _fr_prefix, _fr_project, _fr_copy, _fr_sort)


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
        if "CLARIFY" in (system_prompt or "").split(CONTEXT_MARKER, 1)[0]:   # الترويسة فقط: اسم ملف يحوي CLARIFY (بعد CONTEXT_MARKER) لا يقلب التخطيط إلى توضيح
            # «لم أفهم الطلب» ليست سؤال توضيح: يُعاد الطلب كاملاً من المستخدم (لا يُدمج مع الجديد)
            qs = [a.question] if a.question and a.question != HELP_QUESTION else []
            return json.dumps({"questions": qs, "slots": [a.slot] if qs else []}, ensure_ascii=False)
        if a.steps:
            steps, dropped = [], 0
            for stp in a.steps:   # اسم لن يقبله المخطط (محارف مخادعة…): تُتخطى الخطوة بملاحظة بدل خطة كلها مرفوضة
                names = [stp.get("target", ""), (stp.get("params") or {}).get("dest", ""), (stp.get("params") or {}).get("new_name", "")]
                if any(isinstance(n, str) and n and name_problem(n) for n in names):
                    dropped += 1
                else:
                    steps.append(stp)
            if dropped:
                a = Analysis(steps or None, a.question, _join_notes(a.note, f"تخطّيتُ {dropped} من خطوات الخطة لأن اسماً فيها يحوي محارف غير مرئية أو مخادعة لا أستطيع التعامل معها بأمان."), a.slot)
            if not a.steps:
                return json.dumps({"steps": [], "note": a.note or HELP_QUESTION}, ensure_ascii=False)
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

        skipped: List[Tuple[str, str]] = []

        def ok(entry: "os.DirEntry", rel: str) -> bool:
            if _is_link(Path(entry.path)):
                return False
            why = None
            if any(name_len(part) > MAX_NAME_UNITS for part in rel.split("/")) or (IS_WINDOWS and len(str(root / rel)) > MAX_PATH_CHARS):
                why = "اسمه أو مساره أطول من الحد الذي يقبله النظام"
            elif rel != rel.strip():
                why = "في اسمه فراغ في أوله أو آخره"
            else:
                why = name_problem(rel)
            if why:
                skipped.append((rel, why))   # الفحص نفسه الذي يطبّقه المخطط على الأسماء: لا نبني خطة على اسم سيرفضه، لكننا نبلّغ عنه
                return False
            return True

        try:
            tops = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            return None
        # مستوى الجذر يُسرد كاملاً أولاً (حتى الحد)، وأبناء المجلدات تتقاسم ما تبقّى: الاقتطاع في الأبناء لا يُخفي ملفات الجذر
        # (الترتيب والتنظيف يخصّان ملفات الجذر فقط، فلا يُسأل المستخدم «سمِّ الملفات» وكل ملفات الجذر ظاهرة).
        roots = [e for e in tops if not (e.name == ".trash" or e.name.startswith(".undo_journal") or not ok(e, e.name))]
        root_trunc = len(roots) > _FULL_SCAN_LIMIT
        if root_trunc:
            roots = roots[:_FULL_SCAN_LIMIT]
            truncated = True   # هناك عنصر جذر آخر لا يتسع: الاقتطاع حقيقي (5000 بالضبط بلا زيادة ليس اقتطاعاً)
        for idx, e in enumerate(roots):
            is_dir = e.is_dir(follow_symlinks=False)
            lines.append(e.name + ("/" if is_dir else ""))
            if is_dir:
                try:
                    kids = sorted(os.scandir(e.path), key=lambda c: c.name)
                except OSError:
                    continue
                later = len(roots) - idx - 1   # مقاعد محجوزة لعناصر الجذر التالية
                for c in kids:
                    rel = f"{e.name}/{c.name}"
                    if not ok(c, rel):
                        continue
                    if len(lines) + later >= _FULL_SCAN_LIMIT:
                        truncated = True
                        break
                    lines.append(rel + ("/" if c.is_dir(follow_symlinks=False) else ""))
        if truncated:
            lines.append("… (القائمة مقتطعة)")
        out = _entries(lines)
        out.skipped = tuple(skipped)
        out.root_complete = not root_trunc
        return out

    @staticmethod
    def _skipped_note(ents: "Ents") -> Optional[str]:
        """ملاحظة بالعناصر التي لم تدخل الخطة لأن أسماءها غير صالحة للتعامل الآمن: العدد وأول ثلاثة أسماء."""
        def show(name: str) -> str:
            return "".join(ch if unicodedata.category(ch) != "Cf" else f"‹U+{ord(ch):04X}›" for ch in name)[:60]
        if ents.skipped:
            names = [show(r) for r, _ in ents.skipped]
            n = len(names)
        elif ents.skipped_line:
            m = re.search(r"\((\d+) عنصر\w* باسم غير صالح[^:]*:\s*(.*)\)\s*$", ents.skipped_line)
            if not m:
                return None
            n, names = int(m.group(1)), [x for x in re.split(r"،\s*", m.group(2)) if x]
        else:
            return None
        return (f"استُثني من هذه الخطة {_n(n, 'عنصر')} لأن الاسم غير صالح للتعامل الآمن (محارف ممنوعة أو غير مرئية أو اتجاه خفي أو طول زائد): "
                + "، ".join(names[:3]) + (" …" if n > 3 else "") + ". أعد التسمية من مستكشف الملفات إن كان مقصوداً.")

    @staticmethod
    def _skipped_mention(prompt: str, ents: "Ents") -> Optional[str]:
        """الطلب يسمّي صراحة ملفاً تجاوزه المسح لأن اسمه غير صالح (علامة اتجاه خفية…) ولا يوجد عنصر ظاهر بالاسم نفسه ← رسالة محددة بدل «لم أجد»."""
        if not ents.skipped:
            return None
        t = norm(_QUOTE_CHARS.sub(" ", strip_format_chars(prompt)))
        visible = {norm(r.split("/")[-1]) for r in ents} | {norm(r) for r in ents}
        for rel, why in ents.skipped:
            base = rel.split("/")[-1]
            for cand in {norm(strip_format_chars(base)), norm(strip_format_chars(rel))}:
                if cand and cand not in visible and _mention_re(cand).search(t):
                    shown = "".join(ch if unicodedata.category(ch) != "Cf" else f"‹U+{ord(ch):04X}›" for ch in rel)[:80]
                    return _info(f"«{shown}» موجود في مجلد العمل لكن اسمه غير صالح للتعامل الآمن ({why})، فلا أستطيع التعامل معه من هنا. "
                                 f"أعد تسميته من مستكشف الملفات ثم أعد الطلب.")
        return None

    # ---------- التحليل ----------
    def _mask(self, prompt: str, ents: Ents, files_only: bool = False, quote_tok: str = " § ") -> str:
        """يحجب العبارات المقتبسة وأسماء العناصر الموجودة وكل ما يشبه اسم ملف/مسار، ليبقى نص الأفعال وحده."""
        t = norm(_QUOTE_RE.sub(quote_tok, prompt))
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
        box: Dict[str, "Ents"] = {}
        a = self._analyze(prompt, context, box)
        if not a.steps and not (a.note and a.note.startswith(INFO_PREFIX)):
            fr = self._frames(prompt, context, box) if (not a.question or a.question.startswith(_FR_RETRY_ASKS)) else None
            if fr is not None:
                a = fr
        if a.question and a.slot is None and "موجود في أكثر من مكان" in a.question and "اكتب المسار الكامل" in a.question:
            a.slot = "path"   # الجواب مسار كامل يحلّ محل الاسم الملتبس في الطلب (انظر merge_clarification)
        ents = box.get("ents")
        if not a.steps and a.question and _is_question(prompt):   # «ماذا لو حذفت x»: نعيد القراءة بلا افتتاحية السؤال وبصيغة الأمر لنشرح الأثر
            alt = _PAST_VERBS.sub(lambda m: _PAST_TO_IMP[norm(m.group(1))], prompt)
            qm = _Q_START.match(norm(alt))
            alt = alt[qm.end():] if qm else alt
            if alt.strip() and alt != prompt:
                b = self._analyze(alt, context, {})
                if b.steps and any(s.get("action_type") not in ("inspect", "run_command") for s in b.steps):
                    return _question_reply(b)
        if a.steps and _is_question(prompt) and any(s.get("action_type") not in ("inspect", "run_command") for s in a.steps):
            return _question_reply(a)   # «هل أحذف temp.tmp؟» سؤال لا أمر: نشرح ما سيتأثر ولا نخطّط حذفاً
        if ents is not None and a.steps:
            sk = self._skipped_note(ents)
            if sk:
                a = Analysis(a.steps, a.question, _join_notes(a.note, sk), a.slot)
        return a

    def _frames(self, prompt: str, context: List[str], box: Dict[str, "Ents"]) -> Optional[Analysis]:
        """إعادة صياغة طلب لم يفهمه المحلل (لهجة/حشو/ترتيب كلمات/خطأ إملائي بحرف) إلى صيغته القانونية؛ أي كلمة غير معروفة تلغيها. انظر _FR_FRAMES."""
        ents = box.get("ents")
        if ents is None or "توضيحات المستخدم" in prompt or len(prompt) > 300:
            return None
        names = {norm(r.split("/")[-1]) for r in ents} | {norm(r) for r in ents}
        clean = _clean_prompt(strip_format_chars(prompt), names)
        npr = norm(clean)
        if any(" " in nm and nm in npr for nm in names):
            return None   # اسم موجود من كلمتين («my files»): الصيغ لا تفصله ولا تُسقط كلماته
        toks = _fr_tokens(clean, names)
        raw = toks
        toks = [(o, n) for o, n in toks if n == "¤" or _fr_named(n, names) or n not in _FR_FILL]
        if not toks or len(toks) > 20:
            return None
        for fn in _FR_FRAMES:
            r = fn(toks, names, raw)
            if not r:
                continue
            b = self._analyze(r[0], context, {})
            q = b.question or ""
            if b.steps or (b.note and b.note.startswith(INFO_PREFIX)) or (q and q != HELP_QUESTION and not q.startswith(("لم أفهم", "اسم المجلد يبدو"))):
                return Analysis(b.steps, b.question, _join_notes(b.note, r[1]) if b.steps else b.note, b.slot)
        return None

    def _analyze(self, prompt: str, context: List[str], box: Dict[str, "Ents"]) -> Analysis:
        prompt = strip_format_chars(prompt)
        prompt = re.sub(r"\s*\n?توضيحات المستخدم:\s*", " ", prompt).replace(" | ", " ")  # إجابات أسئلة التوضيح تُدمج بالطلب
        if len(prompt) > 600:
            return Analysis(question="الطلب طويل جداً. اكتبه بجملة قصيرة واحدة (عملية واحدة).")
        prompt = re.sub(r"(?<![\w.])\./", "", re.sub(r"(?<![\w.])(و)[\u064b-\u0652]*\./", r"\1 ", prompt.replace("\\", "/")))  # Docs\a.txt و ./a.txt كما في ويندوز
        ents = _entries(context)
        if ents.partial and self.workspace is not None:
            ents = self._full_ents() or ents   # قائمة النموذج مقتطعة؛ القواعد تمسح هيكل المجلد كاملاً (حد كبير) فلا تتعطل عند >60 عنصراً
        box["ents"] = ents
        hid = self._skipped_mention(prompt, ents)
        if hid:
            return hid
        raw_prompt = prompt
        prompt = _clean_prompt(prompt, {norm(r.split("/")[-1]) for r in ents} | {norm(r) for r in ents})
        top_files = [r for r, d in ents.items() if not d and "/" not in r]
        top_dirs = [r for r, d in ents.items() if d and "/" not in r]
        forb = _forbidden(self._mask(prompt, ents, quote_tok=" ¤ "))
        if forb:
            return forb
        und = self._mask(prompt, ents, quote_tok=" ¤ ")
        if any(rx.search(und) for rx in _UNDO_RES):
            return _info(_UNDO_MSG)
        clash = _function_name_clash(prompt, ents)
        if clash:
            return Analysis(question=clash)
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
            other = W_WHOAMI.sub(" ", text)
            if not W_INSPECT.search(other) or not [t for t in re.findall(r"[^\W_]+|§", W_INSPECT.sub(" ", other))
                                                   if t not in _INSPECT_FILL and t not in _AGAIN_WORDS]:
                intents.discard("inspect")   # «اعرض الهوية»/«ما اسم المستخدم» أمر واحد؛ أما «من أنا وافحص x» فعمليتان
        if W_DUP.search(text):
            intents.add("dup")
        if "move" not in intents and W_PUT.search(text) and (_DEST_TO.search(prompt) or _DEST_IN.search(prompt)):
            intents.add("move")

        if "copy" in intents and W_CREATE.search(text) and re.search(r"(?<!\w)نسخه\s+احتياطيه(?!\w)", text):
            return Analysis(question=MULTI_QUESTION)   # «اعمل نسخة احتياطية من X وانسخ Y إلى Z»: عمليتان
        bm = _BACKUP_VERB.search(text)
        if bm:   # «اعمل نسخة احتياطية واحذف a.txt»: لا تُسقط إحدى العمليتين بصمت (كانت تُخطَّط الثانية وحدها فيظن المستخدم أن نسخة أُخذت)
            rest = text[:bm.start()] + " § " + text[bm.end():]
            rest_ops = (W_DELETE, W_MOVE, W_COPY, W_RENAME, W_ORGANIZE, W_INSPECT, W_UPDATE, W_WHOAMI, W_DUP, W_ADD_PREFIX)
            if (intents - {"backup"} or any(w.search(rest) for w in rest_ops) or len(_BACKUP_VERB.findall(text)) > 1
                    or (W_CREATE.search(rest) and W_CREATE_OBJ.search(self._mask(prompt, ents, files_only=True)))):
                return Analysis(question=BACKUP_MULTI_QUESTION)
        strip_info = _strip_affix_parse(prompt)
        if strip_info:   # «بحذف/إزالة البادئة old_»: إزالة لا إضافة (كان يُضيف البادئة)
            intents -= {"delete", "rename", "rename1"}
            intents.add("strip")
        if "dup" in intents:
            others = intents - {"dup", "delete", "move", "inspect"}
            if others or {"delete", "move"} <= intents or ("inspect" in intents and intents & {"delete", "move"}):
                return Analysis(question=MULTI_QUESTION)
        elif len(intents) > 1:
            return Analysis(question=MULTI_QUESTION)
        if not intents:
            if _START_RE.match(norm(prompt)):
                return _info("لا توجد خطة جاهزة للتنفيذ. اكتب طلبك أولاً (مثل «رتّب الملفات حسب النوع») ثم اضغط ابدأ.")
            return Analysis(question=HELP_QUESTION)
        if W_THEN.search(text):
            return Analysis(question=MULTI_QUESTION)

        if re.search(r"(?<!\w)(?:ال)?(?:مجلد|folder)\s+(?:ال)?(?:مكررات|مكرره|مكرر|duplicates?|dups?)(?!\w)", text):
            # «احذف المجلد مكررات» ولا مجلد بهذا الاسم (أو حُذف في تشغيل سابق): لا نحوّله لحذف الملفات المكررة بالتخمين
            return Analysis(question="لا يوجد مجلد باسم «مكررات» في مجلد العمل. إن قصدت حذف الملفات المكررة فاكتب «احذف المكررات»، وإلا اكتب اسم المجلد كما يظهر.")
        if intents == {"delete"} and not strip_info and not W_DUP.search(text):
            mk = _dest_mask(prompt)
            if _DEST_TO.search(mk):   # «احذف a.txt إلى Docs» / «شيل a.txt الى Docs»: حذف أم نقل؟ لا نحذف مجلد الوجهة ولا نخمّن
                return Analysis(question=("طلب الحذف فيه وجهة («إلى …»). لن أحذف المجلد المذكور كأنه هدف. تقصد نقل الملف إلى ذلك المجلد أم حذفه؟ "
                                          "اكتب «انقل a.txt إلى Docs» للنقل، أو «احذف a.txt» للحذف."))
            if re.search(r"(?<!\w)(?:ال)?ملف\s+(?:و|ثم)(?!\w)|(?<!\w)(?:ال)?ملف\s+و(?=مجلد|\s)", norm(mk)) and not any(
                    norm(r.split("/")[-1]) in ("ملف", "الملف") for r in ents):   # «احذف الملف ومجلد Docs»: «الملف» لا يحدد شيئاً؛ لا نحذف المجلد وحده بصمت
                return Analysis(question=("«الملف» وحدها لا تحدد ملفاً، ولن أحذف المجلد المذكور وحده بالتخمين. "
                                          "اكتب اسم الملف كما يظهر (مثل «احذف notes.txt»)." + self._offer_files(prompt, ents)))
            if _COPY_OF_RE.search(text):   # «احذف نسخة a.txt»: نسخة احتياطية/مكررة أم الملف نفسه؟
                return Analysis(question=("«نسخة» في طلب الحذف ملتبسة: قد تعني نسخة احتياطية أو مكررة، أو الملف نفسه، ولن أحذف الأصل بالتخمين. "
                                          "سمِّ الملف المراد حذفه بدقة (مثل «احذف a (1).txt»)، أو اطلب «احذف المكررات»."))
        if "whoami" in intents:
            return Analysis([_step(1, "run_command", "whoami", "رمز الخروج 0", cmd=["whoami"])])
        if "update" in intents:
            pkgs = [m.rstrip(".") for m in _PKG_RE.findall(prompt)]
            pkgs = [m for m in pkgs if "." in m
                    and m.rsplit(".", 1)[-1].lower() not in _KNOWN_EXTS and norm(m) not in {norm(r.split("/")[-1]) for r in ents}]
            if not pkgs:
                return Analysis(question="ما معرّف الحزمة المراد تحديثها؟ مثال: «حدّث برنامج Git.Git»")
            return Analysis([_step(1, "winget_upgrade", pkgs[0], "رمز الخروج 0 والحزمة ظاهرة في winget list")])
        question_form = bool(re.match(r"\s*(?:هل|do|does|should|can|could|is|are)(?!\w)", norm(raw_prompt), re.I)) or raw_prompt.rstrip().endswith(("؟", "?"))
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
        stripped = re.sub(r"(?<!\w)(?:ال)?(?:مجلد|folder)\s+(?:ال)?(?:جديد|جديده|new)(?!\w)", " § ", stripped)   # «مجلد جديد» وجهة لا شرط
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
        if "strip" in intents:
            return self._strip_affix(prompt, ents, strip_info)
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

    def _resolve(self, head: str, ents: Ents, names_only: bool = False, inspecting: bool = False, strict_depth: bool = False) -> Resolved:
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
            return Resolved(ask=_not_found(tok, ents).replace("لم أجد «", "لم أجد مجلداً باسم «", 1))

        # الاسم المنصَّص يطابق عنصراً موجوداً تماماً (مساراً أو اسماً)؛ وإلا «لم أجد» بنصه كاملاً، لا مطابقة جزئية بكلمات منه (old x.pdf ≠ x.pdf)
        exact_names = {norm(r) for r in ents} | {norm(r.split("/")[-1]) for r in ents}
        for qn in sorted(quoted):
            if qn and not qn.isdigit() and qn.rstrip("/") not in exact_names:
                orig = next((q.strip() for q in _QUOTE_RE.findall(head) if norm(q).strip() == qn), qn)
                return Resolved(ask=_not_found(orig, ents))

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
        depth_notes: List[str] = []
        for rel in ents:
            groups.setdefault(norm(rel.split("/")[-1]), []).append(rel)
        for nb in sorted(groups, key=len, reverse=True):
            if not nb or nb not in work:
                continue
            if nb in _EN_FUNCTION_WORDS and nb not in quoted and not re.search(r"[\u0600-\u06FF]", text):
                if nb in ("a", "an", "the"):   # «create a backup»: أداة التنكير «a» ليست الملف a إلا إن نُصِّص صراحة
                    work = re.sub(r"(?<![\w.\-])" + re.escape(nb) + r"(?![\w\-]|\.\w)", " § ", work)
                continue
            rx = _mention_re(nb)
            mm = rx.search(work)
            if not mm:
                continue
            # أسماء تختلف بحرف عربي فقط (أحمد/احمد، ي/ى، ه/ة) أو بفراغ مزدوج: ما كُتب مطابقاً تماماً لاسم موجود يُفضَّل على المطبَّع؛
            # كُتب أكثر من شكل ← يُحلّ كل شكل على حدة. لا مطابق تام ← الالتباس القديم (سؤال).
            variants: Dict[str, List[str]] = {}
            for c in groups[nb]:
                variants.setdefault(c.split("/")[-1].casefold(), []).append(c)   # فروق الحالة وحدها ليست «شكلاً آخر» (ويندوز يعدّها اسماً واحداً)
            pieces = [groups[nb]]
            if len(variants) > 1:
                exact = [v for v in variants if _mention_re(v).search(head.casefold())]
                if exact:
                    pieces = [variants[v] for v in exact]
            for cands in pieces:
                if scope:
                    cands = [c for c in cands if c.startswith(scope + "/")]
                    if not cands:
                        return Resolved(ask=f"لم أجد «{nb}» داخل «{scope}».")
                    direct = [c for c in cands if c.count("/") == scope.count("/") + 1]
                    cands = direct or cands
                else:
                    shallow = min(c.count("/") for c in cands)
                    deeper = sorted(c for c in cands if c.count("/") != shallow)
                    if deeper and strict_depth:   # حذف/إعادة تسمية باسم مجرد موجود على أكثر من عمق: لا نخمّن أيّها (التشغيل الثاني كان يصيب الآخر)
                        return Resolved(ask=f"الاسم «{nb}» موجود في أكثر من مكان ({'، '.join(sorted(cands)[:3])}). اكتب المسار الكامل.")
                    cands = [c for c in cands if c.count("/") == shallow]
                    if deeper:
                        depth_notes.append(f"اسم «{nb}» موجود أيضاً في {'، '.join(deeper[:3])}؛ اخترت «{cands[0]}» (الأضحل مساراً). اكتب المسار الكامل لغيره.")
                    if ents.partial and shallow > 0:
                        return Resolved(ask=f"قائمة مجلد العمل مقتطعة ولم أجد «{nb}» في المستوى الأول منها (وجدته داخل «{sorted(cands)[0]}» فقط)، "
                                            f"وقد يكون المقصود ملفاً في الجذر لم يظهر لي. اكتب المسار الكامل.")
                if len(cands) > 1:
                    if len({c.split("/")[-1].casefold() for c in cands}) > 1:
                        return Resolved(ask=f"في مجلد العمل أسماء متشابهة ({'، '.join(sorted(cands)[:4])}). اكتب الاسم تماماً كما يظهر (بالهمزات والياء والتاء المربوطة نفسها).")
                    return Resolved(ask=f"الاسم «{nb}» موجود في أكثر من مكان ({'، '.join(sorted(cands)[:3])}). اكتب المسار الكامل.")
                q = dir_context_ask(cands[0], mm.start(), mm.end()) or kind_clash_ask(cands[0], nb, mm.start())
                if q:
                    return Resolved(ask=q)
                if not scope and not inspecting and not names_only and ents.get(cands[0]) and "/" in cands[0] and "/" not in nb:
                    # مجلد متداخل بالاسم المجرد (docs/صور): لا نحذفه/ننقله بتخمين، تُكتب مساره الكامل
                    return Resolved(ask=f"«{nb}» مجلد داخل «{cands[0]}» وليس في الجذر. اكتب المسار الكامل إن قصدته.")
                names.append(cands[0])
            work = rx.sub(" § ", work)

        # 3) كلمات تشبه اسم ملف/مسار ولم تُطابَق ← لا نخمّن
        tok = _unmatched_filelike(work)
        if tok:
            return Resolved(ask=_not_found(tok, ents))
        if not names_only:
            for qn in sorted(quoted, key=len, reverse=True):   # اسم منصَّص لم يُطابق شيئاً ← حرفي غير موجود، لا يُفهم نوعاً (نظف «فيديو» كانت تحذف كل الفيديو)
                if qn and not qn.isdigit() and _mention_re(qn).search(work):
                    return Resolved(ask=_not_found(_orig_word(qn, head), ents))
        if not names_only:   # «مجلد X» بقي X بعد حجب المجلدات الموجودة ← لا مجلد بهذا الاسم؛ لا نُسقط كلمة «مجلد» ونفهم X نوعاً (حذف كل الصور!)
            fm = re.search(r"(?<!\w)(?:(?:ال)?مجلد|folder)\s+(?:(?:اسمه|باسم|named|called)\s+)?([^\s§]+)", work)
            if fm:
                ftok = fm.group(1).strip("،,؛;:.!؟?")
                if ftok and ftok not in _ROOT_TOKENS and ftok not in _STOP_AFTER_FROM and ftok not in ("و", "في", "من", "الى", "الي", "to", "in"):
                    return Resolved(ask=_not_found(_orig_word(ftok, head), ents).replace("لم أجد «", "لم أجد مجلداً باسم «", 1))

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
            return Resolved(kept, "name", note=" ".join(depth_notes) or None)
        if cats or exts or groups:
            sel = [r for r in pool if (EXT_TO_CAT.get(r.rsplit(".", 1)[-1].lower() if "." in r.split("/")[-1] else "") in cats)
                   or (r.rsplit(".", 1)[-1].lower() in exts and "." in r.split("/")[-1])
                   or any(pred(r.split("/")[-1]) for pred in groups)]
            # ملفات القفل ~$cv.docx يفتحها الأوفيس: لا تُختار ضمن نوع/امتداد (إلا حين تُطلب «المؤقتة» صراحة)
            # ملف القفل ~$ لمستند ما زال موجوداً قد يكون مفتوحاً الآن، وملف «اسم (1).tmp» بجانب «اسم.*» قد يكون نسخة تخص المستخدم:
            # لا يُختاران تلقائياً حتى مع «المؤقتة» (يُسمّيان صراحة). القفل اليتيم (بلا مستنده) يُحذف كالمعتاد.
            base_of = lambda r: r.split("/")[-1]
            others = [base_of(r).lower() for r in pool if not base_of(r).startswith("~$")]
            def owner_present(r):
                tail = base_of(r)[2:].lower()
                return len(tail) >= 3 and any(o.endswith(tail) for o in others)
            locks = [r for r in sel if base_of(r).startswith("~$") and (_is_temp not in groups or owner_present(r))]
            sel = [r for r in sel if r not in locks]
            def dupish_of_real(r):
                m = re.match(r"^(.*) \(\d+\)\.[^.]+$", base_of(r))
                return bool(m) and _is_temp in groups and any(o.rsplit(".", 1)[0].lower() == m.group(1).lower()
                                                           or o.startswith(m.group(1).lower() + " (") for o in others if o != base_of(r).lower())
            dupish = [r for r in sel if dupish_of_real(r)]
            sel = [r for r in sel if r not in dupish]
            return Resolved(sorted(dict.fromkeys(sel)), "cat" if (cats or groups) and not exts else "ext", kw=kw_names,
                            note=_join_notes(_skip_notes([], sorted(locks)), _skip_dup_note(sorted(dupish))))
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
        explicit_new = False

        def known_at(t: str):
            nt = norm(t)
            return next((d for d in sorted(top_dirs, key=len, reverse=True)
                         if nt.startswith(norm(d)) and not re.match(r"[\w\-]", nt[len(norm(d)):len(norm(d)) + 1] or " ")), None)
        known = known_at(tail)  # مجلد موجود اسمه «مجلد جديد» يُطابَق قبل قصّ كلمة «مجلد»
        if known:
            dest, rest = tail[:len(known)], tail[len(known):]
        else:
            m_new = re.match(r"^(?:(?:ال)?مجلد|folder)\s+(?:(?:ال)?جديد[ةا]?\s+|new\s+)?(?:(?:اسمه|باسم|بإسم|بأسم|المسمى|named|called)\s*[:：]?\s*)?", tail, flags=re.I)
            if m_new and re.search(r"جديد|new", m_new.group(0), re.I) and not re.search(r"اسمه|باسم|بإسم|بأسم|المسمى|named|called", m_new.group(0), re.I):
                nxt = norm(re.sub(r"[،,؛;.!؟?]+", " ", tail[m_new.end():])).split()
                if not nxt or nxt[0] in ("الان", "now", "please", "لو", "من", "شكرا", "رجاء", "ثم", "و"):   # «NEW» هنا اسم المجلد نفسه لا وصفه
                    m_new = re.match(r"^(?:(?:ال)?مجلد|folder)\s+", tail, flags=re.I)
            if m_new:
                explicit_new = bool(re.search(r"جديد|new|اسمه|باسم|بإسم|بأسم|المسمى|named|called", m_new.group(0), re.I))
                tail = tail[m_new.end():]
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
        if nn in ("جديد", "جديده", "new") and not any(norm(d) == nn for d in top_dirs):
            return None, "ما اسم المجلد الجديد الذي تريد إنشاءه؟ اكتب اسمه (مثل Reports، أو بين تنصيص إن كان من عدة كلمات).", head
        if nn in _GENERIC_DEST and not any(norm(d) == nn for d in top_dirs):
            return None, f"«{name}» ليس اسم مجلد. ما اسم المجلد الذي تريده؟ (إن كان من عدة كلمات فاكتبه بين تنصيص)", head
        for d in top_dirs:  # ويندوز لا يميّز حالة الأحرف، ونطابق بالتوحيد نفسه المستعمل للمصادر (أ/ا، ى/ي، ة/ه، الأرقام الهندية)
            if norm(d) == nn:
                return d, None, head
        if "." in name and name.rsplit(".", 1)[-1].lower() in _KNOWN_EXTS:
            return None, f"«{name}» يبدو اسم ملف لا مجلد. اكتب اسم مجلد الوجهة.", head
        if any(norm(f) == nn for f in top_files):
            return None, f"يوجد ملف باسم «{name}» فلا أستطيع إنشاء مجلد بالاسم نفسه. اختر اسماً آخر.", head
        if not quoted and not explicit_new:
            if re.fullmatch(r"%[^%]+%|\$\w+|\$\{[^}]+\}|~", name):
                return None, f"«{name}» يبدو متغير بيئة أو اختصاراً لمسار، ولا أفسّره. اكتب اسم مجلد داخل مجلد العمل.", head
            near = _near_dir(name, top_dirs)
            if near:   # لا ننشئ مجلداً بصمت بخطأ إملائي: «Dcos» ≠ «Docs»
                return None, (f"لا يوجد مجلد باسم «{name}». هل تقصد «{near}»؟ اكتب اسم المجلد الصحيح، "
                              f"أو اكتب «{name}» بين علامتي تنصيص إن أردت إنشاء مجلد جديد بهذا الاسم."), head
        return name, None, head

    # ---------- النوايا ----------
    def _organize(self, ents: Ents) -> Analysis:
        all_files = [r for r, d in ents.items() if not d and "/" not in r and _visible(r)]
        locks = [f for f in all_files if f.startswith("~$")]   # ملفات قفل برامج الأوفيس: لا تُنقل
        files = [f for f in all_files if f not in locks]
        root_gap = ents.partial and not ents.root_complete   # ملفات الجذر نفسها قد تكون ناقصة في القائمة
        if not files:
            if root_gap:
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
            if root_gap:
                return Analysis(question=PARTIAL_QUESTION)
            return _info("كل الملفات مرتّبة أو وجهاتها موجودة أصلاً." + (" " + extra if extra else ""))
        steps, i = [], 1
        for c in cats:
            steps.append(_step(i, "create_dir", c, "وجود المجلد")); i += 1
        for f, c in moves:
            steps.append(_step(i, "move", f, "المصدر اختفى والوجهة مطابقة", dest=f"{c}/{f}")); i += 1
        return Analysis(steps, note=_join_notes(_trunc_note(len(moves), dropped, root_gap), extra))

    def _rename(self, prompt: str, ents: Ents) -> Analysis:
        """إعادة تسمية كل ملفات مجلد (أو مجلد العمل) بإضافة بادئة. لا يدعم اختيار ملفات بعينها: يسأل بدل أن يعمّم.
        أي كلمة باقية لا نفهمها (مجلد بتهجئة خاطئة، ملف غير موجود، «للملف الأول»…) ← سؤال، لا رجوع صامت لكل ملفات الجذر."""
        dirs = sorted((r for r, d in ents.items() if d), key=len, reverse=True)
        m = _prefix_match(prompt)
        # المقطع الذي يحوي البادئة يُحجب كله، وما يبقى هو ما نبحث فيه عن المجلد والكلمات الزائدة
        rest = norm(_QUOTE_CHARS.sub(" ", prompt[:m.start()] + " § " + prompt[m.end():])) if m else norm(_QUOTE_CHARS.sub(" ", prompt))
        prefix = _safe_name(_prefix_text(m)) if m else None
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
            return Analysis(question=_not_found(bad, ents))
        if re.search(r"(?<!\w)(?:ال)?ملف\s+(?:ال)?(?:حالي|سابق|اول|اخير|تالي|ثاني)(?!\w)", rest):
            return Analysis(question="البادئة تُضاف لكل ملفات المجلد لا لملف بعينه («الملف الحالي/الأول…»). اذكر مجلداً، أو سمِّ الملف بالاسم لإعادة تسميته وحده.")
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

    def _strip_affix(self, prompt: str, ents: Ents, info) -> Analysis:
        """إزالة بادئة/لاحقة من أسماء ملفات مجلد (أو الجذر): تُعاد تسمية الملفات التي تحملها فقط؛ لا شيء منها ← لا شيء للتنفيذ."""
        kind, affix, a0, a1 = info
        word = "البادئة" if kind == "prefix" else "اللاحقة"
        if not affix:
            return Analysis(question=f"ما {word} التي تريد حذفها من أسماء الملفات؟ مثال: «احذف {word} old_»", slot=f"strip_{kind}")
        if not _safe_name(affix):
            return Analysis(question=f"{word} غير صالحة (بلا مسارات ولا رموز ممنوعة). اكتبها مثل «old_».")
        rest = norm(_QUOTE_CHARS.sub(" ", prompt[:a0] + " § " + prompt[a1:]))
        dirs = sorted((r for r, d in ents.items() if d), key=len, reverse=True)
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
        for rel, is_dir in ents.items():
            if not is_dir and _mention_re(norm(rel.split("/")[-1])).search(rest):
                return Analysis(question=f"حذف {word} يُطبَّق على كل ملفات المجلد التي تحملها، لا على ملفات بعينها. اذكر مجلداً بدل اسم ملف.")
        bad = _unmatched_filelike(rest)
        if bad:
            return Analysis(question=_not_found(bad, ents))
        tok = _unknown_token(rest, _RENAME_FILL | {"نزع", "انزع", "ازل", "ازاله", "ازال", "شيل", "امسح", "مسح", "حذف", "بحذف", "remove", "strip"}, pre="وبل")
        if tok:
            return Analysis(question=(f"لم أفهم «{_orig_word(tok, prompt)}» في الطلب. اذكر اسم مجلد موجود كما يظهر، "
                                      f"أو اكتب «احذف {word} {affix}» وحدها لملفات مجلد العمل."))
        if folder:
            files = [e for e, d in ents.items() if not d and e.rsplit("/", 1)[0] == folder and "/" in e]
        else:
            files = [e for e, d in ents.items() if "/" not in e and not d]
        files = [f for f in files if _visible(f)]
        locks = [f for f in files if f.split("/")[-1].startswith("~$")]
        files = [f for f in files if f not in locks]
        existing = {r.casefold() for r in ents}
        planned, taken, skipped = [], set(), []
        af = affix.casefold()
        for f in sorted(files, key=str.lower):
            parent, base = (f.rsplit("/", 1) if "/" in f else ("", f))
            new = None
            if kind == "prefix":
                if base.casefold().startswith(af):
                    new = base[len(affix):]
            elif affix.startswith("."):
                if base.casefold().endswith(af) and len(base) > len(affix):
                    new = base[:-len(affix)]
            else:
                stem, ext = (base.rsplit(".", 1)[0], "." + base.rsplit(".", 1)[1]) if "." in base[1:] else (base, "")
                if stem.casefold().endswith(af) and len(stem) > len(affix):
                    new = stem[:-len(affix)] + ext
            if new is None:
                continue
            safe = _safe_name(new)
            full = (parent + "/" if parent else "") + (safe or "")
            if not safe or (kind == "prefix" and new.startswith(".")) or full.casefold() in existing or full.casefold() in taken:
                skipped.append(f)
                continue
            taken.add(full.casefold())
            planned.append((f, safe))
        if not planned:
            if ents.partial:
                return Analysis(question=PARTIAL_QUESTION)
            where = f" داخل {folder}" if folder else ""
            if skipped:
                return _info(f"الملفات التي تحمل {word} «{affix}»{where} لا يمكن إزالتها لأن الاسم الناتج موجود أصلاً أو غير صالح: " + "، ".join(skipped[:5]) + ".")
            return _info(f"لا توجد ملفات {'تبدأ بـ' if kind == 'prefix' else 'تنتهي بـ'}«{affix}»{where}.")
        dropped = max(0, len(planned) - MAX_AUTO_STEPS)
        planned = planned[:MAX_AUTO_STEPS]
        steps = [_step(i, "rename", f, "الاسم الجديد موجود والمحتوى مطابق", new_name=n) for i, (f, n) in enumerate(planned, 1)]
        extra = None
        if skipped:
            extra = f"تُرك {_n(len(skipped), 'ملف')} لأن الاسم بعد الإزالة موجود أصلاً أو غير صالح: " + "، ".join(skipped[:3]) + ("…" if len(skipped) > 3 else "") + "."
        return Analysis(steps, note=_join_notes(_trunc_note(len(steps), dropped, ents.partial), extra, _skip_notes([], locks)))

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
        for qn in _QUOTE_RE.findall(head):   # الاسم المنصَّص يطابق عنصراً موجوداً تماماً (مساراً أو اسماً)؛ لا تطابق جزئي بكلمات (old x.pdf ≠ x.pdf)
            nq = norm(qn).strip()
            if nq and not any(norm(r) == nq or norm(r.split("/")[-1]) == nq for r in ents):
                return Analysis(question=_not_found(qn.strip(), ents))
        res = self._resolve(head, ents, names_only=True, strict_depth=True)
        if res.ask:
            return Analysis(question=res.ask)
        stray = _unknown_token(W_RENAME.sub(" ", self._mask(head, ents)), _FILLER | {"اسم", "الاسم", "اسمه", "الي"})
        if stray:   # كلمة زائدة بين اسم الملف والوجهة (غير اسم a.txt الباقي إلى n.txt): لا نتجاهلها بصمت
            return Analysis(question=f"لم أفهم «{_orig_word(stray, head)}» في الطلب. سمِّ الملف المراد إعادة تسميته باسمه كما يظهر فقط.")
        if len(res.items) != 1:
            return Analysis(question="حدّد عنصراً واحداً لإعادة تسميته باسمه كما يظهر في مجلد العمل.")
        src = res.items[0]
        name = _safe_name(new)
        if not name:
            return Analysis(question="الاسم الجديد غير صالح (بلا مسارات ولا رموز ممنوعة ولا أسماء ويندوز المحجوزة).")
        base = src.split("/")[-1]
        if name == base:
            return _info("الاسم الجديد مطابق للاسم الحالي؛ لا شيء لتغييره.")
        if "." in base and not ents.get(src) and "." not in name:
            return Analysis(question=f"الاسم الجديد «{name}» بلا امتداد. هل تقصد «{name}.{base.rsplit('.', 1)[-1]}»؟ اكتبه كاملاً.")
        new_ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if new_ext in _EXEC_EXTS and new_ext != (base.rsplit(".", 1)[-1].lower() if "." in base else ""):
            return Analysis(question=f"الاسم الجديد «{name}» يحوّل الملف إلى نوع قابل للتشغيل (.{new_ext}). لا أغيّر الامتداد إلى نوع تنفيذي بهذا الأمر؛ اختر اسماً بامتداد عادي، أو غيّره بنفسك إن كنت تقصد ذلك فعلاً.")
        parent = src.rsplit("/", 1)[0] + "/" if "/" in src else ""
        if (parent + name).casefold() in {r.casefold() for r in ents} and name.casefold() != base.casefold():
            return Analysis(question=f"يوجد «{name}» أصلاً في المكان نفسه. اختر اسماً آخر.")
        return Analysis([_step(1, "rename", src, "الاسم الجديد موجود والمحتوى مطابق", new_name=name)])

    def _offer_files(self, head: str, ents: Ents) -> str:
        """«الملفات الظاهرة في <النطاق>: …» لتلحق سؤال «ما الذي تريد…؟» فيختار المستخدم من القائمة (النطاق: مجلد «من/في X» في الطلب وإلا الجذر)."""
        text = norm(_QUOTE_CHARS.sub(" ", head))
        dirs = sorted((r for r, d in ents.items() if d), key=len, reverse=True)
        scope = None
        for m in _SCOPE_RE.finditer(text):
            hit, ask, _ = self._dir_at(text[m.end():], dirs)
            if hit:
                scope = hit
                break
        pool = sorted((r for r, d in ents.items() if not d and _visible(r) and
                       ((r.startswith(scope + "/") and r.count("/") == scope.count("/") + 1) if scope else "/" not in r)), key=lambda r: r.casefold())
        if not pool:
            return ""
        names = [r.split("/")[-1] for r in pool]
        shown = "، ".join(names[:10]) + (f" … و{_n(len(names) - 10, 'ملف')} أخرى" if len(names) > 10 else "")
        where = f"«{scope}»" if scope else "مجلد العمل"
        part = " (القائمة مقتطعة)" if ents.partial else ""
        return f" الملفات الظاهرة في {where}{part}: {shown}."

    @staticmethod
    def _ask(question: str) -> Analysis:
        """سؤال القواعد مع نوع الخانة الناقصة إن عُرف (ليحوّل المتحكم الجواب العاري إلى جملة صريحة)."""
        slot = "dest" if (question.startswith(("ما اسم المجلد الذي تريد الوجهة", "إلى أي مجلد", "اسم المجلد يبدو من أكثر", "ما اسم المجلد الجديد الذي", "لا يوجد مجلد باسم"))
                          or any(k in question for k in ("ليس اسم مجلد", "يبدو متغير بيئة", "يبدو اسم ملف لا مجلد", "فلا أستطيع إنشاء مجلد بالاسم"))) else None
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
            return Analysis(question="ما اسم الملف أو المجلد الذي تريد نسخه؟ اكتبه كما يظهر في مجلد العمل." + self._offer_files(head, ents), slot="source")
        if len(res.items) > MAX_AUTO_STEPS - 1:
            return Analysis(question=f"عدد العناصر كبير ({_n(len(res.items), 'عنصر')}). حدّد مجموعة أصغر.")
        if not dest_dir:   # «انسخ report.pdf» بلا وجهة: نسأل ولا نخمّن مجلداً (للنسخة الاحتياطية عبارة صريحة: «اعمل نسخة احتياطية من …»)
            return Analysis(question=("إلى أي مجلد تريد النسخ؟ مثال: «انسخ report.pdf إلى Backup». "
                                      "وللنسخة الاحتياطية اكتب «اعمل نسخة احتياطية من report.pdf»."), slot="dest")
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
            return Analysis(question="ما الذي تريد نقله؟ اكتب اسم الملف/المجلد أو نوعه (مثل pdf أو الصور)." + self._offer_files(head, ents), slot="source")
        if len(res.items) > MAX_AUTO_STEPS - 1:
            return Analysis(question=f"عدد العناصر كبير ({_n(len(res.items), 'عنصر')}). حدّد مجموعة أصغر.")
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

    def _same_tree(self, a: str, b: str) -> bool:
        """مجلدان متطابقان (الأسماء والأحجام وSHA-256 لكل ملف)؟ False عند أي شك (رابط، كبير، غير متاح)."""
        if self.workspace is None:
            return False

        def walk(rel: str):
            root = (self.workspace / rel).resolve()
            if self.workspace not in root.parents or not root.is_dir():
                return None
            out: Dict[str, Optional[Path]] = {}
            for dp, dn, fn in os.walk(root):
                for d in dn:
                    q = Path(dp) / d
                    if _is_link(q):
                        return None
                    out[q.relative_to(root).as_posix() + "/"] = None
                for f in fn:
                    q = Path(dp) / f
                    if _is_link(q):
                        return None
                    out[q.relative_to(root).as_posix()] = q
                if len(out) > 3000:
                    return None
            return out
        try:
            wa, wb = walk(a), walk(b)
            if wa is None or wb is None or wa.keys() != wb.keys():
                return False
            for k, fa in wa.items():
                if fa is None:
                    continue
                fb = wb[k]
                if fa.stat().st_size != fb.stat().st_size or fa.stat().st_size > self.MAX_HASH_BYTES:
                    return False
                check_not_big_cloud_file(fa)
                check_not_big_cloud_file(fb)
                ha, hb = hashlib.sha256(), hashlib.sha256()
                for fp, h in ((fa, ha), (fb, hb)):
                    with open(fp, "rb") as fh:
                        while chunk := fh.read(1 << 20):
                            h.update(chunk)
                if ha.digest() != hb.digest():
                    return False
            return True
        except (OSError, CloudFileNotLocalError):
            return False

    def _identical_copy(self, src: str, parent: str, base: str, ents: Ents) -> Optional[str]:
        """نسخة مطابقة المحتوى لـ src (ملف أو مجلد بكل شجرته) موجودة في parent باسم base أو «base (n)»؟ تعيد اسمها أو None."""
        if src not in ents:
            return None
        is_dir = ents[src]
        stem, suffix = (base.rsplit(".", 1)[0], "." + base.rsplit(".", 1)[1]) if ("." in base[1:] and not is_dir) else (base, "")
        pat = re.compile(re.escape(stem.casefold()) + r"(?: \(\d+\))?" + re.escape(suffix.casefold()) + r"$")
        pre = (parent + "/") if parent else ""
        for r, d in ents.items():
            if d != is_dir or not r.casefold().startswith(pre.casefold()) or "/" in r[len(pre):]:
                continue
            if pat.fullmatch(r[len(pre):].casefold()) and (self._same_tree(src, r) if is_dir else self._same_content(src, r)):
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
            return Analysis(question=f"عدد العناصر كبير ({_n(len(res.items), 'عنصر')}). حدّد مجموعة أصغر.")
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
        moves, folders, clash, odd_dates = [], [], [], []
        names_cf = {x.casefold() for x in files}
        for f in sorted(files, key=str.lower):
            st = self._stat(f)
            if not st:
                continue
            d, odd = _mtime_date(st[1].st_mtime)
            if odd:
                odd_dates.append(f)
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
        extra = _join_notes(_skip_notes(clash, locks),
                            (f"تاريخ تعديل {_n(len(odd_dates), 'ملف')} غير اعتيادي (قبل 1970 أو خارج النطاق) فصُنِّفت بأقرب تاريخ صالح: "
                             + "، ".join(odd_dates[:3]) + (" …" if len(odd_dates) > 3 else "") + ".") if odd_dates else None)
        root_gap = ents.partial and not ents.root_complete
        if not moves:
            if root_gap:
                return Analysis(question=PARTIAL_QUESTION)
            return _info("لا توجد ملفات لترتيبها حسب التاريخ في مجلد العمل." + (" " + extra if extra else ""))
        steps, i = [], 1
        for k in folders:
            steps.append(_step(i, "create_dir", k, "وجود المجلد")); i += 1
        for f, k in moves:
            steps.append(_step(i, "move", f, "المصدر اختفى والوجهة مطابقة", dest=f"{k}/{f}")); i += 1
        return Analysis(steps, note=_join_notes(_trunc_note(len(moves), dropped, root_gap), extra))

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
        if delete and len(dups) > MAX_AUTO_DELETE_STEPS - 1 or len(dups) > MAX_AUTO_STEPS - 1:
            return Analysis(question=f"وجدت {_n(len(dups), 'ملف')} مكرراً وهذا أكثر من حد الخطة. نظّف جزءاً منها ثم أعد الطلب.")
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
        more = f" … و{_n(len(groups) - 12, 'مجموعة')} أخرى" if len(groups) > 12 else ""
        n_dup = sum(len(g) - 1 for g in groups)
        note = f"وجدت {_n(len(groups), 'مجموعة')} مكررة ({_n(n_dup, 'نسخة')} زائدة)؛ لم يُحذف أو يُنقل شيء: " + "؛ ".join(shown) + more
        if ents.partial:
            note += ". المسح جزئي لأن مجلد العمل كبير، فقد توجد مكررات أخرى لم أفحصها"
        members = [m for g in groups for m in g][:MAX_AUTO_STEPS]
        return Analysis([_step(i, "inspect", m, "العنصر موجود فعلاً") for i, m in enumerate(members, 1)],
                        note=_join_notes(note, _trunc_note(len(members), max(0, sum(len(g) for g in groups) - len(members)))))

    def _delete(self, prompt: str, text: str, ents: Ents) -> Analysis:
        """الحذف = نقل إلى .trash (قابل للتراجع) ويحتاج موافقة لكل خطوة. لا حذف شامل ولا «محتويات» بلا تحديد."""
        if W_CONTENTS.search(text):
            return Analysis(question="حذف «محتويات» مجلد غير مدعوم تلقائياً. سمِّ الملفات بأسمائها أو اطلب نوعاً (مثل «الصور»).")
        res = self._resolve(prompt, ents, strict_depth=True)
        if res.ask:
            return Analysis(question=res.ask)
        if re.search(r"(?<!\w)(?:نظف|نظفها|تنظيف|clean|cleanup)(?!\w)", text) and res.kind == "name":
            dirs_ = [t for t in res.items if ents.get(t)]
            if dirs_:   # «نظف Docs» لا تعني حذف المجلد كله: نسأل عن المقصود
                d0 = dirs_[0]
                return Analysis(question=(f"«نظّف {d0}» تحتمل أكثر من معنى. اختر واحداً واكتبه صراحة: "
                                          f"حذف المجلد كله («احذف مجلد {d0}»)، أو حذف الملفات المؤقتة داخله فقط («احذف الملفات المؤقتة من {d0}»)، "
                                          f"أو ترتيب ملفات مجلد العمل («رتب الملفات حسب النوع»)."))
        if ents.partial and res.kind in ("cat", "ext"):
            return Analysis(question=PARTIAL_QUESTION)
        if W_ALL.search(text) and (not res.items or any(ents.get(t) for t in res.items)):
            return Analysis(question="الحذف الشامل غير مدعوم تلقائياً. سمِّ ما تريد حذفه بالاسم أو بالنوع (مثل «الصور»).")
        if not res.items:
            return Analysis(question="ما الملف أو المجلد الذي تريد حذفه؟ اكتب اسمه كما يظهر في مجلد العمل." + self._offer_files(prompt, ents), slot="source")
        if len(res.items) > MAX_AUTO_DELETE_STEPS:
            return Analysis(question=f"عدد العناصر كبير ({_n(len(res.items), 'عنصر')}). حدّد مجموعة أصغر للحذف.")
        steps = [_step(i, "delete", t, "العنصر اختفى من مكانه وصار في السلة بنفس المحتوى")
                 for i, t in enumerate(sorted(res.items), 1)]
        note = res.note
        if "المؤقتة" in res.kw:   # «المؤقتة» تقدير بالمعنى: تُعرض أسماء ما سيُحذف قبل الموافقة
            names = sorted(res.items)
            note = _join_notes("ستُحذف هذه الملفات المؤقتة (كل حذف يطلب موافقتك ويمكن التراجع عنه): " + "، ".join(names[:30])
                               + (f" … و{_n(len(names) - 30, 'ملف')} أخرى" if len(names) > 30 else "") + ".", note)
        return Analysis(steps, note=note)

    def _create(self, prompt: str, ents: Ents) -> Analysis:
        # «أنشئ مجلد Tests داخل Docs»: المجلد الجديد داخل مجلد موجود فعلاً
        pm = re.search(r"(?<!\w)(?:داخل|في|ضمن|جوه|بداخل|inside|in)\s+(?:(?:ال)?مجلد\s+|folder\s+)?(?P<d>[^\s«»\"'،,؛;/]+)\s*[.!؟?]*$", prompt, re.I)
        if pm:
            dn = norm(pm.group("d"))
            par = [r for r, d in ents.items() if d and "/" not in r and (norm(r) == dn or norm(r) == "ال" + dn or "ال" + norm(r) == dn)]
            if len(par) == 1 and not re.search(r"(?<!\w)(?:داخل|في|ضمن|جوه|بداخل|inside|in)\s", prompt[:pm.start()], re.I):
                sub = self._create(prompt[:pm.start()], ents)
                if sub.steps and all(st["action_type"] == "create_dir" for st in sub.steps):
                    steps = [dict(st, target=par[0] + "/" + st["target"]) for st in sub.steps]
                    clash = [st["target"] for st in steps if st["target"].casefold() in {r.casefold() for r in ents}]
                    if clash and len(clash) == len(steps):
                        return _info(f"المجلد «{clash[0]}» موجود بالفعل.")
                    return Analysis(steps, note=sub.note)
                if not sub.steps and sub.question is not None and sub.question != HELP_QUESTION and not sub.question.startswith("لم أفهم"):
                    return sub
        # «أنشئ مجلد A ومجلد B»: كل مجلد خطوة مستقلة (لا يُدمج الاسمان في اسم واحد)
        if not _QUOTE_RE.search(prompt):
            parts = re.split(r"(?<=\s)و(?:ال)?(?=مجلد\s|فولدر\s)", prompt)
            if len(parts) >= 2:
                outs = [self._create(parts[0], ents)] + [self._create("انشئ " + x.strip(), ents) for x in parts[1:]]
                if all(o.steps for o in outs):
                    merged, seen, notes = [], set(), []
                    for o in outs:
                        for st in o.steps:
                            if st["target"] not in seen:
                                seen.add(st["target"])
                                merged.append(dict(st, step_id=len(merged) + 1))
                        if o.note:
                            notes.append(o.note)
                    return Analysis(merged, note=_join_notes(*dict.fromkeys(notes)) if notes else None)
                for o in outs:
                    if not o.steps:
                        return o
        name = None
        a_name = False
        q = _QUOTE_RE.search(prompt)
        raw_used, lead_proj = "", False
        if q:
            name, raw_used = q.group(1).strip(), q.group(0)
        else:
            m = (re.search(r"(?<![\w])(?:باسم|بإسم|بأسم|اسمه|اسمها|يسمى|يسمي|مسمى|مسمي|المسمى|المسمي|بعنوان|عنوانه|titled|named|nammed|namd|called|name)\s*[:：]?\s*", prompt, re.I)
                 or re.search(r"(?:مشروع|مجلد|هيكل مشروع|folder|project)[اً]?\s+(?:(?:جديد[اة]?|فرعي[اة]?)\s+)*(?:new\s+)?", prompt, re.I))
            if m:
                # الاسم غير المنصَّص يمتد على كل الكلمات التالية حتى أول كلمة حشو/وجهة (مثل «فواتير 2025»)
                toks = []
                for t in re.finditer(r"[^\s«»\"'،,؛;]+", prompt[m.end():]):
                    nt = norm(t.group(0))
                    if not toks and nt == "المشروع":
                        lead_proj = True
                        continue   # «مجلد المشروع X»: الاسم X والنوع مشروع
                    if not toks and nt in ("مشروع", "project") and re.match(r"(?:مجلد|folder)\s", m.group(0), re.I) and prompt[m.end() + t.end():].strip():
                        return Analysis(question=("«مجلد مشروع X» يحتمل معنيين: مشروع اسمه X (هيكل src/docs/tests)، أو مجلد عادي اسمه «مشروع X». "
                                                  "اكتب «أنشئ مشروع X» أو «أنشئ مجلد «مشروع X»»."), slot="folder_name")
                    if not toks and nt in ("a", "an") and not prompt[m.end() + t.end():].strip(" .!؟?،"):
                        toks.append(t)   # «مجلد A»: الحرف الأخير هو الاسم لا أداة تنكير
                        a_name = True
                        break
                    if (nt in _CREATE_FILLER or nt == "و" or nt.strip(".!؟?") in _CREATE_FILLER or nt in _CREATE_STOP
                            or _FILELIKE_I.search(t.group(0)) or ("/" in t.group(0) and not re.fullmatch(r"[^\s/«»\"]+(?:/[^\s/«»\"]+){1,5}", t.group(0)))
                            or any(w.search(nt) for w in (W_DELETE, W_MOVE, W_COPY, W_PUT, W_CREATE, W_THEN))):
                        break
                    toks.append(t)
                if toks:
                    seg = prompt[m.end():][toks[0].start():toks[-1].end()]
                    name, raw_used = _unq(seg), seg
        if not name or (name.lower() in _CREATE_GENERIC and not (a_name and name.isupper())):
            return Analysis(question="ما اسم المجلد أو المشروع الذي تريد إنشاءه؟ مثال: «أنشئ مجلد Reports»", slot="folder_name")
        # لا نُسقط جزءاً من الطلب بصمت: أي كلمات غير مفهومة بعد الاسم ← سؤال
        left = norm(prompt.replace(raw_used, " § ", 1))
        # «مشروع» تُحدِّد نوع الإنشاء من فعل الطلب وموضوعه فقط، لا من كلمة داخل الاسم نفسه (مجلد «مشروع التخرج» أو project_files مجلد عادي)
        is_project = bool(re.search(r"(?<!\w)(?:ال)?(?:مشروع|هيكل|project)ا?(?!\w)", left))
        subs = re.findall(r"(?<![\w.])و?(src|docs|tests)(?![\w.])", left)
        if set(subs) == {"src", "docs", "tests"}:   # «مشروع X فيه src وdocs وtests»: هي نفسها الهيكل الافتراضي، لا أسماء مجهولة
            left = re.sub(r"(?<![\w.])(?:و?(?:src|docs|tests)|with|مع|فيه|فيها|بي|بيه|containing|contains|includes|يحتوي|تحتوي|folders?|مجلدات|and)(?![\w.])|(?<!\w)و(?=\s|$)", " ", left)
            is_project = True
        for tok in re.findall(r"[^\W_]+", left):
            if tok in ("§", "و") or tok in _CREATE_FILLER or (tok.startswith("و") and tok[1:] in _CREATE_FILLER):
                continue
            return Analysis(question=f"لم أفهم «{_orig_word(tok, prompt)}» في طلب الإنشاء. إن كان اسم المجلد من عدة كلمات فاكتبه بين علامتي تنصيص: «أنشئ مجلد «صور قديمة»».")
        if "/" in name and (":" in name or name.startswith("/") or name.endswith("/")):
            return Analysis(question="مسار المجلد غير صالح (لا مسارات مطلقة ولا أقراص). اكتب اسماً أو مساراً نسبياً داخل مجلد العمل مثل «Reports/2024».")
        if "/" in name and is_project:
            return Analysis(question="مسار المشروع غير مدعوم هنا. اكتب اسماً واحداً للمشروع («أنشئ مشروع App»)، أو أنشئ المجلد الأب أولاً ثم المشروع.")
        if "/" in name:   # «أنشئ مجلد a/b/c»: سلسلة مجلدات داخل مجلد العمل؛ ننشئ ما ليس موجوداً منها فقط
            comps = [_safe_name(c) for c in name.split("/")]
            if len(comps) > 6 or not all(comps) or any("." in c and c.rsplit(".", 1)[-1].lower() in _KNOWN_EXTS for c in comps):
                return Analysis(question="مسار المجلد غير صالح (أسماء عادية مفصولة بـ / بلا نقاط أو رموز ممنوعة، وبعمق أقصاه 6).")
            have = {r.casefold(): d for r, d in ents.items()}
            steps, acc = [], ""
            for c in comps:
                acc = acc + "/" + c if acc else c
                if acc.casefold() in have:
                    if not have[acc.casefold()]:
                        return Analysis(question=f"يوجد ملف باسم «{acc}» فلا أنشئ مجلداً بالمسار نفسه. اختر اسماً آخر.")
                    continue
                steps.append(_step(len(steps) + 1, "create_dir", acc, "وجود المجلد"))
            if not steps:
                return _info(f"المجلد «{'/'.join(comps)}» موجود بالفعل في مجلد العمل.")
            return Analysis(steps)
        safe = _safe_name(name)
        if not safe or (safe.rsplit(".", 1)[-1].lower() in _KNOWN_EXTS and "." in safe):
            return Analysis(question="اسم المجلد غير صالح (بلا مسارات ولا رموز ممنوعة ولا اسم يشبه ملفاً). اكتب اسماً عادياً.")
        if any(not d and "/" not in r and norm(r) == norm(safe) for r, d in ents.items()):
            return Analysis(question=f"يوجد ملف باسم «{safe}» فلا أنشئ مجلداً بالاسم نفسه. اختر اسماً آخر.")
        if not is_project and any(d and "/" not in r and norm(r) == norm(safe) for r, d in ents.items()):
            return _info(f"المجلد «{safe}» موجود بالفعل في مجلد العمل.")
        where = None
        if re.search(r"(?<!\w)(?:على|علي|عند|في|داخل|ضمن|on|at|in)\s+(?:the\s+)?(?:ال)?(?:سطح\s+(?:ال)?مكتب\w*|مكتب\w*|desktop)(?!\w)", left):
            where = "أُنشئ المجلد داخل مجلد العمل الحالي (أعمل داخله فقط، فلا أصل إلى سطح مكتب آخر)."
        if lead_proj and name and name == _unq(name):
            where = _join_notes(where, f"فهمتُ «{safe}» اسماً للمشروع. إن أردت مجلداً اسمه «مشروع {safe}» فاكتبه بين علامتي تنصيص.")
        if is_project:
            return Analysis([
                _step(1, "create_dir", f"{safe}/src", "وجود المجلد"),
                _step(2, "create_dir", f"{safe}/docs", "وجود المجلد"),
                _step(3, "create_dir", f"{safe}/tests", "وجود المجلد"),
            ], note=where)
        return Analysis([_step(1, "create_dir", safe, "وجود المجلد")], note=where)

    def _inspect(self, prompt: str, ents: Ents) -> Analysis:
        # «اعرض ملفات pdf» / «اعرض الصور [من Docs]»: عرض ملفات النوع بدل «لم أجد pdf»
        full = self._resolve(W_INSPECT.sub(" ", norm(prompt)), ents, inspecting=True)
        if not full.ask and full.kind in ("cat", "ext"):
            if ents.partial:
                return Analysis(question=PARTIAL_QUESTION)
            if not full.items:
                return _info("لا توجد ملفات من هذا النوع" + (" في المجلد المذكور." if _SCOPE_RE.search(norm(prompt)) else " في مجلد العمل."))
            shown = full.items[:MAX_AUTO_STEPS]
            return Analysis([_step(i, "inspect", t, "العنصر موجود فعلاً") for i, t in enumerate(shown, 1)],
                            note=_join_notes(f"عدد الملفات المعروضة: {len(shown)}.",
                                             _trunc_note(len(shown), max(0, len(full.items) - MAX_AUTO_STEPS)), full.note))
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
            if tok in _ALL_CAT_EXTS:   # امتداد معروف لا ملفات منه: لا شيء للعرض (لا «لم أجد»)
                return _info(f"لا توجد ملفات {tok} في مجلد العمل.")
            return Analysis(question=_not_found(tok, ents, "، أو اطلب «افحص» وحدها لفحص مجلد العمل كله"))
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
