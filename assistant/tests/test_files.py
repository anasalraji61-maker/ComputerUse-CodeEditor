"""اختبارات وحدة أدوات الملفات وسجل التراجع ومنع الحذف والخروج من المجلد الجذر."""
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.tools.files import FileManager, PathOutOfBoundsError, PermanentDeleteForbidden

    with tempfile.TemporaryDirectory() as tmp_dir:
        root_path = pathlib.Path(tmp_dir)
        fm = FileManager(allowed_root=root_path)

        # 1. اختبار إنشاء المجلدات
        sub = fm.create_dir("documents")
        assert sub.exists() and sub.is_dir(), "فشل إنشاء المجلد"

        # 2. إنشاء ملف تجريبي
        doc1 = sub / "doc1.txt"
        doc1.write_text("محتوى تجريبي للوثيقة الأولى", encoding="utf-8")
        h0 = fm.file_hash(doc1)
        assert len(h0) == 64, "فشل حساب بصمة SHA-256"

        # 3. اختبار النسخ
        copied = fm.copy(doc1, sub / "doc1_copy.txt")
        assert copied.exists(), "فشل نسخ الملف"
        assert fm.file_hash(copied) == h0, "فشل تطابق محتوى الملف المنسوخ"

        # 4. اختبار منع الكتابة فوق ملف موجود (Collision Handling)
        copied2 = fm.copy(doc1, sub / "doc1_copy.txt")
        assert copied2.name == "doc1_copy (1).txt", f"فشل منع الكتابة فوق الملف: {copied2.name}"
        assert copied2.exists() and copied.exists(), "تم الكتابة فوق الملف بدلاً من ترقيمه"

        # 5. اختبار النقل
        moved = fm.move(copied2, "archive/doc1_archived.txt")
        assert moved.exists(), "فشل نقل الملف"
        assert not copied2.exists(), "الملف القديم ما زال موجوداً بعد النقل"

        # 6. اختبار إعادة التسمية
        renamed = fm.rename(moved, "renamed_doc.txt")
        assert renamed.name == "renamed_doc.txt", "فشل إعادة تسمية الملف"
        assert renamed.exists() and not moved.exists(), "الملف القديم موجود بعد إعادة التسمية"

        # 7. اختبار الأمان: رفض الحذف النهائي نهائياً
        try:
            fm.permanent_delete(renamed)
            print("FAIL: كان يجب رفض الحذف النهائي!", file=sys.stderr)
            sys.exit(1)
        except PermanentDeleteForbidden:
            pass  # صحيح ومطلوب

        # 8. اختبار الحذف الآمن (النقل إلى سلة المحذوفات)
        trash_dest = fm.delete(renamed)
        assert not renamed.exists(), "الملف لم يُحذف من مساره الأصلي"
        assert trash_dest.exists(), "الملف غير موجود في سلة المحذوفات"
        assert trash_dest.parent.name == ".trash", "الملف لم يُنقل إلى مجلد .trash"

        # 9. اختبار التراجع (undo_last) لاستعادة الملف المحذوف
        assert fm.undo_last(), "فشل التراجع عن الحذف"
        assert renamed.exists(), "الملف لم يعد إلى مساره الأصلي بالتراجع"
        assert not trash_dest.exists(), "الملف ما زال في سلة المحذوفات بعد التراجع"

        # 10. اختبار التراجع الشامل (undo_all)
        reverted_count = fm.undo_all()
        assert reverted_count > 0, "فشل التراجع الشامل"

        # 11. اختبار الأمان: رفض الخروج من الجذر (Path Traversal / Absolute path outside)
        try:
            fm.validate_path("../outside.txt")
            print("FAIL: كان يجب رفض المسار النسبي الخارجي!", file=sys.stderr)
            sys.exit(1)
        except PathOutOfBoundsError:
            pass  # صحيح ومطلوب

        outside_dir = root_path.parent / "forbidden.txt"
        try:
            fm.validate_path(outside_dir)
            print("FAIL: كان يجب رفض المسار المطلق الخارجي!", file=sys.stderr)
            sys.exit(1)
        except PathOutOfBoundsError:
            pass  # صحيح ومطلوب

        # التأكد من حفظ سجل التراجع في JSON داخل الجذر
        assert fm.journal_path.exists(), "ملف سجل التراجع .undo_journal.json غير موجود"

    print("PASS: test_files completed successfully")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
