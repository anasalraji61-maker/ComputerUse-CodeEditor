"""اختبارات B21: سجل التراجع بمسارات نسبية — يعمل بعد نقل/إعادة تسمية مجلد العمل، ويقرأ السجلات القديمة المطلقة."""
import json
import pathlib
import shutil
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.tools.files import FileManager

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"; ws.mkdir()
        (ws / "a.txt").write_text("A", encoding="utf-8")
        (ws / "b.txt").write_text("B", encoding="utf-8")
        fm = FileManager(ws)
        fm.create_dir("d")
        fm.move("a.txt", "d/a.txt")
        fm.copy("b.txt", "b2.txt")
        fm.rename("b2.txt", "b3.txt")
        fm.delete("b.txt")
        raw = (ws / ".undo_journal.json").read_text(encoding="utf-8")
        assert str(ws) not in raw and "rel:" in raw, raw   # لا مسارات مطلقة على القرص
        # نقل المجلد كله إلى اسم آخر ثم التراجع
        ws2 = base / "renamed_ws"
        shutil.move(str(ws), str(ws2))
        fm2 = FileManager(ws2)
        assert len(fm2.journal) == 5
        assert fm2.undo_all() == 5
        assert (ws2 / "a.txt").exists() and (ws2 / "b.txt").exists()
        assert not (ws2 / "d").exists() and not (ws2 / "b3.txt").exists()

        # سجل قديم بمسارات مطلقة ما زال يعمل
        ws3 = base / "ws3"; ws3.mkdir()
        (ws3 / "x.txt").write_text("X", encoding="utf-8")
        (ws3 / "y").mkdir()
        (ws3 / "y" / "x.txt").write_text("X", encoding="utf-8")
        # حالة: نقل x.txt إلى y/x.txt (محاكاة: الملف في الوجهة فقط)
        (ws3 / "x.txt").unlink()
        (ws3 / ".undo_journal.json").write_text(json.dumps([
            {"action": "move", "src": str(ws3 / "x.txt"), "dest": str(ws3 / "y" / "x.txt")}]), encoding="utf-8")
        fm3 = FileManager(ws3)
        assert fm3.undo_all() == 1 and (ws3 / "x.txt").exists()

        # مسار rel: خبيث (..) يُرفض عند التراجع ولا يخرج من المجلد
        ws4 = base / "ws4"; ws4.mkdir()
        out = base / "victim.txt"; out.write_text("V", encoding="utf-8")
        (ws4 / "z.txt").write_text("Z", encoding="utf-8")
        (ws4 / ".undo_journal.json").write_text(json.dumps([
            {"action": "move", "src": "rel:../victim2.txt", "dest": "rel:z.txt"}]), encoding="utf-8")
        fm4 = FileManager(ws4)
        assert fm4.undo_all() == 0 and out.exists() and (ws4 / "z.txt").exists() and not (base / "victim2.txt").exists()

    print("PASS: test_b21 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b21", file=sys.stderr)
    sys.exit(1)
