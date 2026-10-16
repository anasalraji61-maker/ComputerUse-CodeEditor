"""تغطية التراجع: الاستعادة لا تكتب فوق ملف جديد بنفس الاسم (اسم بديل)، ناتج اختفى فلا يُخترع شيء، سلة مفقودة، نسخة عدّلها المستخدم تبقى،
وفشل خطوة لا يسدّ التراجع عن المستقلّ عنها."""
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

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        n = [0]

        def mk(files):
            n[0] += 1
            ws = base / f"w{n[0]}"
            ws.mkdir()
            for k, v in files.items():
                (ws / k).write_text(v, encoding="utf-8")
            return ws, FileManager(ws, key_dir=base / "keys")

        # 1. التراجع عن حذف بعد أن أنشأ المستخدم ملفاً جديداً بالاسم نفسه: يُستعاد باسم بديل ولا يُكتب فوق عمل المستخدم
        ws, fm = mk({"a.txt": "OLD"})
        fm.delete("a.txt")
        (ws / "a.txt").write_text("USER NEW WORK", encoding="utf-8")
        assert int(fm.undo_all()) == 1
        assert (ws / "a.txt").read_text(encoding="utf-8") == "USER NEW WORK"
        assert (ws / "a (1).txt").read_text(encoding="utf-8") == "OLD"
        assert any("باسم" in x for x in fm.undo_notes), fm.undo_notes

        # 2. نفس الشيء للنقل: المصدر الأصلي صار مشغولاً بعنصر آخر
        ws, fm = mk({"a.txt": "MOVED"})
        fm.move("a.txt", "b.txt")
        (ws / "a.txt").write_text("USER NEW WORK", encoding="utf-8")
        assert int(fm.undo_all()) == 1
        assert (ws / "a.txt").read_text(encoding="utf-8") == "USER NEW WORK" and (ws / "a (1).txt").read_text(encoding="utf-8") == "MOVED"
        assert not (ws / "b.txt").exists()

        # 3. الناتج اختفى (حذفه المستخدم): "missing" -> يُزال الإدخال من السجل ولا استثناء ولا اختراع ملف
        ws, fm = mk({"a.txt": "A"})
        fm.move("a.txt", "b.txt")
        (ws / "b.txt").unlink()
        res = fm.undo_all()
        assert int(res) == 0 and res.missing == 1 and fm.journal == [] and not (ws / "a.txt").exists(), res.to_dict()

        # 4. التراجع عن حذف سلته فُرّغت يدوياً: لا شيء يُستعاد ولا انهيار
        ws, fm = mk({"a.txt": "A"})
        t = fm.delete("a.txt")
        t.unlink()
        res = fm.undo_all()
        assert int(res) == 0 and res.missing == 1 and fm.journal == [], res.to_dict()

        # 5. نسخة عدّلها المستخدم بعد إنشائها: لا يحذفها التراجع (لا تُحذف نهائياً: تبقى أو تُحمى في السلة) ويُبلَّغ بالسبب
        ws, fm = mk({"a.txt": "A"})
        fm.create_dir("d")
        out = fm.copy("a.txt", "d/a_copy.txt")
        out.write_text("EDITED BY USER AFTER COPY - longer", encoding="utf-8")
        res = fm.undo_all()
        kept = [q for q in [out] + list(fm.trash_dir.rglob("a_copy.txt")) if q.exists()]
        assert kept and all(q.read_text(encoding="utf-8").startswith("EDITED") for q in kept), res.to_dict()   # لا ضياع لعمل المستخدم
        assert res.skipped == 1 and res.reason, res.to_dict()

        # 6. فشل التراجع عن خطوة (خطأ إدخال/إخراج مؤقت) لا يسدّ التراجع عن خطوة مستقلّة تحتها
        ws, fm = mk({"x.txt": "X", "y.txt": "Y"})
        fm.rename("x.txt", "x2.txt")
        fm.rename("y.txt", "y2.txt")
        real = FileManager._undo_op
        calls = []

        def flaky(self_fm, op):
            calls.append(op["dest"])
            if str(op["dest"]).endswith("y2.txt"):
                raise OSError(errno.EIO, "transient")
            return real(self_fm, op)
        with mock.patch.object(FileManager, "_undo_op", flaky):
            res = fm.undo_all()
        assert int(res) == 1 and (ws / "x.txt").exists() and (ws / "y2.txt").exists(), res.to_dict()
        assert res.failures and res.remaining == 1 and res.reason, res.to_dict()
        # وبعد زوال العطل يكتمل التراجع
        assert int(fm.undo_all()) == 1 and (ws / "y.txt").exists() and fm.journal == []

        # 7. سجل فيه إدخال بنوع عملية مجهول: يُتخطّى ويُحفظ للفحص ولا يسدّ بقية التراجع
        ws, fm = mk({"a.txt": "A"})
        fm.rename("a.txt", "b.txt")
        fm.journal.append({"action": "format_disk", "path": str(ws), "task": None})
        res = fm.undo_all()
        assert (ws / "a.txt").exists() and res.skipped == 1, res.to_dict()
        assert (ws / ".undo_journal.skipped.jsonl").exists()

        # 8. مجلد منشأ فيه ملفات وقت التراجع: يُنقل للسلة ولا يُحذف (لا حذف نهائي)
        ws, fm = mk({})
        fm.create_dir("proj")
        (ws / "proj" / "user.txt").write_text("precious", encoding="utf-8")
        assert int(fm.undo_all()) == 1 and not (ws / "proj").exists()
        assert any(p.read_text(encoding="utf-8") == "precious" for p in fm.trash_dir.rglob("user.txt"))

    print("PASS: test_cov_files_undo_edges completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_files_undo_edges", file=sys.stderr)
    sys.exit(1)
