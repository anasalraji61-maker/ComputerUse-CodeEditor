"""اختبارات B22 (نواة): نتائج مراجعة الفريق الأحمر السابعة — ملف .tmp للسجل، نسخة ناقصة يتيمة، مجلد أب يحوي سراً،
حدود التراجع القديمة، قفل مؤقت للسجل، معرّف مكرر، winget محدّث أصلاً، create_dir موجود، تقدير الكلفة لطلب طويل."""
import json
import pathlib
import sys
import tempfile
import time
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.ledger import SQLiteLedger
    from assistant.models.rules import RuleProvider
    from assistant.planner import PlanStep
    from assistant.runner import TaskRunner
    from assistant.tools.files import FileManager, ProtectedPathError

    def raises(exc, fn, *a, **k):
        try:
            fn(*a, **k)
        except exc:
            return True
        raise AssertionError(f"توقعت {exc.__name__}")

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"; ws.mkdir()
        led = SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500)
        (ws / "important.txt").write_text("keep", encoding="utf-8")
        fm = FileManager(ws)

        # 1. الأسماء التي تبدأ بـ .undo_journal محجوزة (ومنها .tmp) فلا يضيع ملف المستخدم
        for name in (".undo_journal.json.tmp", ".undo_journal.corrupt.json", ".UNDO_JOURNAL.x"):
            raises(ProtectedPathError, fm.rename, "important.txt", name)
            raises(ProtectedPathError, fm.move, "important.txt", name)
        assert (ws / "important.txt").read_text(encoding="utf-8") == "keep"

        # 2. نسخة ملف فاشلة لا تترك ملفاً ناقصاً يتيماً
        (ws / "big.bin").write_bytes(b"x" * 1000)
        (ws / "backup").mkdir()
        def bad_copy(src, dst, *a, **k):
            pathlib.Path(dst).write_bytes(b"x" * 100)
            raise OSError(28, "No space left")
        with mock.patch("shutil.copy2", bad_copy):
            raises(OSError, fm.copy, "big.bin", "backup/big.bin")
        assert not (ws / "backup" / "big.bin").exists(), "بقيت نسخة ناقصة"
        assert any(p.name.startswith("partial_") for p in fm.trash_dir.iterdir())

        # 3. مجلد أب يحوي .ssh لا يُنسخ/يُنقل/يُحذف
        (ws / "home" / ".ssh").mkdir(parents=True)
        (ws / "home" / ".ssh" / "id_rsa").write_text("SECRET", encoding="utf-8")
        (ws / "home" / "a.txt").write_text("a", encoding="utf-8")
        raises(ProtectedPathError, fm.copy, "home", "share")
        raises(ProtectedPathError, fm.move, "home", "share")
        raises(ProtectedPathError, fm.delete, "home")
        assert (ws / "home" / ".ssh" / "id_rsa").exists() and not (ws / "share").exists()

        # 4. تراجع مهمة لا يُعاد على مهمة أحدث (حدود قديمة)
        ws2 = base / "ws2"; ws2.mkdir()
        for n in ("a.txt", "b.txt", "c.txt"):
            (ws2 / n).write_text(n, encoding="utf-8")
        r = TaskRunner(ws2, RuleProvider(), led)
        assert r.run("A", "x", plan=[PlanStep(1, "move", "a.txt", {"dest": "a_m.txt"})]).success
        assert r.undo_task("A") == 1
        assert r.run("B", "x", plan=[PlanStep(1, "move", "b.txt", {"dest": "b_m.txt"})]).success
        assert r.undo_task("A") == 0 and (ws2 / "b_m.txt").exists(), "تراجع A مسّ مهمة B"

        # 5. معرّف مهمة مكرر: تقرير فشل لا استثناء، وتراجع المهمة الأولى سليم
        assert r.run("C", "x", plan=[PlanStep(1, "move", "c.txt", {"dest": "c_m.txt"})]).success
        rep = r.run("C", "x", plan=[PlanStep(1, "create_dir", "zz")])
        assert not rep.success and any("تعذّر بدء" in f for f in rep.failures), rep.failures
        assert r.file_manager.current_task is None
        assert r.undo_task("C") == 1 and (ws2 / "c.txt").exists()

        # 6. قفل مؤقت لقراءة السجل لا يُنقل السجل إلى corrupt
        ws3 = base / "ws3"; ws3.mkdir()
        (ws3 / "x.txt").write_text("x", encoding="utf-8")
        f3 = FileManager(ws3); f3.move("x.txt", "y.txt")
        real = pathlib.Path.read_text
        calls = {"n": 0}
        def flaky(self, *a, **k):
            if self.name == ".undo_journal.json" and calls["n"] < 2:
                calls["n"] += 1
                raise PermissionError("locked")
            return real(self, *a, **k)
        with mock.patch.object(pathlib.Path, "read_text", flaky):
            f3b = FileManager(ws3)
        assert len(f3b.journal) == 1 and not (ws3 / ".undo_journal.corrupt.json").exists()
        def always(self, *a, **k):
            if self.name == ".undo_journal.json":
                raise PermissionError("locked")
            return real(self, *a, **k)
        with mock.patch.object(pathlib.Path, "read_text", always), mock.patch("time.sleep", lambda s: None):
            raises(ProtectedPathError, FileManager, ws3)
        assert (ws3 / ".undo_journal.json").exists() and not (ws3 / ".undo_journal.corrupt.json").exists()

        # 7. استعادة باسم بديل تُعلَن في ملاحظات التراجع
        ws4 = base / "ws4"; ws4.mkdir()
        (ws4 / "a.txt").write_text("old", encoding="utf-8")
        f4 = FileManager(ws4); f4.move("a.txt", "m.txt")
        (ws4 / "a.txt").write_text("NEW", encoding="utf-8")
        assert f4.undo_all() == 1
        assert (ws4 / "a.txt").read_text(encoding="utf-8") == "NEW" and (ws4 / "a (1).txt").exists()
        assert f4.undo_notes and "a (1).txt" in f4.undo_notes[0], f4.undo_notes

        # 8. create_dir على مجلد موجود: لا يُقال «تم إنشاء»
        ws5 = base / "ws5"; ws5.mkdir(); (ws5 / "Docs").mkdir()
        r5 = TaskRunner(ws5, RuleProvider(), led)
        rep = r5.run("D", "x", plan=[PlanStep(1, "create_dir", "Docs")])
        assert rep.success and any("موجود مسبقاً" in e for e in rep.executed_steps), rep.executed_steps

        # 9. winget: محدّثة أصلاً (سطر لم يتغير + رسالة لا تحديث) = فشل بلا كلفة
        r6 = TaskRunner(ws5, RuleProvider(), led, approver=lambda s: True,
                        winget_runner=lambda cmd: (0, "No available upgrade found.", ""),
                        version_probe=lambda pkg: (0, "Git Git.Git 2.40.0", ""))
        rep = r6.run("W", "x", plan=[PlanStep(1, "winget_upgrade", "Git.Git")])
        assert not rep.success and rep.points_charged == 0 and any("محدّثة أصلاً" in f for f in rep.failures), rep.failures

        # 10. تقدير الكلفة لا يتجمد على طلب طويل بلا فراغات
        t0 = time.time()
        SQLiteLedger.estimate_cost("a" * 60000 + "/", 3)
        assert time.time() - t0 < 0.5

    print("PASS: test_b22 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b22", file=sys.stderr)
    sys.exit(1)
