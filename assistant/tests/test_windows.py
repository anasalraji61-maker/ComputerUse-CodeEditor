"""اختبارات خاصة بويندوز (تعمل فعلياً على لابتوب المالك فقط): تقاطعات junction، تيارات NTFS، أسماء محجوزة."""
import os
import subprocess
import sys
import tempfile
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if os.name != "nt":
    print("SKIP: اختبارات ويندوز تعمل فقط على ويندوز")
    sys.exit(0)

try:
    from assistant.tools.files import FileManager, PathOutOfBoundsError, _is_link
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.models.rules import RuleProvider

    def raises(exc, fn, *a):
        try:
            fn(*a)
        except exc:
            return True
        return False

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        outside = base / "outside"; outside.mkdir()
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        root = base / "ws"; root.mkdir()
        (root / "ok.txt").write_text("ok", encoding="utf-8")
        # junction لا يحتاج صلاحيات مدير
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "jn"), str(outside)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _is_link(root / "jn") and not _is_link(root / "ok.txt")
        fm = FileManager(root)
        assert raises(PathOutOfBoundsError, fm.validate_path, "jn")
        assert raises(PathOutOfBoundsError, fm.delete, "jn/secret.txt")
        assert raises(PathOutOfBoundsError, fm.move, "jn/secret.txt", "stolen.txt")
        assert (outside / "secret.txt").exists() and not (root / "stolen.txt").exists()
        # نسخ مجلد يحوي junction مرفوض (لا ننسخ ما خارج الجذر إلى الداخل)
        (root / "dir").mkdir()
        (root / "dir" / "f.txt").write_text("f", encoding="utf-8")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "dir" / "inner"), str(outside)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert raises(PathOutOfBoundsError, fm.copy, "dir", "dir_copy")
        assert not (root / "dir_copy" / "inner" / "secret.txt").exists()
        # قائمة المجلد للمخطط لا تدخل التقاطع
        led = SQLiteLedger(base / "l.db", initial_balance=100, spending_limit=100)
        listing = TaskRunner(root, RuleProvider(), led).workspace_listing()
        assert "secret.txt" not in listing, listing
        # تيارات NTFS والأسماء المحجوزة
        for bad in ("ok.txt:evil", "CON", "nul.txt", "aux", "name.", "x "):
            assert raises(PathOutOfBoundsError, fm.create_dir, bad), bad
        os.rmdir(root / "jn"); os.rmdir(root / "dir" / "inner")  # إزالة التقاطع لا تمسّ الهدف
        assert (outside / "secret.txt").exists()

    print("PASS: test_windows completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_windows", file=sys.stderr)
    sys.exit(1)
