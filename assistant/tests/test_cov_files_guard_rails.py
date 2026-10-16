"""تغطية FileManager: حواجز ما قبل التنفيذ (حماية .trash والسجل و.git، حذف/نسخ/نقل لعنصر غائب، نسخ مجلد داخل نفسه، أسماء إعادة تسمية خاطئة،
سلة استُبدلت بملف، استرداد ميزانية النسخ عند فشل التجهيز) — لا أثر على القرص عند الرفض."""
import os
import pathlib
import sys
import tempfile
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.tools.files import (FileManager, PathOutOfBoundsError, ProtectedPathError, CopyBudgetExceededError, explain_error,
                                       tree_size, content_fingerprint, pid_alive)
    import errno

    def raises(exc_types, fn, fragment=None):
        try:
            fn()
        except exc_types as exc:
            if fragment is not None:
                assert fragment in str(exc), (fragment, str(exc))
            return exc
        raise AssertionError(f"كان يجب أن يرفع {exc_types}")

    def snapshot(ws):
        return sorted(str(p.relative_to(ws)).replace(os.sep, "/") for p in ws.rglob("*") if not p.name.startswith(".undo_journal"))

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("A", encoding="utf-8")
        (ws / "dir").mkdir()
        (ws / "dir" / "in.txt").write_text("I", encoding="utf-8")
        fm = FileManager(ws, key_dir=base / "keys")
        before = snapshot(ws)

        # --- validate_path: محميات ---
        raises(ProtectedPathError, lambda: fm.validate_path(".trash"))
        raises(ProtectedPathError, lambda: fm.validate_path(".trash/x.txt"), ".trash")
        raises(ProtectedPathError, lambda: fm.validate_path(ws))
        raises(ProtectedPathError, lambda: fm.validate_path(".undo_journal.json"))
        raises(ProtectedPathError, lambda: fm.validate_path(".undo_journal.whatever"))
        raises(ProtectedPathError, lambda: fm.validate_path("dir/.git/config"))
        raises(ProtectedPathError, lambda: fm.validate_path(".SSH"))
        raises(PathOutOfBoundsError, lambda: fm.validate_path("../outside.txt"))
        raises(PathOutOfBoundsError, lambda: fm.validate_path(str(base / "other.txt")))
        # السماح الداخلي للسلة فقط عند الطلب الصريح
        assert fm.validate_path(".trash/x.txt", allow_trash_internal=True).parent == fm.trash_dir

        # --- عناصر غائبة ---
        raises(FileNotFoundError, lambda: fm.delete("ghost.txt"))
        raises(FileNotFoundError, lambda: fm.copy("ghost.txt", "g2.txt"))
        raises(FileNotFoundError, lambda: fm.move("ghost.txt", "g2.txt"))
        raises(FileNotFoundError, lambda: fm.rename("ghost.txt", "g2.txt"))

        # --- نسخ/نقل مجلد إلى داخل نفسه ---
        raises(ValueError, lambda: fm.copy("dir", "dir/sub"), "داخل نفسه")
        raises(ValueError, lambda: fm.copy("dir", "dir"), "داخل نفسه")
        raises(ValueError, lambda: fm.move("dir", "dir/sub"), "داخل نفسه")
        raises(ValueError, lambda: fm.move("a.txt", "a.txt"), "نفس المكان")

        # --- أسماء إعادة التسمية ---
        for bad in ("", "   ", None, "x/y", "x\\y", ".", "..", "a.txt"):
            raises((ValueError, TypeError), lambda b=bad: fm.rename("a.txt", b))
        assert snapshot(ws) == before and fm.journal == [] and not os.path.lexists(str(fm.intent_path))

        # --- ميزانية النسخ: تجهيز الوجهة يفشل -> تُسترد الحصة (لا تُستهلك بلا نسخ) ---
        fm.current_task = "T-refund"
        with mock.patch.object(FileManager, "_ensure_dirs", side_effect=OSError(errno.EACCES, "denied")):
            raises(OSError, lambda: fm.copy("a.txt", "new/a.txt"))
        assert fm._copy_spent.get("T-refund", (0, 0))[0] == 0, fm._copy_spent
        fm.max_copy_bytes = 1
        (ws / "big.bin").write_bytes(b"x" * 64)
        raises(CopyBudgetExceededError, lambda: fm.copy("big.bin", "big2.bin"))
        assert not (ws / "big2.bin").exists()
        fm.current_task = None

        # --- السلة استُبدلت بملف عادي بعد التهيئة: تُصلَح (يُنقل الملف جانباً وتُنشأ سلة جديدة) ثم يمضي الحذف إلى سلة سليمة، لا إلى مكان آخر ---
        fm2 = FileManager(ws, key_dir=base / "keys")
        os.rmdir(str(fm2.trash_dir))
        fm2.trash_dir.write_text("not a dir", encoding="utf-8")
        out = fm2.delete("a.txt")
        assert fm2.trash_dir.is_dir() and out.parent == fm2.trash_dir and out.read_text(encoding="utf-8") == "A"
        assert (ws / ".trash.was_a_file").read_text(encoding="utf-8") == "not a dir"
        assert any(".trash" in n for n in fm2.undo_notes), fm2.undo_notes
        assert int(fm2.undo_all()) == 1 and (ws / "a.txt").read_text(encoding="utf-8") == "A"

        # --- مساعدات: رسائل الأخطاء وبصمة المحتوى وفحص العمليات ---
        assert "طويل" in explain_error(OSError(errno.ENAMETOOLONG, "x")) or explain_error(OSError(errno.ENAMETOOLONG, "x"))
        assert explain_error(OSError(errno.ENOSPC, "x"))
        assert explain_error(OSError(errno.EEXIST, "x")) and explain_error(FileExistsError("x"))
        assert explain_error(OSError(errno.ENOTEMPTY, "x"))
        assert explain_error(OSError(errno.EINVAL, "x"))
        assert content_fingerprint(ws / "dir") is None            # مجلد: لا بصمة
        assert content_fingerprint(ws / "nope") is None
        big = ws / "dir" / "big.bin"
        big.write_bytes(b"z" * 5000)
        assert content_fingerprint(big, full_limit=100).startswith("s:") and content_fingerprint(big, full_limit=10 ** 6).startswith("f:")
        assert tree_size(ws / "nope") == 0
        assert pid_alive(os.getpid()) is True
        assert pid_alive(0) is False and pid_alive(-3) is False and pid_alive("x") is False

    print("PASS: test_cov_files_guard_rails completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_files_guard_rails", file=sys.stderr)
    sys.exit(1)
