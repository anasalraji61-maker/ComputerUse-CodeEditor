"""اختبارات B17: صياغات عربية طبيعية (إعادة تسمية عنصر، إضافة بادئة، مرادفات الفئات، وجهات عامة)
وبوابة المزامنة: tools/run_tests.py يعيد رمز خروج 1 عند الفشل ويبقى الفشل قائماً حتى يتغير الكود."""
import importlib.util
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.rules import RuleProvider
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger

    rp = RuleProvider()

    def plan(text, ctx):
        a = rp.analyze(text, ctx)
        return [(s["action_type"], s["target"], s["params"].get("dest") or s["params"].get("new_name")) for s in a.steps or []], a

    def asks(text, ctx):
        steps, a = plan(text, ctx)
        assert not steps and (a.question or a.note), (text, steps)

    CTX = ["cv.docx", "notes.txt", "Docs/", "Docs/a.txt", "a.txt", "readme", "Projects/", "song.mp3", "clip.mp4", "p.jpg"]

    # 1. إعادة تسمية عنصر واحد
    assert plan("غير اسم cv.docx الى resume.docx", CTX)[0] == [("rename", "cv.docx", "resume.docx")]
    assert plan("rename cv.docx to resume.docx", CTX)[0] == [("rename", "cv.docx", "resume.docx")]
    assert plan("اعد تسمية cv.docx الى «سيرتي الذاتية.docx»", CTX)[0] == [("rename", "cv.docx", "سيرتي الذاتية.docx")]
    assert plan("غير اسم Docs/a.txt الى c.txt", CTX)[0] == [("rename", "Docs/a.txt", "c.txt")]
    for t in ("غير اسم cv.docx الى resume", "غير اسم cv.docx الى notes.txt", "غير اسم cv.docx الى cv.docx", "غير اسم ghost.txt الى x.txt",
              "غير اسم cv.docx", "غير اسم cv.docx الى my resume.docx", "اعد تسمية cv.docx الى con.docx",
              "غير اسم cv.docx الى ../x.docx", "غير اسم cv.docx و notes.txt الى x.txt"):
        asks(t, CTX)

    # 2. إضافة بادئة بصيغ أخرى
    s, _ = plan("أضف بادئة 2026_ لأسماء الملفات", CTX)
    assert s and all(x[2].startswith("2026_") for x in s) and len(s) >= 5, s
    s, _ = plan("اضف تاريخ اليوم لاسماء الملفات", CTX)
    assert s and all(x[2].startswith("20") and "_" in x[2] for x in s), s

    # 3. مرادفات الفئات وضمائر الترتيب
    s, _ = plan("ضع الفيديو والصوت في مجلد وسائط", CTX)
    assert [x[1] for x in s if x[0] == "move"] == ["clip.mp4", "song.mp3"], s
    assert plan("احذف الموسيقى", CTX)[0] == [("delete", "song.mp3", None)]
    assert plan("رتبهم حسب النوع", CTX)[0] and plan("رتب ملفاتي حسب النوع لو سمحت", CTX)[0]

    # 4. وجهات عامة ليست أسماء مجلدات
    for t in ("اجمع الصور في مجلد واحد", "انقل الصور الى مجلد جديد", "انقل الصور الى هنا"):
        asks(t, CTX)
    s, _ = plan("ابغى انقل الصور في مجلد اسمه صور", CTX)
    assert s[0] == ("create_dir", "صور", None), s
    assert plan("اعمل نسخة احتياطية من cv.docx", CTX)[0] == [("create_dir", "نسخة_احتياطية", None), ("copy", "cv.docx", "نسخة_احتياطية/cv.docx")]
    assert plan("what is in Projects", CTX)[0] == [("inspect", "Projects", None)]

    # 5. تنفيذ حقيقي لإعادة التسمية الفردية مع التراجع
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        root = base / "ws"
        root.mkdir()
        (root / "cv.docx").write_text("cv", encoding="utf-8")
        (root / "other.txt").write_text("o", encoding="utf-8")
        led = SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500)
        r = TaskRunner(root, RuleProvider(), led)
        assert r.run("r1", "غير اسم cv.docx الى resume.docx").success
        assert (root / "resume.docx").read_text(encoding="utf-8") == "cv" and not (root / "cv.docx").exists()
        assert r.run("r2", "غير اسم resume.docx الى RESUME.DOCX").success
        assert (root / "RESUME.DOCX").exists()
        assert r.undo_task("r2") >= 1 and (root / "resume.docx").exists()

    # 6. بوابة المزامنة: رمز الخروج 1 عند الفشل ويستمر حتى يتغير الكود
    spec = importlib.util.spec_from_file_location("rt_gate", ROOT / "tools" / "run_tests.py")
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)
    with tempfile.TemporaryDirectory() as tmp:
        fake = pathlib.Path(tmp).resolve()
        (fake / "docs").mkdir()
        (fake / "tools").mkdir()
        good = fake / "test_good.py"
        good.write_text("import sys\nprint('PASS: x')\nsys.exit(0)\n", encoding="utf-8")
        bad = fake / "test_bad.py"
        bad.write_text("import sys\nprint('FAIL: x')\nsys.exit(1)\n", encoding="utf-8")
        rt.ROOT, rt.OUT, rt.STATE = fake, fake / "docs" / "TEST_RESULTS.md", fake / "tools" / ".last_tested"
        assert rt.main() == 1                      # فشل فعلي
        assert "فشل: 1" in rt.OUT.read_text(encoding="utf-8")
        stamp = rt.OUT.stat().st_mtime_ns
        assert rt.main() == 1                      # لم يتغير الكود: يبقى الفشل بلا إعادة تشغيل
        assert rt.OUT.stat().st_mtime_ns == stamp
        bad.write_text("import sys\nprint('PASS: x')\nsys.exit(0)\n", encoding="utf-8")
        assert rt.main() == 0 and "فشل: 0" in rt.OUT.read_text(encoding="utf-8")
        assert rt.main() == 0                      # لا تغيير: نجاح مخزَّن
    bat = (ROOT / "tools" / "sync_repo.bat").read_bytes()
    assert b"\r\n" in bat and b"errorlevel 1" in bat and b"code not pushed" in bat

    print("PASS: test_b17 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b17", file=sys.stderr)
    sys.exit(1)
