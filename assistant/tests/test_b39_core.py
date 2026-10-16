"""اختبارات B39 (نواة): حراسة الخطوات بعد التخطيط (استبدال ملف بمجلد، تعديل الضحية/النسخة المُبقاة في المكررات)، params.guard، تبنّي عملية
اكتملت ثم فشلت قبل حفظ السجل، سقف 400 خطوة للخطط القابلة للتراجع، وسمات قائمة المجلد (الإجمالي/الاقتطاع)."""
import errno
import json
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
    import assistant.tools.files as ft
    from assistant.tools.files import FileManager
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.planner import PlanStep, Planner, PlanParseError, validate_step, MAX_PLAN_STEPS, MAX_DANGEROUS_PLAN_STEPS
    from assistant.models.fake import FakeProvider

    def mk(tmp, files):
        ws = pathlib.Path(tmp).resolve() / "ws"
        ws.mkdir(parents=True)
        for n, content in files.items():
            (ws / n).parent.mkdir(parents=True, exist_ok=True)
            (ws / n).write_text(content, encoding="utf-8")
        return ws

    def runner(tmp, ws, approver=lambda s: True, **kw):
        return TaskRunner(ws, FakeProvider(default_response="{}"), SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db", initial_balance=1000, spending_limit=1000),
                          approver=approver, **kw)

    def live(ws):
        return sorted(str(p.relative_to(ws)).replace(os.sep, "/") for p in ws.rglob("*")
                      if p.is_file() and ".trash" not in p.parts and not p.name.startswith(".undo_journal"))

    def bump(p):   # تعديل يغيّر mtime حتى على أنظمة بدقة زمنية خشنة
        st = os.stat(str(p))
        os.utime(str(p), ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    STALE = "تغيّر"

    # ---------- 1. المكررات: تعديل الضحية بعد التخطيط ----------
    def dup_plan(r, objective, move=False):
        steps = [PlanStep(1, "create_dir", "مكررات")] if move else []
        steps.append(PlanStep(len(steps) + 1, "move" if move else "delete", "b.txt", {"dest": "مكررات/b.txt"} if move else {}))
        r._stamp_plan(steps, objective)
        return steps

    for move in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            ws = mk(tmp, {"a.txt": "same", "b.txt": "same"})
            r = runner(tmp, ws)
            plan = dup_plan(r, "انقل المكررات" if move else "احذف المكررات", move)
            victim = plan[-1]
            assert victim.sig and victim.sig["keep"] == "a.txt" and victim.sig["h"] and victim.sig["kh"] == victim.sig["h"], victim.sig
            (ws / "b.txt").write_text("NEW unique user work", encoding="utf-8")
            rep = r.run("T1", "x", plan=plan)
            assert (ws / "b.txt").read_text(encoding="utf-8") == "NEW unique user work" and (ws / "a.txt").exists()
            assert not (ws / "مكررات" / "b.txt").exists()
            assert any("b.txt" in s and STALE in s for s in rep.skipped), rep.skipped
            if move:
                assert rep.success                       # create_dir نُفّذت والنقل تُخطّي
            else:
                assert not rep.success and any(STALE in f for f in rep.failures), rep.failures   # كل الخطوات تُخطّيت

    # نفس الحجم والـ mtime لكن محتوى مختلف: تكشفه بصمة المحتوى (حراسة params.guard/hash)
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"a.txt": "same", "b.txt": "same"})
        r = runner(tmp, ws)
        plan = dup_plan(r, "احذف المكررات")
        st = os.stat(str(ws / "b.txt"))
        (ws / "b.txt").write_text("SAME", encoding="utf-8")        # نفس الطول
        os.utime(str(ws / "b.txt"), ns=(st.st_atime_ns, st.st_mtime_ns))
        rep = r.run("T1", "x", plan=plan)
        assert (ws / "b.txt").read_text(encoding="utf-8") == "SAME" and any(STALE in s for s in rep.skipped), rep.skipped

    # النسخة المُبقاة حُذفت أو عُدّلت: كل الضحايا تُتخطّى (لا يُحذف آخر ما تبقى)
    for how in ("deleted", "edited"):
        with tempfile.TemporaryDirectory() as tmp:
            ws = mk(tmp, {"a.txt": "same", "b.txt": "same", "c.txt": "same"})
            r = runner(tmp, ws)
            plan = [PlanStep(1, "delete", "b.txt"), PlanStep(2, "delete", "c.txt")]
            r._stamp_plan(plan, "احذف المكررات")
            assert all(s.sig["keep"] == "a.txt" for s in plan)
            if how == "deleted":
                (ws / "a.txt").unlink()
            else:
                (ws / "a.txt").write_text("different now", encoding="utf-8")
            rep = r.run("T1", "x", plan=plan)
            assert not rep.success and len(rep.skipped) == 2 and live(ws) == (["b.txt", "c.txt"] if how == "deleted" else ["a.txt", "b.txt", "c.txt"]), (how, rep.skipped, live(ws))
            assert any("المُبقاة" in s for s in rep.skipped)

    # لا نسخة مطابقة أصلاً: لا keep (الضحية وحدها تُحرس)؛ وطلب حذف عادي لا يحسب keep إطلاقاً
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"a.txt": "same", "b.txt": "same", "u.txt": "unique"})
        r = runner(tmp, ws)
        p1 = [PlanStep(1, "delete", "u.txt")]
        r._stamp_plan(p1, "احذف المكررات")
        assert "keep" not in p1[0].sig and "h" in p1[0].sig
        p2 = [PlanStep(1, "delete", "b.txt")]
        r._stamp_plan(p2, "احذف b.txt")
        assert p2[0].sig and "keep" not in p2[0].sig and "h" not in p2[0].sig

    # خطوة سليمة وأخرى متغيّرة: الأولى تُنفَّذ والخطة ناجحة
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"a.txt": "same", "b.txt": "same", "c.txt": "same"})
        r = runner(tmp, ws)
        plan = [PlanStep(1, "delete", "b.txt"), PlanStep(2, "delete", "c.txt")]
        r._stamp_plan(plan, "احذف المكررات")
        (ws / "c.txt").write_text("user edit", encoding="utf-8")
        asked = []
        r.approver = lambda s: asked.append(s.target) or True
        rep = r.run("T1", "x", plan=plan)
        assert rep.success and live(ws) == ["a.txt", "c.txt"] and asked == ["b.txt"], (rep.failures, live(ws), asked)   # لا موافقة على المتغيّرة
        assert len(rep.skipped) == 1 and rep.undo_available
        assert r.undo_task("T1") == 1 and live(ws) == ["a.txt", "b.txt", "c.txt"]

    # ---------- 2. استبدال ملف بمجلد (أو العكس) بعد التخطيط ----------
    def to_dir(ws, name):
        (ws / name).unlink()
        (ws / name).mkdir()
        (ws / name / "user_new.txt").write_text("user work", encoding="utf-8")

    for action in ("delete", "move", "copy", "rename"):
        with tempfile.TemporaryDirectory() as tmp:
            ws = mk(tmp, {"a.txt": "x", "b.txt": "y"})
            r = runner(tmp, ws)
            params = {"delete": {}, "move": {"dest": "Arch/a.txt"}, "copy": {"dest": "a_copy.txt"}, "rename": {"new_name": "z.txt"}}[action]
            plan = [PlanStep(1, action, "a.txt", params)]
            r._stamp_plan(plan, "x")
            assert plan[0].sig["k"] == "file"
            to_dir(ws, "a.txt")
            rep = r.run("T1", "x", plan=plan)
            assert not rep.success and any(STALE in s for s in rep.skipped), (action, rep.skipped, rep.failures)
            assert live(ws) == ["a.txt/user_new.txt", "b.txt"] and not (ws / "Arch").exists(), (action, live(ws))
    with tempfile.TemporaryDirectory() as tmp:     # مجلد صار ملفاً
        ws = mk(tmp, {"d/x.txt": "x"})
        r = runner(tmp, ws)
        plan = [PlanStep(1, "delete", "d")]
        r._stamp_plan(plan, "x")
        assert plan[0].sig == {"k": "dir"}
        import shutil
        shutil.rmtree(str(ws / "d"))
        (ws / "d").write_text("now a file", encoding="utf-8")
        rep = r.run("T1", "x", plan=plan)
        assert not rep.success and (ws / "d").read_text(encoding="utf-8") == "now a file"
    # مجلد تغيّر محتواه فقط (خطوات سابقة/المستخدم أضافوا ملفاً): ليس تغيّراً في النوع فيُنفَّذ
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"d/x.txt": "x"})
        r = runner(tmp, ws)
        plan = [PlanStep(1, "delete", "d")]
        r._stamp_plan(plan, "x")
        (ws / "d" / "y.txt").write_text("y", encoding="utf-8")
        assert r.run("T1", "x", plan=plan).success and not (ws / "d").exists()
    # ملف عُدّل بعد التخطيط: الحذف والنقل يُتخطّيان، والنسخ وإعادة التسمية تُنفَّذان (لا ضرر على المحتوى)
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"a.txt": "x", "b.txt": "y", "c.txt": "z", "d.txt": "w"})
        r = runner(tmp, ws)
        plan = [PlanStep(1, "delete", "a.txt"), PlanStep(2, "move", "b.txt", {"dest": "B/b.txt"}),
                PlanStep(3, "copy", "c.txt", {"dest": "c2.txt"}), PlanStep(4, "rename", "d.txt", {"new_name": "d2.txt"})]
        r._stamp_plan(plan, "x")
        for n in "abcd":
            bump(ws / f"{n}.txt")
        rep = r.run("T1", "x", plan=plan)
        assert rep.success and len(rep.skipped) == 2 and live(ws) == ["a.txt", "b.txt", "c.txt", "c2.txt", "d2.txt"], (rep.failures, rep.skipped, live(ws))
    # خطة بلا بصمات (نُشئت يدوياً) أو مسار لمسته خطوة سابقة: سلوك قديم
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"a.txt": "x"})
        r = runner(tmp, ws)
        plan = [PlanStep(1, "move", "a.txt", {"dest": "B/a.txt"}), PlanStep(2, "create_dir", "C"),
                PlanStep(3, "copy", "B/a.txt", {"dest": "C/a.txt"}), PlanStep(4, "rename", "C/a.txt", {"new_name": "k.txt"})]
        r._stamp_plan(plan, "x")
        assert plan[0].sig and plan[2].sig is None and plan[3].sig is None     # B/a.txt وC/a.txt ناتجان عن الخطة نفسها
        assert r.run("T1", "x", plan=plan).success and live(ws) == ["B/a.txt", "C/k.txt"]
        assert PlanStep(1, "inspect", "a.txt") == PlanStep(1, "inspect", "a.txt", sig={"k": "file"})   # البصمة خارج المقارنة
    # خطوة لاحقة تعتمد على وجهة خطوة تُخطّيت: تُتخطّى أيضاً بدل أن تكسر الخطة
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"a.txt": "x", "b.txt": "y"})
        r = runner(tmp, ws)
        plan = [PlanStep(1, "move", "a.txt", {"dest": "B/a.txt"}), PlanStep(2, "rename", "B/a.txt", {"new_name": "q.txt"}), PlanStep(3, "delete", "b.txt")]
        r._stamp_plan(plan, "x")
        to_dir(ws, "a.txt")
        rep = r.run("T1", "x", plan=plan)
        assert rep.success and len(rep.skipped) == 2 and live(ws) == ["a.txt/user_new.txt"], (rep.failures, rep.skipped, live(ws))
    # make_plan_with_note يبصم الخطة تلقائياً
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"a.txt": "x"})
        steps = json.dumps({"steps": [{"step_id": 1, "action_type": "delete", "target": "a.txt"}]})
        r = TaskRunner(ws, FakeProvider(default_response=steps), SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db"), approver=lambda s: True)
        plan, _n = r.make_plan_with_note("احذف a.txt")
        assert plan[0].sig and plan[0].sig["k"] == "file"

    # ---------- 3. params.guard من القواعد: يُتحقق منه في المخطط ويُحترم في المنفّذ ----------
    ok = validate_step({"step_id": 1, "action_type": "delete", "target": "b.txt",
                        "params": {"guard": {"size": 4, "mtime_ns": 1, "hash": "f:abc", "keep": "a.txt", "keep_size": 4, "keep_hash": "f:abc", "evil": "x"}, "junk": 1}}, 1)
    assert ok.params == {"guard": {"size": 4, "mtime_ns": 1, "hash": "f:abc", "keep": "a.txt", "keep_size": 4, "keep_hash": "f:abc"}}, ok.params
    for bad in ({"size": "4"}, {"size": -1}, {"size": True}, {"keep": "../x"}, {"keep": "/etc/passwd"}, {"hash": "a b"}, {"keep": ""}, {"hash": "x" * 200}, "str", [1]):
        try:
            validate_step({"step_id": 1, "action_type": "delete", "target": "b.txt", "params": {"guard": bad}}, 1)
            raise AssertionError(("guard غير صالح قُبل", bad))
        except PlanParseError:
            pass
    assert "guard" not in validate_step({"step_id": 1, "action_type": "inspect", "target": "b.txt", "params": {"guard": {"size": 1}}}, 1).params
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"a.txt": "same", "b.txt": "same"})
        r = runner(tmp, ws)
        size = (ws / "b.txt").stat().st_size
        g = {"size": size, "hash": ft.content_fingerprint(ws / "b.txt"), "keep": "a.txt", "keep_size": size, "keep_hash": ft.content_fingerprint(ws / "a.txt")}
        plan = [validate_step({"step_id": 1, "action_type": "delete", "target": "b.txt", "params": {"guard": g}}, 1)]
        (ws / "a.txt").unlink()
        rep = r.run("T1", "x", plan=plan)
        assert not rep.success and live(ws) == ["b.txt"] and any("المُبقاة" in s for s in rep.skipped), (rep.skipped, rep.failures)
        (ws / "a.txt").write_text("same", encoding="utf-8")
        assert r.run("T2", "x", plan=plan).success and live(ws) == ["a.txt"]

    # ---------- 4. عملية اكتملت ثم فشلت قبل حفظ السجل: تُتبنّى ولا تضيع ----------
    real_rename = os.rename

    def after_fault(*a, **k):
        real_rename(*a, **k)
        raise OSError(errno.EIO, "injected EIO after effect")

    def before_fault(*a, **k):
        raise OSError(errno.EIO, "injected EIO before effect")

    ops = {
        "move": (lambda fm: fm.move("a.txt", "B/a.txt"), {"a.txt": "a"}),
        "delete": (lambda fm: fm.delete("a.txt"), {"a.txt": "a"}),
        "rename": (lambda fm: fm.rename("a.txt", "z.txt"), {"a.txt": "a"}),
        "copy": (lambda fm: fm.copy("a.txt", "a_copy.txt"), {"a.txt": "a"}),
    }
    for name, (op, files) in ops.items():
        with tempfile.TemporaryDirectory() as tmp:
            ws = mk(tmp, files)
            fm = FileManager(ws)
            fm.current_task = "T1"
            before = live(ws)
            if name in ("move",):
                fm._ensure_dirs(ws / "B")        # المجلد يُنشأ قبل الفشل المحقون (create_dir تسجيل مستقل)
                fm._save_journal()
                n0 = len(fm.journal)
            else:
                n0 = 0
            with mock.patch("os.rename", after_fault):
                try:
                    op(fm)
                    raise AssertionError((name, "كان يجب أن يفشل"))
                except OSError:
                    pass
            assert not os.path.lexists(str(fm.intent_path)), (name, "النية بقيت")
            acts = [e["action"] for e in fm.journal[n0:]]
            assert acts and acts[-1] == name.replace("_case", ""), (name, acts)
            fm2 = FileManager(ws)                                  # ومن القرص أيضاً
            assert fm2.journal[-1]["action"] == name.replace("_case", ""), (name, fm2.journal)
            res = fm2.undo_all()
            assert int(res) >= 1 and live(ws) == before, (name, live(ws), before, res.to_dict())
        # فشل قبل الأثر: لا تبنّي ولا نية عالقة
        with tempfile.TemporaryDirectory() as tmp:
            ws = mk(tmp, files)
            fm = FileManager(ws)
            if name == "move":
                fm._ensure_dirs(ws / "B")
            n0 = len(fm.journal)
            with mock.patch("os.rename", before_fault):
                try:
                    op(fm)
                    raise AssertionError((name, "كان يجب أن يفشل"))
                except OSError:
                    pass
            assert not os.path.lexists(str(fm.intent_path)) and len(fm.journal) == n0 and live(ws) == sorted(files), (name, live(ws), fm.journal)

    # ---------- 4b. تغيير حالة الأحرف فقط (a.txt → A.txt) على نظام لا يميّز الحالة (محاكاة NTFS على أي منصة) ----------
    import contextlib
    import pathlib as _pl

    def _ci_find(p):
        p = str(p)
        parent, name = os.path.split(p)
        try:
            names = os.listdir(parent or ".")
        except OSError:
            return None
        if name in names:
            return p
        for n in names:
            if n.casefold() == name.casefold():
                return os.path.join(parent, n)
        return None

    _real = dict(lexists=os.path.lexists, exists=os.path.exists, samefile=os.path.samefile, rename=os.rename, normcase=os.path.normcase,
                 pexists=_pl.Path.exists, replace=os.replace, resolve=_pl.Path.resolve)

    def ci_rename(src, dst, *a, **k):
        fs = _ci_find(src) or str(src)
        fd = _ci_find(dst)
        if fd is not None and fd != fs:
            raise FileExistsError(errno.EEXIST, "exists", str(dst))     # ويندوز: لا كتابة فوق عنصر آخر
        return _real["replace"](fs, dst)    # replace لا rename: تعارض الوجهة فُحص أعلاه، وrename قد تكون مغلّفة بفحص lexists (المحاكاة) المتأثر بتصحيحنا

    def ci_resolve(self, *a, **k):
        """ويندوز: resolve() يوحّد حالة المكوّن الأخير مع الموجود فعلاً (a.txt حتى لو طُلب A.txt) — هذا ما كان يجعل وجهة النية = المصدر."""
        r = _real["resolve"](self, *a, **k)
        found = _ci_find(r)
        return _pl.Path(found) if found else r

    @contextlib.contextmanager
    def ci_fs(rename=ci_rename):
        with mock.patch("os.path.lexists", lambda p: _real["lexists"](p) or _ci_find(p) is not None), \
                mock.patch("os.path.exists", lambda p: _real["exists"](p) or (_ci_find(p) is not None and _real["exists"](_ci_find(p)))), \
                mock.patch.object(_pl.Path, "exists", lambda self, *a, **k: os.path.exists(str(self))), \
                mock.patch.object(_pl.Path, "resolve", ci_resolve), \
                mock.patch("os.path.samefile", lambda a, b: (_ci_find(a) is not None and _ci_find(a) == _ci_find(b)) or _real["samefile"](a, b)), \
                mock.patch("os.path.normcase", lambda p: _real["normcase"](p).casefold()), \
                mock.patch("os.rename", rename):
            yield

    def listed(ws):
        return sorted(n for n in os.listdir(str(ws)) if not n.startswith("."))

    def make_case_ws(tmp):
        ws = mk(tmp, {"a.txt": "a", "keep.txt": "k"})
        fm = FileManager(ws)
        fm.current_task = "T1"
        return ws, fm

    # (أ) اكتمال طبيعي: الاسم يتغير حرفياً، ويُسجَّل، والتراجع يعيد الاسم القديم (المقارنة بأسماء listdir لا exists)
    with tempfile.TemporaryDirectory() as tmp:
        ws, fm = make_case_ws(tmp)
        with ci_fs():
            assert os.path.exists(str(ws / "A.txt")) and os.path.exists(str(ws / "a.txt"))     # المحاكاة فعّالة: كلاهما «موجود»
            out = fm.rename("a.txt", "A.txt")
            assert listed(ws) == ["A.txt", "keep.txt"] and out.name == "A.txt", listed(ws)
            assert [e["action"] for e in fm.journal] == ["rename"] and not os.path.lexists(str(fm.intent_path))
            res = fm.undo_all()
            assert int(res) == 1 and listed(ws) == ["a.txt", "keep.txt"], (listed(ws), res.to_dict())
            assert int(fm.undo_all()) == 0

    # (ب) فشل قبل أي أثر (أول خطوة): لا تُتبنّى عملية لم تقع رغم أن exists('A.txt') صحيحة
    def fail_before(*a, **k):
        raise OSError(errno.EIO, "injected EIO before effect")
    with tempfile.TemporaryDirectory() as tmp:
        ws, fm = make_case_ws(tmp)
        with ci_fs(fail_before):
            try:
                fm.rename("a.txt", "A.txt")
                raise AssertionError("كان يجب أن يفشل")
            except OSError:
                pass
            assert listed(ws) == ["a.txt", "keep.txt"] and fm.journal == [] and not os.path.lexists(str(fm.intent_path)), (listed(ws), fm.journal)
            fm2 = FileManager(ws)
            assert fm2.journal == [] and listed(ws) == ["a.txt", "keep.txt"]

    # (ج) فشل بعد الأثر في الخطوة الأولى (الملف باسمه المؤقت): يُعاد لاسمه الأصلي ولا يُسجَّل شيء
    def after_first(*a, **k):
        ci_rename(*a, **k)
        raise OSError(errno.EIO, "injected EIO after effect")
    with tempfile.TemporaryDirectory() as tmp:
        ws, fm = make_case_ws(tmp)
        with ci_fs(after_first):
            try:
                fm.rename("a.txt", "A.txt")
                raise AssertionError("كان يجب أن يفشل")
            except OSError:
                pass
        with ci_fs():
            assert listed(ws) == ["a.txt", "keep.txt"] and fm.journal == [] and not os.path.lexists(str(fm.intent_path)), (listed(ws), fm.journal)

    # (د) فشل بعد الأثر في الخطوة الثانية (اكتمل الاسم الجديد): تُتبنّى العملية ويمكن التراجع عنها
    calls = [0]

    def after_second(*a, **k):
        calls[0] += 1
        ci_rename(*a, **k)
        if calls[0] == 2:
            raise OSError(errno.EIO, "injected EIO after effect")
    with tempfile.TemporaryDirectory() as tmp:
        ws, fm = make_case_ws(tmp)
        with ci_fs(after_second):
            try:
                fm.rename("a.txt", "A.txt")
                raise AssertionError("كان يجب أن يفشل")
            except OSError:
                pass
        with ci_fs():
            assert listed(ws) == ["A.txt", "keep.txt"], listed(ws)
            assert [e["action"] for e in fm.journal] == ["rename"] and not os.path.lexists(str(fm.intent_path)), fm.journal
            assert int(FileManager(ws).undo_all()) == 1 and listed(ws) == ["a.txt", "keep.txt"], listed(ws)

    # (هـ) تعافٍ عند البدء بعد انقطاع كامل: نية بلا أثر لا تُتبنّى، ونية مكتملة تُتبنّى (بالأسماء الحرفية)
    for done in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            ws, fm = make_case_ws(tmp)
            with ci_fs():
                tmpname = ws / ".cos_rename_tmp_a.txt"
                fm._intent_begin({"action": "rename", "src": str(ws / "a.txt"), "dest": str(ws / "A.txt"), "tmp": str(tmpname)})
                if done:
                    os.rename(str(ws / "a.txt"), str(ws / "A.txt"))
                fm2 = FileManager(ws)
                assert listed(fm2.allowed_root) == (["A.txt", "keep.txt"] if done else ["a.txt", "keep.txt"]), (done, listed(ws))
                assert [e["action"] for e in fm2.journal] == (["rename"] if done else []), (done, fm2.journal, fm2.undo_notes)
                assert not os.path.lexists(str(fm2.intent_path))

    # تعذّر حفظ السجل أثناء التبنّي: تبقى النية فيتبنّاها بدء التشغيل التالي (لا ضياع)
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"a.txt": "a"})
        fm = FileManager(ws)
        fm.current_task = "T9"
        real_save = FileManager._save_journal

        def save_fails(self_fm):
            raise OSError(errno.EIO, "save down")
        with mock.patch("os.rename", after_fault), mock.patch.object(FileManager, "_save_journal", save_fails):
            try:
                fm.delete("a.txt")
                raise AssertionError("كان يجب أن يفشل")
            except OSError:
                pass
        assert os.path.lexists(str(fm.intent_path)) and not (ws / "a.txt").exists()
        fm3 = FileManager(ws)
        assert [e["action"] for e in fm3.journal] == ["delete"] and fm3.journal[0]["task"] == "T9" and not os.path.lexists(str(fm3.intent_path))
        assert any("سُجِّلت" in n for n in fm3.undo_notes), fm3.undo_notes
        assert int(fm3.undo_all()) == 1 and live(ws) == ["a.txt"]

    # ---------- 5. سقف الخطط: 400 للقابل للتراجع و200 إن وُجدت خطوة خطرة ----------
    assert MAX_PLAN_STEPS == 400 and MAX_DANGEROUS_PLAN_STEPS == 200

    def plan_json(n, extra=None):
        steps = [{"step_id": i, "action_type": "move", "target": f"f{i}.txt", "params": {"dest": f"D/f{i}.txt"}} for i in range(1, n + 1)]
        if extra:
            steps.append(extra)
        return json.dumps({"steps": steps})

    assert len(Planner(FakeProvider(default_response=plan_json(400))).plan("x")) == 400
    for resp, why in ((plan_json(401), "401"), (plan_json(200, {"step_id": 201, "action_type": "delete", "target": "z.txt"}), "201+delete")):
        try:
            Planner(FakeProvider(default_response=resp)).plan("x")
            raise AssertionError(("يجب رفض", why))
        except PlanParseError:
            pass
    assert len(Planner(FakeProvider(default_response=plan_json(199, {"step_id": 200, "action_type": "delete", "target": "z.txt"}))).plan("x")) == 200
    with tempfile.TemporaryDirectory() as tmp:     # تنفيذ 300 نقلة في خطة واحدة (سقف المنسّق الافتراضي يتسع)
        files = {f"f{i}.txt": str(i) for i in range(1, 301)}
        ws = mk(tmp, files)
        plan = [PlanStep(i, "move", f"f{i}.txt", {"dest": f"D/f{i}.txt"}) for i in range(1, 301)]
        r = runner(tmp, ws)
        assert r.max_steps >= 400
        rep = r.run("T1", "x", plan=plan)
        assert rep.success and len(live(ws)) == 300 and all(p.startswith("D/") for p in live(ws)), rep.failures
        res = r.undo_task("T1")
        assert res == 301 or res == 300, res.to_dict()      # +1 إن أُنشئ المجلد D
        assert live(ws) == sorted(files)

    # ---------- 6. سمات قائمة المجلد: الإجمالي والاقتطاع ----------
    with tempfile.TemporaryDirectory() as tmp:
        files = {f"f{i:03d}.txt": "x" for i in range(100)}
        files.update({f"sub/g{i:02d}.txt": "y" for i in range(20)})
        ws = mk(tmp, files)
        r = runner(tmp, ws)
        txt = r.workspace_listing()
        assert r.last_listing_truncated and r.last_listing_total == 121 and r.last_listing_total_exact, (r.last_listing_total, r.last_listing_truncated)
        assert "مقتطعة" in txt and len(txt.splitlines()) <= 62
        txt = r.workspace_listing(max_entries=500)
        assert not r.last_listing_truncated and r.last_listing_total == 121 and r.last_listing_skipped == []
        (ws / ".trash").mkdir(exist_ok=True)
        (ws / ".trash" / "t.txt").write_text("t", encoding="utf-8")
        r.workspace_listing()
        assert r.last_listing_total == 121        # السلة والسجل لا يُعدّان

    # ---------- 7. نصوص عربية: الأحجام والأخطاء وسجلات التنفيذ والموافقة ----------
    from assistant.tools.files import human_size, explain_error
    assert [human_size(x) for x in (0, 1, 512, 1024, 90000, 1 << 20, 3 << 20, int(2.5 * (1 << 20)), 3 << 30)] == \
        ["0 بايت", "بايت واحد", "512 بايت", "كيلوبايت واحد", "87.9 كيلوبايت", "ميجابايت واحد", "3 ميجابايت", "2.5 ميجابايت", "3 جيجابايت"]
    import re as _re
    for e in (OSError(errno.ENOSPC, "x"), OSError(errno.EIO, "x"), OSError(errno.ENAMETOOLONG, "x"), PermissionError(errno.EACCES, "x")):
        why = explain_error(e)
        assert why and not _re.search(r"[A-Za-z]", why) and ("أعد" in why or "اختصر" in why or "أغلق" in why or "صلاحية" in why), why
    assert "مساحة" in explain_error(OSError(errno.ENOSPC, "x")) and "اختصر" in explain_error(OSError(errno.ENAMETOOLONG, "x"))
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk(tmp, {"a.txt": "x", "b.txt": "y"})
        seen = []
        r = runner(tmp, ws, approver=lambda s: seen.append(s) or True)
        rep = r.run("T1", "x", plan=[PlanStep(1, "create_dir", "Docs"), PlanStep(2, "move", "a.txt", {"dest": "Docs/a.txt"}),
                                     PlanStep(3, "copy", "b.txt", {"dest": "Docs/b2.txt"}), PlanStep(4, "delete", "b.txt")])
        assert rep.success, rep.failures
        assert rep.approvals and "→" not in rep.approvals[0] and "delete" not in rep.approvals[0] and "نقل للسلة: b.txt" in rep.approvals[0], rep.approvals
        root = str(ws)
        assert all(root not in x for x in rep.executed_steps), rep.executed_steps
        assert "تم إنشاء المجلد: Docs" in rep.executed_steps and "تم نقل a.txt إلى Docs/a.txt" in rep.executed_steps and "تم نسخ b.txt إلى Docs/b2.txt" in rep.executed_steps, rep.executed_steps

    print("PASS: test_b39_core completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b39_core", file=sys.stderr)
    sys.exit(1)
