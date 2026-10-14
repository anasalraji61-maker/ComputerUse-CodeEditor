"""اختبارات B15: إصلاحات المراجعة الثالثة — فهم الطلبات الآمن، os.rename، حالة الأحرف، OneDrive، تطبيع البصمة، الوثائق."""
import os
import sys
import tempfile
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.rules import RuleProvider
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.tools import files as files_mod
    from assistant.tools.files import FileManager, _safe_move, _is_link
    from assistant.verifier import Verifier
    from assistant.planner import PlanStep

    rp = RuleProvider()
    CTX = ["Docs/", "Docs/in1.txt", "Docs/a.txt", "a.txt", "b.txt", "pic1.jpg", "pic2.jpg", "تقرير الجمعة.docx",
           "صورة هوية.jpg", "notes.tmp", "Backup/", "صور العائلة/", "update_report.txt", "data.csv", "desktop.ini"]

    def plan(text, ctx=None):
        a = rp.analyze(text, ctx if ctx is not None else CTX)
        return [(s["action_type"], s["target"], s["params"].get("dest") or s["params"].get("new_name")) for s in a.steps or []], a

    def asks(text, ctx=None):
        steps, a = plan(text, ctx)
        assert not steps and a.question, (text, steps, a.question)
        return a.question

    # 1. أسماء الملفات لا تُفهم كأفعال (الجمعة/هوية/update_)
    s, _ = plan("انسخ تقرير الجمعة.docx الى Backup")
    assert s == [("copy", "تقرير الجمعة.docx", "Backup/تقرير الجمعة.docx")], s
    s, _ = plan("انقل صورة هوية.jpg الى Backup")
    assert s == [("move", "صورة هوية.jpg", "Backup/صورة هوية.jpg")], s
    s, _ = plan("احذف update_report.txt")
    assert s == [("delete", "update_report.txt", None)], s
    s, _ = plan("أعد تسمية الملفات بإضافة البادئة new_")
    assert s and all(x[0] == "rename" for x in s) and all(x[1] != "desktop.ini" for x in s), s  # desktop.ini مستثنى
    # «date» ككلمة كاملة فقط
    s, _ = plan("أعد تسمية الملفات بإضافة البادئة update_")
    assert s and all(x[2].startswith("update_") for x in s), s

    # 2. مسار متداخل لا يتحول لحذف كل ملفات txt
    s, _ = plan("احذف Docs/in1.txt")
    assert s == [("delete", "Docs/in1.txt", None)], s
    s, _ = plan("احذف in1.txt من Docs")
    assert s == [("delete", "Docs/in1.txt", None)], s
    s, a = plan("احذف a.txt")  # (B32) موجود في الجذر وفي Docs: الحذف يسأل بدل أن يختار الأضحل (التشغيل الثاني كان يصيب الآخر)
    assert not s and a.question, s
    s, _ = plan("انقل a.txt إلى Backup")  # النقل ما زال يختار الأضحل مع ملاحظة صريحة
    assert s and s[-1][1] == "a.txt", s
    s, _ = plan("احذف a.txt من Docs")
    assert s == [("delete", "Docs/a.txt", None)], s
    asks("احذف in1.txt من NoSuchFolder")
    asks("احذف ghost.docx")            # اسم مكتوب غير موجود: لا رجوع لامتداد
    asks("احذف Docs/ghost.txt")
    asks("احذف in1.txt من Backup")     # ليس داخل النطاق

    # 3. استثناءات/شروط/عمليات متعددة/حذف شامل ← سؤال
    for t in ("احذف الصور ما عدا pic1.jpg", "احذف الملفات بدون a.txt", "delete everything except a.txt",
              "احذف الملفات القديمة", "احذف الصور الكبيرة", "انقل الملفات التي تنتهي بـ txt إلى Backup",
              "انسخ a.txt الى Backup ثم احذف a.txt", "احذف a.txt وانسخ b.txt", "احذف الكل", "احذف جميع محتويات Docs",
              "احذف كل شيء", "انقل الصور الى مجلد جديد كبير"):
        asks(t)
    s, _ = plan("غير اسم a.txt")  # عبارة «غير اسم» ليست نفياً (لكنها غير مدعومة بلا بادئة)
    assert not s

    # 4. فئة/امتداد ما زالت تعمل، ومن نوع
    s, _ = plan("احذف الصور")
    assert [x[1] for x in s] == ["pic1.jpg", "pic2.jpg", "صورة هوية.jpg"], s
    s, _ = plan("احذف ملفات tmp")
    assert s == [("delete", "notes.tmp", None)], s
    s, _ = plan("احذف الملفات من نوع tmp")
    assert s == [("delete", "notes.tmp", None)], s

    # 5. الوجهة: كلمة واحدة، مجلد موجود بكلمات، تنصيص، متعددة بلا تنصيص ← سؤال، تعارض وجهة، نفس المكان
    s, _ = plan("انقل الصور الى صور العائلة")
    assert s and all(x[2].startswith("صور العائلة/") for x in s) and not any(x[0] == "create_dir" for x in s), s
    s, _ = plan("انقل pic1.jpg الى «ألبوم جديد»")
    assert s[0] == ("create_dir", "ألبوم جديد", None) and s[1][2] == "ألبوم جديد/pic1.jpg", s
    asks("انقل pic1.jpg الى ألبوم جديد")
    s, a = plan("انقل a.txt الى Docs")           # Docs/a.txt موجود: B29 يحفظه باسم «(1)» بدل سؤال لا يُجاب عنه ولا يستبدل شيئاً
    assert s == [("move", "a.txt", "Docs/a (1).txt")] and a.note, (s, a.note)
    s, a = plan("انقل Docs/in1.txt الى Docs")
    assert not s and a.note
    s, _ = plan("انسخ a.txt الى backup")         # حالة الأحرف: يستعمل Backup الموجود
    assert s == [("copy", "a.txt", "Backup/a.txt")], s
    s, _ = plan("انسخ a.txt الى Backup من فضلك")
    assert s == [("copy", "a.txt", "Backup/a.txt")], s
    asks("انقل a.txt الى ..")
    asks("انقل a.txt الى C:\\Windows")

    # 6. ملف يحمل اسم مجلد الوجهة
    asks("انقل pic1.jpg الى data.csv")

    # 7. ترتيب: ملفات النظام مستثناة؛ ملف باسم مجلد فئة يمنع
    steps, a = plan("رتب الملفات حسب النوع")
    assert steps and not any(x[1] == "desktop.ini" for x in steps), steps
    asks("رتب الملفات حسب النوع", ["صور", "a.jpg"])

    # 8. قائمة مقتطعة: لا تعميم على الفئة ولا كشف مكررات
    trunc = ["a.txt", "p.jpg", "… (القائمة مقتطعة)"]
    asks("احذف الصور", trunc)
    asks("احذف المكررات", trunc)
    s, _ = plan("احذف a.txt", trunc)
    assert s == [("delete", "a.txt", None)], s

    # 9. تنفيذ حقيقي: المسار المتداخل لا يمس غيره، والتراجع يعيد كل شيء
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        root = base / "ws"
        (root / "Docs").mkdir(parents=True)
        for f in ("Docs/in1.txt", "Docs/a.txt", "a.txt", "b.txt", "تقرير الجمعة.docx", "صورة هوية.jpg"):
            (root / f).write_text("x" + f, encoding="utf-8")
        led = SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500)
        r = TaskRunner(root, RuleProvider(), led, approver=lambda s: True)
        assert r.run("n1", "احذف Docs/in1.txt").success
        assert not (root / "Docs" / "in1.txt").exists() and (root / "Docs" / "a.txt").exists() and (root / "a.txt").exists() and (root / "b.txt").exists()
        assert r.run("n2", "انسخ تقرير الجمعة.docx الى Backup").success and (root / "Backup" / "تقرير الجمعة.docx").exists()
        assert r.run("n3", "انقل صورة هوية.jpg الى Backup").success and (root / "Backup" / "صورة هوية.jpg").exists()
        assert not (root / "صورة هوية.jpg").exists()
        rep = r.run("n4", "احذف الصور ما عدا pic1.jpg")
        assert not rep.success and (root / "Backup" / "صورة هوية.jpg").exists()
        assert r.undo_task("n3") >= 1 and (root / "صورة هوية.jpg").exists()  # n4 لم يغيّر شيئاً فـ n3 هي الأحدث
        assert (root / "a.txt").read_text(encoding="utf-8") == "xa.txt"

    # 10. os.rename: لا كتابة فوق موجود، نفس المكان/الاسم خطأ، تسمية بحالة الأحرف
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve() / "ws"
        root.mkdir()
        (root / "a.txt").write_text("A", encoding="utf-8")
        (root / "b.txt").write_text("B", encoding="utf-8")
        try:
            _safe_move(str(root / "a.txt"), str(root / "b.txt"))
            raise AssertionError("كان يجب رفض الكتابة فوق موجود")
        except FileExistsError:
            pass
        assert (root / "b.txt").read_text(encoding="utf-8") == "B" and (root / "a.txt").exists()
        fm = FileManager(root)
        for bad in (lambda: fm.move("a.txt", "a.txt"), lambda: fm.rename("a.txt", "a.txt")):
            try:
                bad()
                raise AssertionError("نقل/تسمية إلى نفس الاسم يجب أن يفشل")
            except ValueError:
                pass
        assert (root / "a.txt").read_text(encoding="utf-8") == "A" and not (root / "a (1).txt").exists()
        out = fm.move("a.txt", "b.txt")  # الوجهة موجودة: اسم فريد لا استبدال
        assert out.name == "b (1).txt" and (root / "b.txt").read_text(encoding="utf-8") == "B"
        fm.rename("b (1).txt", "C.txt")
        assert (root / "C.txt").read_text(encoding="utf-8") == "A"
        fm.undo_last()
        assert (root / "b (1).txt").exists()
        fm.undo_last()
        assert (root / "a.txt").read_text(encoding="utf-8") == "A"

    # 11. المدقق: البصمة بحروف كبيرة أو بادئة sha256: مقبولة، والاسم الفريد بلا حساسية للحالة
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve() / "ws"
        root.mkdir()
        (root / "p.txt").write_text("secret", encoding="utf-8")
        v = Verifier(allowed_root=root)
        h = v.calculate_hash(root / "p.txt")
        assert v._is_unique_variant(root / "Report.PDF", root / "report (1).pdf")
        assert not v._is_unique_variant(root / "Report.pdf", root / "Other (1).pdf")
        for variant in (h.upper(), "sha256:" + h, "  " + h + " "):
            fmm = FileManager(root)
            st = PlanStep(1, "copy", "p.txt", {"dest": "q.txt", "expected_hash": variant})
            before = v.snapshot(st)
            out = fmm.copy("p.txt", "q.txt")
            ok, ev, err = v.verify_step(st, {"step_id": 1, "success": True, "dest": str(out)}, before)
            assert ok, (variant, err)
            fmm.undo_last()
        st = PlanStep(1, "copy", "p.txt", {"dest": "q.txt", "expected_hash": "0" * 64})
        before = v.snapshot(st)
        out = FileManager(root).copy("p.txt", "q.txt")
        ok, _, err = v.verify_step(st, {"step_id": 1, "success": True, "dest": str(out)}, before)
        assert not ok and "البصمة" in err

    # 12. _is_link: ملفات OneDrive (reparse غير رابط) ليست روابط، أما symlink/mount-point فنعم
    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "x.txt"
        f.write_text("x", encoding="utf-8")
        real_lstat = os.lstat

        class FakeStat:
            st_mode = 0o100644
            st_file_attributes = 0x400

            def __init__(self, tag):
                self.st_reparse_tag = tag

        def with_tag(tag):
            os.lstat = lambda p, *a, **k: FakeStat(tag) if str(p) == str(f) else real_lstat(p, *a, **k)
            try:
                return _is_link(f)
            finally:
                os.lstat = real_lstat
        assert with_tag(0x9000701A) is False   # IO_REPARSE_TAG_CLOUD (OneDrive placeholder)
        assert with_tag(0xA000000C) is True    # SYMLINK
        assert with_tag(0xA0000003) is True    # MOUNT_POINT (junction)
        assert _is_link(f) is False

    # 13. الوثائق والمستودع
    assert "__author__" not in (ROOT / "assistant" / "__init__.py").read_text(encoding="utf-8")
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in (".trash/", ".undo_journal*.json", "*.db", ".cos_assistant/"):
        assert entry in gi, entry
    guide = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    assert "لا يلمس شيئاً خارجه" not in guide and "عملية واحدة في كل طلب" in guide
    assert "IMPLEMENTED-awaiting-real-PASS" not in (ROOT / "docs" / "REVIEW.md").read_text(encoding="utf-8")

    print("PASS: test_b15 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b15", file=sys.stderr)
    sys.exit(1)
