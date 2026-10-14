"""اختبارات B28 (نواة/مخطِّط): مراجعة الفريق الأحمر التاسعة — رابط معلّق، تخطي الإدخال الفاسد في التراجع، تبنّي .tmp،
BOM، تراجع حالة الأحرف، نسخة لا تُحذف نهائياً، مخطِّط: new_name/dest/'..'/step_id/ترتيب الخطوات."""
import json
import os
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.planner import PlanParseError, Planner
    from assistant.models.base import ModelProvider
    from assistant.tools.files import FileManager, ProtectedPathError, PathOutOfBoundsError

    def raises(exc, fn, *a, **k):
        try:
            fn(*a, **k)
        except exc:
            return True
        raise AssertionError(f"توقعت {exc.__name__}")

    class Fixed(ModelProvider):
        def __init__(self, steps):
            self.steps = steps
        def generate(self, prompt, system_prompt=""):
            return json.dumps({"steps": self.steps})
        def name(self):
            return "fixed"

    def plan(steps):
        return Planner(Fixed(steps)).plan("x")

    def st(i, a, t, **p):
        return {"step_id": i, "action_type": a, "target": t, "params": p}

    # 1. رابط معلّق باسم النسخة المتوقعة: لا كتابة عبره خارج المجلد
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"; ws.mkdir(); out = base / "outside"; out.mkdir()
        (ws / "a.txt").write_text("a", encoding="utf-8")
        try:
            os.symlink(str(out / "newfile.txt"), str(ws / "a (1).txt"))
            linked = True
        except (OSError, NotImplementedError):
            linked = False
        if linked:
            fm = FileManager(ws)
            fm.copy("a.txt", "a.txt")
            assert not (out / "newfile.txt").exists()
            assert (ws / "a (2).txt").exists()

    # 2. إدخال فاسد في أعلى السجل لا يسدّ التراجع عمّا تحته
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp).resolve()
        (ws / "f.txt").write_text("f", encoding="utf-8")
        (ws / "g.txt").write_text("g", encoding="utf-8")
        fm = FileManager(ws)
        fm.delete("f.txt")
        fm.journal.append({"task": None, "action": "frobnicate"})
        fm._save_journal()
        assert fm.undo_all() == 1
        assert (ws / "f.txt").exists() and not fm.journal
        assert fm.undo_notes and (ws / ".undo_journal.skipped.jsonl").exists()

    # 3. ملف .tmp لا علاقة له بالسجل لا يُتبنّى (يمسح التاريخ) بل يُحفظ جانباً
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp).resolve()
        for n in "ab":
            (ws / f"{n}.txt").write_text(n, encoding="utf-8")
        fm = FileManager(ws)
        fm.delete("a.txt"); fm.delete("b.txt")
        assert len(fm.journal) == 2
        tmpf = ws / ".undo_journal.json.tmp"
        tmpf.write_text("[]", encoding="utf-8")
        later = os.stat(ws / ".undo_journal.json").st_mtime_ns + 5_000_000_000
        os.utime(tmpf, ns=(later, later))
        fm2 = FileManager(ws)
        assert len(fm2.journal) == 2
        assert not tmpf.exists() and (ws / ".undo_journal.rejected.json").exists()
        # امتداد بخطوة واحدة (انهيار بعد عملية قبل الاستبدال) يُتبنّى
        (ws / "c.txt").write_text("c", encoding="utf-8")
        fm2.delete("c.txt")
        cur = json.loads((ws / ".undo_journal.json").read_text(encoding="utf-8"))
        ext = cur + [dict(cur[-1])]
        tmpf.write_text(json.dumps(ext), encoding="utf-8")
        os.utime(tmpf, ns=(later + 10**9, later + 10**9))
        assert len(FileManager(ws).journal) == 4

    # 4. سجل بـ BOM يُقرأ؛ عناصر غير قاموس تُنبَّه وتُحفظ نسخة
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp).resolve()
        (ws / "x.txt").write_text("x", encoding="utf-8")
        fm = FileManager(ws)
        fm.delete("x.txt")
        raw = (ws / ".undo_journal.json").read_bytes()
        (ws / ".undo_journal.json").write_bytes(b"\xef\xbb\xbf" + raw)
        fm2 = FileManager(ws)
        assert len(fm2.journal) == 1 and not (ws / ".undo_journal.corrupt.json").exists()
        data = json.loads(raw.decode("utf-8")); data.append("junk")
        (ws / ".undo_journal.json").write_text(json.dumps(data), encoding="utf-8")
        fm3 = FileManager(ws)
        assert len(fm3.journal) == 1 and (ws / ".undo_journal.corrupt.json").exists() and fm3.undo_notes

    # 5. فشل حفظ السجل بعد التراجع لا يحوّل النتيجة إلى خطأ
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp).resolve()
        (ws / "y.txt").write_text("y", encoding="utf-8")
        fm = FileManager(ws)
        fm.delete("y.txt")
        orig = fm._save_journal
        def boom():
            raise OSError("locked")
        fm._save_journal = boom
        assert fm.undo_step() == "ok" and (ws / "y.txt").exists()
        fm._save_journal = orig

    # 6. تراجع نسخة بعد فشل الحفظ لا يحذف نهائياً (النسخة تنتقل للسلة)
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp).resolve()
        (ws / "z.txt").write_text("z", encoding="utf-8")
        fm = FileManager(ws)
        def boom2():
            raise OSError("disk")
        fm._save_journal = boom2
        raises(OSError, fm.copy, "z.txt", "z2.txt")
        assert not (ws / "z2.txt").exists()
        assert any(p.name.startswith("partial_z2") for p in (ws / ".trash").iterdir())

    # 7. مخطِّط: رفض new_name بمسار، dest = المصدر، مقاطع '..' و'.'، step_id خارج المدى
    raises(PlanParseError, plan, [st(1, "rename", "a.txt", new_name="s/b.txt")])
    raises(PlanParseError, plan, [st(1, "rename", "a.txt", new_name=" b.txt")])
    raises(PlanParseError, plan, [st(1, "move", "a.txt", dest="a.txt")])
    raises(PlanParseError, plan, [st(1, "delete", "D/../a.txt")])
    raises(PlanParseError, plan, [st(1, "delete", "D/.")])
    raises(PlanParseError, plan, [st(0, "delete", "a.txt")])
    raises(PlanParseError, plan, [st(10**30, "delete", "a.txt")])
    assert plan([st(1, "inspect", ".")])
    assert plan([st(1, "create_dir", "X"), st(2, "move", "a.txt", dest="X/a.txt"), st(3, "move", "b.txt", dest="X/b.txt")])

    # 8. مخطِّط: خطوة تستهدف ما حُذف/نُقل سابقاً تُرفض قبل أي تنفيذ
    raises(PlanParseError, plan, [st(1, "delete", "D"), st(2, "delete", "D/a.txt")])
    raises(PlanParseError, plan, [st(1, "move", "D", dest="E"), st(2, "delete", "d")])
    assert plan([st(1, "rename", "a.txt", new_name="b.txt"), st(2, "delete", "b.txt")])

    print("PASS: test_b28 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b28", file=sys.stderr)
    sys.exit(1)
