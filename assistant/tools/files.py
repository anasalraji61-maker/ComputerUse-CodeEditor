"""أدوات الملفات الآمنة مع قيد المجلد الجذر وحماية المسارات وسجل التراجع ومنع الحذف النهائي."""
import json
import shutil
import hashlib
from pathlib import Path
from typing import Union, List, Dict, Any, Optional


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

    def __init__(self, allowed_root: Union[str, Path]):
        self.allowed_root = Path(allowed_root).resolve()
        if not self.allowed_root.exists():
            self.allowed_root.mkdir(parents=True, exist_ok=True)
        self.trash_dir = (self.allowed_root / ".trash").resolve()
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = (self.allowed_root / ".undo_journal.json").resolve()
        self.journal: List[Dict[str, Any]] = self._load_journal()

    def _load_journal(self) -> List[Dict[str, Any]]:
        if self.journal_path.exists():
            try:
                return json.loads(self.journal_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_journal(self):
        """حفظ سجل التراجع في JSON مع رفع استثناء عند الفشل لضمان عدم ضياع التوثيق (R11)."""
        data = json.dumps(self.journal, ensure_ascii=False, indent=2)
        self.journal_path.write_text(data, encoding="utf-8")

    def validate_path(self, path: Union[str, Path], allow_trash_internal: bool = False) -> Path:
        """التحقق الأمني الصارم من المسار (R7 و R8):

        - داخل الجذر المسموح.
        - فحص الروابط الرمزية (symlinks) للتأكد من عدم الإشارة لخارج الجذر.
        - رفض استهداف الجذر نفسه أو ملف السجل أو مجلد السلة مباشرة.
        """
        p = Path(path)
        # تتبع وفحص الروابط الرمزية قبل أو أثناء الحل
        if not p.is_absolute():
            unresolved = self.allowed_root / p
        else:
            unresolved = p

        resolved = unresolved.resolve()

        # فحص الخروج من الجذر المسموح
        try:
            resolved.relative_to(self.allowed_root)
        except ValueError:
            raise PathOutOfBoundsError(f"المسار '{path}' يقع خارج المجلد الجذر المسموح به '{self.allowed_root}'.")

        # فحص إذا كان هناك رابط رمزي في المسار يشير إلى خارج الجذر
        curr = unresolved
        while curr != self.allowed_root and curr != curr.parent:
            if curr.is_symlink():
                target = curr.resolve()
                try:
                    target.relative_to(self.allowed_root)
                except ValueError:
                    raise PathOutOfBoundsError(f"الرابط الرمزي '{curr}' يشير إلى هدف خارج الجذر: '{target}'.")
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

    def create_dir(self, dir_path: Union[str, Path]) -> Path:
        """إنشاء مجلد داخل الجذر وتوثيقه في سجل التراجع."""
        p = self.validate_path(dir_path)
        if p.exists():
            return p
        p.mkdir(parents=True, exist_ok=True)
        self.journal.append({
            "action": "create_dir",
            "path": str(p),
        })
        self._save_journal()
        return p

    def move(self, src: Union[str, Path], dest: Union[str, Path]) -> Path:
        """نقل ملف أو مجلد مع تجنب التعارض وتسجيل العملية للتراجع."""
        source = self.validate_path(src)
        if not source.exists():
            raise FileNotFoundError(f"الملف المصدر غير موجود: {source}")

        destination = self.validate_path(dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        final_dest = self._get_unique_path(destination)

        shutil.move(str(source), str(final_dest))
        self.journal.append({
            "action": "move",
            "src": str(source),
            "dest": str(final_dest),
        })
        self._save_journal()
        return final_dest

    def copy(self, src: Union[str, Path], dest: Union[str, Path]) -> Path:
        """نسخ ملف أو مجلد مع منع نسخ المجلد إلى داخل نفسه (R12) وتسجيل العملية."""
        source = self.validate_path(src)
        if not source.exists():
            raise FileNotFoundError(f"الملف المصدر غير موجود: {source}")

        destination = self.validate_path(dest)

        # R12: منع نسخ مجلد إلى داخل نفسه أو إلى مجلد فرعي منه
        if source.is_dir():
            if destination == source or source in destination.parents:
                raise ValueError(f"لا يمكن نسخ المجلد '{source}' إلى داخل نفسه أو مجلد فرعي منه '{destination}'.")

        destination.parent.mkdir(parents=True, exist_ok=True)
        final_dest = self._get_unique_path(destination)

        if source.is_dir():
            shutil.copytree(str(source), str(final_dest))
        else:
            shutil.copy2(str(source), str(final_dest))

        self.journal.append({
            "action": "copy",
            "src": str(source),
            "dest": str(final_dest),
            "is_dir": source.is_dir(),
        })
        self._save_journal()
        return final_dest

    def rename(self, src: Union[str, Path], new_name: str) -> Path:
        """إعادة تسمية ملف أو مجلد مع رفض أي فواصل مسار أو .. (R12)."""
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
        final_dest = self._get_unique_path(final_target)

        source.rename(final_dest)
        self.journal.append({
            "action": "rename",
            "src": str(source),
            "dest": str(final_dest),
        })
        self._save_journal()
        return final_dest

    def delete(self, target: Union[str, Path]) -> Path:
        """لا يوجد حذف نهائي: النقل إلى مجلد .trash داخل الجذر مع سجل تراجع."""
        p = self.validate_path(target)
        if not p.exists():
            raise FileNotFoundError(f"الملف المراد حذفه غير موجود: {p}")

        dest = self.trash_dir / p.name
        final_dest = self._get_unique_path(dest)
        shutil.move(str(p), str(final_dest))

        self.journal.append({
            "action": "delete",
            "original_path": str(p),
            "trash_path": str(final_dest),
        })
        self._save_journal()
        return final_dest

    def permanent_delete(self, target: Union[str, Path]):
        """محظور نهائياً بموجب قواعد الأمان."""
        raise PermanentDeleteForbidden("الحذف النهائي محظور نهائياً بموجب قواعد الأمان. استخدم دالة delete للنقل إلى سلة المحذوفات.")

    def undo_last(self) -> bool:
        """التراجع عن آخر عملية مع التحقق الأمني من المسارات (R8)، وعدم الحذف النهائي (R9)، وعدم فقدان السجل عند الفشل (R10)."""
        if not self.journal:
            return False

        # R10: نقرأ العملية من نهاية السجل دون حذفها مسبقاً
        op = self.journal[-1]
        action = op.get("action")

        try:
            if action in ("move", "rename"):
                # R8: إعادة التحقق من المسارات عبر validate_path
                src = self.validate_path(op["src"])
                dest = self.validate_path(op["dest"])
                if dest.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    # R10: عدم الكتابة فوق ملف موجود بالتراجع
                    final_src = self._get_unique_path(src)
                    shutil.move(str(dest), str(final_src))

            elif action == "copy":
                # R9: تراجع النسخ ينقل الناتج إلى سلة المحذوفات بدل حذفه نهائياً
                dest = self.validate_path(op["dest"])
                if dest.exists():
                    dest_trash = self.trash_dir / dest.name
                    final_trash = self._get_unique_path(dest_trash)
                    shutil.move(str(dest), str(final_trash))

            elif action == "create_dir":
                p = self.validate_path(op["path"])
                if p.exists() and p.is_dir():
                    # إن كان المجلد يحتوي على ملفات، يُنقل للسلة لحماية الملفات
                    if any(p.iterdir()):
                        p_trash = self.trash_dir / p.name
                        shutil.move(str(p), str(self._get_unique_path(p_trash)))
                    else:
                        p.rmdir()

            elif action == "delete":
                # R8: التحقق من المسارات مع السماح الداخلي بالسلة
                orig = self.validate_path(op["original_path"])
                trash = self.validate_path(op["trash_path"], allow_trash_internal=True)
                if trash.exists():
                    orig.parent.mkdir(parents=True, exist_ok=True)
                    final_orig = self._get_unique_path(orig)
                    shutil.move(str(trash), str(final_orig))

            # R10: نحذف العملية من السجل ونحفظ فقط بعد نجاح التراجع الفعلي
            self.journal.pop()
            self._save_journal()
            return True

        except Exception as err:
            # R10: في حال حدوث أي خطأ، تبقى العملية في السجل ولا تضيع
            return False

    def undo_all(self) -> int:
        """التراجع عن جميع العمليات المسجلة بالترتيب العكسي."""
        count = 0
        while self.journal:
            if self.undo_last():
                count += 1
            else:
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
