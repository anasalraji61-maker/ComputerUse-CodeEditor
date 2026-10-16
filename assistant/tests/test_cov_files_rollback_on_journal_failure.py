"""تغطية FileManager: عند فشل حفظ سجل التراجع بعد تنفيذ العملية تُعاد الحالة كما كانت (نقل/إعادة تسمية/حذف/نسخة/إنشاء مجلدات متداخلة)،
فلا ملف يضيع ولا عملية بلا سجل، ولا يبقى ملف نية معلّق."""
import errno
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
    from assistant.tools.files import FileManager

    def live(ws):
        return sorted(str(p.relative_to(ws)).replace(os.sep, "/") for p in ws.rglob("*")
                      if p.is_file() and ".trash" not in p.parts and not p.name.startswith(".undo_journal"))

    def trash_files(fm):
        return sorted(p.name for p in fm.trash_dir.rglob("*") if p.is_file())

    def save_down(self_fm):
        raise OSError(errno.EIO, "disk write failed")

    def fresh(tmp, files):
        ws = pathlib.Path(tmp).resolve() / "ws"
        ws.mkdir()
        for n, c in files.items():
            (ws / n).parent.mkdir(parents=True, exist_ok=True)
            (ws / n).write_text(c, encoding="utf-8")
        return ws, FileManager(ws, key_dir=pathlib.Path(tmp).resolve() / "keys")

    def expect_oserror(fn):
        try:
            fn()
        except OSError:
            return
        raise AssertionError("كان يجب أن يفشل")

    # نقل
    with tempfile.TemporaryDirectory() as tmp:
        ws, fm = fresh(tmp, {"a.txt": "AAA"})
        with mock.patch.object(FileManager, "_save_journal", save_down):
            expect_oserror(lambda: fm.move("a.txt", "b.txt"))
        assert live(ws) == ["a.txt"] and (ws / "a.txt").read_text(encoding="utf-8") == "AAA", live(ws)
        assert fm.journal == [] and not os.path.lexists(str(fm.intent_path))

    # إعادة تسمية
    with tempfile.TemporaryDirectory() as tmp:
        ws, fm = fresh(tmp, {"a.txt": "AAA"})
        with mock.patch.object(FileManager, "_save_journal", save_down):
            expect_oserror(lambda: fm.rename("a.txt", "z.txt"))
        assert live(ws) == ["a.txt"] and fm.journal == [] and not os.path.lexists(str(fm.intent_path))

    # حذف (إلى السلة): يعود الملف لمكانه ولا يبقى في السلة
    with tempfile.TemporaryDirectory() as tmp:
        ws, fm = fresh(tmp, {"a.txt": "AAA"})
        with mock.patch.object(FileManager, "_save_journal", save_down):
            expect_oserror(lambda: fm.delete("a.txt"))
        assert live(ws) == ["a.txt"] and trash_files(fm) == [] and fm.journal == [] and not os.path.lexists(str(fm.intent_path))

    # نسخة: النسخة غير المسجلة تُنقل إلى السلة (لا حذف نهائي) والأصل سليم
    with tempfile.TemporaryDirectory() as tmp:
        ws, fm = fresh(tmp, {"a.txt": "AAA"})
        with mock.patch.object(FileManager, "_save_journal", save_down):
            expect_oserror(lambda: fm.copy("a.txt", "c.txt"))
        assert live(ws) == ["a.txt"], live(ws)
        assert len(trash_files(fm)) == 1 and fm.journal == [] and not os.path.lexists(str(fm.intent_path)), trash_files(fm)

    # إنشاء مجلدات متداخلة: كل المستويات المنشأة تُزال ولا يبقى مجلد يتيم
    with tempfile.TemporaryDirectory() as tmp:
        ws, fm = fresh(tmp, {})
        with mock.patch.object(FileManager, "_save_journal", save_down):
            expect_oserror(lambda: fm.create_dir("p/q/r"))
        assert not (ws / "p").exists() and fm.journal == [] and not os.path.lexists(str(fm.intent_path))

    # الرجوع نفسه يفشل (os.rename معطّل): الاستثناء الأصلي يُرفع والنية تبقى ليتبنّاها الإقلاع التالي (لا ضياع للتراجع)
    with tempfile.TemporaryDirectory() as tmp:
        ws, fm = fresh(tmp, {"a.txt": "AAA"})
        real_rename = os.rename

        calls = []

        def rename_down(a, b, *x, **k):
            calls.append(1)
            if len(calls) == 1:      # التنفيذ الأول يمرّ؛ محاولة الرجوع بعد فشل الحفظ تفشل
                return real_rename(a, b, *x, **k)
            raise OSError(errno.EIO, "rename failed")
        with mock.patch.object(FileManager, "_save_journal", save_down), mock.patch("os.rename", rename_down):
            expect_oserror(lambda: fm.rename("a.txt", "z.txt"))
        assert (ws / "z.txt").exists() and os.path.lexists(str(fm.intent_path))
        fm2 = FileManager(ws, key_dir=pathlib.Path(tmp).resolve() / "keys")
        assert [e["action"] for e in fm2.journal] == ["rename"] and int(fm2.undo_all()) == 1 and live(ws) == ["a.txt"], (fm2.journal, live(ws))

    print("PASS: test_cov_files_rollback_on_journal_failure completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_files_rollback_on_journal_failure", file=sys.stderr)
    sys.exit(1)
