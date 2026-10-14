"""اختبارات B16: ثغرات المراجعة الرابعة في فهم الطلبات (استثناءات، أسماء جزئية، نطاق، وجهات، إنشاء، مسارات ويندوز)
+ اختبار خصائص عشوائي: لا استثناءات، ولا تخطيط يمس عنصراً غير موجود في القائمة."""
import random
import sys
import time
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.rules import RuleProvider

    rp = RuleProvider()
    BASE = ["a.txt", "b.txt", "report.pdf", "photo.jpg", "readme", "data.csv", "Docs/", "Docs/a.txt", "Docs/b.pdf", "Docs/Sub/",
            "Docs/Sub/a.txt", "Old/", "Old/a.txt", "Project/", "Project/src/", "Project/src/main.py", "Project/src/util.py"]
    CTX2 = BASE + ["notes.txt", "2025.pdf", "2024/", "report 2024.pdf", "صور/", "مجلد جديد/", "Folder 1/", "نسخة_احتياطية"]

    def plan(text, ctx=None):
        a = rp.analyze(text, ctx if ctx is not None else BASE)
        return [(s["action_type"], s["target"], s["params"].get("dest") or s["params"].get("new_name")) for s in a.steps or []], a

    def asks(text, ctx=None):
        steps, a = plan(text, ctx)
        assert not steps and (a.question or a.note), (text, steps)

    def targets(text, ctx=None):
        steps, _ = plan(text, ctx)
        return [t for _, t, _ in steps]

    # 1. كلمات الاستثناء الإضافية
    for t in ("احذف الملفات ماعدا a.txt", "delete everything and keep a.txt", "delete all files besides a.txt",
              "delete all files other than a.txt", "delete all files apart from a.txt", "احذف الكل دون a.txt",
              "احذف الكل ولكن a.txt", "احذف الكل لكن ابق a.txt", "احذف الكل وبدون a.txt", "رتب الملفات ماعدا a.txt",
              "احذف الكل واحتفظ بـ a.txt", "delete a.txt only"):
        asks(t)

    # 2. كلمات متبقية غير مفهومة ← سؤال لا اختيار جزئي
    for t in ("delete old files", "delete old a.txt", "احذف my notes.txt", "احذف تقرير 2025.pdf", "احذف صور السفر",
              "انقل صور العائلة الى Docs", "احذف report 2024", "احذف 3 ملفات"):
        asks(t, CTX2)

    # 3. الفئة لا تُطابَق كبادئة ولا مع _ أو فاصلة عربية
    for t in ("احذف صور_العائلة", "احذف مستندات_قديمه", "انقل مستندات العمل الى Docs", "احذف a،txt"):
        asks(t)
    t2 = targets("احذف الصور والمستندات", CTX2)
    assert "photo.jpg" in t2 and "a.txt" in t2 and "data.csv" not in t2, t2  # اتحاد فئتين

    # 4. نطاق باسم مجرّد لمجلد متداخل
    assert targets("احذف main.py من src") == ["Project/src/main.py"]
    assert targets("انقل main.py من src الى Docs") == ["Project/src/main.py"]
    assert targets("انسخ main.py من src الى Old") == ["Project/src/main.py"]
    assert targets("احذف a.txt من Docs") == ["Docs/a.txt"]   # الأبناء المباشرون أولاً (Docs/Sub/a.txt لا يلتبس)

    # 5. إعادة التسمية لا تعمّم عند وجود اختيار
    for t in ("rename a.txt prefix x_", "اعد تسميه ملفات pdf بادئه x_", "اعد تسميه الصور بادئه x_",
              "اعد تسميه a.txt و b.txt بادئه x_"):
        asks(t)
    assert targets("اعد تسمية ملفات Docs بادئة x_") == ["Docs/a.txt", "Docs/b.pdf"]
    assert targets("اعد تسميه ملفات src بادئه x_") == ["Project/src/main.py", "Project/src/util.py"]
    steps, _ = plan('اعد تسمية ملفات Docs بادئة "my prefix_"')
    assert steps and steps[0][2] == "my prefix_a.txt", steps

    # 6. ترتيب: معيار واحد فقط وبلا نطاق
    for t in ("رتب الصور", "رتب Docs", "رتب ملفات Docs", "رتب الملفات في Docs", "رتب حسب الاسم", "sort by name",
              "رتب الملفات حسب النوع والتاريخ"):
        asks(t)
    assert plan("رتب الملفات حسب النوع")[0]

    # 7. إنشاء: تنصيص، كلمات الاسم، حماية
    for t, want in (('انشئ مجلد "My Folder"', "My Folder"), ('انشئ مجلد باسم "صور العائلة"', "صور العائلة"),
                    ("create a folder called Test", "Test"), ("create folder named Test", "Test"),
                    ("انشئ مجلد بإسم Test", "Test"), ("انشئ مجلد باسم: Test", "Test"), ("أنشئ مجلد Reports", "Reports")):
        steps, _ = plan(t)
        assert steps == [("create_dir", want, None)], (t, steps)
    for t in ("انشئ مجلد Test/sub", "انشئ مجلد b.txt", "انشئ مجلد جديد", "انشئ مجلد Test وضع a.txt فيه", "create folder AUX",
              "انشئ مجلد readme"):
        asks(t)
    assert plan("أنشئ مشروع my_app")[0][0][1] == "my_app/src"
    # مجلد اسمه Project لا يخفي كلمة project
    assert plan("create project Demo", BASE)[0][0][1] == "Demo/src"

    # 8. وجهة مجلد موجود باسم يبدأ بـ«مجلد»/Folder
    assert plan("انقل a.txt الى مجلد جديد", CTX2)[0] == [("move", "a.txt", "مجلد جديد/a.txt")]
    assert plan("move a.txt to Folder 1", CTX2)[0] == [("move", "a.txt", "Folder 1/a.txt")]

    # 9. وجهات تفشل عند التنفيذ ← سؤال
    for t in ("انقل b.txt الى CON", "انقل b.txt الى nul", "create folder com1", "انسخ b.txt الى com1"):
        asks(t)
    asks("انسخ b.txt", CTX2)   # ملف باسم المجلد الافتراضي

    # 10. وجهة تشبه اسم ملف
    for t in ("copy a.txt to a.txt.bak", "انسخ a.txt الى a_copy.txt", "انقل b.txt الى archive.zip"):
        asks(t)

    # 11. مسارات ويندوز
    assert targets("احذف Docs\\b.pdf") == ["Docs/b.pdf"]
    assert targets("انقل Docs\\b.pdf الى Old") == ["Docs/b.pdf"]
    assert targets("احذف Project\\src\\main.py") == ["Project/src/main.py"]
    assert targets("احذف ./Docs/b.pdf") == ["Docs/b.pdf"]

    # 13. «ملفات X» لا تعني المجلد X كله
    for t in ("احذف ملفات Docs", "delete Docs files", "انقل ملفات Docs الى Old", "delete old files"):
        asks(t)
    assert targets("احذف مجلد Docs") == ["Docs"]
    assert targets("احذف Docs") == ["Docs"]

    # 15. نحو وتحديث/فحص
    assert targets("ممكن تحذف a.txt") == ["a.txt"]
    for t, pkg in (("update Git.Git.", "Git.Git"), ("update 7zip.7zip", "7zip.7zip"), ("update Notepad++.Notepad++", "Notepad++.Notepad++")):
        assert plan(t)[0] == [("winget_upgrade", pkg, None)], t
    assert plan("ماذا يوجد في Docs")[0] == [("inspect", "Docs", None)]
    assert plan("تحقق من Docs")[0] == [("inspect", "Docs", None)]
    t0 = time.time()
    a = rp.analyze("احذف " + "x" * 30000, BASE)
    assert not a.steps and time.time() - t0 < 1.0   # طلب طويل جداً يُرفض فوراً

    # 12. المكررات لا تعيد نقل مجلد «مكررات» نفسه (يحتاج مجلد عمل حقيقي)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve()
        for f in ("a.txt", "b.txt", "مكررات/c.txt"):
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            (root / f).write_text("same content", encoding="utf-8")
        r2 = RuleProvider(workspace=root)
        a = r2.analyze("انقل المكررات", ["a.txt", "b.txt", "مكررات/", "مكررات/c.txt"])
        moved = [s["target"] for s in a.steps if s["action_type"] == "move"]
        assert moved == ["b.txt"], (moved, a.question)

    # 14. اختبار خصائص: لا استثناءات، ولا مساس بعنصر غير موجود، ولا وجهة موجودة أو محجوزة
    rnd = random.Random(20261014)
    VERBS = ["احذف", "انقل", "انسخ", "delete", "move", "copy", "شيل", "تخلص من", "أعد تسمية", "رتب", "انشئ مجلد", "افحص", "ضع", "حدث"]
    WORDS = ["a.txt", "b.txt", "Docs", "Docs/a.txt", "main.py", "src", "الصور", "pdf", "ملفات", "الى", "من", "في", "Backup", "Old",
             "ثم", "ما", "عدا", "الكل", "كل", "القديمة", "«صور العائلة»", "\"x y\"", "..", "C:\\x", "CON", "بادئة", "x_", "التاريخ",
             "حسب", "النوع", "Git.Git", "مجلد", "files", "ghost.doc", "و", "،", ".", "/", "\\", "§", "…", "\n"]
    ctxs = [BASE, CTX2, [], ["…"], BASE + ["… (القائمة مقتطعة)"], ["a.b/", "a.b/c.d", "weird name (1).txt", "x\u202e.txt"]]
    for _ in range(3000):
        ctx = rnd.choice(ctxs)
        text = " ".join([rnd.choice(VERBS)] + [rnd.choice(WORDS) for _ in range(rnd.randint(0, 6))])
        a = rp.analyze(text, ctx)   # يجب ألا يرفع استثناء
        known = {e.rstrip("/") for e in ctx if not e.startswith("…")}
        for s in a.steps or []:
            act, tgt, params = s["action_type"], s["target"], s["params"]
            assert ".." not in tgt.split("/") and not tgt.startswith("/") and ":" not in tgt, (text, s)
            if act in ("delete", "move", "copy", "rename", "inspect") and tgt != ".":
                assert tgt in known, (text, ctx, s)
            if act in ("move", "copy"):
                dest = params["dest"]
                assert dest.casefold() not in {k.casefold() for k in known}, (text, dest)
            if act == "create_dir" and "/" not in tgt:
                files_only = {e.rstrip("/").casefold() for e in ctx if not e.startswith("…") and not e.endswith("/")}
                assert tgt.casefold() not in files_only, (text, tgt)  # لا مجلد فوق ملف

    print("PASS: test_b16 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b16", file=sys.stderr)
    sys.exit(1)
