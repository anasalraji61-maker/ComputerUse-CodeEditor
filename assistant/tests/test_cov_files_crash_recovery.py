"""تغطية التعافي بعد انقطاع: نية create_dir مكتملة تُتبنّى، نية حذف/نقل ملتبسة لا تُنفَّذ ولا تُمسّ ملفاتها، عطب أثناء الاستعادة لا يُسقط الإقلاع،
ملف قفل يتيم (فارغ/قديم) يُكسر وحديث لا يُكسر، وسجل تراجع فيه إدخال خارج المجلد يُحجر ولا يُنفَّذ."""
import json
import os
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
    import assistant.tools.files as ft
    from assistant.tools.files import FileManager, INTENT_NAME, LOCK_NAME, WorkspaceBusyError, _WorkspaceLock

    DEAD = 2 ** 22 - 5      # رقم عملية غير موجود

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        keys = base / "keys"
        n = [0]

        def mkws(files=()):
            n[0] += 1
            ws = base / f"ws{n[0]}"
            ws.mkdir()
            for f in files:
                (ws / f).write_text(f, encoding="utf-8")
            return ws

        def write_intent(ws, rec):
            rec = dict(rec, v=1, pid=DEAD)
            (ws / INTENT_NAME).write_text(json.dumps(FileManager(ws, key_dir=keys).sign_disk_entry(rec)), encoding="utf-8")

        # 1. create_dir نُفّذ ثم انقطع قبل حفظ السجل: يُتبنّى فيمكن التراجع عنه
        ws = mkws()
        (ws / "newdir").mkdir()
        write_intent(ws, {"action": "create_dir", "paths": ["rel:newdir"], "task": "T1"})
        fm = FileManager(ws, key_dir=keys)
        assert [e["action"] for e in fm.journal] == ["create_dir"] and fm.journal[0]["task"] == "T1", fm.journal
        assert not os.path.lexists(str(fm.intent_path))
        assert int(fm.undo_all()) == 1 and not (ws / "newdir").exists()

        # 1b. create_dir لم يُنفَّذ أصلاً: لا تبنٍّ ولا نية عالقة
        ws = mkws()
        write_intent(ws, {"action": "create_dir", "paths": ["rel:never_made"], "task": "T1"})
        fm = FileManager(ws, key_dir=keys)
        assert fm.journal == [] and not os.path.lexists(str(fm.intent_path)) and not (ws / "never_made").exists()

        # 2. حذف ملتبس: الأصل والمنقول كلاهما موجودان -> لا نخمّن؛ لا نقل ولا حذف، ونبلّغ المستخدم
        ws = mkws(["a.txt"])
        fm0 = FileManager(ws, key_dir=keys)
        (fm0.trash_dir / "a.txt").write_text("other", encoding="utf-8")
        write_intent(ws, {"action": "delete", "original_path": "rel:a.txt", "trash_path": "rel:.trash/a.txt", "task": "T2"})
        fm = FileManager(ws, key_dir=keys)
        assert (ws / "a.txt").read_text(encoding="utf-8") == "a.txt" and (fm.trash_dir / "a.txt").read_text(encoding="utf-8") == "other"
        assert fm.journal == [] and any("الجزم" in x for x in fm.undo_notes), fm.undo_notes
        assert not os.path.lexists(str(fm.intent_path))

        # 3. نقل ملتبس: المصدر والوجهة موجودان وهما ملفان مختلفان
        ws = mkws(["s.txt", "d.txt"])
        write_intent(ws, {"action": "move", "src": "rel:s.txt", "dest": "rel:d.txt", "task": "T3"})
        fm = FileManager(ws, key_dir=keys)
        assert (ws / "s.txt").exists() and (ws / "d.txt").exists() and fm.journal == []
        assert any("الجزم" in x for x in fm.undo_notes), fm.undo_notes

        # 4. خطأ غير متوقع أثناء الاستعادة لا يُسقط الإقلاع: يُبلَّغ وتُمحى النية
        ws = mkws()
        (ws / "nd").mkdir()
        write_intent(ws, {"action": "create_dir", "paths": ["rel:nd"], "task": "T4"})
        with mock.patch.object(FileManager, "_journal_has", side_effect=RuntimeError("boom")):
            fm = FileManager(ws, key_dir=keys)
        assert any("تعذّر إكمال استعادة" in x for x in fm.undo_notes), fm.undo_notes
        assert not os.path.lexists(str(fm.intent_path)) and (ws / "nd").is_dir()

        # 5. بقايا نسخة مؤقتة يتيمة تُنقل للسلة عند الإقلاع (وأسماء المستخدم المشابهة لا تُمسّ)
        ws = mkws([".cos_copy_tmp_deadbeef", ".cos_copy_tmp_notmine"])
        fm = FileManager(ws, key_dir=keys)
        assert not (ws / ".cos_copy_tmp_deadbeef").exists() and (ws / ".cos_copy_tmp_notmine").exists()
        assert any(p.name.startswith("partial_") for p in fm.trash_dir.iterdir()) and any("بقايا نسخة" in x for x in fm.undo_notes)

        # 6. سجل فيه إدخال بمسار خارج المجلد: يُحجر ولا يُنفَّذ، وبقية السجل سليمة
        ws = mkws(["keep.txt"])
        outside = base / "victim.txt"
        outside.write_text("do not touch", encoding="utf-8")
        fmj = FileManager(ws, key_dir=keys)
        fmj.current_task = "T6"
        fmj.move("keep.txt", "moved.txt")
        good = json.loads(fmj.journal_path.read_text(encoding="utf-8"))
        evil = {"action": "move", "src": "rel:moved.txt", "dest": str(outside), "task": "T6"}
        evil2 = {"action": "delete", "original_path": "rel:../victim.txt", "trash_path": "rel:.trash/victim.txt"}
        fmj.journal_path.write_text(json.dumps(good + [evil, evil2]), encoding="utf-8")
        fm = FileManager(ws, key_dir=keys)
        assert [e["action"] for e in fm.journal] == ["move"], fm.journal
        assert (ws / ".undo_journal.rejected.jsonl").exists() and any("رُفض" in x for x in fm.undo_notes), fm.undo_notes
        assert outside.read_text(encoding="utf-8") == "do not touch"
        assert int(fm.undo_all()) == 1 and (ws / "keep.txt").exists()

        # 6b. نفس السجل لكن مجلد العمل مشغول بنسخة أخرى (لا قفل): الإدخال الفاسد يُتجاهل دون إعادة كتابة السجل أو حجره
        ws = mkws(["keep.txt"])
        fmj = FileManager(ws, key_dir=keys)
        fmj.move("keep.txt", "moved.txt")
        good = json.loads(fmj.journal_path.read_text(encoding="utf-8"))
        raw = json.dumps(good + [{"action": "move", "src": "rel:moved.txt", "dest": str(outside)}])
        fmj.journal_path.write_text(raw, encoding="utf-8")
        with mock.patch.object(_WorkspaceLock, "acquire", side_effect=WorkspaceBusyError("busy")):
            fmb = FileManager(ws, key_dir=keys)
        assert any("مشغول" in x for x in fmb.undo_notes) and any("تُجوهل" in x for x in fmb.undo_notes), fmb.undo_notes
        assert fmj.journal_path.read_text(encoding="utf-8") == raw and not (ws / ".undo_journal.rejected.jsonl").exists()

        # 7. ملف قفل: فارغ حديث لا يُكسر (صاحبه قد يكون يكتبه الآن)، وفارغ قديم يتيم فيُكسر
        lock_path = base / "x.lock"
        lock_path.write_text("", encoding="utf-8")
        lk = _WorkspaceLock(lock_path, timeout=0.3)
        try:
            lk.acquire()
            raise AssertionError("كان يجب أن يُرفض قفل حديث")
        except WorkspaceBusyError:
            pass
        old = time.time() - 60
        os.utime(str(lock_path), (old, old))
        lk2 = _WorkspaceLock(lock_path, timeout=2.0)
        assert lk2.acquire() is True and lk2.took_over is True
        lk2.release()
        assert not lock_path.exists()
        # قفل بطابع زمني مستقبلي بعيد (مزروع أو ساعة معطوبة) يُعدّ يتيماً
        lock_path.write_text(json.dumps({"pid": os.getpid() + 1, "t": 4e18}), encoding="utf-8")
        lk3 = _WorkspaceLock(lock_path, timeout=2.0)
        assert lk3.acquire() is True and lk3.took_over is True
        lk3.release()

    print("PASS: test_cov_files_crash_recovery completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_files_crash_recovery", file=sys.stderr)
    sys.exit(1)
