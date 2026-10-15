"""اختبارات B36 (نواة): تحصين ملفات النية والسجل المزروعة (مسارات + توقيع HMAC بمفتاح خارج مجلد العمل)، قفل بطابع مستقبلي،
إعادة إنشاء .trash، هوية النسخة عند التراجع، متابعة التراجع بعد فشل خطوة مستقلة، وسقف الخطوات 200."""
import json
import os
import pathlib
import shutil
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
    from assistant.tools.files import FileManager, UndoResult, INTENT_NAME, LOCK_NAME, KEY_NAME, MIGRATED_NAME
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.planner import PlanStep, MAX_PLAN_STEPS, PlanParseError
    from assistant.models.fake import FakeProvider
    from assistant.models import rules as R

    def mk(tmp, name="ws", files=("a.txt", "b.txt", "c.txt")):
        ws = pathlib.Path(tmp).resolve() / name
        ws.mkdir(parents=True)
        for n in files:
            (ws / n).write_text(n, encoding="utf-8")
        return ws

    def rejected_intents(ws):
        return list(ws.glob(".undo_journal.intent.rejected*.json"))

    # ---------- 1. ملف نية مزروع: كل مسار يمرّ بالتحقق ولا يُنفَّذ شيء ----------
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        home = base / "home"
        ws = mk(home, "COS_Workspace", files=("evil.bat",))
        startup = home / "AppData" / "Startup"
        startup.mkdir(parents=True)
        (home / "secret.txt").write_text("secret")
        K = base / "keys"
        fm = FileManager(ws, key_dir=K)
        victim_abs = str(home / "secret.txt")
        cases = [
            {"action": "move", "src": "rel:../AppData/Startup/evil.bat", "dest": "rel:whatever", "tmp": "rel:evil.bat"},
            {"action": "rename", "src": "rel:evil.bat", "dest": "rel:E.bat", "tmp": "rel:../secret.txt"},       # مؤقت خارج المجلد
            {"action": "rename", "src": "rel:evil.bat", "dest": "rel:E.bat", "tmp": "rel:evil2.bat"},          # ليس شكل المؤقت
            {"action": "copy", "src": "rel:evil.bat", "dest": "rel:c.bat", "tmp": "rel:.cos_copy_tmp_zz"},     # ليس 8 hex
            {"action": "move", "src": victim_abs, "dest": str(home / "moved.txt")},                            # مطلقة خارجية
            {"action": "move", "src": "rel:/etc/passwd", "dest": "rel:x"},                                     # rel: مطلق
            {"action": "delete", "original_path": "rel:evil.bat", "trash_path": "rel:evil.bat"},               # الوجهة ليست في السلة
            {"action": "delete", "original_path": "rel:evil.bat", "trash_path": "rel:.trash/../../x"},
            {"action": "create_dir", "paths": ["rel:../AppData/Startup/x"]},
            {"action": "bogus"},
        ]

        def untouched():
            return ((ws / "evil.bat").exists() and not (startup / "evil.bat").exists() and (home / "secret.txt").read_text() == "secret"
                    and not (home / "moved.txt").exists() and not (startup / "x").exists())
        for c in cases:
            (ws / INTENT_NAME).write_text(json.dumps(fm.sign_disk_entry(c)))     # حتى الموقّع الصحيح لا يتجاوز فحص المسارات
            f2 = FileManager(ws, key_dir=K)
            assert not (ws / INTENT_NAME).exists() and rejected_intents(ws) and any("غير موثوق" in n for n in f2.undo_notes), (c, f2.undo_notes)
            assert untouched(), c
            for p in rejected_intents(ws):
                p.unlink()
        # غير موقّع (ولو بمسارات سليمة): مرفوض
        good = {"action": "move", "src": "rel:evil.bat", "dest": "rel:z/evil.bat"}
        (ws / "z").mkdir()
        (ws / "z" / "evil.bat").write_text("moved")
        (ws / INTENT_NAME).write_text(json.dumps(good))
        f3 = FileManager(ws, key_dir=K)
        assert rejected_intents(ws) and not [e for e in f3.journal if e["action"] == "move"]
        for p in rejected_intents(ws):
            p.unlink()
        # التطبيق يعمل: الملف يُزرع بعد البدء ويُفحص عند أول عملية (refresh_journal)
        f4 = FileManager(ws, key_dir=K)
        (ws / INTENT_NAME).write_text(json.dumps(f4.sign_disk_entry(cases[0])))
        f4.refresh_journal()
        assert untouched() and rejected_intents(ws) and not (ws / INTENT_NAME).exists()
        # الشرعي يعمل (نية موقّعة بمسارات سليمة وبمؤقت بالشكل الدقيق)
        (ws / "q.txt").write_text("q")
        (ws / ".cos_rename_tmp_q.txt").write_text("tmpq")
        (ws / "q.txt").unlink()
        legit = {"action": "rename", "src": "rel:q.txt", "dest": "rel:Q.txt", "tmp": "rel:.cos_rename_tmp_q.txt", "task": "t"}
        (ws / INTENT_NAME).write_text(json.dumps(f4.sign_disk_entry(legit)))
        f5 = FileManager(ws, key_dir=K)
        assert (ws / "q.txt").read_text() == "tmpq" and not (ws / ".cos_rename_tmp_q.txt").exists()

    # ---------- 2. سجل التراجع: توقيع HMAC بمفتاح خارج مجلد العمل ----------
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("a.txt", "b.txt"))
        (ws / "Important").mkdir()
        (ws / "Important" / "taxes.xlsx").write_text("data")
        K = base / "keys"
        fm = FileManager(ws, key_dir=K)
        fm.current_task = "t"
        fm.move("a.txt", "x/a.txt")
        assert (K / KEY_NAME).stat().st_size == 32 and not list(ws.rglob("*.key")) and K not in ws.parents
        on_disk = json.loads((ws / ".undo_journal.json").read_text())
        assert all(isinstance(e.get("mac"), str) and len(e["mac"]) == 64 for e in on_disk), on_disk
        assert ws.name and json.loads((K / MIGRATED_NAME).read_text())    # المجلد مُرقّى: لا غير موقّع بعد الآن
        # (أ) إدخال مزروع غير موقّع يحاول نقل مجلد حقيقي إلى السلة بـ«تراجع»
        planted = {"action": "move", "src": "rel:Important_old", "dest": "rel:Important", "task": "t"}
        (ws / ".undo_journal.json").write_text(json.dumps(on_disk + [planted]))
        f2 = FileManager(ws, key_dir=K)
        assert [e["action"] for e in f2.journal] == ["create_dir", "move", "move"] and f2.journal[-1].get("unverified")   # لا يُفقد ولا يُنفَّذ
        r = f2.undo_all()          # المزروع غير الموقّع ينتظر تأكيداً؛ الموقّع يُتراجع عنه
        assert int(r) == 2 and r.unverified == 1 and r.needs_confirmation and (ws / "Important" / "taxes.xlsx").exists() and (ws / "a.txt").exists() \
            and not (ws / "Important_old").exists() and len(f2.journal) == 1
        # (ب) إدخال موقّع ثم مُعدَّل (توقيع لا يطابق) وتوقيع مزوَّر
        f2.move("b.txt", "y/b.txt")
        sj = json.loads((ws / ".undo_journal.json").read_text())
        forged = [dict(sj[-1], dest="rel:Important"), dict(sj[-1], mac="0" * 64), dict(sj[-1], mac="é\u0600" * 20)]
        (ws / ".undo_journal.json").write_text(json.dumps(sj[:-1] + forged))
        f3 = FileManager(ws, key_dir=K)
        assert [bool(e.get("unverified")) for e in f3.journal][-3:] == [True, True, True], f3.journal    # توقيع غير ASCII لا يُسقط السجل كله
        r3 = f3.undo_all()
        assert r3.unverified >= 3 and (ws / "Important" / "taxes.xlsx").exists() and (ws / "y" / "b.txt").exists()      # لم يُمَس شيء بالمزوَّر
        kept = json.loads((ws / ".undo_journal.json").read_text())
        assert {e.get("mac") for e in kept[-3:]} >= {"0" * 64, "é\u0600" * 20}                                       # توقيعاتها الأصلية محفوظة كما هي
        # (ج) إدخالات مزروعة بمسارات خارجية تُحجر عند التحميل
        (ws / ".undo_journal.json").write_text(json.dumps([
            {"action": "move", "src": "rel:../../stolen.txt", "dest": "rel:../../outside.txt"},
            {"action": "move", "src": str(base / "stolen2.txt"), "dest": str(base / "outside.txt")},
            {"action": "delete", "original_path": "rel:lnk/x", "trash_path": "rel:.trash/../../outside.txt"}]))
        f4 = FileManager(ws, key_dir=K)
        assert f4.journal == [] and any("رُفض" in n for n in f4.undo_notes)

    # قديم قبل التوقيع / مزروع: غير موثَّق دائماً؛ يُنفَّذ فقط إن عُرفت مهمته في الدفتر (أو بتأكيد) وبفحص حالة القرص
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("b.txt",))
        (ws / "Important").mkdir()
        (ws / "Important" / "t.xlsx").write_text("d")
        legacy = [{"task": "L1", "action": "copy", "src": "rel:missing_src", "dest": "rel:Important"},          # لا يطابق الحالة
                  {"task": "L1", "action": "move", "src": "rel:a.txt", "dest": "rel:b.txt"}]                      # قديم سليم
        (ws / ".undo_journal.json").write_text(json.dumps(legacy))
        K = base / "keys2"
        fm = FileManager(ws, key_dir=K)
        assert len(fm.journal) == 2 and all(e.get("unverified") for e in fm.journal)
        assert json.loads((ws / ".undo_journal.json").read_text()) == legacy or all("mac" not in e for e in json.loads((ws / ".undo_journal.json").read_text()))
        r = fm.undo_all()                                   # لا دفتر ولا تأكيد: لا شيء يُنفَّذ
        assert int(r) == 0 and r.unverified == 2 and r.needs_confirmation and not (ws / "a.txt").exists() and "تأكيد" in r.reason, r.to_dict()
        fl = FileManager(ws, key_dir=K, known_task_ids={"L1"})   # مهمته في الدفتر: يُنفَّذ بفحص الحالة
        r = fl.undo_all()
        assert int(r) == 1 and r.skipped == 1 and (ws / "a.txt").exists() and (ws / "Important" / "t.xlsx").exists() and not r.needs_confirmation, r.to_dict()
        # مزروع من جديد: يبقى غير موثَّق (لا رفض ولا ترقية)
        (ws / ".undo_journal.json").write_text(json.dumps(legacy))
        assert len(FileManager(ws, key_dir=K).journal) == 2

    # مفتاح غير مقروء: نعمل بلا توقيع مع تحذير مرة واحدة؛ لا مجلد بيانات: رجوع إلى مجلد مؤقت؛ المجلد الافتراضي من COS_DATA_DIR
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("a.txt", "b.txt"))
        K = base / "k3"
        K.mkdir()
        (K / KEY_NAME).write_bytes(os.urandom(32))
        real_rb = pathlib.Path.read_bytes

        def rb(self):
            if self.name == KEY_NAME:
                raise PermissionError(13, "denied")
            return real_rb(self)
        ft._WARNED_ONCE.clear()
        ft._KEY_CACHE.clear()
        (ws / "c.txt").write_text("c")
        f0 = FileManager(ws, key_dir=K)        # جلسة بالمفتاح: إدخالات موقّعة
        f0.move("c.txt", "x0/c.txt")
        signed_before = json.loads((ws / ".undo_journal.json").read_text())
        ft._KEY_CACHE.clear()
        with mock.patch.object(pathlib.Path, "read_bytes", rb):
            fu = FileManager(ws, key_dir=K)
            fu2 = FileManager(ws, key_dir=K)
        assert fu._key is None and sum("مفتاح" in n for n in fu.undo_notes) == 1 and not any("مفتاح" in n for n in fu2.undo_notes)
        fu.move("a.txt", "x/a.txt")
        disk_now = json.loads((ws / ".undo_journal.json").read_text())
        assert disk_now[:len(signed_before)] == signed_before and all("mac" not in e for e in disk_now[len(signed_before):])   # الجلسة بلا مفتاح لا تجرّد توقيع الموجود
        assert (K / KEY_NAME).stat().st_size == 32            # لم يُكتب فوق المفتاح غير المقروء
        ru = fu.undo_all()                                    # ما أنشأته الجلسة يُتراجع عنه؛ الموقّع بمفتاح لا نملكه ينتظر
        assert ru == 2 and (ws / "a.txt").exists() and ru.unverified == 2 and (ws / "x0" / "c.txt").exists()
        ft._KEY_CACHE.clear()
        ft._WARNED_ONCE.clear()
        blocker = base / "afile"
        blocker.write_text("x")
        fb = FileManager(ws, key_dir=blocker / "keys")        # الأب ملف: لا يمكن إنشاء المجلد
        assert fb._key is not None and fb._key_dir.name == "cos_assistant_keys"
        with mock.patch.dict(os.environ, {"COS_DATA_DIR": str(base / "data")}):
            ft._KEY_CACHE.clear()
            fd = FileManager(ws)
        assert (base / "data" / KEY_NAME).exists() and fd._key is not None
        ft._KEY_CACHE.clear()

    # المفتاح يُكتب ثنائياً بالضبط (ويندوز: وضع النص يحوّل \n إلى \r\n فيفسد المفتاح العشوائي ويكسر التوقيع بعد إعادة التشغيل)
    with tempfile.TemporaryDirectory() as tmp:
        Kn = pathlib.Path(tmp) / "kn"
        ft._KEY_CACHE.clear()
        with mock.patch.object(ft.os, "urandom", lambda n: b"\n" * n):
            fkn = FileManager(mk(tmp, "wsn"), key_dir=Kn)
        assert (Kn / KEY_NAME).read_bytes() == b"\n" * 32 and fkn._key == b"\n" * 32
        ft._KEY_CACHE.clear()
        assert FileManager(mk(tmp, "wsn2"), key_dir=Kn)._key == b"\n" * 32
        ft._KEY_CACHE.clear()

    # ---------- 3. قفل بطابع زمني مستقبلي لا يحجب المجلد ----------
    for t_lit in ("4102444800.0", "NaN", "Infinity"):
        with tempfile.TemporaryDirectory() as tmp:
            ws = mk(tmp)
            (ws / LOCK_NAME).write_text('{"pid":1,"t":%s}' % t_lit)
            t0 = time.time()
            fm = FileManager(ws, key_dir=pathlib.Path(tmp) / "k")
            fm.create_dir("d")
            fm.move("a.txt", "d/a.txt")
            assert time.time() - t0 < 5 and not fm.undo_notes and not (ws / LOCK_NAME).exists(), t_lit

    # ---------- 4. .trash حُذف/استُبدل أثناء عمل التطبيق ----------
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("a.txt", "b.txt", "c.txt"))
        fm = FileManager(ws, key_dir=base / "k")
        shutil.rmtree(ws / ".trash")
        fm.delete("a.txt")
        assert (ws / ".trash" / "a.txt").exists() and any("أُعيد إنشاؤه" in n for n in fm.undo_notes)
        shutil.rmtree(ws / ".trash")
        (ws / ".trash").write_text("x")                        # استُبدل بملف عادي
        fm.delete("b.txt")
        assert (ws / ".trash" / "b.txt").exists() and (ws / ".trash.was_a_file").read_text() == "x" and any("ملفاً عادياً" in n for n in fm.undo_notes)
        r = fm.undo_all()
        assert (ws / "b.txt").exists() and r.missing == 1            # الأول ضاع مع السلة المحذوفة: «ناتج مفقود» لا خطأ
        # رابط/تقاطع يبقى مرفوضاً (لا نتبع روابط السلة)
    # رسالة الفشل: لا «ما نُفّذ يمكن التراجع عنه» حين لم ينفَّذ شيء
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("a.txt", "b.txt"))
        with mock.patch.dict(os.environ, {"COS_DATA_DIR": str(base / "data")}):
            led = SQLiteLedger(db_path=base / "l.db", initial_balance=100, spending_limit=100)
            r = TaskRunner(ws, FakeProvider(default_response="{}"), led, approver=lambda s: True)
            with mock.patch.object(FileManager, "move", side_effect=PermissionError(13, "denied")):
                rep0 = r.run("n0", "x", plan=[PlanStep(1, "move", "a.txt", {"dest": "B/a.txt"})])
                rep1 = r.run("n1", "x", plan=[PlanStep(1, "create_dir", "D"), PlanStep(2, "move", "a.txt", {"dest": "D/a.txt"})])
        assert not rep0.success and not any("ما نُفّذ يمكن التراجع عنه" in f for f in rep0.failures) and any("لم تُنفَّذ أي خطوة" in f for f in rep0.failures), rep0.failures
        assert not rep1.success and any("ما نُفّذ يمكن التراجع عنه" in f for f in rep1.failures), rep1.failures

    # ---------- 5. هوية النسخة عند التراجع، ومتابعة التراجع بعد فشل خطوة مستقلة ----------
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("a.txt", "d1.txt"))
        (ws / "dir").mkdir()
        (ws / "dir" / "f.txt").write_text("f")
        fm = FileManager(ws, key_dir=base / "k")
        fm.current_task = "t"
        fm.copy("a.txt", "a_copy.txt")
        fm.copy("dir", "dir_copy")
        e = [x for x in fm.journal if x["action"] == "copy"]
        assert isinstance(e[0]["sig"], list) and len(e[0]["sig"]) == 2 and len(e[1]["sig"]) == 3
        # نسخة المجلد عدّلها المستخدم: لا تُنقل للسلة ولا تُحذف
        (ws / "dir_copy" / "f.txt").write_text("user edits are valuable")
        r = fm.undo_all()
        assert (ws / "dir_copy" / "f.txt").read_text() == "user edits are valuable" and r.skipped == 1 and "عُدّلت" in (r.reason or ""), r.to_dict()
        assert not (ws / "a_copy.txt").exists() and (ws / ".trash" / "a_copy.txt").exists()        # السليمة تُنقل للسلة كالمعتاد
        assert fm.journal == [] and (ws / ".undo_journal.skipped.jsonl").exists()
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("a.txt", "b.txt", "c.txt", "d.txt"))
        fm = FileManager(ws, key_dir=base / "k")
        fm.current_task = "T"
        fm.move("a.txt", "x/a.txt")
        fm.move("b.txt", "x/b.txt")
        fm.move("c.txt", "x/c.txt")
        real_move = ft._safe_move

        def failing(src, dst, *a, **k):
            if os.path.basename(str(src)) == "c.txt":
                raise PermissionError(13, "locked")
            return real_move(src, dst, *a, **k)
        with mock.patch.object(ft, "_safe_move", failing), mock.patch.object(ft, "retry_io", lambda f, *a, **k: f(*a)):
            r = fm.undo_all()
        assert int(r) == 2 and (ws / "a.txt").exists() and (ws / "b.txt").exists() and (ws / "x" / "c.txt").exists(), r.to_dict()   # a وb تابعتا؛ create_dir x (أصل مسار الفاشلة) تُرك
        assert r.remaining == 2 and len(r.failures) == 2 and "c.txt" in r.failures[0], r.failures
        assert fm.journal[-1]["src"].endswith("c.txt")
        r2 = fm.undo_all()                                      # بعد زوال العطل يكتمل
        assert int(r2) == 2 and (ws / "c.txt").exists() and r2.complete
    # خطوة تعتمد على مسار خطوة فاشلة لا تُتراجع قبلها (سلامة الترتيب)
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("a.txt", "z.txt"))
        fm = FileManager(ws, key_dir=base / "k")
        fm.current_task = "T"
        fm.move("a.txt", "b.txt")        # E1
        fm.move("b.txt", "c.txt")        # E2 (يلمس b.txt أيضاً)
        fm.move("z.txt", "zz.txt")       # E3 مستقلة
        real_move = ft._safe_move

        def failing2(src, dst, *a, **k):
            if os.path.basename(str(src)) == "c.txt":
                raise PermissionError(13, "locked")
            return real_move(src, dst, *a, **k)
        with mock.patch.object(ft, "_safe_move", failing2), mock.patch.object(ft, "retry_io", lambda f, *a, **k: f(*a)):
            r = fm.undo_all()
        assert (ws / "z.txt").exists() and (ws / "c.txt").exists() and not (ws / "a.txt").exists(), r.to_dict()   # E3 تراجعت؛ E1 تركت؛ E2 فشلت
        assert r.remaining == 2 and len(r.failures) == 2 and any("لاحقة" in f for f in r.failures), r.failures
    # عبر المنسّق: undo_task يتابع المستقلّ ويعدّ المتبقي
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=("a.txt", "b.txt"))
        with mock.patch.dict(os.environ, {"COS_DATA_DIR": str(base / "data")}):
            led = SQLiteLedger(db_path=base / "l.db", initial_balance=100, spending_limit=100)
            rn = TaskRunner(ws, FakeProvider(default_response="{}"), led, approver=lambda s: True)
            rep = rn.run("T9", "x", plan=[PlanStep(1, "move", "a.txt", {"dest": "A/a.txt"}), PlanStep(2, "move", "b.txt", {"dest": "B/b.txt"})])
            assert rep.success
            real_move = ft._safe_move

            def failing3(src, dst, *a, **k):
                if os.path.basename(str(src)) == "b.txt":
                    raise PermissionError(13, "locked")
                return real_move(src, dst, *a, **k)
            with mock.patch.object(ft, "_safe_move", failing3), mock.patch.object(ft, "retry_io", lambda f, *a, **k: f(*a)):
                ur = rn.undo_task("T9")
        assert isinstance(ur, UndoResult) and (ws / "a.txt").exists() and ur.remaining == 2 and len(ur.failures) == 2 and not ur.complete, ur.to_dict()

    # ---------- 6. سقف 200 خطوة وأداء ترتيب 1500 ملف ----------
    assert MAX_PLAN_STEPS == 200 and R.MAX_AUTO_STEPS == 200 and R.MAX_AUTO_DELETE_STEPS == 48
    from assistant.planner import Planner
    pl = Planner(FakeProvider(default_response=json.dumps(
        {"steps": [{"step_id": i, "action_type": "inspect", "target": f"d{i}", "params": {}} for i in range(1, 201)]})))
    assert len(pl.plan("x")) == 200
    pl2 = Planner(FakeProvider(default_response=json.dumps(
        {"steps": [{"step_id": i, "action_type": "inspect", "target": f"d{i}", "params": {}} for i in range(1, 202)]})))
    try:
        pl2.plan("x")
        raise AssertionError("201 خطوة يجب أن تُرفض")
    except PlanParseError:
        pass
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = mk(base, files=())
        exts = ["pdf", "jpg", "docx", "zip", "mp3", "txt"]
        N = 1500
        for i in range(N):
            (ws / f"file_{i:04d}.{exts[i % 6]}").write_text("x")
        for i in range(60):
            (ws / f"tmp_{i}.tmp").write_text("x")
        with mock.patch.dict(os.environ, {"COS_DATA_DIR": str(base / "data")}):
            led = SQLiteLedger(db_path=base / "l.db", initial_balance=10000, spending_limit=10000)
            rn = TaskRunner(ws, R.RuleProvider(workspace=ws), led, approver=lambda s: True)
            # حذف المؤقتة بموافقة لكل خطوة: السقف يبقى 48 فتُطلب مجموعة أصغر
            t0 = time.time()
            try:
                plan_d, note_d = rn.make_plan_with_note("احذف الملفات المؤقتة")
                big_delete = len(plan_d) > 48
            except PlanParseError as exc:
                big_delete = False
            assert not big_delete
            for p in ws.glob("tmp_*.tmp"):
                p.unlink()
            t0 = time.time()
            runs = steps = 0
            while True:
                try:
                    plan, _n = rn.make_plan_with_note("رتب الملفات حسب النوع")
                except PlanParseError:
                    break
                assert len(plan) <= 200
                rep = rn.run(f"S{runs}", "رتب الملفات حسب النوع", plan=plan)
                assert rep.success, rep.failures
                runs += 1
                steps += len(plan)
                assert runs <= 12
            elapsed = time.time() - t0
            left = [p for p in ws.iterdir() if p.is_file() and not p.name.startswith(".")]
            print(f"sort {N} files: {runs} runs, {steps} steps, {elapsed:.1f}s")
            limit = 240 if ft.IS_WINDOWS else 30     # fsync وAV على ويندوز أبطأ بكثير؛ المهم هنا السلامة وعدد الطلبات
            assert not left and runs <= 9 and elapsed < limit, (len(left), runs, elapsed)
            ur = rn.undo_task(f"S{runs - 1}")
            assert ur.done > 0

    print("PASS: test_b36_core completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b36_core", file=sys.stderr)
    sys.exit(1)
