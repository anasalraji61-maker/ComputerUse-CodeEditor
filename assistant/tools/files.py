"""أدوات الملفات الآمنة مع قيد المجلد الجذر وحماية المسارات وسجل التراجع ومنع الحذف النهائي."""
import json
import os
import shutil
import hashlib
import re
from pathlib import Path
from typing import Union, List, Dict, Any, Optional

from assistant.safety import KillSwitch, EmergencyStopTriggered


_WIN_RESERVED = ({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                 | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
                 | {f"COM{c}" for c in "¹²³"} | {f"LPT{c}" for c in "¹²³"})
_PROTECTED_NAMES = {".git", ".ssh", ".gnupg", ".aws"}  # لا تُمَس عبر خطط المساعد حتى لو وُجدت داخل مجلد العمل
_REPARSE_FLAG = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (روابط/تقاطعات ويندوز)
_LINK_REPARSE_TAGS = {0xA000000C, 0xA0000003}  # IO_REPARSE_TAG_SYMLINK / MOUNT_POINT (junction)


def _bad_component(name: str) -> bool:
    """أسماء أجهزة ويندوز المحجوزة (CON, NUL, COM1…) وأسماء تنتهي بنقطة أو مسافة أو فيها : (تيارات NTFS)."""
    stem = name.split(".")[0].strip().upper()
    return stem in _WIN_RESERVED or name != name.rstrip(" .") or ":" in name


_ALIAS83 = re.compile(r"^\.?(git|ssh|gnupg|aws)~\d+$", re.IGNORECASE)  # الأسماء القصيرة 8.3 لمجلدات محمية (GIT~1)


def is_unc_like(value: Any) -> bool:
    """مسار شبكي/جهاز (\\\\server\\share أو //x أو \\\\?\\) — مرفوض قبل أي resolve لأنه قد يلمس الشبكة."""
    if not isinstance(value, (str, Path)):
        return False
    t = str(value).strip().replace("/", "\\")
    return t.startswith("\\\\")


def _safe_move(src: str, dst: str) -> None:
    """نقل بنفس القرص عبر os.rename: لا يكتب فوق موجود (حتى على لينكس) ولا ينسخ ثم يحذف المصدر (لا حذف نهائي خفي)."""
    if os.path.lexists(dst):
        raise FileExistsError(f"الوجهة موجودة أصلاً: {dst}")
    os.rename(src, dst)


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


class PathOutOfBoundsError(Exception):
    """استثناء عند محاولة الوصول إلى مسار خارج المجلد الجذر المسموح به أو عبر روابط غير آمنة."""
    pass


class ProtectedPathError(Exception):
    """استثناء عند محاولة التلاعب بالمسارات النظامية المحمية (الجذر، السلة، سجل التراجع)."""
    pass


class PermanentDeleteForbidden(Exception):
    """استثناء عند محاولة حذف ملف نهائياً."""
    pass


class FileManager:
    """إدارة آمنة للملفات محصورة داخل مجلد جذر مسموح مع سجل تراجع محمي ومنع قاطع للحذف النهائي."""

    def __init__(self, allowed_root: Union[str, Path], kill_switch: Optional[KillSwitch] = None):
        self.kill_switch = kill_switch
        self.allowed_root = Path(allowed_root).resolve()
        if not self.allowed_root.exists():
            self.allowed_root.mkdir(parents=True, exist_ok=True)
        # مجلد السلة والسجل لا يُحلّان عبر روابط: رابط باسم .trash قد يرسل «المحذوفات» إلى خارج مجلد العمل
        self.trash_dir = self.allowed_root / ".trash"
        self.journal_path = self.allowed_root / ".undo_journal.json"
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
        self.journal: List[Dict[str, Any]] = self._load_journal()
        self.current_task: Optional[str] = None  # معرّف المهمة الجارية؛ يُوسَم به كل إدخال لتبقى المهمة قابلة للتراجع بعد إعادة التشغيل

    def _load_journal(self) -> List[Dict[str, Any]]:
        if self.journal_path.exists():
            try:
                data = json.loads(self.journal_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [e for e in data if isinstance(e, dict)]
            except Exception:
                pass
            # سجل تالف: لا نفقده بصمت، نحفظ نسخة للفحص ثم نبدأ سجلاً جديداً
            try:
                backup = self._get_unique_path(self.allowed_root / ".undo_journal.corrupt.json")
                self.journal_path.replace(backup)
            except Exception:
                pass
            return []
        return []

    def _save_journal(self):
        """حفظ سجل التراجع في JSON مع رفع استثناء عند الفشل لضمان عدم ضياع التوثيق (R11)."""
        data = json.dumps(self.journal, ensure_ascii=False, indent=2)
        tmp = self.journal_path.with_name(self.journal_path.name + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(str(tmp), str(self.journal_path))  # كتابة ذرّية: لا يبقى سجل نصف مكتوب

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

        # فحص الخروج من الجذر المسموح
        try:
            resolved.relative_to(self.allowed_root)
        except ValueError:
            raise PathOutOfBoundsError(f"المسار '{path}' يقع خارج المجلد الجذر المسموح به '{self.allowed_root}'.")
        for rp in resolved.relative_to(self.allowed_root).parts:  # بعد الحل: الاسم القصير GIT~1 يظهر باسمه الحقيقي .git
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

        if resolved == self.trash_dir and not allow_trash_internal:
            raise ProtectedPathError("لا يمكن استهداف مجلد سلة المحذوفات .trash مباشرة.")

        if self.trash_dir in resolved.parents and not allow_trash_internal:
            raise ProtectedPathError("المسار يقع داخل سلة المحذوفات .trash المحمية.")

        return resolved

    def _get_unique_path(self, target: Path) -> Path:
        """توليد مسار فريد في حال وجود تعارض لمنع الكتابة فوق ملفات موجودة."""
        if not target.exists():
            return target
        parent = target.parent
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while True:
            candidate = parent / f"{stem} ({counter}){suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def _check_stop(self):
        if self.kill_switch is not None:
            self.kill_switch.check()

    def _copy_tree(self, source: Path, dest: Path):
        """نسخ مجلد ملفاً ملفاً مع فحص زر الإيقاف (R23). عند الإيقاف يُنقل الناتج الجزئي إلى السلة."""
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
                    shutil.copy2(str(f), str(dest / rel / name))
        except BaseException:
            if created_any and dest.exists():
                try:
                    _safe_move(str(dest), str(self._get_unique_path(self.trash_dir / ("partial_" + dest.name))))
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
        made = []
        try:
            for m in reversed(missing):
                m.mkdir(exist_ok=True)
                made.append(m)
                self.journal.append({"action": "create_dir", "path": str(m), "task": self.current_task})
            if made:
                self._save_journal()
        except Exception:
            for m in reversed(made):
                if self.journal and self.journal[-1].get("path") == str(m):
                    self.journal.pop()
                try:
                    m.rmdir()
                except Exception:
                    pass
            raise

    def create_dir(self, dir_path: Union[str, Path]) -> Path:
        """إنشاء مجلد (وأي آباء ناقصين) داخل الجذر وتوثيق كل مستوى في سجل التراجع."""
        p = self.validate_path(dir_path)
        if p.exists():
            return p
        self._ensure_dirs(p)
        return p

    def move(self, src: Union[str, Path], dest: Union[str, Path]) -> Path:
        """نقل ملف أو مجلد مع تجنب التعارض وتسجيل العملية للتراجع والتراجع إن فشل الحفظ."""
        source = self.validate_path(src)
        if not source.exists():
            raise FileNotFoundError(f"الملف المصدر غير موجود: {source}")

        destination = self.validate_path(dest)
        if destination.is_dir() and destination != source:
            destination = self.validate_path(destination / source.name)  # الوجهة مجلد موجود: ضع العنصر بداخله
        if destination == source or os.path.normcase(str(destination)) == os.path.normcase(str(source)):
            raise ValueError(f"المصدر والوجهة هما نفس المكان: {source}")
        if source.is_dir() and (source in destination.parents or destination == source):
            raise ValueError(f"لا يمكن نقل المجلد '{source}' إلى داخل نفسه أو مجلد فرعي منه '{destination}'.")
        self._ensure_dirs(destination.parent)
        final_dest = self._get_unique_path(destination)

        _safe_move(str(source), str(final_dest))
        self.journal.append({
            "task": self.current_task,
            "action": "move",
            "src": str(source),
            "dest": str(final_dest),
        })
        try:
            self._save_journal()
        except Exception:
            self.journal.pop()
            try:
                os.rename(str(final_dest), str(source))
            except Exception:
                pass
            raise
        return final_dest

    def copy(self, src: Union[str, Path], dest: Union[str, Path]) -> Path:
        """نسخ ملف أو مجلد مع منع نسخ المجلد إلى داخل نفسه وتسجيل العملية والتراجع إن فشل الحفظ."""
        source = self.validate_path(src)
        if not source.exists():
            raise FileNotFoundError(f"الملف المصدر غير موجود: {source}")

        destination = self.validate_path(dest)

        # R12: منع نسخ مجلد إلى داخل نفسه أو إلى مجلد فرعي منه
        if source.is_dir():
            if destination == source or source in destination.parents:
                raise ValueError(f"لا يمكن نسخ المجلد '{source}' إلى داخل نفسه أو مجلد فرعي منه '{destination}'.")

        if destination.is_dir() and destination != source:
            destination = self.validate_path(destination / source.name)
            if source.is_dir() and (destination == source or source in destination.parents):
                raise ValueError(f"لا يمكن نسخ المجلد '{source}' إلى داخل نفسه أو مجلد فرعي منه '{destination}'.")
        self._ensure_dirs(destination.parent)
        final_dest = self._get_unique_path(destination)

        if source.is_dir():
            self._copy_tree(source, final_dest)
        else:
            self._check_stop()
            shutil.copy2(str(source), str(final_dest))

        self.journal.append({
            "task": self.current_task,
            "action": "copy",
            "src": str(source),
            "dest": str(final_dest),
            "is_dir": source.is_dir(),
        })
        try:
            self._save_journal()
        except Exception:
            self.journal.pop()
            try:
                if source.is_dir():
                    shutil.rmtree(str(final_dest))
                else:
                    final_dest.unlink()
            except Exception:
                pass
            raise
        return final_dest

    def rename(self, src: Union[str, Path], new_name: str) -> Path:
        """إعادة تسمية ملف أو مجلد مع تجنب التعارض والتراجع إن فشل الحفظ."""
        if not isinstance(new_name, str) or not new_name.strip():
            raise ValueError("يجب تحديد اسم جديد صالح.")

        # R12: رفض أي فواصل مسار أو مسارات نسبية تحوّل التسمية إلى نقل
        if "/" in new_name or "\\" in new_name or ".." in new_name or new_name in (".", ".."):
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
            tmp = self._get_unique_path(source.parent / (".cos_rename_tmp_" + source.name))
            _safe_move(str(source), str(tmp))
            try:
                _safe_move(str(tmp), str(final_dest))
            except BaseException:
                _safe_move(str(tmp), str(source))
                raise
        else:
            final_dest = self._get_unique_path(final_target)
            _safe_move(str(source), str(final_dest))
        self.journal.append({
            "task": self.current_task,
            "action": "rename",
            "src": str(source),
            "dest": str(final_dest),
        })
        try:
            self._save_journal()
        except Exception:
            self.journal.pop()
            try:
                os.rename(str(final_dest), str(source))
            except Exception:
                pass
            raise
        return final_dest

    def delete(self, target: Union[str, Path]) -> Path:
        """لا يوجد حذف نهائي: النقل إلى مجلد .trash مع التراجع إن فشل حفظ السجل."""
        p = self.validate_path(target)
        if not p.exists():
            raise FileNotFoundError(f"الملف المراد حذفه غير موجود: {p}")

        if _is_link(self.trash_dir) or not self.trash_dir.is_dir():
            raise ProtectedPathError("مجلد السلة .trash لم يعد مجلداً عادياً — أوقفت الحذف حمايةً للملفات.")
        dest = self.trash_dir / p.name
        final_dest = self._get_unique_path(dest)
        if final_dest.parent != self.trash_dir:
            raise ProtectedPathError("وجهة السلة خارج مجلد السلة.")
        _safe_move(str(p), str(final_dest))

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
            except Exception:
                pass
            raise
        return final_dest

    def permanent_delete(self, target: Union[str, Path]):
        """محظور نهائياً بموجب قواعد الأمان."""
        raise PermanentDeleteForbidden("الحذف النهائي محظور نهائياً بموجب قواعد الأمان. استخدم دالة delete للنقل إلى سلة المحذوفات.")

    def _undo_op(self, op: Dict[str, Any]) -> str:
        """يتراجع عن عملية واحدة. يعيد "ok" أو "missing" (الناتج المتوقع لم يعد موجوداً فلا شيء يُستعاد) ويرفع عند الخطأ."""
        action = op.get("action")
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
                    _safe_move(str(tmp), str(dest.parent / raw_src.name))
                    return "ok"
            _safe_move(str(dest), str(self._get_unique_path(src)))  # لا كتابة فوق موجود
            return "ok"
        if action == "copy":
            dest = self.validate_path(op["dest"])  # تراجع النسخ ينقل الناتج إلى السلة ولا يحذفه نهائياً (R9)
            if not dest.exists():
                return "missing"
            _safe_move(str(dest), str(self._get_unique_path(self.trash_dir / dest.name)))
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
            _safe_move(str(trash), str(self._get_unique_path(orig)))
            return "ok"
        raise ValueError(f"عملية غير معروفة في السجل: {action!r}")

    def undo_step(self) -> str:
        """يتراجع عن آخر عملية: "ok" | "missing" (أُزيلت من السجل لأن الناتج اختفى) | "error" (بقيت في السجل) | "empty"."""
        if not self.journal:
            return "empty"
        try:
            result = self._undo_op(self.journal[-1])  # R10: لا نحذف من السجل قبل نجاح الفعل
            self.journal.pop()
            self._save_journal()
            return result
        except Exception:
            return "error"

    def undo_last(self) -> bool:
        """True فقط إذا استُعيد شيء فعلاً. إن اختفى الناتج المتوقع أُزيلت العملية من السجل وتعاد False."""
        return self.undo_step() == "ok"

    def undo_all(self) -> int:
        """التراجع عن جميع العمليات بالترتيب العكسي؛ تتخطى العمليات التي اختفى ناتجها وتتوقف عند أي خطأ."""
        count = 0
        while self.journal:
            r = self.undo_step()
            if r == "ok":
                count += 1
            elif r != "missing":
                break
        return count

    @staticmethod
    def file_hash(file_path: Union[str, Path]) -> str:
        """حساب بصمة SHA-256 للملف للتأكد من سلامة المحتوى."""
        p = Path(file_path)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
