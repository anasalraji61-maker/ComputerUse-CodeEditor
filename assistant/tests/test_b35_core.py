"""اختبارات B35 (نواة): قفل مجلد العمل بين النسخ، نية العملية (write-ahead) والتعافي، نسخ عبر مؤقت، تبنّي السجل بالمحتوى،
إعادة المحاولة عند أقفال لحظية، طول المسار على ويندوز، تصنيف أخطاء النظام، محارف ويندوز الممنوعة، شكل نتيجة التراجع، وفلتر القائمة."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import assistant.tools.files as ft
    from assistant.tools.files import (FileManager, PathOutOfBoundsError, WorkspaceBusyError, UndoResult, explain_error,
                                       name_problem, retry_io, pid_alive, INTENT_NAME, LOCK_NAME)
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.models.fake import FakeProvider

    def raises(exc, fn, *a, **k):
        try:
            fn(*a, **k)
        except exc:
            return True
        raise AssertionError(f"توقعت {exc.__name__}")

    def mkws(tmp, files=("a.txt", "b.txt", "c.txt", "d.txt")):
        ws = pathlib.Path(tmp).resolve() / "ws"
        ws.mkdir()
        for n in files:
            (ws / n).write_text(n, encoding="utf-8")
        return ws

    def child(code: str, *args):
        return subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r)\n" % str(ROOT) + code, *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    # ---------- 1. نسختان على مجلد واحد لا تفقدان سجل بعضهما ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp)
        A, B = FileManager(ws), FileManager(ws)
        A.current_task, B.current_task = "A", "B"
        A.move("a.txt", "A_dir/a.txt")
        B.move("b.txt", "B_dir/b.txt")
        C = FileManager(ws)
        assert {e.get("task") for e in C.journal} == {"A", "B"}, C.journal
        assert len(A.journal) >= 2 and len(B.journal) >= 2   # كل نسخة تدمج ما كتبته الأخرى قبل أن تكتب
        r = A.undo_all()
        assert r == 4 and (ws / "a.txt").exists() and (ws / "b.txt").exists(), (int(r), r.reason)
        assert not list(ws.glob(".undo_journal.json.*.tmp")) and not (ws / LOCK_NAME).exists() and not (ws / INTENT_NAME).exists()

    # عمليتان (OS) معاً: لا إدخال مفقود
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=())
        for t in "XY":
            for i in range(15):
                (ws / f"{t}{i}.txt").write_text("x", encoding="utf-8")
        code = ("from assistant.tools.files import FileManager\nws, tag = sys.argv[1], sys.argv[2]\n"
                "fm = FileManager(ws); fm.current_task = tag; bad = 0\n"
                "for i in range(15):\n    try: fm.move(f'{tag}{i}.txt', f'out_{tag}/{tag}{i}.txt')\n    except Exception as e: bad += 1; print(repr(e))\n"
                "print('bad', bad)\n")
        ps = [subprocess.Popen([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r)\n" % str(ROOT) + code, str(ws), t],
                               stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace") for t in "XY"]
        outs = [p.communicate()[0] for p in ps]
        assert all("bad 0" in o for o in outs), outs
        fm = FileManager(ws)
        assert len([e for e in fm.journal if e["action"] == "move"]) == 30, len(fm.journal)

    # قفل يتيم (عملية ميتة) يُكسر، وقفل حيّ يُنتظر ثم WorkspaceBusyError
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp)
        (ws / LOCK_NAME).write_text(json.dumps({"pid": 2 ** 22 - 3, "token": "dead", "t": time.time()}), encoding="utf-8")
        fm = FileManager(ws)
        fm.move("a.txt", "x/a.txt")
        assert not (ws / LOCK_NAME).exists()
        other = FileManager(ws)   # نسخة حيّة في العملية نفسها تحمل القفل فعلاً
        other._wslock.acquire()
        fm._wslock.timeout = 0.3
        raises(WorkspaceBusyError, fm.move, "b.txt", "y/b.txt")
        assert (ws / "b.txt").exists()
        other._wslock.release()
        assert not (ws / LOCK_NAME).exists()
        # قفل قديم جداً حتى لو صاحبه «حيّ» (إعادة استعمال رقم عملية)
        (ws / LOCK_NAME).write_text(json.dumps({"pid": os.getpid(), "token": "old", "t": time.time() - 99999}), encoding="utf-8")
        fm.move("b.txt", "y/b.txt")
        # فرع ويندوز لفحص حياة العملية قابل للاختبار بالاستبدال
        with mock.patch.object(ft, "IS_WINDOWS", True), mock.patch.object(ft, "_pid_alive_windows", lambda pid: pid == 4242):
            assert pid_alive(4242) and not pid_alive(4243)

    # ---------- 2. نية العملية والتعافي بعد انقطاع ----------
    mv = ("from assistant.tools.files import FileManager\nimport os\nws = sys.argv[1]; fm = FileManager(ws); fm.current_task = 't'\n"
          "fm._ensure_dirs(fm.allowed_root / 'Archive')\nfm._save_journal = lambda: os._exit(9)\nfm.move('report.docx', 'Archive/report.docx')\n")
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=())
        (ws / "report.docx").write_bytes(b"R" * 100)
        assert child(mv, str(ws)).returncode == 9
        assert (ws / INTENT_NAME).exists() and not (ws / "report.docx").exists()
        fm = FileManager(ws)
        assert [e["action"] for e in fm.journal] == ["create_dir", "move"], fm.journal
        assert any("سُجِّلت الآن" in n for n in fm.undo_notes) and not (ws / INTENT_NAME).exists()
        assert fm.undo_all() == 2 and (ws / "report.docx").exists()
    cp = ("from assistant.tools.files import FileManager\nimport os, shutil\nfrom pathlib import Path\nws = sys.argv[1]; fm = FileManager(ws); fm.current_task = 't'\n"
          "def half(s, d, **k):\n    data = Path(s).read_bytes(); Path(d).write_bytes(data[:len(data)//2]); os._exit(9)\n"
          "shutil.copy2 = half\nfm.copy('big.bin', 'backup/big.bin')\n")
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=())
        (ws / "big.bin").write_bytes(os.urandom(5000))
        assert child(cp, str(ws)).returncode == 9
        assert any(p.name.startswith(".cos_copy_tmp_") for p in (ws / "backup").iterdir())   # النسخة الناقصة لم تكن باسمها النهائي
        assert not (ws / "backup" / "big.bin").exists()
        fm = FileManager(ws)
        assert not any(p.name.startswith(".cos_copy_tmp_") for p in (ws / "backup").iterdir())
        assert any(p.name.startswith("partial_") for p in (ws / ".trash").iterdir()) and any("نسخ" in n for n in fm.undo_notes), fm.undo_notes
    # نسخة اكتملت (إعادة تسمية المؤقت) وانقطع قبل السجل: تُتبنّى
    cp2 = ("from assistant.tools.files import FileManager\nimport os\nws = sys.argv[1]; fm = FileManager(ws); fm.current_task = 't'\n"
           "fm._save_journal = lambda: os._exit(9)\nfm.copy('big.bin', 'big2.bin')\n")
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=())
        (ws / "big.bin").write_bytes(b"1" * 100)
        assert child(cp2, str(ws)).returncode == 9 and (ws / "big2.bin").exists()
        fm = FileManager(ws)
        assert [e["action"] for e in fm.journal] == ["copy"] and fm.undo_all() == 1 and not (ws / "big2.bin").exists() and (ws / "big.bin").exists()
    # تغيير حالة الأحرف عبر اسم مؤقت وانقطاع بين الخطوتين
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=("a.txt",))
        intent = {"v": 1, "pid": 2 ** 22 - 5, "action": "rename", "src": "rel:a.txt", "dest": "rel:A.txt", "tmp": "rel:.cos_rename_tmp_a.txt", "task": "t"}
        (ws / "a.txt").rename(ws / ".cos_rename_tmp_a.txt")
        (ws / INTENT_NAME).write_text(json.dumps(FileManager(ws).sign_disk_entry(intent)), encoding="utf-8")   # كاتب شرعي يوقّع نيته
        fm = FileManager(ws)
        assert (ws / "a.txt").exists() and not (ws / ".cos_rename_tmp_a.txt").exists() and any("إعادة تسمية" in n for n in fm.undo_notes), fm.undo_notes
    # نية تالفة: تُبلَّغ ولا تُسقط التشغيل
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=())
        (ws / INTENT_NAME).write_text("{not json", encoding="utf-8")
        fm = FileManager(ws)
        assert not (ws / INTENT_NAME).exists() and any("تالف" in n for n in fm.undo_notes)
        assert list(ws.glob(".undo_journal.intent.rejected*.json"))
    # اسم مستخدم يشبه المؤقت لا يُمَس عند البدء
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=(".cos_rename_tmp_a.txt", ".cos_copy_tmp_mine"))
        FileManager(ws)
        assert (ws / ".cos_rename_tmp_a.txt").exists() and (ws / ".cos_copy_tmp_mine").exists()

    # ---------- 3. نسخة عبر مؤقت: الاسم النهائي لا يظهر ناقصاً ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=("f.bin",))
        fm = FileManager(ws)
        seen = []
        real = ft.shutil.copy2
        def spy(s, d, **k):
            seen.append(pathlib.Path(d).name)
            return real(s, d, **k)
        with mock.patch.object(ft.shutil, "copy2", spy):
            dest = fm.copy("f.bin", "g.bin")
        assert dest.name == "g.bin" and seen and seen[0].startswith(".cos_copy_tmp_") and not list(ws.glob(".cos_copy_tmp_*"))

    # ---------- 4. تبنّي السجل المؤقت بالمحتوى لا بوقت الملف ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=("a.txt", "b.txt"))
        fm = FileManager(ws)
        fm.move("a.txt", "a2.txt")
        cur = json.loads((ws / ".undo_journal.json").read_text(encoding="utf-8"))
        ext = cur + [{"task": None, "action": "move", "src": "rel:b.txt", "dest": "rel:b2.txt"}]
        ext[-1] = fm.sign_disk_entry(ext[-1])
        tmpj = ws / f".undo_journal.json.{2 ** 22 - 7}.deadbeef.tmp"
        tmpj.write_text(json.dumps(ext), encoding="utf-8")
        old = time.time() - 86400
        os.utime(tmpj, (old, old))                     # ساعة رجعت للوراء: التاريخ لا يهم
        (ws / "b.txt").rename(ws / "b2.txt")
        fm2 = FileManager(ws)
        assert [e["action"] for e in fm2.journal] == ["move", "move"] and not tmpj.exists()
        # غير متصل بالسجل الحالي: يُرفض لا يُتبنّى
        tmpj.write_text(json.dumps([{"action": "move", "src": "x", "dest": "y"}]), encoding="utf-8")
        fm3 = FileManager(ws)
        assert len(fm3.journal) == 2 and not tmpj.exists() and list(ws.glob(".undo_journal.rejected*.json"))

    # ---------- 5. إعادة المحاولة عند أقفال لحظية ----------
    calls = []
    def flaky(n, exc):
        def f(*a):
            calls.append(1)
            if len(calls) <= n:
                raise exc
            return "ok"
        return f
    sleeps = []
    with mock.patch.object(ft.time, "sleep", lambda t: sleeps.append(t)):
        calls.clear(); assert retry_io(flaky(3, PermissionError(13, "denied"))) == "ok" and len(calls) == 4
        e32 = OSError(32, "used"); e32.winerror = 32
        calls.clear(); assert retry_io(flaky(2, e32)) == "ok"
        calls.clear(); raises(PermissionError, retry_io, flaky(10, PermissionError(13, "d"))); assert len(calls) == 5   # 5 محاولات
        calls.clear(); raises(FileNotFoundError, retry_io, flaky(10, FileNotFoundError(2, "x"))); assert len(calls) == 1  # غير لحظي
    assert all(0.05 <= x <= 0.3 for x in sleeps), sleeps
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=("a.txt",))
        fm = FileManager(ws)
        real_replace, n = os.replace, [0]
        def rep(a, b):
            if str(b).endswith(".undo_journal.json") and n[0] < 2:
                n[0] += 1
                raise PermissionError(13, "AV scanning")
            return real_replace(a, b)
        with mock.patch.object(ft.time, "sleep", lambda t: None), mock.patch("os.replace", rep):
            fm.move("a.txt", "z.txt")
        assert n[0] == 2 and (ws / "z.txt").exists() and len(FileManager(ws).journal) == 1

    # ---------- 6. طول المسار الكلي على ويندوز ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=())
        fm = FileManager(ws)
        deep = "/".join(["d" * 40] * 8)
        if ft.IS_WINDOWS:   # على ويندوز الحد الكلي 259 فعّال أصلاً (مجلد الاختبار المؤقت هناك طويل أيضاً)
            try:
                fm.validate_path(deep)
                raise AssertionError("يجب الرفض على ويندوز")
            except PathOutOfBoundsError as e:
                assert "259" in str(e) and "ويندوز" in str(e)
        else:
            fm.validate_path(deep)   # على لينكس لا حد كلي
        with mock.patch.object(ft, "IS_WINDOWS", True):
            try:
                fm.validate_path(deep)
                raise AssertionError("يجب الرفض")
            except PathOutOfBoundsError as e:
                assert "259" in str(e) and "ويندوز" in str(e)
            fm.validate_path("short.txt")

    # ---------- 7. تصنيف أخطاء النظام ----------
    def oserr(win=None, no=None, cls=OSError, msg="C:\\Users\\bob\\secret\\file.txt"):
        e = cls(no, msg) if no is not None else cls(msg)
        if win is not None:
            e.winerror = win
        return e
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=())
        r = TaskRunner(ws, FakeProvider(default_response="{}"), SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db"))
        cases = {32: "مستخدم من برنامج آخر", 5: "لا صلاحية", 3: "لم يعد موجوداً", 123: "غير صالح", 206: "طويل جداً", 112: "مساحة كافية"}
        for w, word in cases.items():
            msg = r._short_error(oserr(win=w, no=13))
            assert word in msg and "bob" not in msg and "C:" not in msg, (w, msg)
        # رسالة عربية داخل OSError لا تُعامل كرسالتنا الجاهزة (قد تحوي مسارات)
        assert "bob" not in r._short_error(OSError(5, "الوصول مرفوض C:\\Users\\bob\\x.txt"))
        assert "مسار" not in r._short_error(ValueError("خطأ في الاسم")) and "خطأ في الاسم" in r._short_error(ValueError("خطأ في الاسم"))
        assert r._short_error(RuntimeError("English only")) == "خطأ غير متوقع أثناء التنفيذ"
        assert explain_error(ValueError("x")) is None

    # ---------- 8. محارف ويندوز الممنوعة ----------
    for bad in ['a<b', 'a>b', 'a"b', 'a|b', 'a?b', 'a*b', "a\x07b"]:
        assert name_problem(bad), bad
        assert ft._bad_component(bad), bad
    for good in ["تقرير ٢٠٢٤ (نسخة 2).pdf", "café.txt", "a-b_c.d", "x'y.txt"]:
        assert name_problem(good) is None and not ft._bad_component(good), good

    # ---------- 9. شكل نتيجة التراجع ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp)
        led = SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db")
        steps = [{"step_id": 1, "action_type": "move", "target": "a.txt", "params": {"dest": "M/a.txt"}},
                 {"step_id": 2, "action_type": "move", "target": "b.txt", "params": {"dest": "M/b.txt"}}]
        r = TaskRunner(ws, FakeProvider(default_response=json.dumps({"steps": steps})), led)
        assert r.run("T", "x").success
        # فشل التراجع: قفل مستمر على إعادة تسمية
        real_rename = os.rename
        def deny(a, b):
            raise PermissionError(13, "locked")
        with mock.patch.object(ft.time, "sleep", lambda t: None), mock.patch("os.rename", deny):
            res = r.undo_task("T")
        assert isinstance(res, int) and res == 0 and res.done == 0 and res.remaining >= 1 and not res.complete
        assert res.reason and "صلاحية" in res.reason and isinstance(res.to_dict()["notes"], list), res.to_dict()
        res = r.undo_task("T")
        assert res == 3 and res.complete and res.remaining == 0 and res.reason is None and res.notes is not None  # create_dir + 2 move
        # مهمة أحدث تمنع
        assert r.undo_task("T") == 0
    ur = UndoResult(2, remaining=1, reason="س", notes=["n"])
    assert ur + 1 == 3 and ur.to_dict() == {"done": 2, "remaining": 1, "reason": "س", "notes": ["n"], "skipped": 0, "missing": 0, "failures": [], "unverified": 0, "needs_confirmation": False, "complete": False}

    # ---------- 10. القائمة: لا إسقاط صامت ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=("ok.txt", "bad\u200b.txt"))
        # اسم أطول من حد 120 القديم لكنه يتسع في الحد الفعلي: على ويندوز المسار الكلي ≤ 259 (مجلد الاختبار المؤقت يأكل جزءاً)
        nlen = 250 if not ft.IS_WINDOWS else max(121, min(250, ft.MAX_PATH_CHARS - len(str(ws)) - 1 - 2))
        longname = "N" * nlen + ".t"
        for odd in ("q?.txt", longname):          # قد يرفض نظام الملفات (محاكي ويندوز) إنشاء بعضها
            try:
                (ws / odd).write_text("x", encoding="utf-8")
            except OSError:
                pass
        made_q, made_n = (ws / "q?.txt").exists(), (ws / longname).exists()
        r = TaskRunner(ws, FakeProvider(default_response="{}"), SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db"))
        lst = r.workspace_listing().splitlines()
        too_long = ft.IS_WINDOWS and len(str(ws / longname)) > ft.MAX_PATH_CHARS     # أطول من حد المسار الكلي: يُبلَّغ عنه لا يُعرض
        assert "ok.txt" in lst and (not made_n or too_long or longname in lst)       # حد 255 الحقيقي لا 120
        assert lst[-1].startswith("…") and "bad?.txt" in lst[-1], lst
        if made_n and too_long:
            assert any(longname == x or longname in x for x in r.last_listing_skipped), r.last_listing_skipped
        assert len(r.last_listing_skipped) == 1 + int(made_q) + int(made_n and too_long)
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=("ok.txt", "longer_name_for_limit.txt"))
        r = TaskRunner(ws, FakeProvider(default_response="{}"), SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db"))
        with mock.patch("assistant.runner.IS_WINDOWS", True), mock.patch("assistant.runner.MAX_PATH_CHARS", len(str(ws)) + 10):
            lst = r.workspace_listing().splitlines()
        assert any(x.startswith("…") for x in lst) and "longer_name_for_limit.txt" not in lst[:-1], lst

    # ---------- 11. استلام قفل يتيم: تسوية نية صاحبه السابق وبقاياه قبل عمليتنا ----------
    dl = ("from assistant.tools.files import FileManager\nimport os\nws = sys.argv[1]; fm = FileManager(ws); fm.current_task = 'X'\n"
          "fm._save_journal = lambda: os._exit(9)\nfm.delete('victim.txt')\n")
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=("victim.txt", "other.txt"))
        Y = FileManager(ws)     # حيّة قبل أن يموت X
        Y.current_task = "Y"
        assert child(dl, str(ws)).returncode == 9
        assert (ws / INTENT_NAME).exists() and (ws / LOCK_NAME).exists() and not (ws / "victim.txt").exists()
        Y.move("other.txt", "m/other.txt")
        assert not (ws / INTENT_NAME).exists() and not (ws / LOCK_NAME).exists()
        acts = [(e["action"], e.get("task")) for e in Y.journal]
        assert acts[0] == ("delete", "X") and acts[-1] == ("move", "Y"), acts   # عملية X سُجّلت قبل عمليتنا ولم تُضِع
        assert Y.undo_all() >= 3 and (ws / "victim.txt").exists() and (ws / "other.txt").exists()
    # نسخة نصف مكتملة من عملية ماتت: لا يبقى مؤقتها ظاهراً في المجلد
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=("o.txt",))
        (ws / "docs").mkdir()
        (ws / "docs" / "big.bin").write_bytes(b"x" * 5000)
        Y = FileManager(ws)
        assert child(cp.replace("'big.bin', 'backup/big.bin'", "'docs/big.bin', 'docs/big_copy.bin'"), str(ws)).returncode == 9
        assert any(p.name.startswith(".cos_copy_tmp_") for p in (ws / "docs").iterdir())
        Y.move("o.txt", "m/o.txt")
        assert sorted(p.name for p in (ws / "docs").iterdir()) == ["big.bin"], list((ws / "docs").iterdir())
        assert any(p.name.startswith("partial_") for p in (ws / ".trash").iterdir())
    # نية قديمة من عملية ميتة دون قفل يتيم: لا تُكتب فوقها
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=("b.txt",))
        Y = FileManager(ws)
        Y.current_task = "Y"
        (ws / "z").mkdir()
        (ws / "z" / "a.txt").write_text("a", encoding="utf-8")      # نُقل لكن لم يُسجَّل
        planted = {"v": 1, "pid": 2 ** 22 - 7, "action": "move", "src": "rel:a.txt", "dest": "rel:z/a.txt", "task": "X"}
        (ws / INTENT_NAME).write_text(json.dumps(Y.sign_disk_entry(planted)), encoding="utf-8")
        Y.move("b.txt", "m/b.txt")
        assert [e.get("src") for e in Y.journal if e["action"] == "move"][0].endswith("a.txt"), Y.journal
        assert Y.undo_all() >= 2 and (ws / "a.txt").exists() and (ws / "b.txt").exists() and not (ws / INTENT_NAME).exists()

    # ---------- 12. كسر القفل اليتيم لا ينقل قفلاً حيّاً، وسباق عدة عمليات بلا خرق للاستبعاد ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp, files=())
        L = ft._WorkspaceLock(ws / LOCK_NAME)
        (ws / LOCK_NAME).write_text("LIVE-NEW", encoding="utf-8")
        assert L._remove_if_same(ws / LOCK_NAME, "OLD-STALE") is False and (ws / LOCK_NAME).read_text(encoding="utf-8") == "LIVE-NEW"   # أُعيد كما كان
        assert L._remove_if_same(ws / LOCK_NAME, "LIVE-NEW") is True and not (ws / LOCK_NAME).exists()
        assert not list(ws.glob(".undo_journal.lock.stale.*"))
        # قفل يتيم + كاسر حيّ آخر يعمل: ننتظر ولا نكسر
        (ws / LOCK_NAME).write_text(json.dumps({"pid": 2 ** 22 - 3, "token": "d", "t": time.time()}), encoding="utf-8")
        hold = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            (ws / (LOCK_NAME + ".brk")).write_text(json.dumps({"pid": hold.pid, "t": time.time()}), encoding="utf-8")
            assert L._break_stale() is False and (ws / LOCK_NAME).exists()
            (ws / (LOCK_NAME + ".brk")).write_text(json.dumps({"pid": 2 ** 22 - 3, "t": time.time()}), encoding="utf-8")   # كاسر ميت: يُزال ويُكسر
            assert L._break_stale() is True and not (ws / LOCK_NAME).exists() and not (ws / (LOCK_NAME + ".brk")).exists()
        finally:
            hold.kill()
            hold.wait()
    race = ("import time\nfrom pathlib import Path\nfrom assistant.tools.files import _WorkspaceLock\n"
            "ws = Path(sys.argv[1]); L = _WorkspaceLock(ws / '.undo_journal.lock', timeout=20)\n"
            "while time.time() < float(sys.argv[2]): pass\nL.acquire()\nm = ws / 'CS'\n"
            "try:\n    fd = os.open(str(m), os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.close(fd); time.sleep(0.01); os.unlink(str(m)); print('ok')\n"
            "except FileExistsError:\n    print('VIOLATION')\nL.release()\n")
    race = "import os\n" + race
    viol = 0
    for it in range(14):
        with tempfile.TemporaryDirectory() as tmp:
            ws = mkws(tmp, files=())
            (ws / LOCK_NAME).write_text(json.dumps({"pid": 2 ** 22 - 3, "token": "dead", "t": time.time()}), encoding="utf-8")
            go = time.time() + 0.5
            ps = [subprocess.Popen([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r)\n" % str(ROOT) + race, str(ws), str(go)],
                                   stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace") for _ in range(6)]
            outs = [p.communicate()[0].strip() for p in ps]
            viol += outs.count("VIOLATION")
            assert outs.count("ok") == 6, outs
    assert viol == 0

    # ---------- 13. فشل تحرير القفل لا يترك قفلاً حيّ الرقم إلى الأبد ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp)
        fm = FileManager(ws)
        fm._wslock.timeout = 1.0
        orig_read = ft._WorkspaceLock._read
        calls = {"n": 0}

        def bad_read(self):
            calls["n"] += 1
            return None if calls["n"] <= 2 else orig_read(self)
        with mock.patch.object(ft._WorkspaceLock, "_read", bad_read):
            fm.move("a.txt", "o1/a.txt")
        assert not (ws / LOCK_NAME).exists()                       # قراءة عابرة الفشل أُعيدت ثم حُذف قفلنا
        orig_read_ok = ft._WorkspaceLock._read
        with mock.patch.object(ft._WorkspaceLock, "_read", lambda self: None):   # غير مقروء دائماً وهو موجود: نحذفه ونحن نحمل رمزه
            fm.move("b.txt", "o2/b.txt")
        assert not (ws / LOCK_NAME).exists()
        real_unlink = os.unlink

        def bad_unlink(p, *a, **k):
            if str(p).endswith(LOCK_NAME):
                raise PermissionError(13, "pending delete")
            return real_unlink(p, *a, **k)
        with mock.patch.object(ft.os, "unlink", bad_unlink), mock.patch.object(ft, "retry_io", lambda f, *a, **k: f(*a)):
            fm.move("c.txt", "o3/c.txt")
        assert (ws / LOCK_NAME).exists()                           # تسرّب فعلاً
        t0 = time.time()
        fm.create_dir("next")                                      # لكن لا يحجز: قفل برقم عمليتنا بلا كائن حيّ = يتيم
        FileManager(ws).move("d.txt", "o4/d.txt")
        assert time.time() - t0 < 3 and not (ws / LOCK_NAME).exists()

    # ---------- 14. التراجع والمجلد مشغول: لا استثناء، سبب عربي، انتظار قصير ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp)
        fm = FileManager(ws)
        fm.current_task = "T1"
        fm.move("a.txt", "x/a.txt")
        fm.move("b.txt", "x/b.txt")
        runner = TaskRunner(ws, FakeProvider(default_response="{}"), SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db"))
        runner.file_manager = fm
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            rec = {"pid": holder.pid, "token": "o", "t": time.time()}
            st = ft.proc_start(holder.pid)
            if st:
                rec["st"] = st[0]
            (ws / LOCK_NAME).write_text(json.dumps(rec), encoding="utf-8")
            assert FileManager.undo_step.__defaults__ == (ft.UNDO_LOCK_TIMEOUT_SECONDS, 0) and ft.UNDO_LOCK_TIMEOUT_SECONDS <= 5 < ft.LOCK_TIMEOUT_SECONDS
            n = len(fm.journal)
            t0 = time.time()
            assert fm.undo_step(0.3) == "error" and fm.last_undo_reason == ft.BUSY_UNDO_REASON
            assert fm.last_undo_reason.startswith("نافذة أخرى تعمل على نفس المجلد")
            r = fm.undo_all(0.3)
            assert isinstance(r, UndoResult) and int(r) == 0 and r.remaining == n and r.reason == ft.BUSY_UNDO_REASON and not r.complete
            with mock.patch("assistant.runner.UNDO_LOCK_TIMEOUT_SECONDS", 0.3):
                r2 = runner.undo_task("T1")
                r3 = runner.undo_untagged()
            assert int(r2) == 0 and r2.reason == ft.BUSY_UNDO_REASON and r2.remaining == n, r2.to_dict()
            assert r3.reason in (None, ft.BUSY_UNDO_REASON)
            assert len(fm.journal) == n and (ws / "x" / "a.txt").exists()
            assert fm.undo_last(0.3) is False
            # وبدون تنفيذ: خيط آخر في العملية يحمل القفل
            holder.kill()
            holder.wait()
            (ws / LOCK_NAME).unlink()
            gate, done = threading.Event(), threading.Event()

            def hold_thread():
                with fm._op():
                    gate.set()
                    done.wait(10)
            th = threading.Thread(target=hold_thread)
            th.start()
            gate.wait(5)
            t0 = time.time()
            assert fm.undo_step(0.3) == "error" and fm.last_undo_reason == ft.BUSY_UNDO_REASON and time.time() - t0 < 5
            done.set()
            th.join()
            assert fm.undo_all() == n and (ws / "a.txt").exists()      # بعد الانفراج يعمل التراجع
        finally:
            holder.kill()

    # ---------- 15. إعادة استعمال رقم العملية بعد إعادة التشغيل ----------
    ps_live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            ws = mkws(tmp)
            now = time.time()
            # قفل قديم (بلا بصمة بداية) كتبته عملية أخرى؛ الرقم الآن لعملية حيّة بدأت بعده: يتيم
            (ws / LOCK_NAME).write_text(json.dumps({"pid": ps_live.pid, "token": "pre", "t": now - 600}), encoding="utf-8")
            t0 = time.time()
            fm = FileManager(ws)
            assert time.time() - t0 < 5 and not fm.undo_notes, fm.undo_notes
            fm._wslock.timeout = 2
            fm.move("a.txt", "x/a.txt")
            # بصمة بداية لا تطابق عملية الرقم الحالية
            (ws / LOCK_NAME).write_text(json.dumps({"pid": ps_live.pid, "token": "pre", "t": now, "st": "lx:other-boot:123"}), encoding="utf-8")
            fm.move("b.txt", "x/b.txt")
            # بصمة مطابقة وعملية حيّة وقفل حديث: قفل حقيقي لا يُكسر
            st = ft.proc_start(ps_live.pid)
            assert st is not None and st[1] <= time.time() + 1
            (ws / LOCK_NAME).write_text(json.dumps({"pid": ps_live.pid, "token": "real", "t": now, "st": st[0]}), encoding="utf-8")
            fm._wslock.timeout = 0.3
            raises(WorkspaceBusyError, fm.move, "c.txt", "x/c.txt")
            (ws / LOCK_NAME).unlink()
            assert ft.proc_start(os.getpid())[0] == ft.proc_start(os.getpid())[0] and ft._own_start_id() == ft.proc_start(os.getpid())[0]
            # قفل أقدم من إقلاع النظام (حين تتعذر بصمة العملية)
            with mock.patch.object(ft, "proc_start", lambda pid: None), mock.patch.object(ft, "_boot_time", lambda: time.time() - 100):
                (ws / LOCK_NAME).write_text(json.dumps({"pid": ps_live.pid, "token": "x", "t": time.time() - 1000}), encoding="utf-8")
                fm.move("c.txt", "x/c.txt")
                (ws / LOCK_NAME).write_text(json.dumps({"pid": ps_live.pid, "token": "x", "t": time.time() - 10}), encoding="utf-8")
                raises(WorkspaceBusyError, fm.move, "d.txt", "x/d.txt")     # كُتب بعد الإقلاع وصاحبه حيّ: يُنتظر
                (ws / LOCK_NAME).unlink()
            # فرع ويندوز (GetProcessTimes) بالمحاكاة
            with mock.patch.object(ft, "IS_WINDOWS", True), mock.patch.object(ft, "_pid_alive_windows", lambda pid: True), \
                    mock.patch.object(ft, "_proc_start_windows", lambda pid: ("win:777", time.time() - 5)):
                assert ft.proc_start(ps_live.pid)[0] == "win:777"
                (ws / LOCK_NAME).write_text(json.dumps({"pid": ps_live.pid, "token": "w", "t": time.time() - 3, "st": "win:111"}), encoding="utf-8")
                fm.move("d.txt", "x/d.txt")
            with mock.patch.object(ft, "IS_WINDOWS", True):
                import ctypes
                if not hasattr(ctypes, "windll") and ft._proc_start_windows.__name__ == "_proc_start_windows":
                    assert ft.proc_start(os.getpid()) is None   # بلا ctypes.windll: None لا استثناء
    finally:
        ps_live.kill()
        ps_live.wait()

    # ---------- 16. منع دائم عند إنشاء القفل: يُبلَّغ بسببه الحقيقي لا كـ«نافذة أخرى» ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = mkws(tmp)
        real_open = os.open

        def deny(p, flags, *a, **k):
            if str(p).endswith(LOCK_NAME):
                raise PermissionError(13, "Access denied")
            return real_open(p, flags, *a, **k)
        with mock.patch.object(ft.os, "open", deny), mock.patch.object(ft, "LOCK_EACCES_GRACE", 0.2):
            t0 = time.time()
            fm = FileManager(ws)
            assert any("لا صلاحية" in n for n in fm.undo_notes), fm.undo_notes
            try:
                fm.move("a.txt", "b/a.txt")
                raise AssertionError("expected PermissionError")
            except WorkspaceBusyError:
                raise AssertionError("PermissionError دائم لا يُعرض كمجلد مشغول")
            except PermissionError as exc:
                assert "لا صلاحية" in explain_error(exc) and "نافذة" not in explain_error(exc)
            assert time.time() - t0 < 5
        # عابر: يزول بعد محاولات فينجح
        n = {"c": 0}

        def flaky(p, flags, *a, **k):
            if str(p).endswith(LOCK_NAME):
                n["c"] += 1
                if n["c"] <= 3:
                    raise PermissionError(13, "pending delete")
            return real_open(p, flags, *a, **k)
        with mock.patch.object(ft.os, "open", flaky):
            fm2 = FileManager(ws)
            fm2.move("a.txt", "b/a.txt")
        assert (ws / "b" / "a.txt").exists() and n["c"] > 3

    print("PASS: test_b35_core completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b35_core", file=sys.stderr)
    sys.exit(1)
