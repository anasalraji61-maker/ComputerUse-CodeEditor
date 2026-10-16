"""اختبارات B28: نتائج المراجعة المستقلة العاشرة لمزوّد القواعد والمدقّق: عند الشك تسأل القواعد ولا تخمّن خطة واسعة.
عمليتان بعلامتَي وجهة، مكررات مع نوع/نطاق/وجهة، بادئة مع كلمات غير مفهومة، التاريخ مقابل بادئة صريحة، المفرد و«ملف txt»،
مجلد باسم نوع، وجهات عامة/الجذر، تطبيع أسماء الوجهة والنقاط، قوائم مقتطعة، project داخل الاسم، محارف Cf، فحص مجلد غير موجود،
ثم المدقّق: winget بمطابقة المعرّف الكامل ومن دون الاعتماد على الإنجليزية، تنبيه إعادة تسمية الوجهة، وسجل التراجع لكل خطوة."""
import datetime
import json
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import ModelProvider
    from assistant.models.fake import FakeProvider
    from assistant.models.rules import RuleProvider, RuleFirstProvider, HELP_QUESTION, norm
    from assistant.models.base import CONTEXT_MARKER
    from assistant.planner import PlanStep
    from assistant.runner import TaskRunner
    from assistant.ui.controller import AssistantController
    from assistant.verifier import Verifier

    rp = RuleProvider(today=lambda: datetime.date(2026, 1, 2))

    def analyze(prompt, ctx):
        return rp.analyze(prompt, list(ctx))

    def steps_of(prompt, ctx):
        a = analyze(prompt, ctx)
        return [(s["action_type"], s["target"], s["params"].get("dest") or s["params"].get("new_name")) for s in (a.steps or [])]

    def asks(prompt, ctx, why=""):
        a = analyze(prompt, ctx)
        assert not a.steps and a.question, f"كان يجب السؤال: {prompt!r} {why} ← {steps_of(prompt, ctx)}"
        return a.question

    # ------------------------------------------------------------------ 1. عمليتان بعلامتَي وجهة / فعلان
    L1 = ["a.txt", "b.txt", "c.txt", "Backup/", "Docs/", "Docs/q.txt", "pic.jpg"]
    for p in ("انقل a.txt إلى Backup و b.txt إلى Docs", "انقل a.txt إلى Backup وانقل b.txt إلى Docs",
              "انقل a.txt إلى Backup، وانقل b.txt إلى Docs", "انسخ a.txt إلى Backup وانسخ b.txt إلى Docs",
              "انقل a.txt إلى Backup. انقل b.txt إلى Docs", "انقل a.txt وانقل b.txt إلى Docs",
              "انقل a.txt إلى Docs وانقل b.txt", "انقل a.txt إلى Docs وb.txt"):
        q = asks(p, L1)
        assert "أكثر من عملية" in q, (p, q)
    # الطلب الواحد السليم لا يتأثر: وجهة واحدة، «نسخة احتياطية» ليست فعلاً ثانياً، مصدران بوجهة واحدة
    assert steps_of("انسخ a.txt إلى Backup", L1) == [("copy", "a.txt", "Backup/a.txt")]
    assert steps_of("انسخ نسخة احتياطية من a.txt إلى Backup", L1) == [("copy", "a.txt", "Backup/a.txt")]
    assert [s[1] for s in steps_of("انقل a.txt و b.txt إلى Docs", L1)] == ["a.txt", "b.txt"]
    assert steps_of("انقل a.txt to Docs", L1) == [("move", "a.txt", "Docs/a.txt")]

    # ------------------------------------------------------------------ 2. المكررات مع نوع/نطاق/وجهة
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp).resolve()
        for f in ("a.txt", "b.txt", "photo.jpg", "photo2.jpg", "Docs/a.txt"):
            (d / f).parent.mkdir(exist_ok=True)
            (d / f).write_text("same", encoding="utf-8")
        (d / "Backup").mkdir()
        rpd = RuleProvider(workspace=d)
        LD = ["a.txt", "b.txt", "photo.jpg", "photo2.jpg", "Docs/", "Docs/a.txt", "Backup/"]
        for p in ("احذف الصور المكررة", "احذف المكررات من Docs", "احذف الملفات المكررة في Docs", "انقل المكررات إلى Backup",
                  "احذف المكررات في Backup", "احذف مكررات a.txt", "انقل المكررات إلى Archive", "انقل المكررات إلى «Backup»"):
            a = rpd.analyze(p, LD)
            assert not a.steps and a.question and "المكررات" in a.question, (p, a.question)
        # الصيغ البسيطة تعمل كما كانت
        assert rpd.analyze("احذف المكررات", LD).steps
        assert rpd.analyze("انقل المكررات", LD).steps
        assert rpd.analyze("احذف كل الملفات المكررة", LD).steps

    # ------------------------------------------------------------------ 3. بادئة بكلمات متبقية غير مفهومة ← سؤال
    L3 = ["a.txt", "b.txt", "report.pdf", "Docs/", "Docs/q.txt", "التاريخ/", "التاريخ/z.txt", "date/", "date/d.txt"]
    for p in ("أعد تسمية ملفات Docz بإضافة البادئة x_", "أضف بادئة x_ لـ missing.txt", "اضف بادئة x_ للملف الاول",
              "أضف بادئة x_ لملفات docx", "أضف بادئة x_ لأول 3 ملفات في Docs", "أضف بادئة x_ لملفات Docs وملفات الجذر"):
        asks(p, L3)
    # الصيغ السليمة تبقى
    assert [s[1] for s in steps_of("أضف بادئة x_", L3)] == ["a.txt", "b.txt", "report.pdf"]
    assert steps_of("أعد تسمية الملفات بإضافة البادئة x_ في Docs", L3) == [("rename", "Docs/q.txt", "x_q.txt")]
    assert steps_of("أعد تسمية ملفات Docs بإضافة التاريخ في البداية", ["Docs/", "Docs/q.txt"]) == [("rename", "Docs/q.txt", "2026-01-02_q.txt")]

    # ------------------------------------------------------------------ 4. «تاريخ/date» بادئة تاريخ فقط بلا بادئة صريحة
    assert steps_of("أضف بادئة «تاريخ-» لملفات Docs", L3) == [("rename", "Docs/q.txt", "تاريخ-q.txt")]
    assert steps_of("rename files in Docs with prefix date-", L3) == [("rename", "Docs/q.txt", "date-q.txt")]
    assert steps_of("أضف بادئة x_ لملفات التاريخ", L3) == [("rename", "التاريخ/z.txt", "x_z.txt")]
    assert steps_of("أعد تسمية ملفات date بالبادئة x_", L3) == [("rename", "date/d.txt", "x_d.txt")]
    assert steps_of("أضف تاريخ لملفات Docs", ["Docs/", "Docs/q.txt"]) == [("rename", "Docs/q.txt", "2026-01-02_q.txt")]
    assert steps_of("أضف تاريخ لملفات التاريخ", ["التاريخ/", "التاريخ/z.txt"]) == [("rename", "التاريخ/z.txt", "2026-01-02_z.txt")]
    asks("أعد تسمية ملفات التاريخ بإضافة", ["التاريخ/", "التاريخ/z.txt"], "اسم المجلد وحده ليس طلب تاريخ")

    # ------------------------------------------------------------------ 5. المفرد و«ملف» + امتداد مجرّد ← سؤال
    L5 = ["a.txt", "b.txt", "pic.jpg", "song.mp3", "setup.exe", "data.csv", "clip.mp4", "report.pdf", "Docs/"]
    for p in ("احذف الصورة", "احذف ملف الصوت", "احذف برنامج", "احذف جدول", "احذف الجدول", "احذف مستند", "انقل الصورة إلى Docs",
              "احذف ملف .txt", "احذف ملف txt", "انقل ملف pdf إلى Docs", "انسخ ملف txt إلى Docs", "احذف الملف pdf"):
        asks(p, L5)
    # الجمع/الفئة الصريحة تعمل، واسم الملف الكامل بعد «ملف» يعمل
    assert [s[1] for s in steps_of("احذف الصور", L5)] == ["pic.jpg"]
    assert [s[1] for s in steps_of("احذف ملفات txt", L5)] == ["a.txt", "b.txt"]
    assert [s[1] for s in steps_of("احذف ملف a.txt", L5)] == ["a.txt"]
    assert [s[1] for s in steps_of("احذف pdf", L5)] == ["report.pdf"]

    # ------------------------------------------------------------------ 6. مجلد باسم نوع/امتداد: الكلمة المجرّدة ← سؤال
    L6 = ["صور/", "x.png", "pdf/", "report.pdf", "a.txt", "Docs/"]
    for p in ("احذف صور", "انقل صور إلى Docs", "انسخ صور إلى Docs", "احذف pdf", "انسخ pdf إلى Docs", "احذف x.png و صور"):
        q = asks(p, L6)
        assert "مجلد" in q
    assert steps_of("احذف مجلد صور", L6) == [("delete", "صور", None)]
    assert steps_of("احذف «pdf»", L6) == [("delete", "pdf", None)]
    assert steps_of("احذف مجلد pdf", L6) == [("delete", "pdf", None)]

    # ------------------------------------------------------------------ 7. وجهات عامة / الجذر
    L7 = ["a.txt", "Docs/", "Docs/q.txt", "Docs/sub/", "Docs/sub/z.txt", "pic.jpg"]
    for p in ("انقل Docs/q.txt إلى الجذر", "انقل Docs/q.txt إلى المجلد الرئيسي", "انقل Docs/q.txt إلى مجلد العمل",
              "انقل Docs/q.txt إلى المجلد الحالي", "انقل Docs/q.txt الى root"):
        assert steps_of(p, L7) == [("move", "Docs/q.txt", "q.txt")], (p, steps_of(p, L7))   # بلا create_dir لمجلد وهمي
    assert steps_of("انقل Docs/sub إلى الجذر", L7) == [("move", "Docs/sub", "sub")]
    for p in ("انقل الصور إلى مجلد", "انقل Docs/q.txt إلى الخارج", "انقل a.txt إلى الأعلى", "انقل a.txt إلى المجلد الاول",
              "انقل q.txt من Docs إلى هنا", "انقل a.txt إلى هذا"):
        q = asks(p, L7)
    # عنصر في الجذر أصلاً: لا نقل (ملاحظة لا خطة) ولا إنشاء مجلد «الجذر»
    a = analyze("انقل a.txt إلى الجذر", L7)
    assert not a.steps and not any("create_dir" in str(s) for s in (a.steps or []))
    # الوجهة المنصَّصة حرفية: مقصودة بالاسم
    assert steps_of("انقل a.txt إلى «الجذر»", L7)[0] == ("create_dir", "الجذر", None)

    # ------------------------------------------------------------------ 8. توحيد أسماء الوجهة والنقاط
    L8 = ["a.txt", "مدرسة/", "أرشيف/", "2026/", "Docs/"]
    assert steps_of("انقل a.txt الى مدرسه", L8) == [("move", "a.txt", "مدرسة/a.txt")]          # ة/ه: المجلد الموجود لا مجلد شبيه جديد
    assert steps_of("انقل a.txt الى ارشيف", L8) == [("move", "a.txt", "أرشيف/a.txt")]          # أ/ا
    assert steps_of("انقل a.txt الى ٢٠٢٦", L8) == [("move", "a.txt", "2026/a.txt")]            # أرقام هندية
    assert steps_of("انقل a.txt الى docs", L8) == [("move", "a.txt", "Docs/a.txt")]
    assert norm("٢٠٢٦") == "2026"
    a = analyze("انقل a.txt إلى «مدرسة»", L8 + ["مدرسه"])  # ملف باسم يطابق بالتوحيد ← لا مجلد بالاسم نفسه
    assert not a.steps or all(s["action_type"] != "create_dir" for s in a.steps)
    # النقاط في أول/آخر الاسم لا تُحذف بصمت
    for p in ("انقل a.txt إلى «.hidden»", "انقل a.txt إلى «Reports.»", "أنشئ مجلد «.config»", "أنشئ مجلد «Reports.»",
              "غير اسم a.txt إلى «.env»", "أضف بادئة «v2.» لملفات Docs", "أضف بادئة «.old_» لملفات Docs"):
        asks(p, L8 + ["Docs/q.txt"])
    # لكن نقطة الجملة بعد اسم غير منصَّص ليست من الاسم
    assert steps_of("انقل a.txt إلى Docs.", L8) == [("move", "a.txt", "Docs/a.txt")]
    assert steps_of("أنشئ مجلد Reports.", L8) == [("create_dir", "Reports", None)]

    # ------------------------------------------------------------------ 9. القائمة المقتطعة
    L9 = ["Docs/", "Docs/zeta.txt", "a.txt", "b.pdf", "… (القائمة مقتطعة)"]
    for p in ("احذف zeta.txt", "انقل zeta.txt إلى Archive", "غير اسم zeta.txt إلى y.txt"):
        asks(p, L9, "اسم مجرّد غير موجود في الجذر والقائمة مقتطعة")
    assert steps_of("احذف Docs/zeta.txt", L9) == [("delete", "Docs/zeta.txt", None)]
    assert steps_of("احذف zeta.txt", ["Docs/", "Docs/zeta.txt", "a.txt"]) == [("delete", "Docs/zeta.txt", None)]   # قائمة كاملة: كما كان
    for p in ("رتب الملفات حسب النوع", "أضف بادئة x_", "أضف بادئة x_ لملفات Docs"):
        a = analyze(p, L9)
        assert a.steps and a.note and "مقتطعة" in a.note, (p, a.note)
        assert json.loads(rp.generate(p, "x\n" + CONTEXT_MARKER + "\n" + "\n".join(L9)))["note"] == a.note
    assert analyze("رتب الملفات حسب النوع", L9[:-1]).note is None

    # ------------------------------------------------------------------ 10. project داخل الاسم، Cf، فحص غير موجود
    assert [s[1] for s in steps_of("أنشئ مجلد باسم «مشروع التخرج»", [])] == ["مشروع التخرج"]
    assert [s[1] for s in steps_of("أنشئ مجلد project_files", [])] == ["project_files"]
    assert [s[1] for s in steps_of("أنشئ مجلد «my.project»", [])] == ["my.project"]
    assert [s[1] for s in steps_of("أنشئ مشروع Foo", [])] == ["Foo/src", "Foo/docs", "Foo/tests"]
    assert [s[1] for s in steps_of("أنشئ project Foo", [])] == ["Foo/src", "Foo/docs", "Foo/tests"]
    L10 = ["a.txt", "Docs/", "Docs/q.txt"]
    for p in ("انقل a.txt إلى Docs‏", "‏انقل a.txt إلى ‪Docs‬", "احذف a‏.txt", "احذف​ a.txt", "احذف ⁦a.txt⁩"):
        s = steps_of(p, L10)
        assert s and all("‏" not in str(x) and "‪" not in str(x) for x in s), (p, s)
    assert steps_of("أضف بادئة x_‏ لملفات Docs", L10) == [("rename", "Docs/q.txt", "x_q.txt")]
    assert steps_of("انقل a.txt إلى NewDir‏", L10)[0] == ("create_dir", "NewDir", None)
    for p in ("افحص Docz", "ماذا يوجد في Docz", "افحص a.txt وDocz", "اعرض Docz"):
        q = asks(p, L10)
        assert "docz" in q.lower(), q
    assert steps_of("افحص Docs", L10) == [("inspect", "Docs", None)]
    assert steps_of("افحص", L10) == [("inspect", ".", None)]
    assert steps_of("افحص المجلد الحالي", L10) == [("inspect", ".", None)]

    # المتحكم يحذف محارف Cf قبل أي فهم
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"; ws.mkdir()
        (ws / "a.txt").write_text("1", encoding="utf-8")
        led = SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500)
        c = AssistantController(RuleProvider(), ws, led)
        c.submit_request("‏انقل a.txt إلى Archive‏")
        ev = c.wait_for("plan")
        assert ev and [s["target"] for s in ev["steps"]] == ["Archive", "a.txt"], ev
        assert c._request == "انقل a.txt إلى Archive"

    # ------------------------------------------------------------------ تصميم: الأسئلة لا تُحال للنموذج، «لم أفهم» تُحال
    fb = FakeProvider(default_response=json.dumps({"steps": [{"step_id": 1, "action_type": "delete", "target": "a.txt", "params": {}}]}))
    rf = RuleFirstProvider(fb, rp)
    ctx = "x\n" + CONTEXT_MARKER + "\n" + "\n".join(L1)
    for p in ("انقل a.txt إلى Backup و b.txt إلى Docs", "احذف الكل ماعدا a.txt", "احذف الصورة"):
        out = json.loads(rf.generate(p, ctx))
        assert out["steps"] == [] and out["note"], (p, out)
    assert fb.call_history == [] and rf.fallback_calls == 0
    out = json.loads(rf.generate("حلّل هذا الوضع المعقد", ctx))
    assert out["steps"] and len(fb.call_history) == 1 and rf.fallback_calls == 1
    assert analyze("حلّل هذا الوضع المعقد", L1).question == HELP_QUESTION
    # أثناء التوضيح: سؤال القواعد يصل للمستخدم كسؤال
    cl = json.loads(rf.generate("انقل a.txt إلى Backup و b.txt إلى Docs", "CLARIFY\n" + CONTEXT_MARKER + "\n" + "\n".join(L1)))
    assert cl["questions"] and len(fb.call_history) == 1

    # ------------------------------------------------------------------ 12. المدقّق: winget
    from assistant.verifier import Verifier as V
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve()
        step = PlanStep(1, "winget_upgrade", "Git.Git")
        line = "Git  Git.Git  2.40.0  2.45.0  winget"
        hdr = "Name Id Version Available Source\n"

        def verify(probe_out, stdout, before_out=None):
            v = V(root, version_probe=lambda pkg: (0, probe_out, ""))
            if before_out is not None:
                v0 = V(root, version_probe=lambda pkg: (0, before_out, ""))
                before = v0.snapshot(step)
            else:
                before = v.snapshot(step)
            info = {"step_id": 1, "success": True, "exit_code": 0, "stdout": stdout, "stderr": ""}
            return v.verify_step(step, info, before)

        # لغة غير إنجليزية، والسطر لم يتغير وإصدار أحدث ما زال متاحاً ← فشل (دليل من القرص لا من النص)
        ok, ev, err = verify(hdr + line + "\n", "no message at all")
        assert not ok and "لم تتم الترقية" in err, (ok, err)
        ok, ev, err = verify(hdr + line + "\n", "لا يوجد ترقية متاحة")
        assert not ok, err
        ok, ev, err = verify(hdr + line + "\n", "No applicable upgrade found.")
        assert not ok
        # تغيّر السطر فعلاً ← نجاح
        ok, ev, err = verify(hdr + "Git  Git.Git  2.45.0  winget\n", "Successfully installed", before_out=hdr + line + "\n")
        assert ok, err
        # سطر ثابت بلا عمود Available ولا دليل ← كما كان (لا فشل بلا دليل)
        same = "Git  Git.Git  2.45.0  winget"
        ok, ev, err = verify(hdr + same + "\n", "done")
        assert ok, err
        # معرّف آخر يحوي المعرّف كجزء منه (Git.GitLFS) لا يكفي
        ok, ev, err = verify("Name Id Version\nGit LFS  Git.GitLFS  3.0\n", "ok")
        assert not ok and "لم يجد الحزمة" in err, err
        # المطابقة غير حساسة لحالة الأحرف
        ok, ev, err = verify("Name Id Version\nGit  git.git  2.45.0\n", "ok")
        assert ok, err

    # ------------------------------------------------------------------ 12. المدقّق: تنبيه إعادة تسمية الوجهة
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"; ws.mkdir()
        (ws / "a.txt").write_text("new", encoding="utf-8")
        (ws / "B").mkdir()
        (ws / "B" / "a.txt").write_text("old", encoding="utf-8")
        led = SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500)
        r = TaskRunner(ws, RuleProvider(), led, approver=lambda s: True)
        rep = r.run("T-ren", "x", plan=[PlanStep(1, "move", "a.txt", {"dest": "B/a.txt"})])
        assert rep.success and (ws / "B" / "a (1).txt").exists()
        vals = [str(e.get("value")) for e in rep.evidence]
        assert any("الوجهة أُعيدت تسميتها إلى «a (1).txt»" in v for v in vals), vals
        # وجهة عادية بلا تنبيه
        (ws / "c.txt").write_text("c", encoding="utf-8")
        rep = r.run("T-plain", "x", plan=[PlanStep(1, "move", "c.txt", {"dest": "B/c.txt"})])
        assert rep.success and not any("renamed" in str(e.get("value")) for e in rep.evidence)

    # ------------------------------------------------------------------ 12. المدقّق: سجل التراجع لكل خطوة
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve()
        v = Verifier(root)
        steps = [PlanStep(1, "move", "a.txt", {"dest": "N/a.txt"}), PlanStep(2, "move", "b.txt", {"dest": "N/b.txt"})]
        jp = root / ".undo_journal.json"

        def journal(*entries):
            jp.write_text(json.dumps(list(entries)), encoding="utf-8")

        mk_dir = {"action": "create_dir", "path": "rel:N", "task": "t"}
        mv_a = {"action": "move", "src": "rel:a.txt", "dest": "rel:N/a.txt", "task": "t"}
        mv_b = {"action": "move", "src": "rel:b.txt", "dest": "rel:N/b.txt", "task": "t"}
        # مجلد أنشأه النقل ضمنياً + نقل واحد = عدد كافٍ، لكن نقل b غير مسجَّل ← فشل
        journal(mk_dir, mv_a)
        ok, ev, err = v.verify_journal(steps, {}, 0)
        assert not ok and "b.txt" in err, (ok, err)
        journal(mk_dir, mv_a, mv_b)
        ok, ev, err = v.verify_journal(steps, {}, 0)
        assert ok, err
        # إدخال مسجَّل لملف آخر لا يغطي الخطوة
        journal(mk_dir, mv_a, {"action": "move", "src": "rel:zzz.txt", "dest": "rel:N/zzz.txt", "task": "t"})
        assert not v.verify_journal(steps, {}, 0)[0]
        # journal_start يستثني مهام سابقة
        journal(mv_a, mv_b, mk_dir, mv_a)
        assert not v.verify_journal(steps, {}, 2)[0]
        assert v.verify_journal(steps, {}, 0)[0]
        # حذف ونسخ وإعادة تسمية وإنشاء مجلد
        more = [PlanStep(1, "delete", "x.txt"), PlanStep(2, "copy", "y.txt", {"dest": "Z/y.txt"}),
                PlanStep(3, "rename", "w.txt", {"new_name": "v.txt"}), PlanStep(4, "create_dir", "Q/R")]
        journal({"action": "delete", "original_path": "rel:x.txt", "trash_path": "rel:.trash/x.txt"},
                {"action": "copy", "src": "rel:y.txt", "dest": "rel:Z/y.txt"},
                {"action": "rename", "src": "rel:w.txt", "dest": "rel:v.txt"},
                {"action": "create_dir", "path": "rel:Q"}, {"action": "create_dir", "path": "rel:Q/R"})
        assert v.verify_journal(more, {}, 0)[0]
        journal({"action": "delete", "original_path": "rel:x.txt"}, {"action": "copy", "src": "rel:y.txt"},
                {"action": "rename", "src": "rel:w.txt"}, {"action": "create_dir", "path": "rel:Q"})
        ok, _, err = v.verify_journal(more, {}, 0)
        assert not ok and "Q/R" in err, err
        # خطوات بلا target (كما في اختبارات قديمة) تُقارَن بالإجراء وحده
        journal(mv_a)
        assert v.verify_journal([type("S", (), {"step_id": 1, "action_type": "move"})()], {}, 0)[0]

    print("PASS: test_b28_rules completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b28_rules", file=sys.stderr)
    sys.exit(1)
