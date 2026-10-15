"""اختبارات B37 (نواة): لا فقدان صامت لسجل التراجع عند تغيّر مفتاح التوقيع (إدخالات غير موثَّقة تبقى ولا تُنفَّذ إلا بالدفتر/التأكيد)،
ربط التوقيع بمجلد العمل وبرقم الإدخال، فحص الحالة دائماً، روابط الملفات الجانبية، توقيع غير ASCII، وسباق إنشاء المفتاح."""
import hashlib
import json
import os
import pathlib
import subprocess
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
    from assistant.tools.files import FileManager, UndoResult, KEY_NAME, MIGRATED_NAME
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.planner import PlanStep
    from assistant.models.fake import FakeProvider

    def mk(tmp, name="ws", files=("a.txt", "b.txt")):
        ws = pathlib.Path(tmp).resolve() / name
        ws.mkdir(parents=True)
        for n in files:
            (ws / n).write_text(n, encoding="utf-8")
        return ws

    def fresh(kd):
        ft._KEY_CACHE.clear()
        return kd

    def disk(ws):
        return json.loads((ws / ".undo_journal.json").read_text(encoding="utf-8"))

    try:
        _p = pathlib.Path(tempfile.mkdtemp())
        os.symlink(str(_p), str(_p.with_name(_p.name + "_lnk")))
        os.unlink(str(_p.with_name(_p.name + "_lnk")))
        can_link = True
    except (OSError, NotImplementedError):
        can_link = False

    # ---------- 1. تغيّر المفتاح لا يُفقد السجل أبداً ----------
    def setup(tmp):
        ws = mk(tmp, files=("a.txt", "b.txt"))
        kd = pathlib.Path(tmp) / "keys"
        fm = FileManager(ws, key_dir=fresh(kd))
        fm.current_task = "t1"
        fm.move("a.txt", "a2.txt")
        fm.delete("b.txt")
        return ws, kd, fm

    with tempfile.TemporaryDirectory() as tmp:
        ws, kd, fm = setup(tmp)
        before = disk(ws)
        assert len(before) == 2 and all(e["kid"] == fm._kid and e["ws"] == fm._ws_id and len(e["n"]) == 12 for e in before)
        # (أ) مجلد بيانات آخر/جهاز آخر: كل شيء يبقى كما هو (لا حجر) ولا ينفَّذ بلا تأكيد
        fm2 = FileManager(ws, key_dir=fresh(pathlib.Path(tmp) / "keys2"))
        assert len(fm2.journal) == 2 and all(e.get("unverified") for e in fm2.journal) and not (ws / ".undo_journal.rejected.jsonl").exists()
        assert fm2.unverified_count() == 2 and any("غير موثَّق" in n for n in fm2.undo_notes)
        r = fm2.undo_all()
        assert isinstance(r, UndoResult) and int(r) == 0 and r.unverified == 2 and r.needs_confirmation and r.remaining == 2
        assert "تأكيد" in r.reason and (ws / "a2.txt").exists() and r.to_dict()["needs_confirmation"] is True
        assert disk(ws) == before                                   # لم يُعاد توقيع شيء ولم يُجرَّد توقيع
        # تأكيد صريح -> يُنفَّذ (بفحص الحالة)
        r = fm2.undo_all(allow_unverified=True)
        assert int(r) == 2 and (ws / "a.txt").exists() and (ws / "b.txt").exists() and not r.needs_confirmation
    with tempfile.TemporaryDirectory() as tmp:
        ws, kd, fm = setup(tmp)
        before = disk(ws)
        (kd / KEY_NAME).unlink()                                    # (ب) حُذف المفتاح (تنظيف ~/.cos_assistant)
        fm2 = FileManager(ws, key_dir=fresh(kd))
        assert fm2._kid != fm._kid and all(e.get("unverified") for e in fm2.journal) and int(fm2.undo_all()) == 0 and disk(ws) == before
        # الدفتر يعرف المهمة (ترقية من تثبيت سابق/مفتاح ضائع) -> يُنفَّذ بلا تأكيد
        fm3 = FileManager(ws, key_dir=fresh(kd), known_task_ids={"t1"})
        assert int(fm3.undo_all()) == 2 and (ws / "a.txt").exists()
    with tempfile.TemporaryDirectory() as tmp:
        ws, kd, fm = setup(tmp)
        first = disk(ws)
        ft._KEY_CACHE.clear()
        with mock.patch.object(ft, "load_journal_key", lambda kdir=None: (None, "warn", None)):     # (ج) جلسة بلا مفتاح
            fk = FileManager(ws, key_dir=kd)
            (ws / "c.txt").write_text("c")
            fk.move("c.txt", "c2.txt")
        d = disk(ws)
        assert d[:2] == first and "mac" not in d[2] and len(d) == 3                 # لا تجريد لتوقيع موجود
        fr = FileManager(ws, key_dir=fresh(kd))                                     # المفتاح الأصلي عاد: الأصلي موثوق من جديد
        assert [bool(e.get("unverified")) for e in fr.journal] == [False, False, True]
        assert fr.unverified_count() == 1
        rr = fr.undo_all()
        assert int(rr) == 2 and rr.unverified == 1 and rr.needs_confirmation and (ws / "a.txt").exists() and (ws / "c2.txt").exists()
    # (د) مفتاح مؤقت لأن مجلد البيانات غير صالح: لا إعادة توقيع ولا «مُرقّى»، وتنبيه
    with tempfile.TemporaryDirectory() as tmp:
        old_tmp = tempfile.tempdir
        tempfile.tempdir = str(mk(tmp, "tmpdir", files=()))
        try:
            ws, kd, fm = setup(tmp)
            before = disk(ws)
            mig_before = (kd / MIGRATED_NAME).read_text()
            blocker = pathlib.Path(tmp) / "blocker"
            blocker.write_text("x")
            fe = FileManager(ws, key_dir=fresh(blocker / "sub"))
            assert fe.key_ephemeral and fe._key_dir.name == "cos_assistant_keys" and any("مؤقت" in n for n in fe.undo_notes)
            assert len(fe.journal) == 2 and all(e.get("unverified") for e in fe.journal) and int(fe.undo_all()) == 0
            (ws / "c.txt").write_text("c")
            fe.move("c.txt", "c2.txt")
            d = disk(ws)
            assert d[:2] == before                                  # الموجود كما هو
            assert not (blocker / "sub").exists()
            fe_map = fe._migrated_map()
            fe._mark_migrated()
            assert (kd / MIGRATED_NAME).read_text() == mig_before   # المؤقت لا يسجّل المجلد
            ft._KEY_CACHE.clear()
            fb = FileManager(ws, key_dir=fresh(kd))                  # الأصلي يعود: الأصلي موثوق، الجديد (بمفتاح مؤقت) غير موثَّق
            assert [bool(e.get("unverified")) for e in fb.journal] == [False, False, True]
        finally:
            tempfile.tempdir = old_tmp

    # ---------- 2. عبر المنسّق: الدفتر يعرّف المهام؛ المجهول يحتاج تأكيداً ----------
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("a.txt", "b.txt", "Report.docx"))
        led = SQLiteLedger(db_path=base / "l.db", initial_balance=100, spending_limit=100)
        with mock.patch.dict(os.environ, {"COS_DATA_DIR": str(base / "data1")}):
            ft._KEY_CACHE.clear()
            r1 = TaskRunner(ws, FakeProvider(default_response="{}"), led, approver=lambda s: True)
            rep = r1.run("T-known", "x", plan=[PlanStep(1, "move", "a.txt", {"dest": "A/a.txt"})])
            assert rep.success
        # إعادة تثبيت: مفتاح جديد. مهمة الدفتر تُتراجع بلا تأكيد؛ ما يُزرع بمعرّف غير معروف أو بلا معرّف يحتاج تأكيداً
        planted = [{"action": "move", "src": "rel:Elsewhere.docx", "dest": "rel:Report.docx", "task": "task_unknown"},
                   {"action": "move", "src": "rel:Else2.docx", "dest": "rel:Report.docx"}]
        (ws / ".undo_journal.json").write_text(json.dumps(disk(ws) + planted))
        with mock.patch.dict(os.environ, {"COS_DATA_DIR": str(base / "data2")}):
            ft._KEY_CACHE.clear()
            r2 = TaskRunner(ws, FakeProvider(default_response="{}"), led, approver=lambda s: True)
            assert r2.file_manager.unverified_count(False) == 4 and r2.file_manager.unverified_count() == 2
            u0 = r2.undo_untagged()                                  # بلا معرّف مهمة: لا دفتر يعرفه
            assert int(u0) == 0 and u0.needs_confirmation and u0.unverified == 1 and (ws / "Report.docx").exists()
            u1 = r2.undo_task("task_unknown")                        # فوقه إدخال أحدث (بلا معرّف): الترتيب يمنع
            assert int(u1) == 0 and (ws / "Report.docx").exists() and not (ws / "Elsewhere.docx").exists()
            assert disk(ws)[-2:] == planted                           # باقية كما هي
            u2 = r2.undo_task("T-known")                             # معروفة في الدفتر لكن فوقها مهام أحدث غير معروفة
            assert int(u2) == 0 and not (ws / "a.txt").exists()
            # بعد تأكيد المستخدم الصريح
            u3 = r2.undo_untagged(allow_unverified=True)
            assert int(u3) == 1 and not u3.needs_confirmation and (ws / "Else2.docx").exists() and not (ws / "Report.docx").exists()
            u4 = r2.undo_task("task_unknown")
            assert int(u4) == 0 and u4.needs_confirmation and u4.unverified == 1
            u5 = r2.undo_task("task_unknown", allow_unverified=True)
            assert not u5.needs_confirmation and u5.unverified == 0
            u6 = r2.undo_task("T-known")                             # معروفة في الدفتر: بلا تأكيد
            assert int(u6) == 2 and not u6.needs_confirmation and (ws / "a.txt").exists()
    # t12: سجل مزروع في مجلد لم يُر من قبل لا ينقل ملفات المستخدم
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("Report.docx", "decoy"))
        (ws / "Important").mkdir()
        (ws / "Important" / "taxes.xlsx").write_text("data")
        (ws / ".undo_journal.json").write_text(json.dumps([
            {"action": "copy", "src": "rel:decoy", "dest": "rel:Important"},
            {"action": "move", "src": "rel:Elsewhere.docx", "dest": "rel:Report.docx"}]))
        fm = FileManager(ws, key_dir=fresh(base / "k"))
        r = fm.undo_all()
        assert int(r) == 0 and r.unverified == 2 and (ws / "Report.docx").exists() and (ws / "Important" / "taxes.xlsx").exists()
        assert not any((ws / ".trash").iterdir())

    # ---------- 3. توقيع مربوط بمجلد العمل وبرقم الإدخال؛ فحص الحالة دائماً ----------
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        K = fresh(base / "k")
        wsA, wsB = mk(base, "A", files=("a.txt",)), mk(base, "B", files=())
        fa = FileManager(wsA, key_dir=K)
        fa.move("a.txt", "a2.txt")
        (wsB / "a2.txt").write_text("B's precious file")
        (wsB / ".undo_journal.json").write_text((wsA / ".undo_journal.json").read_text())      # نسخ سجل موقّع إلى مجلد آخر
        fb = FileManager(wsB, key_dir=K)
        assert fb.journal and all(e.get("unverified") for e in fb.journal)
        assert int(fb.undo_all()) == 0 and (wsB / "a2.txt").read_text() == "B's precious file"
        assert fa._ws_id != fb._ws_id
        # نقل/إعادة تسمية مجلد العمل نفسه: المعرّف يبقى (ملف .undo_journal.wsid) فيبقى السجل موثوقاً
        wsA2 = wsA.with_name("A_renamed")
        wsA.rename(wsA2)
        fa2 = FileManager(wsA2, key_dir=K)
        assert fa2._ws_id == fa._ws_id and not any(e.get("unverified") for e in fa2.journal) and int(fa2.undo_all()) == 1
        # رقم الإدخال: إدخالان متطابقان المحتوى بتوقيعين مختلفين
        wsC = mk(base, "C", files=("x.txt",))
        fc = FileManager(wsC, key_dir=K)
        fc.move("x.txt", "y.txt")
        fc.move("y.txt", "x.txt")
        dc = disk(wsC)
        assert len(dc) == 2 and dc[0]["n"] != dc[1]["n"] and dc[0]["mac"] != dc[1]["mac"]
        # إعادة سجل قديم بعد أن أنشأ المستخدم ملفاً جديداً بالاسم نفسه: لا يُنقل ملفه
        wsD = mk(base, "D", files=("a.txt",))
        fd = FileManager(wsD, key_dir=K)
        fd.move("a.txt", "a2.txt")
        saved = (wsD / ".undo_journal.json").read_text()
        fd.undo_all()
        (wsD / "a2.txt").write_text("NEW unrelated file the user created later")
        (wsD / ".undo_journal.json").write_text(saved)
        fd2 = FileManager(wsD, key_dir=K)
        r = fd2.undo_all()
        assert (wsD / "a2.txt").read_text() == "NEW unrelated file the user created later" and (wsD / "a.txt").exists() and r.skipped >= 1
        # وسلوك الاستعادة باسم بديل لعنصر آخر يشغل الاسم الأصلي يبقى للإدخال الأصلي نفسه (البصمة تطابق)
        wsE = mk(base, "E", files=("f.txt",))
        fe = FileManager(wsE, key_dir=K)
        fe.move("f.txt", "g.txt")
        (wsE / "f.txt").write_text("user made another f")
        r = fe.undo_all()
        assert int(r) == 1 and (wsE / "f.txt").read_text() == "user made another f" and (wsE / "f (1).txt").read_text() == "f.txt", sorted(p.name for p in wsE.iterdir())

    # ---------- 4. توقيع بمحارف غير ASCII أو من نوع آخر: إدخال واحد فقط يتأثر ----------
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        K = fresh(base / "k")
        ws = mk(base, files=("a.txt",))
        fm = FileManager(ws, key_dir=K)
        fm.move("a.txt", "a2.txt")
        fm.move("a2.txt", "a3.txt")
        d = disk(ws)
        for bad_mac in ("éé", 12345, ["x"], None, {"a": 1}, "\ud800x".encode("utf-8", "surrogatepass").decode("utf-8", "replace")):
            (ws / ".undo_journal.json").write_text(json.dumps(d + [{"action": "create_dir", "path": "rel:zz", "mac": bad_mac}]))
            f2 = FileManager(ws, key_dir=K)
            assert len(f2.journal) == len(d) + 1 and [bool(e.get("unverified")) for e in f2.journal] == [False] * len(d) + [True], bad_mac
            assert not (ws / "zz").exists() and not f2.journal[0].get("unverified")

    # ---------- 5. ملفات جانبية روابط: لا كتابة خارج مجلد العمل ----------
    if can_link:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp).resolve()
            K = fresh(base / "k")
            ws = mk(base, files=("a.txt",))
            victim = base / "victim.txt"
            victim.write_text("ORIGINAL\n")
            for name in (".undo_journal.rejected.jsonl", ".undo_journal.skipped.jsonl", ".undo_journal.intent.rejected.json"):
                os.symlink(str(victim), str(ws / name))
            (ws / ".undo_journal.json").write_text(json.dumps([{"action": "move", "src": "rel:../x", "dest": "rel:../y"}]))
            fm = FileManager(ws, key_dir=K)
            assert victim.read_text() == "ORIGINAL\n" and not (ws / ".undo_journal.rejected.jsonl").is_symlink()
            assert sum("رابطاً" in n for n in fm.undo_notes) == 3 and len(list(ws.glob("*.was_a_link"))) == 3
            assert (ws / ".undo_journal.rejected.jsonl").exists()      # حُجر الإدخال الفاسد في ملف عادي جديد
            # رابط يُزرع بعد البدء: الإلحاق عبر _safe_append يرفض، والمسارات تنقله جانباً
            (ws / ".undo_journal.rejected.jsonl").unlink()
            os.symlink(str(victim), str(ws / ".undo_journal.rejected.jsonl"))
            try:
                ft._safe_append(ws / ".undo_journal.rejected.jsonl", "PWNED\n")
                raise AssertionError("يجب رفض الإلحاق عبر رابط")
            except OSError:
                pass
            assert victim.read_text() == "ORIGINAL\n"
            fm._quarantine([{"action": "x"}], "t")
            assert victim.read_text() == "ORIGINAL\n" and (ws / ".undo_journal.rejected.jsonl").read_text().count("\n") == 1
            os.symlink(str(victim), str(ws / ".undo_journal.skipped.jsonl.2"))
            os.unlink(str(ws / ".undo_journal.skipped.jsonl")) if (ws / ".undo_journal.skipped.jsonl").exists() else None
            os.symlink(str(victim), str(ws / ".undo_journal.skipped.jsonl"))
            fm.journal.append({"action": "bogus", "task": None})
            assert fm.undo_step() == "skipped" and victim.read_text() == "ORIGINAL\n"
    # الإلحاق العادي يعمل
    with tempfile.TemporaryDirectory() as tmp:
        p = pathlib.Path(tmp) / "side.jsonl"
        ft._safe_append(p, "a\n")
        ft._safe_append(p, "b\n")
        assert p.read_text() == "a\nb\n"

    # ---------- 6. سباق إنشاء المفتاح: مفتاح واحد ----------
    child = ("import sys, time, os, hashlib\nsys.path.insert(0, %r)\n" % str(ROOT) +
             "import assistant.tools.files as F\nkd, go, nolink = sys.argv[1], float(sys.argv[2]), sys.argv[3] == '1'\n"
             "if nolink:\n    def bad(*a, **k): raise PermissionError('no hard links')\n    os.link = bad\n"
             "while time.time() < go: pass\nk, w, d = F.load_journal_key(kd)\nprint(hashlib.sha1(k).hexdigest()[:10] if k else 'NONE')\n")
    for nolink in ("0", "1"):
        for trial in range(4):
            with tempfile.TemporaryDirectory() as tmp:
                kd = str(pathlib.Path(tmp) / "k")
                go = time.time() + 0.6
                ps = [subprocess.Popen([sys.executable, "-I", "-c", child, kd, str(go), nolink], stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace") for _ in range(6)]
                outs = {p.communicate()[0].strip() for p in ps}
                final = hashlib.sha1(open(os.path.join(kd, KEY_NAME), "rb").read()[:32]).hexdigest()[:10]
                assert outs == {final}, (nolink, trial, outs, final)
    # مفتاح تالف قديم (ناقص): يُستبدل مرة واحدة ويُحفظ القديم .bad؛ وإنشاء بلا os.link/os.replace
    with tempfile.TemporaryDirectory() as tmp:
        kd = pathlib.Path(tmp) / "k"
        kd.mkdir()
        (kd / KEY_NAME).write_bytes(b"short")
        old = time.time() - 3600
        os.utime(kd / KEY_NAME, (old, old))
        ft._KEY_CACHE.clear()
        with mock.patch.object(ft.os, "link", side_effect=PermissionError), mock.patch.object(ft.os, "replace", wraps=ft.os.replace) as rp:
            k, w, d = ft.load_journal_key(kd)
        assert k is not None and len(k) == 32 and (kd / KEY_NAME).read_bytes() == k and (kd / (KEY_NAME + ".bad")).read_bytes() == b"short"
        assert not (kd / (KEY_NAME + ".fix")).exists()
        # ملف ناقص حديث (قيد الكتابة): ننتظر ولا نستبدل
        kd2 = pathlib.Path(tmp) / "k2"
        kd2.mkdir()
        (kd2 / KEY_NAME).write_bytes(b"")
        import threading
        threading.Timer(0.4, lambda: (kd2 / KEY_NAME).write_bytes(b"K" * 32)).start()
        ft._KEY_CACHE.clear()
        k2, w2, d2 = ft.load_journal_key(kd2)
        assert k2 == b"K" * 32 and not (kd2 / (KEY_NAME + ".bad")).exists()

    print("PASS: test_b37_core completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b37_core", file=sys.stderr)
    sys.exit(1)
