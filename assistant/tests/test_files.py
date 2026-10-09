"""اختبارات وحدة أدوات الملفات وسجل التراجع ومنع الحذف وتغطية مراجعات T2 (R7 إلى R12 و R14)."""
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.tools.files import (
        FileManager,
        PathOutOfBoundsError,
        ProtectedPathError,
        PermanentDeleteForbidden,
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        root_path = pathlib.Path(tmp_dir).resolve()
        fm = FileManager(allowed_root=root_path)

        # 1. إنشاء مجلد وملف تجريبي
        sub = fm.create_dir("documents")
        assert sub.exists() and sub.is_dir()
        doc = sub / "report.txt"
        doc.write_text("تقرير تجريبي للمساعد", encoding="utf-8")
        h0 = fm.file_hash(doc)

        # 2. اختبار النسخ وتجنب التعارض (Collision Handling)
        copy1 = fm.copy(doc, sub / "report_copy.txt")
        assert copy1.exists() and fm.file_hash(copy1) == h0
        copy2 = fm.copy(doc, sub / "report_copy.txt")
        assert copy2.name == "report_copy (1).txt", f"فشل ترقيم التعارض: {copy2.name}"
        assert copy1.exists() and copy2.exists()

        # 3. R14 (هـ) و R9: تراجع النسخ لا يحذف نهائياً، بل ينقل للـ .trash
        assert fm.undo_last(), "فشل التراجع عن النسخ"
        assert not copy2.exists(), "الملف المنسوخ ما زال في موقعه بعد التراجع"
        trash_files = list(fm.trash_dir.glob("report_copy (1)*"))
        assert len(trash_files) > 0, "الناتج لم يُنقل إلى سلة المحذوفات بالتراجع (حُذف نهائياً!)"

        # 4. R14 (ب) و R7: محاولة move/delete على .trash و .undo_journal.json والجذر مرفوضة
        for protected_target in [root_path, fm.trash_dir, fm.journal_path]:
            try:
                fm.delete(protected_target)
                print(f"FAIL: كان يجب رفض حذف المسار المحمي: {protected_target}", file=sys.stderr)
                sys.exit(1)
            except ProtectedPathError:
                pass  # صحيح ومطلوب (R7)

            try:
                fm.move(protected_target, sub / "moved_protected")
                print(f"FAIL: كان يجب رفض نقل المسار المحمي: {protected_target}", file=sys.stderr)
                sys.exit(1)
            except ProtectedPathError:
                pass  # صحيح ومطلوب (R7)

        # 5. R14 (و) و R12: rename باسم فيه ../ أو / مرفوض
        for bad_name in ["../escape.txt", "sub/nested.txt", "..", ".", "folder\\file.txt"]:
            try:
                fm.rename(doc, bad_name)
                print(f"FAIL: كان يجب رفض الاسم غير الصالح في rename: {bad_name}", file=sys.stderr)
                sys.exit(1)
            except ValueError:
                pass  # صحيح ومطلوب (R12)

        # 6. R12: نسخ مجلد إلى داخل نفسه مرفوض
        folder_a = fm.create_dir("folder_a")
        (folder_a / "f.txt").write_text("data", encoding="utf-8")
        try:
            fm.copy(folder_a, folder_a / "nested_copy")
            print("FAIL: كان يجب رفض نسخ المجلد إلى داخل نفسه!", file=sys.stderr)
            sys.exit(1)
        except ValueError:
            pass  # صحيح ومطلوب (R12)

        # 7. R14 (ج): رابط رمزي داخل الجذر يشير للخارج مرفوض
        outside_file = root_path.parent / "secret_outside.txt"
        outside_file.write_text("secret", encoding="utf-8")
        symlink_path = sub / "symlink_to_outside"
        try:
            symlink_path.symlink_to(outside_file)
            try:
                fm.validate_path(symlink_path)
                print("FAIL: كان يجب رفض الرابط الرمزي الذي يشير للخارج!", file=sys.stderr)
                sys.exit(1)
            except PathOutOfBoundsError:
                pass  # صحيح ومطلوب (R14 ج)
        except (OSError, NotImplementedError):
            print("SKIP: symlink creation requires admin/elevated privileges on this OS")

        # 8. R14 (د) و R8: سجل تراجع معدّل يشير لمسار خارج الجذر لا يُنفّذ
        fm.journal.append({
            "action": "move",
            "src": str(outside_file),
            "dest": str(sub / "escaped.txt"),
        })
        fm._save_journal()
        # محاولة تنفيذ التراجع لسجل خبيث يجب أن تفشل ولا تلمس الملف الخارجي
        revert_result = fm.undo_last()
        assert revert_result is False, "تم تنفيذ تراجع يشير لمسار خارج الجذر!"
        assert outside_file.exists(), "تم العبث بالملف الخارجي عبر سجل التراجع الخبيث!"

        # 9. اختبار الحذف الآمن (النقل إلى سلة المحذوفات) واستعادته
        trash_item = fm.delete(doc)
        assert not doc.exists() and trash_item.exists()
        assert fm.undo_last()
        assert doc.exists()

        # 10. التأكد من رفض الحذف النهائي
        try:
            fm.permanent_delete(doc)
            print("FAIL: كان يجب رفض الحذف النهائي!", file=sys.stderr)
            sys.exit(1)
        except PermanentDeleteForbidden:
            pass

    print("PASS: test_files completed successfully")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
