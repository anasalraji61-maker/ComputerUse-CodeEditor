"""أدوات الملفات الآمنة مع قيد المجلد الجذر وحماية المسارات وسجل التراجع ومنع الحذف النهائي."""
import contextlib
import json
import os
import shutil
import hashlib
import hmac
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Union, List, Dict, Any, Optional, Tuple

from assistant.safety import KillSwitch, EmergencyStopTriggered


_WIN_RESERVED = ({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                 | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
                 | {f"COM{c}" for c in "¹²³"} | {f"LPT{c}" for c in "¹²³"})
_PROTECTED_NAMES = {".git", ".ssh", ".gnupg", ".aws"}  # لا تُمَس عبر خطط المساعد حتى لو وُجدت داخل مجلد العمل
_REPARSE_FLAG = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (روابط/تقاطعات ويندوز)
_LINK_REPARSE_TAGS = {0xA000000C, 0xA0000003}  # IO_REPARSE_TAG_SYMLINK / MOUNT_POINT (junction)


_ILLEGAL_NAME_CHARS = frozenset('<>"|?*')   # محارف ممنوعة في أسماء ويندوز: نرفضها على كل المنصات (خطة تُنفَّذ هنا قد تُنقل لويندوز)


def _bad_component(name: str) -> bool:
    """أسماء أجهزة ويندوز المحجوزة (CON, NUL, COM1…) وأسماء تنتهي بنقطة أو مسافة أو فيها : (تيارات NTFS) أو < > " | ? * أو محرف تحكم."""
    stem = name.split(".")[0].strip().upper()
    return (stem in _WIN_RESERVED or name != name.rstrip(" .") or ":" in name
            or any(c in _ILLEGAL_NAME_CHARS or ord(c) < 32 for c in name))


_ALIAS83 = re.compile(r"^\.?(git|ssh|gnupg|aws)~\d+$", re.IGNORECASE)  # الأسماء القصيرة 8.3 لمجلدات محمية (GIT~1)


def is_unc_like(value: Any) -> bool:
    """مسار شبكي/جهاز (\\\\server\\share أو //x أو \\\\?\\) — مرفوض قبل أي resolve لأنه قد يلمس الشبكة."""
    if not isinstance(value, (str, Path)):
        return False
    t = str(value).strip().replace("/", "\\")
    return t.startswith("\\\\")


_TRANSIENT_WINERRORS = frozenset({5, 32, 33})            # وصول مرفوض / ملف مستخدم / جزء مقفل: غالباً مضاد فيروسات أو OneDrive لحظياً
_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.3)                    # 5 محاولات بانتظار متدرج بين 50 و300 مللي ثانية


def _is_transient_io(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS


def retry_io(fn, *args, **kwargs):
    """ينفّذ عملية ملفات ويعيدها حتى 5 مرات عند أخطاء الصلاحية/القفل اللحظية (PermissionError أو winerror 5/32/33)."""
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            return fn(*args, **kwargs)
        except OSError as exc:
            if attempt >= len(_RETRY_DELAYS) or not _is_transient_io(exc):
                raise
            time.sleep(_RETRY_DELAYS[attempt])


def _safe_move(src: str, dst: str) -> None:
    """نقل بنفس القرص عبر os.rename: لا يكتب فوق موجود (حتى على لينكس) ولا ينسخ ثم يحذف المصدر (لا حذف نهائي خفي)."""
    if os.path.lexists(dst):
        raise FileExistsError(f"الوجهة موجودة أصلاً: {dst}")
    retry_io(os.rename, src, dst)


def _same_file(a: Any, b: Any) -> bool:
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return False


def _is_link(p: Path) -> bool:
    """رابط رمزي أو تقاطع (junction) أو نقطة تركيب في ويندوز. ملفات OneDrive/السحابة (reparse لكنها ليست روابط) لا تُعدّ روابط."""
    try:
        if p.is_symlink():
            return True
        isj = getattr(os.path, "isjunction", None)
        if isj is not None and isj(str(p)):
            return True
        st = os.lstat(str(p))
        if getattr(st, "st_file_attributes", 0) & _REPARSE_FLAG:
            tag = getattr(st, "st_reparse_tag", None)
            if tag is None:
                return True  # لا نعرف النوع: نعامله كرابط احتياطاً
            return tag in _LINK_REPARSE_TAGS
        return False
    except OSError:
        return False


_JOURNAL_PATH_KEYS = ("src", "dest", "path", "original_path", "trash_path", "tmp")
_REL = "rel:"


_CLOUD_ATTRS = 0x400000 | 0x40000 | 0x1000  # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE (ملفات OneDrive غير المحمّلة)
CLOUD_HYDRATE_LIMIT = 25 * 1024 * 1024


class CloudFileNotLocalError(Exception):
    """ملف سحابي (OneDrive…) غير محمّل على الجهاز وكبير: قراءته ستنزّله كاملاً بصمت، فنرفضه برسالة واضحة."""


def check_not_big_cloud_file(p: Path, limit: int = CLOUD_HYDRATE_LIMIT, _lstat=None) -> None:
    try:
        st = (_lstat or os.lstat)(str(p))
    except OSError:
        return
    if getattr(st, "st_file_attributes", 0) & _CLOUD_ATTRS and st.st_size > limit:
        raise CloudFileNotLocalError(
            f"الملف '{p.name}' ({st.st_size // (1024 * 1024)}MB) غير محمّل محلياً من السحابة؛ قراءته ستنزّله كاملاً. "
            f"اجعله «الاحتفاظ دائماً على هذا الجهاز» ثم أعد المحاولة.")


# ---------- فحص موحّد للأسماء (يستعمله المخطط وقائمة المجلد والقواعد فلا يختلفون) ----------
IS_WINDOWS = sys.platform.startswith("win")
MAX_NAME_UNITS = 255          # ويندوز: وحدات UTF-16؛ غيره: بايتات UTF-8
MAX_MARK_RUN = 8              # أقصى علامات دمج متتالية بعد حرف أساس
MAX_JOINERS = 4               # أقصى عدد ZWNJ/ZWJ في اسم واحد
_NAME_FORBIDDEN_CATS = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
_FILLER_CHARS = {"\u3164", "\u2800", "\u115f", "\u1160", "\uffa0", "\u180e", "\u034f", "\u17b4", "\u17b5"}
_LOOKALIKE_SLASH_CHARS = {"\u2215", "\u2044", "\u29f8", "\uff0f", "\uff3c", "\u2216", "\u29f5", "\u29f9", "\u2571", "\u2572",
                          "\ufe68", "\u27cb", "\u27cd", "\u1735", "\u2f03", "\u31d3"}   # تبدو فاصل مسار
_OVERLAY_MARKS = set(chr(c) for c in range(0x0334, 0x0339))                          # شطب/تراكب يغيّر شكل الحرف السابق
_NARROW_SPACES = {"\u00a0", "\u202f"}                                                # NBSP وNNBSP (لقطات ماك: «10.00.00 AM»)


def name_len(value: str) -> int:
    """طول الاسم بوحدة النظام: UTF-16 على ويندوز، بايتات UTF-8 على غيره."""
    if IS_WINDOWS:
        return len(value.encode("utf-16-le", "surrogatepass")) // 2
    return len(value.encode("utf-8", "surrogatepass"))


def _is_variation_selector(ch: str) -> bool:
    o = ord(ch)
    return 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF


def _in_ranges(ch: str, ranges) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in ranges)


_ARABIC_RANGES = ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
_INDIC_RANGES = ((0x0900, 0x0DFF), (0x1000, 0x109F), (0x1780, 0x17FF))
_MONGOLIAN_RANGE = ((0x1800, 0x18AF),)


def _is_ideograph(ch: str) -> bool:
    return unicodedata.name(ch, "").startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH"))


def _emoji_like(ch: str) -> bool:
    return unicodedata.category(ch) in ("So", "Sk") or ch in ("\ufe0f", "\ufe0e")


def name_problem(value: str) -> Optional[str]:
    """سبب رفض اسم/مسار لأن عرضه قد يخدع المستخدم، أو None إن كان مقبولاً. يقبل الأسماء المشروعة بكل اللغات:
    علامات دمج بعد حرف أساس (حتى 8 متتالية) كالحركات العربية والتايلندية والهندية والعبرية والتشكيل اللاتيني المفكَّك؛
    FE0F بعد رمز إيموجي (أو بعد رقم/#/* لمفتاح لوحة يتبعه U+20E3)؛ محددات الشكل FE00-FE0D وE0100+ بعد حرف صيني فقط؛
    محددات المنغولية بعد حرف منغولي؛ ZWNJ بين محرفين عربيين/هنديين؛ ZWJ بين محرفين هنديين أو داخل تسلسل إيموجي؛
    NBSP/NNBSP بين رقمين (أو رقم و AM/PM). ويرفض التحكم والاتجاه والفراغات الخفية وتراكم العلامات والشُّرَط المموِّهة."""
    n = len(value)
    run = 0
    joiners = 0
    ar_ind = _ARABIC_RANGES + _INDIC_RANGES
    for i, ch in enumerate(value):
        cat = unicodedata.category(ch)
        prev = value[i - 1] if i > 0 else ""
        nxt = value[i + 1] if i + 1 < n else ""
        if ch in _NARROW_SPACES:
            tail = value[i + 1:i + 3].upper()
            ok = bool(prev) and prev.isdigit() and (nxt.isdigit() or (tail in ("AM", "PM") and not value[i + 3:i + 4].isalpha()))
            if not ok:
                return f"فراغ خاص (U+{ord(ch):04X}) في غير موضعه (يُقبل فقط بين رقمين أو بين رقم وAM/PM)"
            run = 0
            continue
        if ch in ("\u200c", "\u200d"):
            joiners += 1
            if not prev or not nxt or joiners > MAX_JOINERS:
                return f"رابط عرض (U+{ord(ch):04X}) في موضع غير مشروع"
            if ch == "\u200c":
                ok = _in_ranges(prev, ar_ind) and _in_ranges(nxt, ar_ind)
            else:
                ok = (_in_ranges(prev, _INDIC_RANGES) and _in_ranges(nxt, _INDIC_RANGES)) or (_emoji_like(prev) and _emoji_like(nxt))
            if not ok:
                return f"رابط عرض (U+{ord(ch):04X}) في موضع غير مشروع"
            run = 0
            continue
        if ch in _FILLER_CHARS or cat in _NAME_FORBIDDEN_CATS or not ch.isprintable() or (cat == "Zs" and ch != " "):
            return f"محرف غير مرئي/تحكمي (U+{ord(ch):04X})"
        if ch in _ILLEGAL_NAME_CHARS:
            return f"المحرف '{ch}' ممنوع في أسماء ويندوز"
        if ch in _LOOKALIKE_SLASH_CHARS:
            if ch == "\u2044" and prev.isdigit() and nxt.isdigit():
                run = 0
                continue
            return f"شرطة مائلة مموِّهة (U+{ord(ch):04X}) تشبه فاصل المسار"
        if _is_variation_selector(ch):
            pcat = unicodedata.category(prev) if prev else ""
            if ch == "\ufe0f" and prev and (pcat == "Nd" or prev in "#*"):
                ok = nxt == "\u20e3"                       # مفتاح لوحة: 1\uFE0F\u20E3
            elif ch in ("\ufe0e", "\ufe0f"):
                ok = bool(prev) and (pcat in ("So", "Sk") or prev == "\u200d")
            else:
                ok = bool(prev) and (_is_ideograph(prev) or (ord(ch) <= 0xFE0D and pcat == "Sm"))
            if not ok or _is_variation_selector(prev):
                return f"محدد شكل خفي (U+{ord(ch):04X}) بعد محرف لا يقبله"
            run = 0
            continue
        if cat in ("Mn", "Mc", "Me"):
            run += 1
            if not prev or prev.isspace() or prev in "/\\" or run > MAX_MARK_RUN or ch in _OVERLAY_MARKS:
                return f"علامات دمج/تراكب غير مقبولة (U+{ord(ch):04X})"
            o = ord(ch)
            if (0x180B <= o <= 0x180D or o == 0x180F) and not (prev and _in_ranges(prev, _MONGOLIAN_RANGE)):
                return f"محدد منغولي (U+{o:04X}) بعد محرف غير منغولي"
            continue
        run = 0
    return None


def name_ok(value: str) -> bool:
    return name_problem(value) is None


MAX_COPY_BYTES_PER_TASK = 2 * 1024 ** 3  # سقف مجموع ما تنسخه مهمة واحدة (يمنع استنزاف القرص بنسخ متضاعفة)
MAX_FREE_MARGIN_BYTES = 64 * 1024 ** 2    # سقف هامش الأمان من المساحة الحرة (يتناسب مع حجم النسخ: 5% بحد أدنى 64KB)
MIN_FREE_MARGIN_BYTES = 64 * 1024
TRASH_CAP_BYTES = 5 * 1024 ** 3           # سقف سلة المحذوفات: فوقه لا تُقبل نسخ جديدة حتى تُفرَّغ السلة يدوياً (لا حذف نهائي آلي)
COPY_BUDGET_TTL_SECONDS = 3600.0          # عدّاد نسخ مهمة منسية يُهمل بعد ساعة (لا تراكم ولا قفل دائم)


def free_space_margin(total: int, max_margin: int = MAX_FREE_MARGIN_BYTES) -> int:
    """هامش الأمان يتناسب مع حجم النسخ: 5% بين 64KB وسقف (64MB افتراضياً)."""
    return min(max(int(total) // 20, MIN_FREE_MARGIN_BYTES), max_margin)


def free_space_short(total: int, free: Optional[int], max_margin: int = MAX_FREE_MARGIN_BYTES) -> bool:
    """True إن كانت المساحة الحرة لا تكفي. free غير معروف أو 0 (بعض أنظمة الملفات) = غير معروف فلا نرفض بناءً عليه."""
    if total <= 0 or free is None or free <= 0:
        return False
    return total + free_space_margin(total, max_margin) > free


def human_size(n: int) -> str:
    n = int(n)
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def tree_size(p: Path, limit: Optional[int] = None) -> int:
    """مجموع أحجام ملفات الشجرة عبر lstat فقط (لا قراءة محتوى → لا تنزيل لملفات سحابية). يتوقف مبكراً إن تجاوز limit."""
    try:
        st = os.lstat(str(p))
    except OSError:
        return 0
    import stat as _stat
    if not _stat.S_ISDIR(st.st_mode):
        return int(st.st_size)
    total = 0
    for cur, _dirs, files in os.walk(str(p), followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(cur, name)).st_size
            except OSError:
                continue
            if limit is not None and total > limit:
                return total
    return total


class CopyBudgetExceededError(Exception):
    """النسخ سيتجاوز سقف المهمة أو المساحة الحرة المتاحة."""


class PathOutOfBoundsError(Exception):
    """استثناء عند محاولة الوصول إلى مسار خارج المجلد الجذر المسموح به أو عبر روابط غير آمنة."""
    pass


class ProtectedPathError(Exception):
    """استثناء عند محاولة التلاعب بالمسارات النظامية المحمية (الجذر، السلة، سجل التراجع)."""
    pass


class PermanentDeleteForbidden(Exception):
    """استثناء عند محاولة حذف ملف نهائياً."""
    pass


MAX_PATH_CHARS = 259          # حد ويندوز الكلي للمسار بلا مسارات طويلة مفعّلة
LOCK_TIMEOUT_SECONDS = 30.0   # أقصى انتظار لقفل مجلد العمل من نسخة/عملية أخرى
LOCK_STALE_SECONDS = 1800.0   # قفل أقدم من هذا يُعدّ يتيماً حتى لو بدا صاحبه حياً (إعادة استعمال رقم عملية)
INTENT_NAME = ".undo_journal.intent.json"    # بادئة .undo_journal محجوزة أصلاً (لا تُستهدف ولا تُعرض)
LOCK_NAME = ".undo_journal.lock"
UNDO_LOCK_TIMEOUT_SECONDS = 3.0   # التراجع التفاعلي لا يجمّد الواجهة نصف دقيقة وراء نافذة أخرى؛ عمليات الخطة المغيِّرة تنتظر LOCK_TIMEOUT_SECONDS
BUSY_UNDO_REASON = "نافذة أخرى تعمل على نفس المجلد الآن؛ أعد محاولة التراجع بعد لحظات."
COPY_TMP_PREFIX = ".cos_copy_tmp_"
_O_BINARY = getattr(os, "O_BINARY", 0)   # ويندوز: بلا هذا يحوّل وضع النص \n إلى \r\n فيفسد مفتاح التوقيع العشوائي
KEY_NAME = "journal.key"            # مفتاح توقيع السجل: في مجلد بيانات التطبيق (~/.cos_assistant) لا في مجلد العمل أبداً
MIGRATED_NAME = "journal.migrated"  # مجلدات عمل ترقّى سجلها إلى الصيغة الموقّعة (يُرفض فيها أي إدخال غير موقّع)
UNVERIFIED_REASON = ("في سجل التراجع {n} عملية غير موثَّقة (من تثبيت/جهاز آخر، أو بعد تغيّر مفتاح التوقيع، أو سجل عُدّل خارج المساعد)؛ "
                     "لن تُنفَّذ إلا بتأكيدك الصريح.")
_SIDECAR_NAMES = (".undo_journal.rejected.jsonl", ".undo_journal.skipped.jsonl", ".undo_journal.intent.rejected.json",
                  ".undo_journal.intent.corrupt.json", ".undo_journal.corrupt.json", ".undo_journal.rejected.json",
                  ".undo_journal.intent.json", ".undo_journal.lock", ".undo_journal.lock.brk", ".undo_journal.wsid")   # ملفات جانبية لا يجوز أن تكون روابط
FUTURE_LOCK_SLACK = 60.0            # طابع زمني لقفل أبعد من هذا في المستقبل لا يُصدَّق
RENAME_TMP_PREFIX = ".cos_rename_tmp_"
_COPY_TMP_RE = re.compile(r"^\.cos_copy_tmp_[0-9a-f]{8}$")
_SWEEP_MAX_ENTRIES = 20000
_SWEEP_MAX_SECONDS = 2.0


class WorkspaceBusyError(Exception):
    """مجلد العمل مقفل من نسخة أخرى من المساعد أو عملية أخرى."""


def explain_error(exc: BaseException) -> Optional[str]:
    """سبب عربي قصير لخطأ نظام ملفات، مصنّفاً بنوع الاستثناء/winerror/errno أولاً (بلا تسريب مسارات)، أو None إن لم يكن OSError."""
    if not isinstance(exc, OSError):
        return None
    w = getattr(exc, "winerror", None)
    if w is None:
        m = re.search(r"WinError\s+(\d+)", str(exc))
        w = int(m.group(1)) if m else None
    code = getattr(exc, "errno", None)
    import errno as _e
    win = {32: "العنصر مستخدم من برنامج آخر (أغلقه ثم أعد المحاولة)", 33: "العنصر مستخدم من برنامج آخر (أغلقه ثم أعد المحاولة)",
           5: "لا صلاحية على العنصر (قد يكون محمياً أو مفتوحاً في برنامج آخر)",
           2: "المسار أو العنصر لم يعد موجوداً", 3: "المسار أو العنصر لم يعد موجوداً",
           123: "اسم أو مسار غير صالح على ويندوز", 161: "اسم أو مسار غير صالح على ويندوز",
           206: "المسار أو الاسم أطول من الحد المسموح", 112: "القرص ممتلئ", 39: "القرص ممتلئ",
           80: "الوجهة موجودة أصلاً", 183: "الوجهة موجودة أصلاً", 1314: "الصلاحية المطلوبة غير ممنوحة"}
    if w in win:   # winerror أدق من errno المترجَم منه
        return win[w]
    if code in (_e.EACCES, _e.EPERM) or isinstance(exc, PermissionError):
        return win[5]
    if code == _e.ENOENT or isinstance(exc, FileNotFoundError):
        return win[2]
    if code == _e.ENAMETOOLONG:
        return win[206]
    if code == _e.ENOSPC:
        return win[112]
    if code == _e.EEXIST or isinstance(exc, FileExistsError):
        return win[80]
    if code == _e.ENOTEMPTY:
        return "المجلد ليس فارغاً"
    if code == _e.EINVAL:
        return "اسم أو مسار غير صالح"
    return "تعذّرت عملية على نظام الملفات"


class UndoResult(int):
    """نتيجة تراجع: تساوي عدد العمليات المتراجَع عنها (متوافقة مع الأعداد) وتحمل التفاصيل:
    done (= int(r))، remaining (عمليات لم يُتراجع عنها)، reason (أول سبب عربي لتوقف/فشل أو None)،
    notes (ملاحظات التراجع المضافة أثناء هذا الاستدعاء)، skipped، missing؛ complete=True حين remaining==0 وبلا reason."""
    def __new__(cls, done: int = 0, remaining: int = 0, reason: Optional[str] = None, notes=None, skipped: int = 0, missing: int = 0,
                failures=None, unverified: int = 0):
        obj = super().__new__(cls, int(done))
        obj.unverified = int(unverified)      # عمليات غير موثَّقة لم تُنفَّذ بانتظار تأكيدك
        obj.done, obj.remaining, obj.reason = int(done), int(remaining), reason
        obj.notes, obj.skipped, obj.missing = list(notes or []), int(skipped), int(missing)
        obj.failures = list(failures or [])   # خطوات فشل التراجع عنها (نص عربي لكل خطوة) رغم متابعة المستقلّ منها
        return obj

    @property
    def needs_confirmation(self) -> bool:
        """True: توجد عمليات غير موثَّقة لم تُنفَّذ؛ أعد الاستدعاء بـ allow_unverified=True بعد تأكيد صريح من المستخدم."""
        return self.unverified > 0

    @property
    def complete(self) -> bool:
        return self.remaining == 0 and self.reason is None

    def to_dict(self) -> Dict[str, Any]:
        return {"done": self.done, "remaining": self.remaining, "reason": self.reason, "notes": list(self.notes),
                "skipped": self.skipped, "missing": self.missing, "failures": list(self.failures), "unverified": self.unverified,
                "needs_confirmation": self.needs_confirmation, "complete": self.complete}


def _pid_alive_windows(pid: int) -> bool:
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:
        return True   # لا نعرف: نعدّه حياً (الأحوط)


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if IS_WINDOWS:
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


_HELD_LOCK = threading.Lock()
_HELD_TOKENS: Dict[str, set] = {}   # مسار ملف القفل -> رموز الأقفال التي تحملها كائنات حيّة في هذه العملية


def _lock_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _held_add(key: str, token: str) -> None:
    with _HELD_LOCK:
        _HELD_TOKENS.setdefault(key, set()).add(token)


def _held_discard(key: str, token: Optional[str]) -> None:
    with _HELD_LOCK:
        s = _HELD_TOKENS.get(key)
        if s is not None:
            s.discard(token)
            if not s:
                _HELD_TOKENS.pop(key, None)


def _held_has(key: str, token: Any) -> bool:
    with _HELD_LOCK:
        return token in _HELD_TOKENS.get(key, ())


_BOOT_SLACK = 120.0     # هامش لتذبذب حساب زمن الإقلاع/تعديل ساعة النظام
_START_SLACK = 5.0      # عملية بدأت بعد كتابة القفل بأكثر من هذا ليست صاحبه (إعادة استعمال رقم العملية)
LOCK_EACCES_GRACE = 1.0  # PermissionError دائم عند إنشاء ملف القفل أطول من هذا ليس «طرفاً آخر يحذف»


def _boot_time() -> Optional[float]:
    """وقت إقلاع النظام (حقبة يونكس) أو None إن لم يُعرف."""
    try:
        if IS_WINDOWS:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetTickCount64.restype = ctypes.c_ulonglong
            return time.time() - k32.GetTickCount64() / 1000.0
        with open("/proc/stat", "r", encoding="ascii", errors="ignore") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except Exception:
        pass
    return None


def _proc_start_windows(pid: int) -> Optional[Tuple[str, float]]:
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                return None
            ft100 = (c.dwHighDateTime << 32) | c.dwLowDateTime   # وحدات 100ns منذ 1601
            return (f"win:{ft100}", ft100 / 1e7 - 11644473600.0)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def _proc_start_linux(pid: int) -> Optional[Tuple[str, float]]:
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as f:
            raw = f.read().decode("ascii", errors="ignore")
        fields = raw[raw.rindex(")") + 2:].split()
        ticks = int(fields[19])   # الحقل 22 (starttime) = الفهرس 19 بعد اسم العملية والحالة
        try:
            hz = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError, AttributeError):
            hz = 100
        try:
            with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as f:
                boot_id = f.read().strip()
        except OSError:
            boot_id = ""
        bt = _boot_time()
        return (f"lx:{boot_id}:{ticks}", (bt + ticks / float(hz)) if bt is not None else 0.0)
    except Exception:
        return None


def proc_start(pid: int) -> Optional[Tuple[str, float]]:
    """(معرّف بداية العملية، وقت بدايتها بحقبة يونكس) أو None إن تعذّر: يميّز العملية عن أخرى أُعيد استعمال رقمها."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    return _proc_start_windows(pid) if IS_WINDOWS else _proc_start_linux(pid)


_OWN_START: Dict[int, Optional[str]] = {}


def _own_start_id() -> Optional[str]:
    """بصمة بداية عمليتنا (تُحسب مرة لكل رقم عملية: لا تتغير)."""
    pid = os.getpid()
    if pid not in _OWN_START:
        ps = proc_start(pid)
        _OWN_START[pid] = ps[0] if ps else None
    return _OWN_START[pid]


class _WorkspaceLock:
    """قفل مجلد العمل بين العمليات والنسخ: ملف قفل يُنشأ بـ O_EXCL ويحوي رقم العملية ووقت بدايتها؛ يُكتشف اليتيم (عملية ميتة، رقم أُعيد
    استعماله بعد إعادة التشغيل، قفل تحمله عملية بلا كائن حيّ، أو قفل قديم جداً). كسر اليتيم يُسلسَل بملف كاسر O_EXCL ثانٍ. قابل لإعادة الدخول في الخيط نفسه."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_SECONDS, stale: float = LOCK_STALE_SECONDS):
        self.path, self.timeout, self.stale = path, timeout, stale
        self.brk_path = path.with_name(path.name + ".brk")
        self._key = _lock_key(path)
        self._tl = threading.RLock()
        self._depth = 0
        self._token: Optional[str] = None
        self.took_over = False   # آخر دخول خارجي كسر قفلاً يتيماً: على المستدعي أن يتعافى مما خلّفه صاحبه

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """True إن كان هذا أول دخول (الخارجي). timeout يقصّر الانتظار لهذا الاستدعاء (مثلاً للتراجع التفاعلي)."""
        t = self.timeout if timeout is None else timeout
        if not self._tl.acquire(timeout=max(0.0, t)):
            raise WorkspaceBusyError("مجلد العمل مشغول بعملية أخرى داخل هذه النافذة. انتظر لحظات ثم أعد المحاولة.")
        if self._depth > 0:
            self._depth += 1
            return False
        try:
            self.took_over = False
            self._acquire_file(t)
        except BaseException:
            self._tl.release()
            raise
        self._depth = 1
        return True

    def release(self) -> None:
        self._depth -= 1
        try:
            if self._depth == 0:
                self._release_file()
        finally:
            self._tl.release()

    @staticmethod
    def _read_path(path: Path) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            d = json.loads(raw)
            return raw, d if isinstance(d, dict) else {}
        except ValueError:
            return raw, {}

    def _read(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        return self._read_path(self.path)

    def _stale_content(self, path: Path, raw: str, d: Dict[str, Any], stale: float) -> bool:
        pid, t = d.get("pid"), d.get("t")
        try:
            age = time.time() - os.stat(str(path)).st_mtime
        except OSError:
            return False
        if not isinstance(pid, int):
            return age > 5.0    # ملف قفل فارغ/نصف مكتوب: يتيم إن مضى عليه وقت
        if isinstance(t, (int, float)) and (t != t or t > time.time() + FUTURE_LOCK_SLACK):
            return True         # طابع مستقبلي/غير منتهٍ: قفل مزروع أو ساعة معطوبة لا يحجب المجلد إلى الأبد
        if pid == os.getpid() and path == self.path and not _held_has(self._key, d.get("token")):
            return True         # قفلنا نحن لكن لا يحمله أي كائن حيّ (فشل تحريره): لن يحرره أحد
        if not pid_alive(pid):
            return True
        now = time.time()
        if isinstance(t, (int, float)):
            ps = proc_start(pid)
            if isinstance(d.get("st"), str):
                if ps is not None and d["st"] != ps[0]:
                    return True       # الرقم لعملية أخرى غير التي كتبت القفل (بصمة البداية لا تتأثر بتعديل الساعة)
            else:
                # قفل بلا بصمة بداية (إصدار أقدم): نستدل بالأوقات
                bt = _boot_time()
                if bt is not None and t < bt - _BOOT_SLACK:
                    return True       # كُتب قبل إقلاع النظام الحالي
                if ps is not None and ps[1] > t + _START_SLACK:
                    return True       # العملية الحالية بدأت بعد كتابته فليست صاحبه
            return now - t > self.stale
        return False

    def _stale(self) -> bool:
        got = self._read()
        if got is None:
            return False   # اختفى للتو: أعد المحاولة
        return self._stale_content(self.path, got[0], got[1], self.stale)

    def _remove_if_same(self, path: Path, expected: str) -> bool:
        """يزيل الملف إن كان محتواه كما رأيناه: ننقله جانباً (ذرّي) ونتحقق من محتواه؛ إن كان غيره (صار لغيرنا) نعيده بـ os.link (يفشل إن وُجد غيره)."""
        gone = path.with_name(path.name + f".stale.{secrets.token_hex(4)}")
        try:
            os.rename(str(path), str(gone))
        except OSError:
            return False
        try:
            moved = self._read_path(gone)
            if moved is not None and moved[0] != expected:
                try:
                    os.link(str(gone), str(path))
                except FileExistsError:
                    pass   # أنشأ غيرنا قفلاً جديداً في الأثناء: لا نكتب فوقه
                except OSError:
                    if not os.path.lexists(str(path)):   # نظام ملفات بلا روابط صلبة: نعيد التسمية (نافذة السباق أضيق من ضياع قفل حيّ)
                        try:
                            os.rename(str(gone), str(path))
                        except OSError:
                            pass
                return False
            return True
        finally:
            try:
                os.unlink(str(gone))
            except OSError:
                pass

    def _take_breaker(self) -> bool:
        """ملف كاسر O_EXCL يُسلسِل من يكسرون القفل اليتيم (لا يُنقل قفل حيّ جديد بين فحص القاطع ونقله). يتيمه يُزال بعد 10 ثوانٍ أو بموت صاحبه."""
        for _ in range(2):
            try:
                fd = os.open(str(self.brk_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o600)
            except FileExistsError:
                got = self._read_path(self.brk_path)
                if got is not None:
                    pid, t = got[1].get("pid"), got[1].get("t")
                    dead = isinstance(pid, int) and pid != os.getpid() and not pid_alive(pid)
                    old = not isinstance(t, (int, float)) or time.time() - t > 10.0
                    if (dead or old) and self._remove_if_same(self.brk_path, got[0]):
                        continue
                return False
            except OSError:
                return False
            try:
                os.write(fd, json.dumps({"pid": os.getpid(), "t": time.time()}).encode("utf-8"))
            finally:
                os.close(fd)
            return True
        return False

    def _break_stale(self) -> bool:
        got = self._read()
        if got is None or not self._stale_content(self.path, got[0], got[1], self.stale):
            return False
        if not self._take_breaker():
            return False   # قاطع آخر يعمل: ننتظر ونعيد الفحص
        try:
            again = self._read()
            if again is None or again[0] != got[0]:
                return False   # تغيّر بين القراءتين: صار لغيره
            return self._remove_if_same(self.path, got[0])
        finally:
            try:
                os.unlink(str(self.brk_path))
            except OSError:
                pass

    def _acquire_file(self, timeout: Optional[float] = None) -> None:
        now = time.monotonic()
        deadline = now + (self.timeout if timeout is None else timeout)
        token = f"{os.getpid()}.{secrets.token_hex(6)}"
        rec: Dict[str, Any] = {"pid": os.getpid(), "token": token, "t": time.time()}
        st = _own_start_id()
        if st is not None:
            rec["st"] = st
        payload = json.dumps(rec)
        denied_since: Optional[float] = None
        while True:
            try:
                _held_add(self._key, token)   # قبل الإنشاء: لا يبدو قفلنا الجديد «بلا كائن حيّ»
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o600)
            except FileExistsError:
                _held_discard(self._key, token)
                denied_since = None
                if self._break_stale():
                    self.took_over = True
                    continue
            except PermissionError as exc:
                _held_discard(self._key, token)
                # ويندوز: ملف قفل قيد الحذف من الطرف الآخر يزول سريعاً؛ منع دائم (مجلد محمي/للقراءة فقط) يُبلَّغ بسببه الحقيقي لا كـ«نافذة أخرى»
                now = time.monotonic()
                denied_since = denied_since if denied_since is not None else now
                if now - denied_since >= LOCK_EACCES_GRACE and not os.path.lexists(str(self.path)):
                    raise exc
            except BaseException:
                _held_discard(self._key, token)
                raise
            else:
                try:
                    os.write(fd, payload.encode("utf-8"))
                except BaseException:
                    _held_discard(self._key, token)
                    raise
                finally:
                    os.close(fd)
                self._token = token
                return
            if time.monotonic() > deadline:
                raise WorkspaceBusyError("مجلد العمل مشغول بنسخة أخرى من المساعد (أو عملية أخرى). أغلق النافذة الأخرى أو انتظر لحظات ثم أعد المحاولة.")
            time.sleep(0.05)

    def _release_file(self) -> None:
        token = self._token
        try:
            ours = False
            for attempt in range(4):   # قراءة عابرة الفشل (مضاد فيروسات/فهرسة) لا تعني أن القفل ليس لنا
                got = self._read()
                if got is not None:
                    ours = got[1].get("token") == token
                    break
                if not os.path.lexists(str(self.path)):
                    break              # اختفى (كسره غيرنا): لا شيء نحرّره
                if attempt == 3:
                    ours = True        # موجود وغير مقروء وقد كتبناه نحن: نحذفه ولا نتركه يتيماً
                else:
                    time.sleep(0.05)
            if ours:
                try:
                    retry_io(os.unlink, str(self.path))
                except FileNotFoundError:
                    pass
                except OSError:
                    pass   # يبقى باسم عمليتنا بلا كائن حيّ فيُعدّ يتيماً ويكسره أول طالب (لا يحجز المجلد نصف ساعة)
        finally:
            self._token = None
            _held_discard(self._key, token)


class _KeepInPlace(Exception):
    """التراجع عن هذا الإدخال سيمسّ شيئاً لا نثق أنه ما سجّلناه (عدّله المستخدم، أو إدخال غير موثّق لا يمرّ فحص الحالة): لا يُنفَّذ، يُحفظ للفحص ويُبلَّغ بسببه."""


class _Rejected(ValueError):
    """إدخال سجل/نية مزروع أو مزوَّر أو بمسار غير صالح (يُحجر ولا يُنفَّذ)."""


_KEY_LOCK = threading.Lock()
_KEY_CACHE: Dict[str, Tuple[Optional[bytes], Optional[str]]] = {}
_WARNED_ONCE: set = set()


def default_key_dir() -> Path:
    """مجلد بيانات التطبيق (يطابق config.data_dir): COS_DATA_DIR أو ~/.cos_assistant. مفتاح توقيع السجل journal.key يعيش هنا خارج مجلد العمل
    فلا يستطيع ملف مزروع داخل مجلد العمل (أرشيف مشترك مثلاً) أن يوقّع إدخالاته. 32 بايتاً عشوائياً يُنشأ مرة لكل تثبيت."""
    return Path(os.environ.get("COS_DATA_DIR") or (Path.home() / ".cos_assistant"))


class _KeyCreateError(OSError):
    """تعذّر إنشاء ملف المفتاح في هذا المجلد (يُجرَّب المجلد المؤقت)."""


def _key_in_dir(d: Path) -> bytes:
    """يقرأ مفتاح المجلد أو ينشئه: الإنشاء بـ O_EXCL على الاسم النهائي نفسه (بلا os.link/os.replace اللذين يسمحان لعمليتين بإنتاج مفتاحين)،
    ثم يُعاد القراءة من القرص دائماً فيتفق الجميع على ما في الملف. ملف ناقص حديث = قيد الكتابة من عملية أخرى فننتظر؛ ناقص قديم = تالف
    فيُنقل إلى .bad بيد عملية واحدة فقط (ملف كاسر O_EXCL). PermissionError عند القراءة يخرج كما هو (مفتاح موجود غير مقروء)."""
    kp = d / KEY_NAME
    fixer = d / (KEY_NAME + ".fix")
    deadline = time.monotonic() + 8.0
    while True:
        try:
            data = kp.read_bytes()
        except FileNotFoundError:
            try:
                fd = os.open(str(kp), os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o600)
            except FileExistsError:
                continue
            except OSError as exc:
                raise _KeyCreateError(exc.errno, str(exc))
            try:
                os.write(fd, os.urandom(32))
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            except OSError as exc:
                raise _KeyCreateError(exc.errno, str(exc))
            finally:
                os.close(fd)
            continue                      # القراءة التالية هي المرجع (ولو كانت عملية أخرى استبدلته)
        if len(data) >= 32:
            return data[:32]
        if time.monotonic() > deadline:
            raise _KeyCreateError(errno_busy(), "key file stuck")
        try:
            age = time.time() - kp.stat().st_mtime
        except FileNotFoundError:
            continue
        if age < 3.0:
            time.sleep(0.05)              # قيد الكتابة
            continue
        try:                              # تالف قديم: كاسر واحد
            ffd = os.open(str(fixer), os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o600)
        except FileExistsError:
            try:
                if time.time() - fixer.stat().st_mtime > 10.0:
                    os.unlink(str(fixer))
            except OSError:
                pass
            time.sleep(0.05)
            continue
        except OSError as exc:
            raise _KeyCreateError(exc.errno, str(exc))
        os.close(ffd)
        try:
            if len(kp.read_bytes()) < 32:   # ما زال تالفاً بعد أخذ الكاسر
                os.replace(str(kp), str(kp.with_name(KEY_NAME + ".bad")))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _KeyCreateError(exc.errno, str(exc))
        finally:
            try:
                os.unlink(str(fixer))
            except OSError:
                pass


def errno_busy() -> int:
    import errno as _e
    return _e.EBUSY


def load_journal_key(key_dir: Optional[Union[str, Path]] = None) -> Tuple[Optional[bytes], Optional[str], Optional[Path]]:
    """(المفتاح أو None، تحذير عربي أو None، المجلد المستعمل). يُنشأ المفتاح إن غاب. تعذّر الإنشاء في مجلد البيانات -> مجلد مؤقت
    (على المستدعي أن يعدّ الجلسة مؤقتة المفتاح: key_ephemeral)؛ مفتاح موجود غير مقروء -> None مع تحذير (نعمل بلا توقيع ولا نكتب فوقه)."""
    primary = Path(key_dir) if key_dir is not None else default_key_dir()
    cands = [primary, Path(tempfile.gettempdir()) / "cos_assistant_keys"]
    for d in cands:
        ck = os.path.normcase(str(d))
        with _KEY_LOCK:
            if ck in _KEY_CACHE:
                k, w = _KEY_CACHE[ck]
                if k is not None or w is not None:
                    return k, w, d
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        key: Optional[bytes] = None
        warn: Optional[str] = None
        try:
            key = _key_in_dir(d)
        except _KeyCreateError:
            continue                     # المجلد لا يقبل الإنشاء: نجرّب المجلد المؤقت
        except PermissionError:
            warn = "مفتاح توقيع سجل التراجع غير قابل للقراءة؛ يعمل المساعد بلا توقيع (سجل التراجع أقل حماية من التلاعب)."
        except OSError:
            continue
        with _KEY_LOCK:
            _KEY_CACHE[ck] = (key, warn)
        return key, warn, d
    return None, "تعذّر إنشاء مفتاح توقيع سجل التراجع؛ يعمل المساعد بلا توقيع (سجل التراجع أقل حماية من التلاعب).", None


def path_signature(p: Union[str, Path]) -> Optional[List[int]]:
    """بصمة رخيصة لهوية ما على القرص: ملف -> [الحجم، mtime_ns]؛ مجلد -> [مجموع الأحجام، أحدث mtime_ns، عدد الملفات]."""
    try:
        st = os.lstat(str(p))
        if not stat.S_ISDIR(st.st_mode):
            return [int(st.st_size), int(st.st_mtime_ns)]
        total = newest = n = 0
        for cur, _dirs, files in os.walk(str(p), followlinks=False):
            for f in files:
                try:
                    s2 = os.lstat(os.path.join(cur, f))
                except OSError:
                    continue
                total += s2.st_size
                newest = max(newest, s2.st_mtime_ns)
                n += 1
        return [int(total), int(newest), n]
    except OSError:
        return None


def _safe_append(path: Union[str, Path], text: str) -> None:
    """يلحق نصاً بملف جانبي دون اتباع رابط: O_NOFOLLOW حيث يتوفر، وإلا فحص الرابط قبل الفتح (OSError إن كان رابطاً)."""
    p = str(path)
    if os.path.lexists(p) and _is_link(Path(p)):
        raise OSError("sidecar is a link")
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _file_sig(p: Union[str, Path]) -> Optional[List[int]]:
    """[الحجم، mtime_ns] لملف عادي (للمجلدات None): هوية رخيصة تُسجَّل مع النقل/إعادة التسمية."""
    try:
        st = os.lstat(str(p))
        return None if stat.S_ISDIR(st.st_mode) else [int(st.st_size), int(st.st_mtime_ns)]
    except OSError:
        return None


def _paths_overlap(a: str, b: str) -> bool:
    a, b = os.path.normcase(a), os.path.normcase(b)
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


class FileManager:
    """إدارة آمنة للملفات محصورة داخل مجلد جذر مسموح مع سجل تراجع محمي ومنع قاطع للحذف النهائي."""

    def __init__(self, allowed_root: Union[str, Path], kill_switch: Optional[KillSwitch] = None,
                 key_dir: Optional[Union[str, Path]] = None, known_task_ids: Any = None, allow_unverified: bool = False):
        self.kill_switch = kill_switch
        # إدخالات السجل غير الموثَّقة لا تُنفَّذ إلا إن عُرف معرّف مهمتها في الدفتر (known_task_ids: دالة أو مجموعة، تأتي من خارج مجلد العمل)
        # أو أُجيز ذلك صراحة بعد تأكيد المستخدم (allow_unverified)
        self.known_task_ids = known_task_ids
        self.allow_unverified = bool(allow_unverified)
        self.allowed_root = Path(allowed_root).resolve()
        if not self.allowed_root.exists():
            self.allowed_root.mkdir(parents=True, exist_ok=True)
        self.undo_notes: List[str] = []  # ملاحظات التراجع (مثل استعادة عنصر باسم بديل لأن اسمه الأصلي صار مشغولاً)
        # توقيع السجل: HMAC بمفتاح التثبيت (خارج مجلد العمل). key_dir للاختبارات؛ الافتراضي مجلد بيانات التطبيق مع رجوع إلى مجلد مؤقت
        self._key, key_warn, self._key_dir = load_journal_key(key_dir)
        if key_warn:
            wk = ("keywarn", key_warn)
            if wk not in _WARNED_ONCE:   # تحذير مرة واحدة لكل عملية
                _WARNED_ONCE.add(wk)
                self.undo_notes.append(key_warn)
        self._ws_id = hashlib.sha256(os.path.normcase(str(self.allowed_root)).encode("utf-8", "surrogatepass")).hexdigest()[:16]
        self._kid: Optional[str] = hashlib.sha256(b"cos-key-id" + self._key).hexdigest()[:16] if self._key is not None else None
        primary = Path(key_dir) if key_dir is not None else default_key_dir()
        # مفتاح من المجلد المؤقت (تعذّر مجلد البيانات): لا يُعاد توقيع الموجود ولا يُسجَّل المجلد «مُرقّى» ويُنبَّه المستخدم
        self.key_ephemeral = bool(self._key is not None and self._key_dir is not None
                                  and os.path.normcase(str(self._key_dir)) != os.path.normcase(str(primary)))
        if self.key_ephemeral:
            self.undo_notes.append("تعذّر استعمال مجلد بيانات التطبيق لمفتاح التوقيع، فاستُعمل مفتاح مؤقت لهذه الجلسة؛ إدخالات التراجع القديمة تُعدّ غير موثَّقة ولن تُعاد كتابتها.")
        self._migrated_kid: Optional[str] = None
        self._pending_rewrite = False
        self._unverified_noted = 0
        # مجلد السلة والسجل لا يُحلّان عبر روابط: رابط باسم .trash قد يرسل «المحذوفات» إلى خارج مجلد العمل
        self.trash_dir = self.allowed_root / ".trash"
        self.journal_path = self.allowed_root / ".undo_journal.json"
        if os.path.lexists(str(self.trash_dir)) and not _is_link(self.trash_dir) and not self.trash_dir.is_dir():
            self._ensure_trash()    # ملف عادي باسم .trash: يُنقل جانباً ويُنشأ المجلد (لا نرفض العمل كله)
        for internal, want_dir in ((self.trash_dir, True), (self.journal_path, False),
                                   (self.journal_path.with_name(self.journal_path.name + ".tmp"), False)):
            if os.path.lexists(str(internal)):
                if _is_link(internal):
                    raise ProtectedPathError(f"'{internal.name}' رابط/تقاطع — مجلد العمل غير آمن؛ احذف الرابط يدوياً.")
                if want_dir and not internal.is_dir():
                    raise ProtectedPathError(f"'{internal.name}' موجود كملف لا كمجلد — غيّر اسمه يدوياً ثم أعد المحاولة.")
                if not want_dir and internal.is_dir():
                    raise ProtectedPathError(f"'{internal.name}' موجود كمجلد — غيّر اسمه يدوياً ثم أعد المحاولة.")
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        self._aside_sidecar_links()
        self._ws_id = self._load_ws_id()
        self.intent_path = self.allowed_root / INTENT_NAME
        self._wslock = _WorkspaceLock(self.allowed_root / LOCK_NAME)
        self._disk_sig: Optional[Tuple[int, int]] = None
        self.journal: List[Dict[str, Any]] = []
        self.current_task: Optional[str] = None  # معرّف المهمة الجارية؛ يُوسَم به كل إدخال لتبقى المهمة قابلة للتراجع بعد إعادة التشغيل
        self.max_copy_bytes: Optional[int] = MAX_COPY_BYTES_PER_TASK  # سقف بايتات النسخ لكل مهمة (None = بلا سقف)
        self.max_free_margin: int = MAX_FREE_MARGIN_BYTES
        self.trash_cap_bytes: Optional[int] = TRASH_CAP_BYTES
        self._copy_lock = threading.RLock()
        self._copy_spent: Dict[Optional[str], Any] = {}     # مهمة -> (بايتات، وقت أول حجز)
        self._copy_cap_override: Dict[Optional[str], int] = {}  # سقف أعلى وافق عليه المستخدم صراحة لمهمة
        self.last_undo_reason: Optional[str] = None
        locked = False
        try:
            self._wslock.acquire()
            locked = True
        except WorkspaceBusyError:
            self.undo_notes.append("مجلد العمل مشغول بنسخة أخرى؛ لم تُجرَ استعادة العمليات المقطوعة الآن.")
        except OSError as exc:
            self.undo_notes.append(f"تعذّر إنشاء ملف قفل مجلد العمل: {explain_error(exc)}؛ لم تُجرَ استعادة العمليات المقطوعة الآن.")
        try:
            self.journal = self._load_journal(adopt=locked)
            if locked:
                self._reconcile_intent()
                self._sweep_orphans()
        finally:
            if locked:
                self._wslock.release()

    def _load_ws_id(self) -> str:
        """معرّف مجلد العمل داخل التوقيع: ملف .undo_journal.wsid عشوائي يُنشأ مرة (يبقى بعد نقل المجلد/إعادة تسميته)، فلا يُقبل سجل موقّع منسوخ
        إلى مجلد آخر. تعذّر الملف (للقراءة فقط/تالف) -> معرّف من مسار المجلد."""
        p = self.allowed_root / ".undo_journal.wsid"
        fallback = self._ws_id
        for _ in range(60):
            try:
                t = p.read_text(encoding="ascii").strip()
                if re.fullmatch(r"[0-9a-f]{16}", t):
                    return t
                if time.time() - p.stat().st_mtime < 2.0:   # قيد الكتابة من عملية أخرى
                    time.sleep(0.05)
                    continue
                return fallback
            except FileNotFoundError:
                try:
                    fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o600)
                except FileExistsError:
                    continue
                except OSError:
                    return fallback
                try:
                    os.write(fd, secrets.token_hex(8).encode("ascii"))
                finally:
                    os.close(fd)
                continue
            except (OSError, UnicodeDecodeError):
                return fallback
        return fallback

    def _aside_sidecar_links(self) -> None:
        """ملف جانبي (رفض/تخطٍّ/نية/قفل…) موجود كرابط يُنقل جانباً مع ملاحظة: الكتابة عبره ستصيب ملفاً خارج مجلد العمل."""
        for name in _SIDECAR_NAMES:
            p = self.allowed_root / name
            try:
                if os.path.lexists(str(p)) and _is_link(p):
                    aside = self._get_unique_path(self.allowed_root / (name + ".was_a_link"))
                    os.replace(str(p), str(aside))     # يُنقل الرابط نفسه لا هدفه
                    self.undo_notes.append(f"'{name}' كان رابطاً/تقاطعاً (قد يوجّه الكتابة خارج مجلد العمل)؛ نُقل جانباً باسم '{aside.name}'.")
            except OSError:
                pass

    # ---------- قفل مجلد العمل ومزامنة السجل بين النسخ والعمليات ----------
    @contextlib.contextmanager
    def _op(self, timeout: Optional[float] = None):
        """قسم حرج لعملية تغيّر مجلد العمل: قفل بين العمليات، وإن غيّرت نسخة أخرى السجل منذ آخر مزامنة نعيد قراءته (لا نكتب فوقه).
        إن كسرنا قفلاً يتيماً (صاحبه مات) أو وُجدت نية عملية قديمة نتعافى مما خلّفه صاحبه قبل عمليتنا. timeout يقصّر انتظار القفل لهذا الاستدعاء."""
        outer = self._wslock.acquire(timeout)
        try:
            if outer:
                took = self._wslock.took_over
                self._ensure_trash()
                self._sync_journal(force=took)
                if took or os.path.lexists(str(self.intent_path)):
                    self._recover_after_takeover(sweep=took)
            yield
        finally:
            self._wslock.release()

    def _ensure_trash(self) -> None:
        """السلة .trash حذفها المستخدم أو استبدلها أثناء عمل التطبيق: تُنشأ من جديد (وإن كانت ملفاً عادياً يُنقل جانباً مع ملاحظة). رابط/تقاطع يبقى مرفوضاً."""
        try:
            st = os.lstat(str(self.trash_dir))
        except FileNotFoundError:
            try:
                self.trash_dir.mkdir(parents=True, exist_ok=True)
                self.undo_notes.append("مجلد السلة .trash لم يكن موجوداً فأُعيد إنشاؤه؛ ما حُذف سابقاً عبر المساعد لم يعد متاحاً للاستعادة منه.")
            except OSError:
                pass
            return
        except OSError:
            return
        if stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or _is_link(self.trash_dir):
            return
        try:
            aside = self._get_unique_path(self.allowed_root / ".trash.was_a_file")
            os.replace(str(self.trash_dir), str(aside))
            self.trash_dir.mkdir(parents=True, exist_ok=True)
            self.undo_notes.append(f"كان '.trash' ملفاً عادياً لا مجلداً؛ نُقل جانباً باسم '{aside.name}' وأُنشئت سلة جديدة.")
        except OSError as exc:
            self.undo_notes.append(f"'.trash' ليس مجلداً وتعذّر إصلاحه: {explain_error(exc)}.")

    def _recover_after_takeover(self, sweep: bool) -> None:
        """نحن نملك القفل الآن وصاحبه السابق ميت: نسوّي نيته المعلّقة (نتبنّى المكتمل/ننظّف الناقص) ونكنس بقايا النسخ المؤقتة قبل أي عملية لنا."""
        try:
            self._reconcile_intent()
            if sweep:
                self._sweep_orphans()
        except ProtectedPathError:
            raise
        except Exception as exc:
            self.undo_notes.append(f"تعذّر تنظيف بقايا عملية مقطوعة: {explain_error(exc) or 'خطأ غير متوقع'}. تحقق من المجلد يدوياً.")

    def refresh_journal(self, timeout: Optional[float] = None) -> None:
        """يطابق السجل في الذاكرة مع القرص إن غيّرته نسخة/عملية أخرى (يستدعيه المنسّق قبل تسجيل حدود مهمة أو تراجع)."""
        with self._op(timeout):
            pass

    def _sig(self) -> Optional[Tuple[int, int]]:
        try:
            st = os.stat(str(self.journal_path))
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _sync_journal(self, force: bool = False) -> None:
        if force or self._sig() != self._disk_sig:
            self.journal[:] = self._load_journal()

    def _entry_to_disk(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """مسارات داخل مجلد العمل تُحفظ نسبية (rel:…) فلا يتعطل التراجع إن نُقل المجلد أو أُعيدت تسميته."""
        out = dict(entry)
        out.pop("unverified", None)
        out.pop("_mac", None)
        for k in _JOURNAL_PATH_KEYS:
            v = out.get(k)
            if isinstance(v, str) and not v.startswith(_REL):
                try:
                    out[k] = _REL + Path(v).relative_to(self.allowed_root).as_posix()
                except ValueError:
                    pass  # خارج الجذر أو نسبي أصلاً: يبقى كما هو
        return out

    def _path_from_disk(self, v: Any) -> str:
        """مسار من السجل/النية إلى مسار مطلق داخل مجلد العمل؛ يرفع _Rejected لأي مسار خارجه أو يحوي '..' أو مطلق/بحرف قرص في صيغة rel:."""
        if not isinstance(v, str) or not v or "\0" in v:
            raise _Rejected("مسار غير صالح")
        if v.startswith(_REL):
            rel = v[len(_REL):]
            parts = re.split(r"[\\/]+", rel)
            if (not rel or rel[0] in "/\\" or re.match(r"^[A-Za-z]:", rel) or (os.name == "nt" and ":" in parts[0])
                    or any(x == ".." for x in parts) or is_unc_like(rel)):
                raise _Rejected("مسار نسبي يخرج من مجلد العمل أو مطلق")
            return str(self.allowed_root / Path(rel))
        parts = re.split(r"[\\/]+", v)
        if any(x == ".." for x in parts):
            raise _Rejected("مسار فيه '..'")
        if Path(v).is_absolute():
            nv = os.path.normcase(os.path.normpath(v))
            nr = os.path.normcase(str(self.allowed_root))
            if nv != nr and not nv.startswith(nr.rstrip("\\/") + os.sep):
                raise _Rejected("مسار خارج مجلد العمل")
        return v

    def _entry_from_disk(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(entry)
        for k in _JOURNAL_PATH_KEYS:
            if k in out and out[k] is not None:
                out[k] = self._path_from_disk(out[k])
        return out

    # ---------- توقيع السجل (HMAC) ----------
    def _mac(self, disk_entry: Dict[str, Any]) -> str:
        body = json.dumps({k: v for k, v in disk_entry.items() if k != "mac"}, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hmac.new(self._key, body.encode("ascii"), hashlib.sha256).hexdigest()

    def _mac_ok(self, disk_entry: Dict[str, Any]) -> bool:
        """توقيع صالح بمفتاحنا؟ (المقارنة بالبايتات: توقيع بمحارف غير ASCII أو غير نصي يعطي False لا استثناء)."""
        m = disk_entry.get("mac")
        if not isinstance(m, str) or self._key is None:
            return False
        try:
            return hmac.compare_digest(m.encode("utf-8", "replace"), self._mac(disk_entry).encode("ascii"))
        except Exception:
            return False

    def sign_disk_entry(self, disk_entry: Dict[str, Any]) -> Dict[str, Any]:
        """نسخة موقّعة (ws وkid وn وmac) من إدخال بصيغة القرص: لكاتب شرعي ولاختبارات تحاكي انقطاعاً بين الكتابة والاستبدال."""
        d = {k: v for k, v in disk_entry.items() if k != "mac"}
        d.setdefault("n", secrets.token_hex(6))
        d["ws"], d["kid"] = self._ws_id, self._kid
        d["mac"] = self._mac(d)
        return d

    def _bound_ok(self, disk_entry: Dict[str, Any]) -> bool:
        """موقّع بمفتاحنا ولمجلد العمل هذا (نسخ سجل موقّع من مجلد آخر أو من مفتاح آخر لا يكفي)."""
        return (self._mac_ok(disk_entry) and disk_entry.get("ws") == self._ws_id and disk_entry.get("kid") == self._kid)

    def _migrated_map(self) -> Dict[str, Any]:
        """{معرّف مجلد العمل: معرّف المفتاح الذي وُقّع به آخر مرة} من مجلد بيانات التطبيق (خارج مجلد العمل). الصيغة القديمة قائمة -> معرّف مجهول."""
        if self._key_dir is None:
            return {}
        try:
            data = json.loads((self._key_dir / MIGRATED_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if isinstance(data, list):
            return {x: None for x in data if isinstance(x, str)}
        if isinstance(data, dict) and isinstance(data.get("ws"), dict):
            return dict(data["ws"])
        return {}

    def _mark_migrated(self) -> None:
        """يسجّل معرّف مفتاحنا لهذا المجلد (للتنبيه إن تغيّر المفتاح لاحقاً). لا يُسجَّل بمفتاح مؤقت ولا بلا مفتاح."""
        if self._key is None or self._key_dir is None or self.key_ephemeral or self._migrated_kid == self._kid:
            return
        mp = self._key_dir / MIGRATED_NAME
        try:
            cur = self._migrated_map()
            if cur.get(self._ws_id) == self._kid:
                self._migrated_kid = self._kid
                return
            cur[self._ws_id] = self._kid
            while len(cur) > 500:
                cur.pop(next(iter(cur)))
            tmp = mp.with_name(f"{MIGRATED_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
            tmp.write_text(json.dumps({"v": 2, "ws": cur}), encoding="utf-8")
            os.replace(str(tmp), str(mp))
            self._migrated_kid = self._kid
        except OSError:
            pass

    def _quarantine(self, items: List[Dict[str, Any]], why: str) -> None:
        """إدخالات سجل مرفوضة (غير موقّعة/مزوَّرة/بمسار خارج المجلد): تُحفظ للفحص في .undo_journal.rejected.jsonl ولا تُنفَّذ أبداً."""
        try:
            self._aside_sidecar_links()
            _safe_append(self.allowed_root / ".undo_journal.rejected.jsonl",
                         "".join(json.dumps({"entry": it, "why": why}, ensure_ascii=True) + "\n" for it in items))
        except OSError:
            pass
        self.undo_notes.append(f"رُفض {len(items)} إدخال في سجل التراجع ({why}) ولن يُنفَّذ؛ حُفظ للفحص في .undo_journal.rejected.jsonl.")

    def _tmp_journal_candidates(self) -> List[Path]:
        """ملفات السجل المؤقتة: الاسم القديم .tmp والأسماء الفريدة لكل نسخة .undo_journal.json.<pid>.<rand>.tmp."""
        out: List[Path] = []
        legacy = self.journal_path.with_name(self.journal_path.name + ".tmp")
        if os.path.lexists(str(legacy)):
            out.append(legacy)
        try:
            for e in os.scandir(str(self.allowed_root)):
                if e.name.startswith(self.journal_path.name + ".") and e.name.endswith(".tmp") and Path(e.path) != legacy:
                    out.append(Path(e.path))
        except OSError:
            pass
        return out

    def _adopt_tmp_journal(self) -> None:
        """انقطاع (انهيار/قطع كهرباء) بعد تنفيذ العملية وقبل استبدال السجل: ملف .tmp الكامل يحوي العملية؛ نتبنّاه إن كان استمراراً
        للسجل الحالي (يمتدّه أو يقتطع منه خطوة واحدة). القرار بتسلسل المحتوى فقط لا بأوقات الملفات (ساعة النظام قد تتراجع)."""
        for tmp in self._tmp_journal_candidates():
            m = re.match(re.escape(self.journal_path.name) + r"\.(\d+)\.[0-9a-f]+\.tmp$", tmp.name)
            if m and int(m.group(1)) != os.getpid() and pid_alive(int(m.group(1))):
                continue   # كاتب حيّ في عملية أخرى: ليس لنا أن نمسّ ملفه
            try:
                data = json.loads(tmp.read_text(encoding="utf-8-sig"))
                ok = isinstance(data, list) and all(isinstance(e, dict) for e in data)
                if ok and self.journal_path.exists():
                    try:
                        cur = json.loads(self.journal_path.read_text(encoding="utf-8-sig"))
                    except Exception:
                        cur = None
                    if isinstance(cur, list):
                        n = min(len(cur), len(data))
                        ok = cur[:n] == data[:n] and (len(data) >= len(cur) or len(cur) - len(data) <= 1)
                if ok:
                    retry_io(os.replace, str(tmp), str(self.journal_path))
                else:
                    try:
                        os.replace(str(tmp), str(self._get_unique_path(self.allowed_root / ".undo_journal.rejected.json")))
                    except OSError:
                        tmp.unlink()
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _load_journal(self, adopt: bool = True) -> List[Dict[str, Any]]:
        if adopt:
            self._adopt_tmp_journal()
        self._pending_rewrite = False
        try:
            entries = self._read_journal_file(purge=adopt)
            if adopt:
                if self._pending_rewrite:    # حُجرت إدخالات فاسدة: نعيد كتابة السجل نظيفاً (غير الموثَّق يُحفظ كما هو بتوقيعه الأصلي)
                    self.journal[:] = entries
                    try:
                        self._save_journal()
                    except Exception:
                        pass
                if self._key is not None and not any(e.get("unverified") for e in entries):
                    self._mark_migrated()
            return entries
        finally:
            self._disk_sig = self._sig()

    def _vet_entries(self, good: List[Dict[str, Any]], purge: bool) -> List[Dict[str, Any]]:
        """يصنّف إدخالات السجل المقروءة: مسار فاسد/خارج المجلد -> حجر (rejected)؛ موقّع بمفتاحنا ولمجلدنا -> موثوق؛
        أي شيء آخر (توقيع غائب/مختلف لتغيّر المفتاح أو نقل المجلد، أو مفتاح غير متاح) -> unverified: يبقى في السجل كما هو ولا يُفقد ولا يُنفَّذ
        إلا بالشروط في _unverified_allowed."""
        out: List[Dict[str, Any]] = []
        bad: List[Dict[str, Any]] = []
        why = ""
        n_unv = 0
        for e in good:
            try:
                ent = self._entry_from_disk(e)
            except Exception:
                bad.append(e)
                why = why or "مسار خارج مجلد العمل أو غير صالح"
                continue
            raw_mac = ent.pop("mac", None)
            if self._key is not None and self._bound_ok(e):
                out.append(ent)
                continue
            ent["unverified"] = True
            if isinstance(raw_mac, str):
                ent["_mac"] = raw_mac
            n_unv += 1
            out.append(ent)
        if bad:
            if purge:
                self._quarantine(bad, why)
                self._pending_rewrite = True
            else:
                self.undo_notes.append(f"تُجوهل {len(bad)} إدخال في سجل التراجع غير صالح ({why}).")
        if n_unv and n_unv != self._unverified_noted:
            self._unverified_noted = n_unv
            recorded = self._migrated_map().get(self._ws_id)
            extra = " (تغيّر مفتاح التوقيع منذ آخر استعمال لهذا المجلد)" if recorded and self._kid and recorded != self._kid else ""
            self.undo_notes.append(f"{n_unv} إدخال في سجل التراجع غير موثَّق{extra}؛ بقي محفوظاً ولن يُنفَّذ إلا إن عُرفت مهمته في الدفتر أو أكّدتَ ذلك.")
        return out

    def _read_journal_file(self, purge: bool = False) -> List[Dict[str, Any]]:
        if self.journal_path.exists():
            text = None
            for attempt in range(4):  # قفل مؤقت من مضاد فيروسات/OneDrive لا يعني أن السجل تالف
                try:
                    text = self.journal_path.read_text(encoding="utf-8-sig")
                    break
                except UnicodeDecodeError:
                    text = ""
                    break
                except OSError:
                    if attempt == 3:
                        raise ProtectedPathError("تعذّر قراءة سجل التراجع الآن (الملف مقفل). أعد المحاولة بعد لحظات؛ لم يُمَس السجل.")
                    time.sleep(0.3)
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    good = [e for e in data if isinstance(e, dict)]
                    if len(good) != len(data):  # عناصر غير صالحة ستُسقط عند الحفظ التالي: نبقي نسخة للفحص
                        try:
                            shutil.copy2(str(self.journal_path), str(self._get_unique_path(self.allowed_root / ".undo_journal.corrupt.json")))
                            self.undo_notes.append("سجل التراجع فيه عناصر غير صالحة تُجوهلت؛ حُفظت نسخة منه للفحص.")
                        except Exception:
                            pass
                    return self._vet_entries(good, purge)
            except Exception:
                pass
            # سجل تالف فعلاً: لا نفقده بصمت، نحفظ نسخة للفحص ثم نبدأ سجلاً جديداً
            try:
                backup = self._get_unique_path(self.allowed_root / ".undo_journal.corrupt.json")
                self.journal_path.replace(backup)
                self.undo_notes.append("سجل التراجع كان تالفاً؛ حُفظ للفحص وبدأ سجل جديد.")
            except Exception:
                raise ProtectedPathError("سجل التراجع تالف وتعذّر حفظ نسخة منه؛ لم يُمَس. افحص الملف .undo_journal.json يدوياً.")
            return []
        return []

    def _save_journal(self):
        """حفظ سجل التراجع في JSON مع رفع استثناء عند الفشل لضمان عدم ضياع التوثيق (R11).
        ملف مؤقت باسم فريد لكل نسخة/عملية (لا تتصادم نسخ تكتب معاً)، واستبدال ذرّي مع إعادة المحاولة عند قفل لحظي."""
        # كل إدخال يُسلسَل مرة واحدة ويُخزَّن (إعادة كتابة السجل كاملاً بعد كل عملية كانت تربيعية)؛ ASCII: أسماء بمحارف شاذة لا تكسر الترميز
        cache = self.__dict__.setdefault("_ser_cache", {})
        parts = []
        live = {}
        for e in self.journal:
            hit = cache.get(id(e))
            if hit is None or hit[0] is not e or hit[2] != len(e):
                if e.get("unverified"):
                    de = self._entry_to_disk(e)             # كما قُرئ بالضبط: لا يُعاد توقيعه ولا يُجرَّد توقيعه الأصلي
                    if isinstance(e.get("_mac"), str):
                        de["mac"] = e["_mac"]
                else:
                    if self._key is not None:               # مجلد العمل والمفتاح ورقم الإدخال داخل التوقيع: لا نسخ بين مجلدات ولا إعادة تشغيل لقديم
                        e.setdefault("n", secrets.token_hex(6))
                        e["ws"], e["kid"] = self._ws_id, self._kid
                    de = self._entry_to_disk(e)
                    de.pop("mac", None)
                    if self._key is not None:
                        de["mac"] = self._mac(de)
                hit = (e, json.dumps(de, ensure_ascii=True), len(e))
            live[id(e)] = hit
            parts.append(hit[1])
        self._ser_cache = live
        data = "[\n" + ",\n".join(parts) + "\n]"
        tmp = self.journal_path.with_name(f"{self.journal_path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            with open(str(tmp), "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())  # قطع الكهرباء بعد الاستبدال لا يترك سجلاً فارغاً
                except OSError:
                    pass
            retry_io(os.replace, str(tmp), str(self.journal_path))  # كتابة ذرّية: لا يبقى سجل نصف مكتوب
            self._disk_sig = self._sig()
        except BaseException:
            try:
                tmp.unlink()  # مؤقت فاشل لا يبقى فيُتبنّى لاحقاً كسجل
            except OSError:
                pass
            raise

    # ---------- نية العملية (write-ahead) والتعافي بعد انقطاع ----------
    def _intent_begin(self, rec: Dict[str, Any]) -> None:
        """يكتب (مع fsync) ما سنفعله قبل أي أثر على القرص؛ يُمحى بعد حفظ السجل. إن انقطع التيار بينهما تعيد الاستعادة بناء الحقيقة.
        نية موجودة سلفاً (من عملية ماتت) تُسوَّى أولاً ولا تُكتب فوقها."""
        if os.path.lexists(str(self.intent_path)):
            self._reconcile_intent()
        data = dict(rec, pid=os.getpid(), task=self.current_task, v=1)
        out = self._entry_to_disk(data)
        if isinstance(data.get("paths"), list):
            out["paths"] = [self._entry_to_disk({"path": x})["path"] for x in data["paths"]]
        if self._key is not None:
            out["ws"], out["kid"] = self._ws_id, self._kid
            out["mac"] = self._mac(out)
        tmp = self.allowed_root / f"{INTENT_NAME[:-5]}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            with open(str(tmp), "w", encoding="utf-8") as f:
                f.write(json.dumps(out, ensure_ascii=True))
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            retry_io(os.replace, str(tmp), str(self.intent_path))
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _intent_clear(self) -> None:
        try:
            retry_io(os.unlink, str(self.intent_path))
        except OSError:
            pass

    def _journal_has(self, action: str, **match) -> bool:
        return any(e.get("action") == action and all(e.get(k) == v for k, v in match.items()) for e in self.journal[-200:])

    def _vpath(self, v: Any, allow_trash: bool = False) -> str:
        """مسار من ملف النية بعد التحقق الكامل (داخل الجذر، بلا '..'، غير محمي) أو _Rejected."""
        try:
            return str(self.validate_path(self._path_from_disk(v), allow_trash_internal=allow_trash))
        except (PathOutOfBoundsError, ProtectedPathError, OSError, ValueError) as exc:
            raise _Rejected(str(exc))

    def _validated_intent(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """كل مسار في النية يمرّ بـ validate_path، والاسم المؤقت يطابق الشكل الذي نولّده تماماً وفي نفس مجلد الهدف، وإلا _Rejected."""
        action = raw.get("action")
        rec: Dict[str, Any] = {"action": action, "task": raw.get("task") if isinstance(raw.get("task"), (str, type(None))) else None}
        if action in ("move", "rename", "copy"):
            rec["src"] = self._vpath(raw.get("src"))
            rec["dest"] = self._vpath(raw.get("dest"))
            rec["is_dir"] = bool(raw.get("is_dir"))
            tmp = raw.get("tmp")
            if tmp is not None and action in ("rename", "copy"):
                tp = Path(self._vpath(tmp))
                rx = _COPY_TMP_RE.match(tp.name) if action == "copy" else (tp.name.startswith(RENAME_TMP_PREFIX) and len(tp.name) > len(RENAME_TMP_PREFIX))
                same_dir = tp.parent == (Path(rec["dest"]).parent if action == "copy" else Path(rec["src"]).parent)
                if not rx or not same_dir:
                    raise _Rejected("اسم مؤقت غير مألوف")
                rec["tmp"] = str(tp)
            else:
                rec["tmp"] = None
        elif action == "delete":
            rec["original_path"] = self._vpath(raw.get("original_path"))
            tr = self._vpath(raw.get("trash_path"), allow_trash=True)
            if Path(tr).parent != self.trash_dir:
                raise _Rejected("وجهة السلة خارج .trash")
            rec["trash_path"] = tr
        elif action == "create_dir":
            ps = raw.get("paths")
            if not isinstance(ps, list):
                raise _Rejected("قائمة مسارات غير صالحة")
            rec["paths"] = [self._vpath(x) for x in ps]
        else:
            raise _Rejected("نوع عملية غير معروف")
        return rec

    def _reconcile_intent(self) -> None:
        """بعد انقطاع بين الفعل وحفظ السجل: نطابق ملف النية بما على القرص فنتبنّى عملية مكتملة غير مسجلة، أو نعيد/نُنظّف المؤقت."""
        # ملفات نية مؤقتة بقيت من انقطاع قبل استبدالها: لم تبدأ العملية بعد (النية تُكتب قبل أي أثر) فتُزال
        try:
            for e in os.scandir(str(self.allowed_root)):
                m = re.match(re.escape(INTENT_NAME[:-5]) + r"\.(\d+)\.[0-9a-f]+\.tmp$", e.name)
                if m and (int(m.group(1)) == os.getpid() or not pid_alive(int(m.group(1)))):
                    try:
                        os.unlink(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
        if not os.path.lexists(str(self.intent_path)):
            return
        note = self.undo_notes.append
        try:
            raw = json.loads(self.intent_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("intent")
            if self._key is not None and not self._bound_ok(raw):
                raise _Rejected("intent unsigned")
            rec = self._validated_intent(raw)
        except Exception as exc:
            try:
                os.replace(str(self.intent_path), str(self._get_unique_path(self.allowed_root / ".undo_journal.intent.rejected.json")))
            except OSError:
                self._intent_clear()
            if isinstance(exc, _Rejected):
                note("وُجد ملف نية عملية غير موثوق (غير موقّع أو بمسارات خارج مجلد العمل أو بأسماء مؤقتة غير مألوفة)؛ لم يُنفَّذ منه شيء وحُفظ للفحص في .undo_journal.intent.rejected.json.")
            else:
                note("وُجد سجل نية عملية تالف بعد انقطاع سابق؛ حُفظ للفحص ولم يمكن التحقق مما جرى. راجع آخر الملفات التي عملت عليها.")
            return
        action, task = rec.get("action"), rec.get("task")
        ex = lambda x: bool(x) and os.path.lexists(str(x))   # noqa: E731
        adopted = None
        try:
            if action in ("move", "rename"):
                src, dest, tmp = rec.get("src"), rec.get("dest"), rec.get("tmp")
                if self._journal_has(action, src=src, dest=dest):
                    pass
                elif ex(tmp):
                    # انقطاع بين خطوتي تغيير حالة الأحرف: الملف باسمه المؤقت المخفي، نعيده لاسمه الأصلي
                    target = src if not ex(src) else str(self._get_unique_path(Path(src)))
                    _safe_move(str(tmp), target)
                    note(f"اكتُشف أن إعادة تسمية '{Path(src).name}' انقطعت في منتصفها؛ أُعيد الملف إلى اسمه الأصلي '{Path(target).name}'.")
                elif ex(dest) and (not ex(src) or _same_file(src, dest)):
                    adopted = {"task": task, "action": action, "src": src, "dest": dest}
                elif ex(src) and not ex(dest):
                    pass   # لم تُنفَّذ العملية أصلاً
                else:
                    note(f"انقطعت عملية {action} على '{Path(str(src)).name}' ولم أستطع الجزم بحالتها؛ تحقق من الملف يدوياً.")
            elif action == "delete":
                orig, trash = rec.get("original_path"), rec.get("trash_path")
                if self._journal_has("delete", original_path=orig, trash_path=trash):
                    pass
                elif ex(trash) and not ex(orig):
                    adopted = {"task": task, "action": "delete", "original_path": orig, "trash_path": trash}
                elif not ex(trash):
                    pass
                else:
                    note(f"انقطعت عملية حذف '{Path(str(orig)).name}' ولم أستطع الجزم بحالتها؛ تحقق من السلة يدوياً.")
            elif action == "copy":
                src, dest, tmp = rec.get("src"), rec.get("dest"), rec.get("tmp")
                if self._journal_has("copy", src=src, dest=dest):
                    pass
                elif ex(tmp):
                    moved = self._partial_trash_path(Path(str(dest)).name)
                    _safe_move(str(tmp), str(moved))
                    note(f"انقطعت عملية نسخ '{Path(str(src)).name}' قبل اكتمالها؛ نُقلت النسخة الناقصة إلى السلة باسم '{moved.name}'.")
                elif ex(dest):
                    adopted = {"task": task, "action": "copy", "src": src, "dest": dest, "is_dir": bool(rec.get("is_dir"))}
            elif action == "create_dir":
                for pth in rec.get("paths") or []:
                    if os.path.isdir(str(pth)) and not self._journal_has("create_dir", path=pth):
                        self.journal.append({"task": task, "action": "create_dir", "path": pth})
                        adopted = adopted or True
            else:
                note("وُجدت نية عملية غير معروفة بعد انقطاع سابق؛ تُجوهلت.")
        except Exception as exc:
            note(f"تعذّر إكمال استعادة عملية مقطوعة ({action}): {explain_error(exc) or 'خطأ غير متوقع'}. تحقق من الملفات يدوياً.")
            self._intent_clear()
            return
        if adopted:
            if isinstance(adopted, dict):
                self.journal.append(adopted)
            try:
                self._save_journal()
                note(f"اكتُشفت عملية ({action}) نُفّذت قبل انقطاع سابق ولم تُسجَّل، وسُجِّلت الآن فيمكن التراجع عنها.")
            except Exception as exc:
                note(f"عملية ({action}) نُفّذت قبل انقطاع سابق وتعذّر تسجيلها للتراجع: {explain_error(exc) or 'خطأ'}.")
        self._intent_clear()

    def _sweep_orphans(self) -> None:
        """بقايا نسخة مؤقتة يتيمة بعد انقطاع (.cos_copy_tmp_<8 hex>) تنتقل للسلة. (اسم إعادة التسمية المؤقت يُستعاد عبر ملف النية فقط؛ قد يكون اسماً اختاره المستخدم.)"""
        deadline = time.monotonic() + _SWEEP_MAX_SECONDS
        seen = 0
        stack = [self.allowed_root]
        while stack:
            d = stack.pop()
            try:
                entries = list(os.scandir(str(d)))
            except OSError:
                continue
            for e in entries:
                seen += 1
                if seen > _SWEEP_MAX_ENTRIES or time.monotonic() > deadline:
                    return
                if d == self.allowed_root and (e.name == ".trash" or e.name.startswith(".undo_journal")):
                    continue
                pth = Path(e.path)
                try:
                    if _COPY_TMP_RE.match(e.name):   # الشكل الدقيق الذي نولّده فقط (اسم مستخدم يشبهه لا يُمَس)
                        moved = self._partial_trash_path(e.name[len(COPY_TMP_PREFIX):] or e.name)
                        _safe_move(str(pth), str(moved))
                        self.undo_notes.append(f"وُجدت بقايا نسخة غير مكتملة '{e.name}' فنُقلت إلى السلة باسم '{moved.name}'.")
                    elif e.is_dir(follow_symlinks=False) and not _is_link(pth):
                        stack.append(pth)
                except OSError:
                    continue

    def validate_path(self, path: Union[str, Path], allow_trash_internal: bool = False) -> Path:
        """التحقق الأمني الصارم من المسار (R7 و R8):

        - داخل الجذر المسموح.
        - فحص الروابط الرمزية (symlinks) للتأكد من عدم الإشارة لخارج الجذر.
        - رفض استهداف الجذر نفسه أو ملف السجل أو مجلد السلة مباشرة.
        """
        if is_unc_like(path):
            raise PathOutOfBoundsError(f"المسارات الشبكية/الأجهزة غير مسموحة: '{path}'.")
        p = Path(path)
        # تتبع وفحص الروابط الرمزية قبل أو أثناء الحل
        if not p.is_absolute():
            unresolved = self.allowed_root / p
        else:
            unresolved = p

        anchor = Path(path).anchor
        for part in Path(path).parts:
            if part == anchor or part in ("/", "\\") or part in (".", ".."):
                continue
            if _bad_component(part):
                raise PathOutOfBoundsError(f"اسم غير مسموح في المسار (اسم جهاز محجوز أو يحوي ':' أو ينتهي بنقطة/مسافة): '{part}'.")
            if part.lower() in _PROTECTED_NAMES or _ALIAS83.match(part):
                raise ProtectedPathError(f"المسار يمر بمجلد محمي لا يُمَس عبر المساعد: '{part}'.")

        resolved = unresolved.resolve()
        if IS_WINDOWS and len(str(resolved)) > MAX_PATH_CHARS:
            raise PathOutOfBoundsError(
                f"المسار أطول من حد ويندوز ({len(str(resolved))} > {MAX_PATH_CHARS} محرفاً). اختصر الأسماء أو انقل مجلد العمل إلى مسار أقصر.")

        # فحص الخروج من الجذر المسموح
        try:
            rel_parts = resolved.relative_to(self.allowed_root).parts   # مرة واحدة (relative_to مكلفة)
        except ValueError:
            raise PathOutOfBoundsError(f"المسار '{path}' يقع خارج المجلد الجذر المسموح به '{self.allowed_root}'.")
        for rp in rel_parts:  # بعد الحل: الاسم القصير GIT~1 يظهر باسمه الحقيقي .git
            if rp.lower() in _PROTECTED_NAMES:
                raise ProtectedPathError(f"المسار يمر بمجلد محمي لا يُمَس عبر المساعد: '{rp}'.")

        # فحص إذا كان هناك رابط رمزي في المسار يشير إلى خارج الجذر
        curr = unresolved
        while curr != self.allowed_root and curr != curr.parent:
            if _is_link(curr):
                # أي رابط/تقاطع مرفوض (حتى داخل الجذر): قد يُظهر اسماً بريئاً ويؤثر على عنصر آخر
                raise PathOutOfBoundsError(f"الروابط الرمزية والتقاطعات غير مدعومة: '{curr}'.")
            curr = curr.parent

        # R7: حماية المسارات الخاصة (الجذر، السجل، السلة)
        if resolved == self.allowed_root:
            raise ProtectedPathError("لا يمكن استهداف المجلد الجذر نفسه في العمليات.")

        if resolved == self.journal_path:
            raise ProtectedPathError("لا يمكن استهداف ملف سجل التراجع المحمي مباشرة.")

        rel_top = rel_parts
        if rel_top and rel_top[0].lower().startswith(".undo_journal"):  # يشمل الملف المؤقت .tmp ونسخة التالف .corrupt
            raise ProtectedPathError("الأسماء التي تبدأ بـ .undo_journal محجوزة لسجل التراجع.")

        if resolved == self.trash_dir and not allow_trash_internal:
            raise ProtectedPathError("لا يمكن استهداف مجلد سلة المحذوفات .trash مباشرة.")

        if self.trash_dir in resolved.parents and not allow_trash_internal:
            raise ProtectedPathError("المسار يقع داخل سلة المحذوفات .trash المحمية.")

        return resolved

    def _scan_protected(self, source: Path) -> None:
        """نسخ/نقل/حذف مجلد أب لا يلتف على حماية .ssh/.git…: نمرّ على أسماء ما بداخله قبل أي أثر."""
        if not source.is_dir():
            return
        for _cur, dirs, files in os.walk(str(source), followlinks=False):
            for name in list(dirs) + list(files):
                if name.lower() in _PROTECTED_NAMES or _ALIAS83.match(name):
                    raise ProtectedPathError(f"المجلد يحوي عنصراً محمياً '{name}' فلا يُنسخ/يُنقل/يُحذف عبر المساعد.")

    def _scan_links(self, source: Path) -> None:
        """نسخ مجلد فيه رابط/تقاطع غير مدعوم: يُكشف قبل أي أثر لا في منتصف النسخ."""
        if not source.is_dir():
            return
        for cur, dirs, files in os.walk(str(source), followlinks=False):
            for name in list(dirs) + list(files):
                if _is_link(Path(cur) / name):
                    raise PathOutOfBoundsError(f"روابط رمزية داخل المجلد غير مدعومة: {Path(cur) / name}")

    def _scan_cloud(self, source: Path) -> None:
        """قبل نسخ ملف/مجلد: هل فيه ملف سحابي كبير غير محمّل؟ (يُكتشف قبل أي تنفيذ لا في منتصف الخطة)"""
        if source.is_file():
            check_not_big_cloud_file(source)
            return
        for cur, _dirs, files in os.walk(str(source), followlinks=False):
            for name in files:
                check_not_big_cloud_file(Path(cur) / name)

    def _get_unique_path(self, target: Path) -> Path:
        """توليد مسار فريد في حال وجود تعارض لمنع الكتابة فوق ملفات موجودة."""
        if not os.path.lexists(str(target)):  # رابط معلّق يُعدّ موجوداً: وإلا كتبنا عبره خارج المجلد
            return target
        parent = target.parent
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while True:
            tail = f" ({counter}){suffix}"
            st = stem
            while len((st + tail).encode("utf-8", "surrogatepass")) > 240 and len(st) > 1:  # اسم قريب من حد النظام: نقصّ الجذع لا نفشل
                st = st[:-1]
            candidate = parent / f"{st}{tail}"
            if not os.path.lexists(str(candidate)):
                return candidate
            counter += 1

    def _partial_trash_path(self, name: str) -> Path:
        """اسم في السلة لنسخة ناقصة؛ البادئة لا تدفع الاسم الطويل فوق حد النظام (وإلا بقيت النسخة يتيمة في المجلد)."""
        keep = name
        while len(("partial_" + keep).encode("utf-8", "surrogatepass")) > 200 and len(keep) > 1:
            keep = keep[:-1]
        return self._get_unique_path(self.trash_dir / ("partial_" + keep))

    def _discard_copy_tmp(self, tmp: Path, label: str) -> bool:
        """نسخة مؤقتة فاشلة من صنعنا: تُنقل للسلة؛ وإن تعذّر ذلك (مثلاً إعادة التسمية نفسها معطلة) تُزال لأنها نسخة من مصدر ما زال موجوداً
        ولا تحوي شيئاً فريداً (آخر ملاذ؛ لا يمسّ أي اسم لا يطابق شكل مؤقتاتنا بدقة). True إن لم يبقَ أثر."""
        if not os.path.lexists(str(tmp)):
            return True
        try:
            _safe_move(str(tmp), str(self._partial_trash_path(label)))
            return True
        except Exception:
            pass
        if _COPY_TMP_RE.match(tmp.name) and not _is_link(tmp):
            try:
                if tmp.is_dir():
                    shutil.rmtree(str(tmp))
                else:
                    tmp.unlink()
                self.undo_notes.append("تعذّر نقل نسخة غير مكتملة إلى السلة فأُزيلت (هي نسخة مؤقتة من ملف ما زال موجوداً).")
                return True
            except OSError:
                pass
        return False

    def _check_stop(self):
        if self.kill_switch is not None:
            self.kill_switch.check()

    def _copy_tree(self, source: Path, dest: Path, label: Optional[str] = None):
        """نسخ مجلد ملفاً ملفاً (إلى dest المؤقت) مع فحص زر الإيقاف (R23). عند الفشل/الإيقاف يُنقل الناتج الجزئي إلى السلة."""
        created_any = False
        try:
            dest.mkdir(parents=True, exist_ok=False)
            created_any = True
            for current, dirs, files in os.walk(str(source), followlinks=False):
                self._check_stop()
                cur = Path(current)
                rel = cur.relative_to(source)
                for d in dirs:
                    if _is_link(cur / d):
                        raise PathOutOfBoundsError(f"روابط رمزية داخل المجلد غير مدعومة: {cur / d}")
                    (dest / rel / d).mkdir(parents=True, exist_ok=True)
                for name in files:
                    self._check_stop()
                    f = cur / name
                    if _is_link(f):
                        raise PathOutOfBoundsError(f"روابط رمزية داخل المجلد غير مدعومة: {f}")
                    check_not_big_cloud_file(f)
                    retry_io(shutil.copy2, str(f), str(dest / rel / name))
        except BaseException:
            if created_any and os.path.lexists(str(dest)):
                try:
                    if not self._discard_copy_tmp(dest, label or dest.name):
                        pass
                except Exception:
                    pass
            raise

    def _ensure_dirs(self, d: Path):
        """ينشئ كل المجلدات الناقصة بين الجذر و d ويسجل كل واحد منها للتراجع (فلا يبقى مجلد يتيم بعد التراجع)."""
        missing = []
        cur = d
        while cur != self.allowed_root and cur != cur.parent and not cur.exists():
            missing.append(cur)
            cur = cur.parent
        if not missing:
            return
        ordered = list(reversed(missing))
        self._intent_begin({"action": "create_dir", "paths": [str(m) for m in ordered]})
        made = []
        try:
            for m in ordered:
                m.mkdir(exist_ok=True)
                made.append(m)
                self.journal.append({"action": "create_dir", "path": str(m), "task": self.current_task})
            if made:
                self._save_journal()
        except Exception:
            clean = True
            for m in reversed(made):
                if self.journal and self.journal[-1].get("path") == str(m):
                    self.journal.pop()
                try:
                    m.rmdir()
                except Exception:
                    clean = False
            if clean:
                self._intent_clear()
            raise
        self._intent_clear()

    def create_dir(self, dir_path: Union[str, Path]) -> Path:
        """إنشاء مجلد (وأي آباء ناقصين) داخل الجذر وتوثيق كل مستوى في سجل التراجع."""
        with self._op():
            p = self.validate_path(dir_path)
            if p.exists():
                return p
            self._ensure_dirs(p)
            return p

    def move(self, src: Union[str, Path], dest: Union[str, Path]) -> Path:
        """نقل ملف أو مجلد مع تجنب التعارض وتسجيل العملية للتراجع والتراجع إن فشل الحفظ."""
        with self._op():
            return self._move_locked(src, dest)

    def _move_locked(self, src: Union[str, Path], dest: Union[str, Path]) -> Path:
        source = self.validate_path(src)
        if not source.exists():
            raise FileNotFoundError(f"الملف المصدر غير موجود: {source}")

        destination = self.validate_path(dest)
        if destination.is_dir() and destination != source:
            destination = self.validate_path(destination / source.name)  # الوجهة مجلد موجود: ضع العنصر بداخله
        if destination == source or os.path.normcase(str(destination)) == os.path.normcase(str(source)):
            raise ValueError(f"المصدر والوجهة هما نفس المكان: {source}")
        self._scan_protected(source)
        if source.is_dir() and (source in destination.parents or destination == source):
            raise ValueError(f"لا يمكن نقل المجلد '{source}' إلى داخل نفسه أو مجلد فرعي منه '{destination}'.")
        self._ensure_dirs(destination.parent)
        final_dest = self.validate_path(self._get_unique_path(destination))

        self._intent_begin({"action": "move", "src": str(source), "dest": str(final_dest)})
        try:
            _safe_move(str(source), str(final_dest))
        except BaseException:
            self._intent_clear()
            raise
        self.journal.append({
            "task": self.current_task,
            "action": "move",
            "src": str(source),
            "dest": str(final_dest),
            "sig": _file_sig(final_dest),     # هوية الملف المنقول (للمجلدات None): يمنع تراجعاً قديماً مُعاد من نقل عنصر آخر
        })
        try:
            self._save_journal()
        except Exception:
            self.journal.pop()
            try:
                os.rename(str(final_dest), str(source))
                self._intent_clear()
            except Exception:
                pass   # تبقى النية: الاستعادة التالية تتبنّى النقل المنفَّذ
            raise
        self._intent_clear()
        return final_dest

    def reset_copy_budget(self, task: Optional[str] = None) -> None:
        """يصفّر عدّاد بايتات النسخ لمهمة ويُسقط موافقتها على سقف أعلى (يستدعيه المنسّق عند بدء/نهاية كل مهمة)."""
        with self._copy_lock:
            self._copy_spent.pop(task, None)
            self._copy_cap_override.pop(task, None)

    def allow_large_copy(self, task: Optional[str], total_bytes: int) -> None:
        """موافقة صريحة من المستخدم على نسخ يتجاوز السقف الافتراضي لهذه المهمة فقط (حتى total_bytes). المساحة الحرة تبقى حدّاً صارماً."""
        with self._copy_lock:
            self._copy_cap_override[task] = max(int(total_bytes), self.max_copy_bytes or 0)

    def copy_cap_for(self, task: Optional[str]) -> Optional[int]:
        with self._copy_lock:
            if self.max_copy_bytes is None:
                return None
            return max(self.max_copy_bytes, self._copy_cap_override.get(task, 0))

    def trash_size(self, limit: Optional[int] = None) -> int:
        return tree_size(self.trash_dir, limit)

    def _prune_copy_spent(self, now: float) -> None:
        for k in [k for k, (_b, t0) in self._copy_spent.items() if now - t0 > COPY_BUDGET_TTL_SECONDS]:
            self._copy_spent.pop(k, None)
            self._copy_cap_override.pop(k, None)

    def _charge_copy(self, source: Path) -> int:
        """يحجز حجم النسخة القادمة من ميزانية المهمة الجارية ويتحقق من المساحة الحرة وسقف السلة؛ يرفع CopyBudgetExceededError قبل أي أثر."""
        with self._copy_lock:
            now = time.monotonic()
            self._prune_copy_spent(now)
            key = self.current_task
            spent = self._copy_spent.get(key, (0, now))[0]
            cap = self.copy_cap_for(key)
            size = tree_size(source, None if cap is None else cap - spent)
            if cap is not None and spent + size > cap:
                raise CopyBudgetExceededError(
                    f"النسخ سيتجاوز سقف المهمة ({human_size(cap)}): المنسوخ سابقاً {human_size(spent)} والحالي {human_size(size)}+. "
                    f"يلزم موافقتك الصريحة على حجم أكبر. لم يُنسخ شيء.")
            try:
                free = shutil.disk_usage(str(self.allowed_root)).free
            except OSError:
                free = None
            if free_space_short(size, free, self.max_free_margin):
                raise CopyBudgetExceededError(
                    f"المساحة الحرة على القرص ({human_size(free)}) لا تكفي لنسخ {human_size(size)} مع هامش أمان. لم يُنسخ شيء.")
            if size > 0 and self.trash_cap_bytes is not None:
                tsize = self.trash_size(self.trash_cap_bytes)
                if tsize > self.trash_cap_bytes:
                    raise CopyBudgetExceededError(
                        f"سلة المحذوفات .trash ممتلئة ({human_size(tsize)} > {human_size(self.trash_cap_bytes)}). "
                        f"أفرغها يدوياً (لا يحذف المساعد شيئاً نهائياً) ثم أعد المحاولة. لم يُنسخ شيء.")
            self._copy_spent[key] = (spent + size, self._copy_spent.get(key, (0, now))[1])
            return size

    def _refund_copy(self, size: int) -> None:
        with self._copy_lock:
            key = self.current_task
            if key in self._copy_spent:
                b, t0 = self._copy_spent[key]
                self._copy_spent[key] = (max(0, b - size), t0)

    def copy(self, src: Union[str, Path], dest: Union[str, Path]) -> Path:
        """نسخ ملف أو مجلد مع منع نسخ المجلد إلى داخل نفسه وتسجيل العملية والتراجع إن فشل الحفظ.
        يُكتب أولاً باسم مؤقت مخفي (.cos_copy_tmp_*) ثم يُعاد تسميته للاسم النهائي بعملية واحدة: لا ملف ناقص باسمه النهائي أبداً."""
        with self._op():
            return self._copy_locked(src, dest)

    def _copy_locked(self, src: Union[str, Path], dest: Union[str, Path]) -> Path:
        source = self.validate_path(src)
        if not source.exists():
            raise FileNotFoundError(f"الملف المصدر غير موجود: {source}")

        destination = self.validate_path(dest)
        self._scan_protected(source)
        self._scan_links(source)

        # R12: منع نسخ مجلد إلى داخل نفسه أو إلى مجلد فرعي منه
        if source.is_dir():
            if destination == source or source in destination.parents:
                raise ValueError(f"لا يمكن نسخ المجلد '{source}' إلى داخل نفسه أو مجلد فرعي منه '{destination}'.")

        if destination.is_dir() and destination != source:
            destination = self.validate_path(destination / source.name)
            if source.is_dir() and (destination == source or source in destination.parents):
                raise ValueError(f"لا يمكن نسخ المجلد '{source}' إلى داخل نفسه أو مجلد فرعي منه '{destination}'.")
        charged = self._charge_copy(source)  # قبل أي أثر على القرص
        try:
            self._ensure_dirs(destination.parent)
            final_dest = self.validate_path(self._get_unique_path(destination))
        except BaseException:
            self._refund_copy(charged)
            raise

        tmp = final_dest.parent / (COPY_TMP_PREFIX + secrets.token_hex(4))
        is_dir = source.is_dir()
        self._intent_begin({"action": "copy", "src": str(source), "dest": str(final_dest), "tmp": str(tmp), "is_dir": is_dir})
        try:
            if is_dir:
                self._copy_tree(source, tmp, label=final_dest.name)
            else:
                self._check_stop()
                check_not_big_cloud_file(source)
                retry_io(shutil.copy2, str(source), str(tmp))
            _safe_move(str(tmp), str(final_dest))   # الاسم النهائي لا يظهر إلا مكتملاً
        except BaseException:
            if self._discard_copy_tmp(tmp, final_dest.name):   # نسخة ناقصة يتيمة: تُنقل للسلة لا تُترك
                self._intent_clear()
            self._refund_copy(charged)
            raise

        self.journal.append({
            "task": self.current_task,
            "action": "copy",
            "src": str(source),
            "dest": str(final_dest),
            "is_dir": is_dir,
            "sig": path_signature(final_dest),   # هوية النسخة وقت إنشائها: إن عدّلها المستخدم لاحقاً لا يحذفها التراجع
        })
        try:
            self._save_journal()
        except Exception:
            self.journal.pop()
            try:  # لا حذف نهائي: النسخة التي لم تُسجَّل تُنقل للسلة
                _safe_move(str(final_dest), str(self._partial_trash_path(final_dest.name)))
                self._intent_clear()
            except Exception:
                pass
            raise
        self._intent_clear()
        return final_dest

    def rename(self, src: Union[str, Path], new_name: str) -> Path:
        """إعادة تسمية ملف أو مجلد مع تجنب التعارض والتراجع إن فشل الحفظ."""
        with self._op():
            return self._rename_locked(src, new_name)

    def _rename_locked(self, src: Union[str, Path], new_name: str) -> Path:
        if not isinstance(new_name, str) or not new_name.strip():
            raise ValueError("يجب تحديد اسم جديد صالح.")

        # R12: رفض أي فواصل مسار أو مسارات نسبية تحوّل التسمية إلى نقل
        if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
            raise ValueError(f"الاسم الجديد '{new_name}' غير صالح ويجب ألا يحتوي على فواصل مسارات أو '..'.")

        source = self.validate_path(src)
        if not source.exists():
            raise FileNotFoundError(f"الملف المصدر غير موجود: {source}")

        target = source.parent / new_name
        final_target = self.validate_path(target)
        if new_name == source.name:  # نقارن النص الخام: validate_path على ويندوز يعيد حالة الأحرف الموجودة فعلاً
            raise ValueError(f"الاسم الجديد مطابق للاسم الحالي: {source.name}")
        same_entry = False
        if new_name.casefold() == source.name.casefold():
            try:
                same_entry = os.path.lexists(final_target) and os.path.samefile(final_target, source)
            except OSError:
                same_entry = False
        if same_entry:  # تغيير حالة الأحرف فقط (a.txt → A.txt) على نظام لا يميّزها: عبر اسم مؤقت
            final_dest = source.parent / new_name  # بحالة الأحرف التي طلبها المستخدم لا التي أعادها resolve
            tmp = self._get_unique_path(source.parent / (RENAME_TMP_PREFIX + source.name))
            self._intent_begin({"action": "rename", "src": str(source), "dest": str(final_dest), "tmp": str(tmp)})
            try:
                _safe_move(str(source), str(tmp))
            except BaseException:
                self._intent_clear()
                raise
            try:
                _safe_move(str(tmp), str(final_dest))
            except BaseException:
                _safe_move(str(tmp), str(source))
                self._intent_clear()
                raise
        else:
            final_dest = self.validate_path(self._get_unique_path(final_target))
            self._intent_begin({"action": "rename", "src": str(source), "dest": str(final_dest)})
            try:
                _safe_move(str(source), str(final_dest))
            except BaseException:
                self._intent_clear()
                raise
        self.journal.append({
            "task": self.current_task,
            "action": "rename",
            "src": str(source),
            "dest": str(final_dest),
            "sig": _file_sig(final_dest),
        })
        try:
            self._save_journal()
        except Exception:
            self.journal.pop()
            try:
                os.rename(str(final_dest), str(source))
                self._intent_clear()
            except Exception:
                pass
            raise
        self._intent_clear()
        return final_dest

    def delete(self, target: Union[str, Path]) -> Path:
        """لا يوجد حذف نهائي: النقل إلى مجلد .trash مع التراجع إن فشل حفظ السجل."""
        with self._op():
            return self._delete_locked(target)

    def _delete_locked(self, target: Union[str, Path]) -> Path:
        p = self.validate_path(target)
        if not p.exists():
            raise FileNotFoundError(f"الملف المراد حذفه غير موجود: {p}")

        self._scan_protected(p)
        if _is_link(self.trash_dir) or not self.trash_dir.is_dir():
            raise ProtectedPathError("مجلد السلة .trash لم يعد مجلداً عادياً — أوقفت الحذف حمايةً للملفات.")
        dest = self.trash_dir / p.name
        final_dest = self._get_unique_path(dest)
        if final_dest.parent != self.trash_dir:
            raise ProtectedPathError("وجهة السلة خارج مجلد السلة.")
        self._intent_begin({"action": "delete", "original_path": str(p), "trash_path": str(final_dest)})
        try:
            _safe_move(str(p), str(final_dest))
        except BaseException:
            self._intent_clear()
            raise

        self.journal.append({
            "task": self.current_task,
            "action": "delete",
            "original_path": str(p),
            "trash_path": str(final_dest),
        })
        try:
            self._save_journal()
        except Exception:
            self.journal.pop()
            try:
                os.rename(str(final_dest), str(p))
                self._intent_clear()
            except Exception:
                pass
            raise
        self._intent_clear()
        return final_dest

    def permanent_delete(self, target: Union[str, Path]):
        """محظور نهائياً بموجب قواعد الأمان."""
        raise PermanentDeleteForbidden("الحذف النهائي محظور نهائياً بموجب قواعد الأمان. استخدم دالة delete للنقل إلى سلة المحذوفات.")

    def _check_state(self, op: Dict[str, Any], action: str) -> None:
        """فحص حالة القرص قبل أي تراجع (لكل الإدخالات). الموثوق يبقى كما كان: المسار الأصلي المشغول بعنصر آخر يعني استعادة باسم بديل، لكن فقط إن كان
        الناتج المنقول هو نفسه (بصمة الحجم/الوقت المسجَّلة) — سجل قديم أُعيد بعد أن أنشأ المستخدم ملفاً جديداً بالاسم نفسه لا يُنفَّذ.
        غير الموثَّق صارم: الناتج موجود والأصل غائب (نقل/حذف)، المصدر ما زال موجوداً (نسخة)، المجلد فارغ (create_dir)."""
        strict = bool(op.get("unverified"))
        ex = lambda x: os.path.lexists(str(self.validate_path(x, allow_trash_internal=True)))   # noqa: E731
        ok = True
        if action in ("move", "rename"):
            if not ex(op["dest"]):
                return     # الناتج غائب: التراجع العادي يعدّه «missing» ولا يمسّ شيئاً
            src_p = self.validate_path(op["src"])
            dest_p = self.validate_path(op["dest"])
            same = False
            try:
                same = os.path.lexists(str(src_p)) and os.path.samefile(str(src_p), str(dest_p))
            except OSError:
                pass
            if not ex(op["src"]) or same:
                return
            sig = op.get("sig")
            ok = (not strict) and (sig is None or _file_sig(dest_p) == list(sig))
        elif action == "delete":
            if not ex(op["trash_path"]):
                return
            ok = (not strict) or not ex(op["original_path"])
        elif action == "copy":
            if not ex(op["dest"]):
                return
            ok = (not strict) or ex(op["src"])
        elif action == "create_dir":
            p = self.validate_path(op["path"])
            if not p.is_dir():
                return
            ok = (not strict) or not any(p.iterdir())
        if not ok:
            raise _KeepInPlace("حالة القرص لا تطابق ما يدّعيه هذا الإدخال في سجل التراجع (المسار الأصلي صار لعنصر آخر، أو الإدخال غير موثَّق/قديم أُعيد)؛ لم يُنفَّذ وحُفظ للفحص.")

    def _unverified_allowed(self, entry: Dict[str, Any]) -> bool:
        """هل يجوز تنفيذ إدخال غير موثَّق؟ بتأكيد المستخدم الصريح (allow_unverified)، أو إن عُرف معرّف مهمته في الدفتر (خارج مجلد العمل)."""
        if self.allow_unverified:
            return True
        k, t = self.known_task_ids, entry.get("task")
        if k is None or not isinstance(t, str) or not t:
            return False
        try:
            return bool(k(t)) if callable(k) else t in k
        except Exception:
            return False

    def unverified_count(self, pending_only: bool = True) -> int:
        """عدد إدخالات السجل غير الموثَّقة (pending_only: التي لن تُنفَّذ الآن دون تأكيد)."""
        return sum(1 for e in self.journal if e.get("unverified") and (not pending_only or not self._unverified_allowed(e)))

    def _undo_op(self, op: Dict[str, Any]) -> str:
        """يتراجع عن عملية واحدة. يعيد "ok" أو "missing" (الناتج المتوقع لم يعد موجوداً فلا شيء يُستعاد) ويرفع عند الخطأ."""
        action = op.get("action")
        if action in ("move", "rename", "copy", "delete", "create_dir"):
            self._check_state(op, action)
        if action in ("move", "rename"):
            src = self.validate_path(op["src"])
            dest = self.validate_path(op["dest"])
            if not dest.exists():
                return "missing"
            src.parent.mkdir(parents=True, exist_ok=True)
            raw_src, raw_dest = Path(op["src"]), Path(op["dest"])  # الأسماء الخام (validate_path يوحّد الحالة على ويندوز)
            if raw_src.parent == raw_dest.parent and raw_src.name != raw_dest.name \
                    and raw_src.name.casefold() == raw_dest.name.casefold():
                try:
                    case_only = os.path.lexists(src) and os.path.samefile(src, dest)
                except OSError:
                    case_only = False
                if case_only:  # تراجع عن تغيير حالة الأحرف فقط: عبر اسم مؤقت
                    tmp = self._get_unique_path(dest.parent / (".cos_rename_tmp_" + raw_dest.name))
                    _safe_move(str(dest), str(tmp))
                    try:
                        _safe_move(str(tmp), str(dest.parent / raw_src.name))
                    except BaseException:
                        _safe_move(str(tmp), str(dest))  # لا يبقى الملف باسم مؤقت مخفي
                        raise
                    return "ok"
            target = self._get_unique_path(src)  # لا كتابة فوق موجود
            _safe_move(str(dest), str(target))
            if target != src:
                self.undo_notes.append(f"استُعيد '{src.name}' باسم '{target.name}' لأن اسمه الأصلي صار مشغولاً.")
            return "ok"
        if action == "copy":
            dest = self.validate_path(op["dest"])  # تراجع النسخ ينقل الناتج إلى السلة ولا يحذفه نهائياً (R9)
            if not dest.exists():
                return "missing"
            sig = op.get("sig")
            if isinstance(sig, list) and path_signature(dest) != sig:
                raise _KeepInPlace(f"النسخة '{dest.name}' عُدّلت بعد إنشائها فتُركت في مكانها ولم تُحذف؛ راجعها يدوياً أو احذفها بنفسك.")
            size = tree_size(dest)
            _safe_move(str(dest), str(self._get_unique_path(self.trash_dir / dest.name)))
            if size > 0:
                self.undo_notes.append(
                    f"النسخة '{dest.name}' ({human_size(size)}) نُقلت إلى سلة المحذوفات .trash ولا تزال تشغل مساحة على القرص حتى تُفرَّغ السلة.")
            return "ok"
        if action == "create_dir":
            p = self.validate_path(op["path"])
            if not (p.exists() and p.is_dir()):
                return "missing"
            if any(p.iterdir()):  # فيه ملفات: يُنقل للسلة لحمايتها
                _safe_move(str(p), str(self._get_unique_path(self.trash_dir / p.name)))
            else:
                p.rmdir()
            return "ok"
        if action == "delete":
            orig = self.validate_path(op["original_path"])
            trash = self.validate_path(op["trash_path"], allow_trash_internal=True)
            if self.trash_dir not in trash.parents:
                raise ProtectedPathError("مسار السلة في السجل ليس داخل .trash؛ السجل مرفوض.")
            if not trash.exists():
                return "missing"
            orig.parent.mkdir(parents=True, exist_ok=True)
            target = self._get_unique_path(orig)
            _safe_move(str(trash), str(target))
            if target != orig:
                self.undo_notes.append(f"استُعيد '{orig.name}' باسم '{target.name}' لأن اسمه الأصلي صار مشغولاً.")
            return "ok"
        raise ValueError(f"عملية غير معروفة في السجل: {action!r}")

    def undo_step(self, lock_timeout: Optional[float] = UNDO_LOCK_TIMEOUT_SECONDS, skip: int = 0) -> str:
        """يتراجع عن آخر عملية: "ok" | "missing" (أُزيلت من السجل لأن الناتج اختفى) | "skipped" (خطأ دائم: تُخطّيت وحُفظت للفحص) | "error" (بقيت في السجل) | "empty".
        سبب آخر فشل بالعربية في last_undo_reason. مجلد العمل مشغول بنافذة أخرى -> "error" بسبب BUSY_UNDO_REASON بعد انتظار قصير (lock_timeout) ولا استثناء."""
        try:
            return self._undo_step_locked(lock_timeout, skip)
        except WorkspaceBusyError:
            self.last_undo_reason = BUSY_UNDO_REASON
            return "error"

    def _undo_step_locked(self, lock_timeout: Optional[float], skip: int = 0) -> str:
        """skip: عدد إدخالات من آخر السجل تُترك في مكانها (فشل التراجع عنها) فيُتراجَع عن الإدخال الذي تحتها."""
        with self._op(lock_timeout):
            self.last_undo_reason = None
            if len(self.journal) <= skip:
                return "empty"
            entry = self.journal[-1 - skip]
            if entry.get("unverified") and not self._unverified_allowed(entry):
                self.last_undo_reason = UNVERIFIED_REASON.format(n=self.unverified_count())
                return "unverified"        # لا يُنفَّذ ولا يُحذف ولا يُنقل: ينتظر تأكيد المستخدم
            try:
                result = self._undo_op(entry)  # R10: لا نحذف من السجل قبل نجاح الفعل
            except _KeepInPlace as e:
                self.last_undo_reason = str(e)
                return self._skip_entry(entry, e, note=str(e), skip=skip)
            except (ProtectedPathError, PathOutOfBoundsError, KeyError, TypeError, ValueError, AttributeError) as e:
                # خطأ دائم (إدخال فاسد/مسار مرفوض): لا يجوز أن يسدّ التراجع عن كل ما تحته؛ نحفظه للفحص ونتخطاه
                self.last_undo_reason = explain_error(e) or "مسار العملية مرفوض أو سجلها فاسد"
                return self._skip_entry(entry, e, skip=skip)
            except Exception as e:
                self.last_undo_reason = explain_error(e) or "تعذّر التراجع عن العملية الأخيرة لسبب غير متوقع"
                return "error"
            del self.journal[-1 - skip]
            try:
                self._save_journal()
            except Exception:
                # الأثر على القرص حدث فعلاً؛ فشل الحفظ لا يجعل الخطوة «خطأ» (سيُعاد تنظيف الإدخال القديم لاحقاً كـ missing)
                self.undo_notes.append("تم التراجع لكن تعذّر تحديث سجل التراجع؛ قد يظهر إدخال قديم لاحقاً.")
            return result

    def _skip_entry(self, entry: Dict[str, Any], err: BaseException, note: Optional[str] = None, skip: int = 0) -> str:
        try:
            self._aside_sidecar_links()
            _safe_append(self.allowed_root / ".undo_journal.skipped.jsonl",
                         json.dumps({"entry": self._entry_to_disk(entry) if isinstance(entry, dict) else str(entry),
                                     "error": str(err)}, ensure_ascii=True) + "\n")
        except Exception:
            pass
        idx = len(self.journal) - 1 - skip
        del self.journal[idx]
        try:
            self._save_journal()
        except Exception:
            self.journal.insert(idx, entry)
            return "error"
        what = entry.get("action") if isinstance(entry, dict) else "?"
        self.undo_notes.append(note or f"تعذّر التراجع عن عملية ({what}) لأن مسارها مرفوض أو سجلها فاسد؛ تُخطّيت وحُفظت للفحص.")
        return "skipped"

    def undo_last(self, lock_timeout: Optional[float] = UNDO_LOCK_TIMEOUT_SECONDS) -> bool:
        """True فقط إذا استُعيد شيء فعلاً. إن اختفى الناتج المتوقع أُزيلت العملية من السجل وتعاد False."""
        return self.undo_step(lock_timeout) == "ok"

    @staticmethod
    def entry_paths(entry: Dict[str, Any]) -> List[str]:
        return [str(entry[k]) for k in _JOURNAL_PATH_KEYS if isinstance(entry, dict) and isinstance(entry.get(k), str) and k != "tmp"]

    def undo_run(self, gate, lock_timeout: Optional[float] = UNDO_LOCK_TIMEOUT_SECONDS, max_failed: int = 50,
                 allow_unverified: Optional[bool] = None) -> "UndoResult":
        prev = self.allow_unverified
        if allow_unverified is not None:
            self.allow_unverified = bool(allow_unverified)
        try:
            return self._undo_run(gate, lock_timeout, max_failed)
        finally:
            self.allow_unverified = prev

    def _undo_run(self, gate, lock_timeout: Optional[float], max_failed: int) -> "UndoResult":
        """حلقة التراجع المشتركة. gate(skip) -> (متابعة؟، سبب إيقاف أو None) يحدد هل الإدخال عند -1-skip مطلوب التراجع عنه.
        فشل مؤقت في خطوة (قفل ملف…) لا يوقف المستقلّ عنها: نتابع الخطوات الأقدم التي لا تمسّ مساراً من الخطوات الفاشلة (ولا أصلاً/فرعاً منها)،
        ونترك كل ما يلمس مسار خطوة فاشلة في مكانه حفاظاً على سلامة الترتيب. التفصيل لكل خطوة في result.failures."""
        n_notes = len(self.undo_notes)
        count = skipped = missing = unverified = 0
        reason: Optional[str] = None
        failures: List[str] = []
        failed_paths: List[str] = []
        skip = 0
        while True:
            go, stop_reason = gate(skip)
            if not go:
                reason = reason or stop_reason
                break
            entry = self.journal[-1 - skip]
            paths = self.entry_paths(entry)
            if skip and any(_paths_overlap(a, b) for a in paths for b in failed_paths):
                failed_paths.extend(paths)    # يلمس مساراً فشلت خطوة لاحقة عليه: يبقى (الترتيب)
                skip += 1
                if entry.get("unverified") and not self._unverified_allowed(entry):
                    unverified += 1
                else:
                    failures.append(f"{entry.get('action')} '{Path(paths[0]).name if paths else '?'}': تُركت لأن خطوة لاحقة على المسار نفسه فشلت")
                continue
            r = self.undo_step(lock_timeout, skip)
            if r == "ok":
                count += 1
            elif r == "missing":
                missing += 1
            elif r == "skipped":
                skipped += 1
                reason = reason or self.last_undo_reason
            elif r == "empty":
                break
            elif r == "unverified":      # ينتظر تأكيداً: لا يمسّه شيء ولا ما يلمس مساره، ونتابع المستقلّ عنه
                unverified += 1
                failed_paths.extend(paths)
                skip += 1
            else:
                why = self.last_undo_reason or "تعذّر التراجع عن العملية الأخيرة"
                reason = reason or why
                if why == BUSY_UNDO_REASON or len(failed_paths) >= max_failed:
                    break
                failures.append(f"{entry.get('action')} '{Path(paths[0]).name if paths else '?'}': {why}")
                failed_paths.extend(paths)
                skip += 1
        if unverified:
            reason = reason or UNVERIFIED_REASON.format(n=unverified)
        return UndoResult(count, remaining=len(self.journal), reason=reason, notes=self.undo_notes[n_notes:],
                          skipped=skipped, missing=missing, failures=failures, unverified=unverified)

    def undo_all(self, lock_timeout: Optional[float] = UNDO_LOCK_TIMEOUT_SECONDS, allow_unverified: Optional[bool] = None) -> "UndoResult":
        """التراجع عن جميع العمليات بالترتيب العكسي؛ تتخطى العمليات التي اختفى ناتجها، وعند فشل خطوة تتابع المستقلّ عنها (انظر undo_run).
        تعيد UndoResult (تساوي عدد ما تُراجِع عنه فعلاً، ومعها remaining/reason/notes/failures)."""
        return self.undo_run(lambda skip: (len(self.journal) > skip, None), lock_timeout, allow_unverified=allow_unverified)

    @staticmethod
    def file_hash(file_path: Union[str, Path]) -> str:
        """حساب بصمة SHA-256 للملف للتأكد من سلامة المحتوى."""
        p = Path(file_path)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
