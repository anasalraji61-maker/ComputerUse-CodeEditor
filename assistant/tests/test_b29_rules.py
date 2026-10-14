"""اختبارات B29: نتائج تقرير QA على سيناريوهات واقعية: جواب التوضيح العاري يصير جملة صريحة، مسح كامل للقواعد بدل قائمة الستين،
«backup» ليست فعل نسخ، حشو مهذب لترتيب النوع، بادئة تشبه مؤهِّلاً، بحث المكررات للعرض فقط، ملاحظات التخطي (تصادم/قفل ~$)،
قائمة محتويات المجلد في تقرير الفحص، فئات المؤقتة/الفواتير/الإكسل، رسائل «لا شيء للتنفيذ» وفشل منتصف الخطة، اسم «(1)» عند وجود الوجهة،
واسم المجلد متعدد الكلمات بلا تنصيص."""
import datetime
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
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import CONTEXT_MARKER
    from assistant.models.rules import RuleProvider, merge_clarification, INFO_PREFIX
    from assistant.planner import PlanStep
    from assistant.runner import TaskRunner
    from assistant.tools.files import FileManager
    from assistant.ui.controller import AssistantController

    rp = RuleProvider(today=lambda: datetime.date(2026, 1, 2))

    def analyze(prompt, ctx, provider=None):
        return (provider or rp).analyze(prompt, list(ctx))

    def steps_of(prompt, ctx, provider=None):
        a = analyze(prompt, ctx, provider)
        return [(s["action_type"], s["target"], s["params"].get("dest") or s["params"].get("new_name")) for s in (a.steps or [])]

    def asks(prompt, ctx, provider=None):
        a = analyze(prompt, ctx, provider)
        assert not a.steps and a.question, f"كان يجب السؤال: {prompt!r} ← {steps_of(prompt, ctx, provider)} {a.note}"
        return a.question

    def mk_ws(tmp, name, files, dirs=()):
        ws = pathlib.Path(tmp).resolve() / name
        ws.mkdir()
        for d in dirs:
            (ws / d).mkdir(parents=True, exist_ok=True)
        for f, c in files.items():
            (ws / f).parent.mkdir(parents=True, exist_ok=True)
            (ws / f).write_text(c, encoding="utf-8")
        return ws

    def controller(tmp, ws, name="c"):
        led = SQLiteLedger(pathlib.Path(tmp).resolve() / f"{name}.db", initial_balance=5000, spending_limit=5000)
        return AssistantController(RuleProvider(), ws, led, approval_timeout=5.0)

    def clarify(c, request, answer):
        """طلب ناقص ← سؤال ← جواب عارٍ ← خطة. يعيد قائمة (action, target, dest) أو ('ERR', نص) أو ('Q', أسئلة)."""
        c.drain_events()
        c.submit_request(request)
        ev = c.wait_for("questions", 5) or None
        assert ev, f"لم يُطرح سؤال لـ {request!r}"
        c.answer_questions([answer])
        for _ in range(100):
            e = c.next_event(timeout=0.1)
            if e is None:
                continue
            if e["type"] == "plan":
                out = [(s["action"], s["target"], s["params"].get("dest") or s["params"].get("new_name")) for s in e["steps"]]
                c.cancel_plan()
                return out
            if e["type"] in ("error", "info"):
                return ("ERR", e["text"])
            if e["type"] == "questions":
                c.cancel_plan()
                return ("Q", e["questions"])
        raise AssertionError("لا حدث")

    with tempfile.TemporaryDirectory() as tmp:
        # ================================================================== 1. جواب التوضيح العاري
        ws = mk_ws(tmp, "w1", {"pic1.jpg": "1", "pic2.png": "2", "notes.txt": "n", "Docs/d.txt": "d", "a.txt": "a", "b.txt": "b"})
        c = controller(tmp, ws, "c1")
        r = clarify(c, "انقل الصور", "Docs")
        assert r == [("move", "pic1.jpg", "Docs/pic1.jpg"), ("move", "pic2.png", "Docs/pic2.png")], r
        r = clarify(c, "انقل الصور", "صور العائلة")           # اسم متعدد الكلمات يُقبل كله اسماً واحداً
        assert r[0] == ("create_dir", "صور العائلة", None) and r[1][2] == "صور العائلة/pic1.jpg", r
        r = clarify(c, "غير اسم notes.txt", "ملاحظات.txt")
        assert r == [("rename", "notes.txt", "ملاحظات.txt")], r
        r = clarify(c, "اعمل مجلد", "فواتير 2025")
        assert r == [("create_dir", "فواتير 2025", None)], r
        r = clarify(c, "انقل ملف إلى Backup", "notes.txt")    # الجواب مصدر: يدخل قبل علامة الوجهة
        assert r == [("create_dir", "Backup", None), ("move", "notes.txt", "Backup/notes.txt")], r
        r = clarify(c, "أعد تسمية الملفات", "2025_")
        assert [x[2] for x in r][:2] == ["2025_a.txt", "2025_b.txt"], r
        r = clarify(c, "احذف", "notes.txt")
        assert r == [("delete", "notes.txt", None)], r
        r = clarify(c, "رتب الملفات حسب الحجم", "حسب النوع")   # معيار ترتيب مدعوم يستبدل غير المدعوم
        assert any(x[0] == "move" and x[2].startswith("صور/") for x in r), r
        r = clarify(c, "انقل a.txt إلى", "Docs")              # علامة وجهة بلا اسم
        assert r == [("move", "a.txt", "Docs/a.txt")], r
        # الجملة الكاملة تبقى بالسلوك القديم (لصق)
        r = clarify(c, "انقل الصور", "انقلها إلى Docs")
        assert isinstance(r, list) or r[0] in ("ERR", "Q")   # لا انهيار؛ لا تُحوَّل لأنها جملة بفعل ووجهة
        assert merge_clarification("انقل الصور", "dest", "انقلها إلى Docs") is None
        assert merge_clarification("انقل الصور", "dest", "إلى Docs") is None
        assert merge_clarification("انقل الصور", "dest", "Docs") == "انقل الصور إلى Docs"
        assert merge_clarification("انقل الصور", "dest", "صور العائلة") == "انقل الصور إلى «صور العائلة»"
        assert merge_clarification("اعمل مجلد", "folder_name", "فواتير 2025") == "اعمل مجلد باسم «فواتير 2025»"
        assert merge_clarification("انقل الصور", None, "Docs") is None
        # الأسئلة تحمل نوع الخانة
        import json as _j
        g = _j.loads(rp.generate("انقل الصور", "CLARIFY\n" + CONTEXT_MARKER + "\npic.jpg"))
        assert g["slots"] == ["dest"], g
        assert _j.loads(rp.generate("اعمل مجلد", "CLARIFY\n" + CONTEXT_MARKER + "\npic.jpg"))["slots"] == ["folder_name"]
        assert _j.loads(rp.generate("غير اسم a.txt", "CLARIFY\n" + CONTEXT_MARKER + "\na.txt"))["slots"] == ["new_name"]

        # ================================================================== 2. مسح كامل للقواعد (>60 عنصراً)
        files = {f"doc_{i:03d}.pdf": "p" for i in range(40)}
        files.update({f"img_{i:03d}.tmp": "t" for i in range(30)})
        files.update({f"pic_{i:03d}.jpg": "j" for i in range(30)})
        files.update({f"note_{i:03d}.txt": "n" for i in range(130)})
        ws2 = mk_ws(tmp, "w2", files)
        c2 = controller(tmp, ws2, "c2")
        listing = c2.runner.workspace_listing()
        assert "مقتطعة" in listing                           # قائمة النموذج تبقى مقتطعة
        prov = c2.runner.planner.provider
        ctx = [ln for ln in listing.splitlines()]
        a = analyze("انقل ملفات pdf إلى PDFs", ctx, prov)
        assert a.steps and len([s for s in a.steps if s["action_type"] == "move"]) == 40, (a.question, a.note)
        a = analyze("احذف ملفات tmp", ctx, prov)
        assert a.steps and len(a.steps) == 30, a.question
        a = analyze("رتب الملفات حسب النوع", ctx, prov)
        assert a.steps and a.note and "بقي" in a.note, a.note  # الحد 48 مع ملاحظة الاقتطاع
        # لا تقول «لا شيء» زوراً: كل المتبقي في الجذر يظل يُرتَّب في التشغيل التالي
        c2.submit_request("رتب الملفات حسب النوع")
        assert c2.wait_for("plan", 5)
        c2.start()
        assert c2.wait_for("report", 20)
        c2.submit_request("رتب الملفات حسب النوع")
        ev = c2.wait_for("plan", 5)
        assert ev and len(ev["steps"]) > 1, ev
        c2.cancel_plan()
        # قائمة مقتطعة ومسح غير ممكن (بلا مجلد مربوط): لا نقول «لا شيء لترتيبه»
        trunc = ["Docs/", "… (القائمة مقتطعة)"]
        a = analyze("رتب الملفات حسب النوع", trunc)
        assert not a.steps and a.question and not (a.note or "").startswith(INFO_PREFIX)

        # ================================================================== 3. «backup» ليست فعل نسخ
        L3 = ["a.txt", "Docs/", "Docs/q.txt", "cv.docx", "x.pdf", "y.pdf"]
        assert steps_of("انقل ملفات pdf إلى Backup", L3) == [("create_dir", "Backup", None), ("move", "x.pdf", "Backup/x.pdf"), ("move", "y.pdf", "Backup/y.pdf")]
        assert steps_of("اعمل نسخة احتياطية من المجلد Docs", L3) == [("create_dir", "نسخة_احتياطية", None), ("copy", "Docs", "نسخة_احتياطية/Docs")]
        assert steps_of("اعمل نسخة احتياطية من cv.docx", L3) == [("create_dir", "نسخة_احتياطية", None), ("copy", "cv.docx", "نسخة_احتياطية/cv.docx")]
        assert steps_of("انسخ a.txt إلى backup", L3) == [("create_dir", "backup", None), ("copy", "a.txt", "backup/a.txt")]
        asks("اعمل نسخة احتياطية", L3)
        asks("انقل a.txt وانسخ cv.docx", L3)

        # ================================================================== 4. حشو مهذب/عامي لترتيب النوع
        L4 = ["a.txt", "p.jpg"]
        for p in ("رتب الملفات حسب النوع من فضلك", "رتب لي الملفات حسب نوعها", "الرجاء ترتيب الملفات", "رتب الملفات في مجلدات حسب النوع",
                  "رتب سطح المكتب", "ممكن ترتب الملفات؟", "رتب الملفات من فضلك"):
            assert steps_of(p, L4), p
        asks("رتب الملفات من Docs", L4 + ["Docs/"])            # النطاق ليس حشواً آمناً
        asks("رتب الملفات حسب الحجم", L4)

        # ================================================================== 5. بادئة تشبه مؤهِّلاً
        L5 = ["a.txt", "b.txt", "Docs/", "Docs/q.txt"]
        for pre in ("جديد_", "قديم_", "بعد_", "old_", "new-"):
            assert [x[2] for x in steps_of(f"أضف بادئة {pre}", L5)] == [pre + "a.txt", pre + "b.txt"], pre
        assert steps_of("أضف بادئة قديم_ لملفات Docs", L5) == [("rename", "Docs/q.txt", "قديم_q.txt")]
        asks("أضف بادئة x_ للملفات القديمة", L5)               # المؤهِّل خارج البادئة ما زال يسأل
        asks("احذف الملفات القديمة", L5)

        # ================================================================== 6. بحث المكررات للعرض فقط
        ws6 = mk_ws(tmp, "w6", {"a.txt": "same", "b.txt": "same", "Docs/a.txt": "same", "Docs/deep/z.txt": "same", "u.txt": "unique"})
        rp6 = RuleProvider(workspace=ws6)
        L6 = ["a.txt", "b.txt", "u.txt", "Docs/", "Docs/a.txt", "Docs/deep/"]
        for p in ("ابحث عن الملفات المكررة", "اعرض لي الملفات المكررة", "هل توجد ملفات مكررة؟", "ابحث عن التكرار"):
            a = rp6.analyze(p, L6)
            assert a.steps and all(s["action_type"] == "inspect" for s in a.steps), (p, a.question, a.note)
            assert a.note and "لم يُحذف" in a.note and "a.txt" in a.note, a.note
        assert not any(s["action_type"] in ("delete", "move") for s in rp6.analyze("ابحث عن الملفات المكررة", L6).steps)
        assert rp6.analyze("احذف المكررات", L6).steps[0]["action_type"] == "delete"
        assert rp6.analyze("انقل المكررات", L6).steps[0]["action_type"] == "create_dir"
        q = rp6.analyze("ابحث عن الملفات المكررة في Docs", L6)
        assert not q.steps and q.question                       # نطاق/وجهة مع المكررات ما زال يسأل
        # عمق المسح الكامل: مجموعة تضم ملفاً من المستوى الثاني
        a = RuleProvider(workspace=ws6).analyze("ابحث عن الملفات المكررة", ["… (القائمة مقتطعة)"])
        assert a.steps and "Docs/a.txt" in a.note, a.note
        a = RuleProvider(workspace=mk_ws(tmp, "w6b", {"x.txt": "1", "y.txt": "2"})).analyze("ابحث عن الملفات المكررة", ["x.txt", "y.txt"])
        assert not a.steps and a.note.startswith(INFO_PREFIX)

        # ================================================================== 7. ملاحظة التخطي: تصادم/قفل ~$
        L7 = ["a.txt", "b.txt", "p.jpg", "~$tmp.docx", "مستندات/", "مستندات/a.txt", "صور/"]
        a = analyze("رتب الملفات حسب النوع", L7)
        moved = [s["target"] for s in a.steps if s["action_type"] == "move"]
        assert moved == ["b.txt", "p.jpg"], moved
        assert a.note and "a.txt" in a.note and "~$tmp.docx" in a.note and "تُخطي 1" in a.note, a.note
        a = analyze("أضف بادئة x_", ["a.txt", "~$w.docx"])
        assert [s["target"] for s in a.steps] == ["a.txt"] and "~$w.docx" in a.note
        a = analyze("رتب الملفات حسب النوع", ["~$w.docx"])
        assert not a.steps and a.note.startswith(INFO_PREFIX) and "~$w.docx" in a.note
        a = analyze("رتب الملفات حسب النوع", ["مستندات/", "مستندات/a.txt", "a.txt"])
        assert not a.steps and "a.txt" in a.note
        # ملفان يختلفان بحالة الأحرف فقط لا يتصادمان في الوجهة
        a = analyze("رتب الملفات حسب النوع", ["P.jpg", "p.jpg"])
        assert len([s for s in a.steps if s["action_type"] == "move"]) == 1 and "تُخطي" in a.note

        # ================================================================== 8. تقرير الفحص يسرد العناصر
        ws8 = mk_ws(tmp, "w8", {"Docs/a.txt": "1", "Docs/b.txt": "2", "Docs/sub/c.txt": "3", "top.txt": "t"})
        c8 = controller(tmp, ws8, "c8")
        for req in ("اعرض محتويات Docs", "ماذا يوجد في Docs"):
            c8.submit_request(req)
            assert c8.wait_for("plan", 5)
            c8.start()
            rep = c8.wait_for("report", 10)
            assert rep and rep["success"], rep
            assert "a.txt" in rep["text"] and "b.txt" in rep["text"] and "sub/" in rep["text"] and "3 عنصراً" in rep["text"], rep["text"]
        c8.submit_request("ماذا يوجد في المجلد؟")
        assert c8.wait_for("plan", 5)
        c8.start()
        rep = c8.wait_for("report", 10)
        assert "top.txt" in rep["text"] and "Docs/" in rep["text"] and "يحوي 2 عنصراً" in rep["text"], rep["text"]
        big = mk_ws(tmp, "w8b", {f"f{i:03d}.txt": "x" for i in range(60)})
        from assistant.verifier import Verifier
        txt = Verifier(big)._listing(big)
        assert "60 عنصراً" in txt and "و20 عنصراً آخر" in txt, txt

        # ================================================================== 9. المؤقتة / الفواتير / الإكسل / رسالة «لم أفهم»
        L9 = ["a.txt", "x.tmp", "old.bak", "dl.crdownload", "~$w.docx", "فاتورة_1.pdf", "invoice-2.pdf", "ايصال.pdf", "receipt3.pdf",
              "data.xlsx", "r.docx", "keep.txt", "Docs/", "Docs/in.tmp"]
        # (B30) «المؤقتة» = tmp/temp/crdownload/~$ فقط؛ .bak نسخ احتياطية يدوية فلا تُحذف بهذه العبارة
        assert sorted(s[1] for s in steps_of("نظف الملفات المؤقتة", L9)) == sorted(["dl.crdownload", "x.tmp", "~$w.docx"])
        assert all(s[0] == "delete" for s in steps_of("احذف الملفات المؤقتة", L9))
        assert sorted(s[1] for s in steps_of("احذف الفواتير", L9)) == sorted(["فاتورة_1.pdf", "invoice-2.pdf"])
        assert sorted(s[1] for s in steps_of("انقل الايصالات إلى Backup", L9) if s[0] == "move") == sorted(["ايصال.pdf", "receipt3.pdf"])
        assert [s[1] for s in steps_of("احذف ملفات الاكسل", L9)] == ["data.xlsx"]
        assert [s[1] for s in steps_of("احذف ملفات Excel", L9)] == ["data.xlsx"]
        assert [s[1] for s in steps_of("احذف ملفات word", L9)] == ["r.docx"]   # (B30) ملف القفل ~$ لا يدخل نوعاً
        asks("احذف الفاتورة", L9)                                 # مفرد ← سؤال
        q = asks("احذف Zorro", L9)
        assert "Zorro" in q and "zorro" not in q                 # الكلمة كما كتبها المستخدم
        q = asks("انقل ملفات Fatoora إلى Docs", L9)
        assert "Fatoora" in q
        q = asks("أضف بادئة x_ لملفات Docz", L9)
        assert "Docz" in q
        q = asks("افحص Docz", L9)
        assert "Docz" in q or "docz" in q.lower()

        # ================================================================== 10. رسائل: لا شيء للتنفيذ ≠ خطأ؛ فشل منتصف الخطة
        ws10 = mk_ws(tmp, "w10", {"a.txt": "1", "Docs/x.txt": "2"})
        c10 = controller(tmp, ws10, "c10")
        for req in ("احذف المكررات", "انقل Docs/x.txt إلى الجذر" if False else "انقل a.txt إلى الجذر"):
            c10.drain_events()
            c10.submit_request(req)
            got = None
            for _ in range(50):
                e = c10.next_event(timeout=0.2)
                if e and e["type"] in ("info", "error", "plan"):
                    got = e
                    break
            assert got and got["type"] == "info" and got["text"].startswith(INFO_PREFIX), (req, got)
            assert c10.state == "IDLE"
        c10.drain_events()
        c10.submit_request("ما هذا الكلام الغريب")
        e = None
        for _ in range(50):
            e = c10.next_event(timeout=0.2)
            if e and e["type"] in ("info", "error", "plan", "questions"):
                break
        assert e and e["type"] == "error", e                     # عدم الفهم يبقى خطأً حقيقياً

        ws10b = mk_ws(tmp, "w10b", {"a.txt": "1", "b.txt": "2"})
        led = SQLiteLedger(pathlib.Path(tmp).resolve() / "l10b.db", initial_balance=5000, spending_limit=5000)
        r10 = TaskRunner(ws10b, RuleProvider(), led, approver=lambda s: True)
        orig = FileManager.move

        def boom(self, src, dest):
            if str(src) == "b.txt":
                raise PermissionError(13, "Permission denied", str(ws10b / "b.txt"))
            return orig(self, src, dest)
        FileManager.move = boom
        try:
            rep = r10.run("T10", "x", plan=[PlanStep(1, "move", "a.txt", {"dest": "N/a.txt"}), PlanStep(2, "move", "b.txt", {"dest": "N/b.txt"})])
        finally:
            FileManager.move = orig
        assert not rep.success
        msg = " ".join(rep.failures)
        assert "PermissionError" not in msg and str(ws10b) not in msg and "Errno" not in msg, msg
        assert "نُفّذت 1 من 2" in msg and "ولم تُنفَّذ 1" in msg, msg

        # ================================================================== 11. الوجهة موجودة ← اسم «(1)» بدل سؤال
        L11 = ["Projects/", "Projects/p.txt", "Backup/", "Backup/Projects/", "Backup/a.txt", "a.txt", "b.txt"]
        a = analyze("انسخ مجلد Projects إلى Backup", L11)
        assert [(s["action_type"], s["target"], s["params"]["dest"]) for s in a.steps] == [("copy", "Projects", "Backup/Projects (1)")], (a.question, a.steps)
        assert "Projects (1)" in a.note
        a = analyze("انقل a.txt إلى Backup", L11)
        assert [(s["action_type"], s["params"]["dest"]) for s in a.steps] == [("move", "Backup/a (1).txt")] and "a (1).txt" in a.note
        a = analyze("انقل a.txt و b.txt إلى Backup", L11)
        assert [s["params"]["dest"] for s in a.steps] == ["Backup/a (1).txt", "Backup/b.txt"], a.steps
        a = analyze("انقل a.txt إلى Backup", L11 + ["Backup/a (1).txt"])
        assert a.steps[0]["params"]["dest"] == "Backup/a (2).txt"
        # تنفيذ فعلي: لا شيء يُستبدل ويُدقَّق بلا خطأ
        ws11 = mk_ws(tmp, "w11", {"a.txt": "NEW", "Backup/a.txt": "OLD", "Projects/p.txt": "p", "Backup/Projects/old.txt": "o"})
        c11 = controller(tmp, ws11, "c11")
        for req in ("انقل a.txt إلى Backup", "انسخ مجلد Projects إلى Backup"):
            c11.submit_request(req)
            assert c11.wait_for("plan", 5)
            c11.start()
            rep = c11.wait_for("report", 10)
            assert rep and rep["success"], rep
        assert (ws11 / "Backup" / "a.txt").read_text() == "OLD" and (ws11 / "Backup" / "a (1).txt").read_text() == "NEW"
        assert (ws11 / "Backup" / "Projects" / "old.txt").exists() and (ws11 / "Backup" / "Projects (1)" / "p.txt").exists()
        c11.undo_last(); c11.undo_last()
        assert (ws11 / "a.txt").read_text() == "NEW" and not (ws11 / "Backup" / "Projects (1)").exists()

        # ================================================================== 12. اسم مجلد متعدد الكلمات بلا تنصيص
        assert steps_of("أنشئ مجلد فواتير 2025", []) == [("create_dir", "فواتير 2025", None)]
        assert steps_of("اعمل مجلد باسم فواتير 2025", []) == [("create_dir", "فواتير 2025", None)]
        assert steps_of("أنشئ مجلد Reports Q1.", []) == [("create_dir", "Reports Q1", None)]
        assert steps_of("أنشئ مجلد Reports", []) == [("create_dir", "Reports", None)]
        assert steps_of("أنشئ مجلد «صور قديمة»", []) == [("create_dir", "صور قديمة", None)]
        asks("انشئ مجلد Test وضع a.txt فيه", [])                  # فعل بعد الاسم ليس جزءاً منه
        asks("أنشئ مجلد", [])

    print("PASS: test_b29_rules completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b29_rules", file=sys.stderr)
    sys.exit(1)
