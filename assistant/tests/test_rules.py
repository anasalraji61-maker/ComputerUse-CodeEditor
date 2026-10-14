"""اختبارات المخطط المحلي بالقواعد: كل نية من البداية للنهاية عبر TaskRunner الحقيقي على مجلد مؤقت."""
import datetime
import hashlib
import json
import sys
import tempfile
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.rules import RuleProvider, RuleFirstProvider, norm, parse_context
    from assistant.models.base import CONTEXT_MARKER
    from assistant.models.fake import FakeProvider
    from assistant.planner import Planner, PlanParseError
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger

    FIXED = datetime.date(2026, 10, 14)
    rp = RuleProvider(today=lambda: FIXED)

    def sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def env(tmp, files=None, dirs=None):
        root = pathlib.Path(tmp).resolve() / "ws"
        root.mkdir()
        for d in dirs or []:
            (root / d).mkdir(parents=True, exist_ok=True)
        for f, c in (files or {}).items():
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            (root / f).write_text(c, encoding="utf-8")
        ledger = SQLiteLedger(pathlib.Path(tmp).resolve() / "l.db", initial_balance=500, spending_limit=500)
        return root, ledger

    def runner(root, ledger, **kw):
        return TaskRunner(root, rp, ledger, **kw)

    def types(root_runner, text):
        return [(s.action_type, s.target) for s in root_runner.make_plan(text)]

    assert norm("رَتِّب") == "رتب" and norm("أنشئ") == norm("انشئ") and norm("الهويّة") == norm("الهويه")

    # 1. ترتيب حسب النوع + تحقق + تراجع كامل
    with tempfile.TemporaryDirectory() as tmp:
        files = {"a.JPG": "img", "b.pdf": "doc", "c.xlsx": "xl", "d.mp3": "au", "e.zip": "zp", "f.exe": "ex",
                 "g.weird": "wd", "noext": "ne", "h.pptx": "pp", "i.mp4": "vd"}
        root, ledger = env(tmp, files)
        r = runner(root, ledger)
        rep = r.run("org", "رتّب الملفات حسب النوع")
        assert rep.success, rep.failures
        expect = {"a.JPG": "صور", "b.pdf": "مستندات", "c.xlsx": "جداول", "d.mp3": "صوتيات", "e.zip": "مضغوطات",
                  "f.exe": "برامج", "g.weird": "أخرى", "noext": "أخرى", "h.pptx": "عروض", "i.mp4": "فيديو"}
        for f, cat in expect.items():
            assert (root / cat / f).read_text(encoding="utf-8") == files[f], f
            assert not (root / f).exists()
        assert r.undo_task("org") == len(expect) + len(set(expect.values()))
        for f, c in files.items():
            assert (root / f).read_text(encoding="utf-8") == c
        # فارغ: رسالة واضحة
    with tempfile.TemporaryDirectory() as tmp:
        root, ledger = env(tmp, dirs=["x"])
        try:
            runner(root, ledger).make_plan("رتب الملفات")
            raise AssertionError("كان يجب رفض الخطة الفارغة")
        except PlanParseError as exc:
            assert "لا توجد ملفات" in str(exc)

    # 2. أكثر من 48 ملفاً: الخطة محدودة وتنجح، والباقي لمرة ثانية
    with tempfile.TemporaryDirectory() as tmp:
        root, ledger = env(tmp, {f"f{i:02d}.txt": str(i) for i in range(60)})
        r = runner(root, ledger)
        plan = r.make_plan("رتب الملفات")
        assert len(plan) <= 48
        assert r.run("big", "رتب الملفات").success
        left = [p for p in root.iterdir() if p.is_file() and not p.name.startswith(".")]
        assert 0 < len(left) < 60
        assert r.run("big2", "رتب الملفات").success
        assert not [p for p in root.iterdir() if p.is_file() and not p.name.startswith(".")]

    # 3. إعادة تسمية بالبادئة وبالتاريخ، داخل مجلد محدد فقط
    with tempfile.TemporaryDirectory() as tmp:
        root, ledger = env(tmp, {"DCIM/img1.jpg": "1", "DCIM/img2.jpg": "2", "other.txt": "o"})
        r = runner(root, ledger)
        assert r.run("rn1", "أعد تسمية الملفات في DCIM بإضافة البادئة trip_").success
        assert (root / "DCIM" / "trip_img1.jpg").read_text(encoding="utf-8") == "1" and (root / "other.txt").exists()
        assert r.run("rn2", "أعد تسمية ملفات DCIM بإضافة التاريخ في البداية").success
        assert (root / "DCIM" / "2026-10-14_trip_img1.jpg").exists()
        # بلا بادئة: سؤال توضيحي
        q = json.loads(rp.generate("أعد تسمية الملفات بإضافة", "CLARIFY\n" + CONTEXT_MARKER + "\nDCIM/"))
        assert q["questions"] and "بادئة" in q["questions"][0], q

    # 4. نسخ ملف ومجلد (الأصل يبقى، البصمة مطابقة) + مجلد وجهة جديد/موجود
    with tempfile.TemporaryDirectory() as tmp:
        root, ledger = env(tmp, {"project.txt": "secret", "Docs/a.txt": "A"}, dirs=["Archive"])
        r = runner(root, ledger)
        assert r.run("cp1", "انسخ project.txt إلى Backup").success
        assert sha(root / "Backup" / "project.txt") == sha(root / "project.txt")
        assert r.run("cp2", "انسخ المجلد Docs إلى Archive").success
        assert (root / "Archive" / "Docs" / "a.txt").read_text(encoding="utf-8") == "A" and (root / "Docs" / "a.txt").exists()
        assert r.run("cp3", "انسخ project.txt").success  # وجهة افتراضية
        assert (root / "نسخة_احتياطية" / "project.txt").exists()
        # مصدر غير موجود → سؤال
        q = json.loads(rp.generate("انسخ ghost.txt إلى X", "CLARIFY\n" + CONTEXT_MARKER + "\nproject.txt"))
        assert q["questions"]
        # وجهة خارج الجذر مرفوضة بالاسم نفسه
        ctx = ["project.txt"]
        a = rp.analyze("انسخ project.txt إلى ../outside", ctx)
        assert not a.steps and a.question, (a.steps, a.question)

    # 5. إنشاء مجلد ومشروع
    with tempfile.TemporaryDirectory() as tmp:
        root, ledger = env(tmp)
        r = runner(root, ledger)
        assert r.run("c1", "أنشئ مشروع my_app").success
        for d in ("src", "docs", "tests"):
            assert (root / "my_app" / d).is_dir()
        assert r.run("c2", "أنشئ مجلد باسم Reports").success and (root / "Reports").is_dir()
        assert r.run("c3", "أنشئ مجلد تقارير_2026").success and (root / "تقارير_2026").is_dir()
        q = json.loads(rp.generate("أنشئ مجلد", "CLARIFY"))
        assert q["questions"] and "اسم" in q["questions"][0]

    # 6. فحص: عنصر محدد وجذر
    with tempfile.TemporaryDirectory() as tmp:
        root, ledger = env(tmp, {"DCIM/x.jpg": "x", "notes.txt": "n"})
        r = runner(root, ledger)
        assert types(r, "افحص DCIM") == [("inspect", "DCIM")]
        assert r.run("i1", "افحص DCIM").success
        assert types(r, "اعرض ما في المجلد") == [("inspect", ".")]
        assert r.run("i2", "اعرض ما في المجلد").success

    # 7. whoami وwinget: خطرة وتحتاج موافقة
    with tempfile.TemporaryDirectory() as tmp:
        root, ledger = env(tmp)
        assert not runner(root, ledger).run("w0", "اعرض الهوية").success  # بلا موافقة
        r = runner(root, ledger, approver=lambda s: True, winget_runner=lambda cmd: (0, "Git.Git 2.50", ""))
        assert r.run("w1", "اعرض الهوية").success
        assert types(r, "حدّث برنامج Git.Git") == [("winget_upgrade", "Git.Git")]
        assert r.run("w2", "حدّث برنامج Git.Git").success

    # 8. طلب غير مفهوم: سؤال واحد ثم رفض واضح
    with tempfile.TemporaryDirectory() as tmp:
        root, ledger = env(tmp, {"a.txt": "a"})
        q = json.loads(rp.generate("افعل شيئاً سحرياً", "CLARIFY"))
        assert len(q["questions"]) == 1 and "رتّب" in q["questions"][0]
        try:
            runner(root, ledger).make_plan("افعل شيئاً سحرياً")
            raise AssertionError
        except PlanParseError:
            pass
        # طلب مفهوم لا يسأل
        assert json.loads(rp.generate("رتب الملفات", "CLARIFY\n" + CONTEXT_MARKER + "\na.txt"))["questions"] == []

    # 9. القواعد أولاً: المعروف بلا نموذج، والمجهول يُحال
    fb = FakeProvider(default_response=json.dumps({"steps": [{"step_id": 1, "action_type": "inspect", "target": ".", "params": {}}]}))
    rf = RuleFirstProvider(fb, rp)
    out = json.loads(rf.generate("رتب الملفات", "مخطط\n" + CONTEXT_MARKER + "\na.txt"))
    assert out["steps"][0]["action_type"] == "create_dir" and fb.call_history == [] and rf.rule_hits == 1
    rf.generate("حلّل هذا الوضع المعقد", "مخطط\n" + CONTEXT_MARKER + "\na.txt")
    assert len(fb.call_history) == 1 and rf.fallback_calls == 1
    assert parse_context("x") == [] and parse_context(CONTEXT_MARKER + "\n(فارغ)") == []

    # 10. أسماء ويندوز المحجوزة والأسماء المنتهية بنقطة/مسافة مرفوضة في أي أداة ملفات
    from assistant.tools.files import FileManager, PathOutOfBoundsError
    with tempfile.TemporaryDirectory() as tmp:
        fm = FileManager(pathlib.Path(tmp).resolve())
        for bad in ("CON", "nul", "COM1.txt", "a/LPT3", "name.", "name "):
            try:
                fm.create_dir(bad)
                raise AssertionError(f"كان يجب رفض {bad!r}")
            except PathOutOfBoundsError:
                pass
        fm.create_dir("console")  # اسم عادي يحوي CON لا يُرفض
        assert (pathlib.Path(tmp) / "console").is_dir()

    print("PASS: test_rules completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_rules", file=sys.stderr)
    sys.exit(1)
