"""أدوات الملفات الآمنة مع قيد المجلد الجذر وسجل التراجع ومنع الحذف النهائي."""
import json
import shutil
import hashlib
from pathlib import Path
from typing import Union, List, Dict, Any, Optional


class PathOutOfBoundsError(Exception):
    """استثناء عند محاولة الوصول إلى مسار خارج المجلد الجذر المسموح به."""
    pass


class PermanentDeleteForbidden(Exception):
    """استثناء عند محاولة حذف ملف نهائياً."""
    pass


class FileManager:
    """إدارة آمنة للملفات محصورة داخل مجلد جذر مسموح مع سجل تراجع كامل."""

    def __init__(self, allowed_root: Union[str, Path]):
        self.allowed_root = Path(allowed_root).resolve()
        if not self.allowed_root.exists():
            self.allowed_root.mkdir(parents=True, exist_ok=True)
        self.trash_dir = self.allowed_root / ".trash"
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.allowed_root / ".undo_journal.json"
        self.journal: List[Dict[str, Any]] = self._load_journal()

    def _load_journal(self) -> List[Dict[str, Any]]:
        if self.journal_path.exists():
            try:
                return json.loads(self.journal_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_journal(self):
        try:
            self.journal_path.write_text(
                json.dumps(self.journal, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def validate_path(self, path: Union[str, Path]) -> Path:
        """التحقق من أن المسار يقع تماماً داخل المجلد الجذر المسموح به."""
        p = Path(path)
        if not p.is_absolute():
            resolved = (self.allowed_root / p).resolve()
        else:
            resolved = p.resolve()

        # التأكد من أن المسار داخل الجذر
        try:
            resolved.relative_to(self.allowed_root)
        except ValueError:
            raise PathOutOfBoundsError(f"المسار '{path}' خارج المجلد الجذر المسموح به '{self.allowed_root}'.")

        # فحص الروابط الرمزية (symlinks) للتأكد من عدم الإشارة لخارج الجذر
        if resolved.is_symlink():
            target = resolved.resolve()
            try:
                target.relative_to(self.allowed_root)
            except ValueError:
                raise PathOutOfBoundsError(f"الرابط الرمزي '{path}' يشير إلى مسار خارج الجذر.")

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
        """نسخ ملف مع تجنب التعارض وتسجيل العملية للتراجع."""
        source = self.validate_path(src)
        if not source.exists():
            raise FileNotFoundError(f"الملف المصدر غير موجود: {source}")

        destination = self.validate_path(dest)
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
        """إعادة تسمية ملف أو مجلد داخل نفس المجلد مع تجنب التعارض."""
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
        """التراجع عن آخر عملية مسجلة في السجل."""
        if not self.journal:
            return False

        op = self.journal.pop()
        action = op.get("action")

        try:
            if action in ("move", "rename"):
                src = Path(op["src"])
                dest = Path(op["dest"])
                if dest.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest), str(src))

            elif action == "copy":
                dest = Path(op["dest"])
                if dest.exists():
                    if op.get("is_dir"):
                        shutil.rmtree(str(dest))
                    else:
                        dest.unlink()

            elif action == "create_dir":
                p = Path(op["path"])
                if p.exists() and p.is_dir():
                    try:
                        p.rmdir()
                    except OSError:
                        pass

            elif action == "delete":
                orig = Path(op["original_path"])
                trash = Path(op["trash_path"])
                if trash.exists():
                    orig.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(trash), str(orig))

            self._save_journal()
            return True
        except Exception:
            self._save_journal()
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
