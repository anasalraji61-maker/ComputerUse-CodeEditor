"""اختبارات B39 (القواعد): «اعمل نسخة احتياطية و<عملية>» لا تُسقط إحداهما بصمت، «تخلص من ملفات x» مع مجلد اسمه ملفات، الاسم المنصَّص يطابق تماماً
في التسمية والحذف، تكرار الترتيب بعد اقتطاع أبناء المجلدات (ملفات الجذر ظاهرة كلها)، «احذف مكررات» بلا أداة التعريف مع مجلد بهذا الاسم،
واستعمال مساعد الجمع المشترك assistant/arabic.py."""
import itertools
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import assistant.models.rules as R
    from assistant.arabic import n_units
    from assistant.models.rules import RuleProvider, INFO_PREFIX

    rp = RuleProvider()

    def analyze(prompt, ctx, provider=None):
        return (provider or rp).analyze(prompt, list(ctx))

    def steps_of(prompt, ctx, provider=None):
        a = analyze(prompt, ctx, provider)
        return [(s["action_type"], s["target"], s["params"].get("dest") or s["params"].get("new_name")) for s in (a.steps or [])]

    def asks(prompt, ctx, provider=None):
        a = analyze(prompt, ctx, provider)
        assert not a.steps and a.question, f"كان يجب السؤال: {prompt!r} ← {steps_of(prompt, ctx, provider)} {a.note}"
        return a.question

    def info(prompt, ctx, provider=None):
        a = analyze(prompt, ctx, provider)
        assert not a.steps and a.note and a.note.startswith(INFO_PREFIX), (prompt, a.steps, a.question, a.note)
        return a.note

    # ================================================================== 1. عمليتان بالواو: لا إسقاط صامت
    W = ["a.txt", "b.txt", "report.pdf", "photo.jpg", "pic.png", "temp.tmp", "Docs/", "Docs/n.txt", "Images/", "Archive/"]
    for req in ("اعمل نسخة احتياطية واحذف a.txt", "اعمل backup واحذف a.txt", "اعمل backup وانقل a.txt إلى Archive", "خذ نسخة احتياطية وانقل b.txt إلى Docs",
                "اعمل نسخة احتياطية، واحذف a.txt", "احذف a.txt واعمل نسخة احتياطية", "اعمل نسخة احتياطية ونظف الملفات المؤقتة",
                "اعمل نسخة احتياطية و رتب الملفات حسب النوع", "اعمل نسخة احتياطية وأنشئ مجلد Reports", "اعمل نسخة احتياطية واعمل backup",
                "سوي نسخة احتياطية واحذف a.txt", "اعمل نسخة احتياطية وأعد تسمية a.txt إلى z.txt", "اعمل نسخة احتياطية وافحص a.txt"):
        q = asks(req, W)
        assert "نسخة احتياطية" in q and "أولاً" in q, (req, q)
    # نسخ احتياطي وحده أو اسم وجهة «نسخة احتياطية» لا يتأثران
    assert steps_of("اعمل نسخة احتياطية من Docs", W)[-1][0] == "copy"
    assert steps_of("انسخ a.txt إلى Archive", W) == [("copy", "a.txt", "Archive/a.txt")]
    # أنماط عامة: أي عمليتين بواو/فاصلة لا تُخطَّط إحداهما وحدها
    OPS = ["احذف a.txt", "انقل b.txt إلى Docs", "انسخ report.pdf إلى Archive", "غير اسم photo.jpg إلى p2.jpg", "أنشئ مجلد Reports",
           "اعمل نسخة احتياطية من Docs", "اعمل نسخة احتياطية", "رتب الملفات حسب النوع", "افحص a.txt", "حدّث برنامج Git.Git", "احذف المكررات",
           "من أنا", "أضف بادئة new_ لملفات Docs", "احذف البادئة old_", "اعرض ملفات pdf", "ضع pic.png في Images", "نظف الملفات المؤقتة"]
    for a_, b_ in itertools.permutations(OPS, 2):
        for joiner in (" و", "و", "، و", " & "):
            req = a_ + joiner + ("" if joiner.endswith(" ") else " ") + b_ if joiner != "و" else a_ + " و" + b_
            res = analyze(req, W)
            assert not res.steps, (req, [(s["action_type"], s["target"]) for s in res.steps])
    # «اعرض الهوية» أمر واحد، و«من أنا وافحص x» اثنان
    assert [s["action_type"] for s in analyze("اعرض الهوية", W).steps] == ["run_command"]
    assert [s["action_type"] for s in analyze("اعرض الهوية whoami مرة أخرى", W).steps] == ["run_command"]
    assert not analyze("من أنا وافحص a.txt", W).steps and not analyze("من أنا واعرض ملفات pdf", W).steps

    # ================================================================== 2. «تخلص من ملفات x» مع مجلد اسمه ملفات
    W2 = ["ملفات/", "ملفات/k.txt", "report.pdf", "a.txt"]
    q = asks("تخلص من ملفات report.pdf", W2)
    assert "ملفات" in q and "تنصيص" in q, q
    q2 = asks("احذف ملفات report.pdf", W2)
    assert "ملفات" in q2
    assert steps_of("تخلص من «report.pdf»", W2) == [("delete", "report.pdf", None)]
    assert steps_of("تخلص من report.pdf", W2) == [("delete", "report.pdf", None)]
    assert steps_of("احذف مجلد ملفات", W2) == [("delete", "ملفات", None)]

    # ================================================================== 3. الاسم المنصَّص يطابق تماماً
    W3 = ["x.pdf", "Docs/", "Docs/(1).pdf", "Docs/a.pdf", "a b.pdf"]
    for req in ("غير اسم 'old x.pdf' إلى new.pdf", "غير اسم «old x.pdf» إلى new.pdf", "غير اسم «report (1).pdf» إلى new.pdf", "غير اسم 'zzz.pdf' إلى new.pdf"):
        q = asks(req, W3)
        assert "لم أجد" in q, (req, q)
    assert steps_of("غير اسم «x.pdf» إلى new.pdf", W3) == [("rename", "x.pdf", "new.pdf")]
    assert steps_of("غير اسم «a b.pdf» إلى c.pdf", W3) == [("rename", "a b.pdf", "c.pdf")]
    assert steps_of("غير اسم 'a.pdf' إلى new.pdf", W3) == [("rename", "Docs/a.pdf", "new.pdf")]
    assert steps_of("غير اسم «Docs/a.pdf» إلى new.pdf", W3) == [("rename", "Docs/a.pdf", "new.pdf")]
    # بعد التنفيذ: إعادة الطلب نفسه لا تغيّر ملفاً آخر
    W3b = ["new.pdf", "Docs/", "Docs/(1).pdf"]
    asks("غير اسم «x.pdf» إلى new.pdf", W3b)
    for req in ("احذف «report (1).pdf»", "انسخ «old x.pdf» إلى Docs", "افحص «old x.pdf»", "انقل «old x.pdf» إلى Docs"):
        q = asks(req, W3)
        assert "لم أجد" in q and "«" in q and "old" in q or "report (1)" in q, (req, q)

    # ================================================================== 4. تكرار الترتيب بعد اقتطاع أبناء المجلدات
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp).resolve() / "w4"
        ws.mkdir()
        for d in ("صور", "مستندات"):
            (ws / d).mkdir()
            for i in range(30):
                (ws / d / f"f{i:02d}.{'jpg' if d == 'صور' else 'txt'}").write_text("x", encoding="utf-8")
        old = R._FULL_SCAN_LIMIT
        try:
            R._FULL_SCAN_LIMIT = 40   # أقل من مجموع الأبناء (62) فالقائمة مقتطعة، لكن مستوى الجذر ظاهر كله
            prov = RuleProvider(workspace=ws)
            ctx = ["… (القائمة مقتطعة)"]
            e = prov._full_ents()
            assert e.partial and e.root_complete
            n = info("رتب الملفات حسب النوع", ctx, prov)
            assert "لا توجد ملفات مبعثرة" in n, n
            n = info("رتب الملفات حسب الشهر", ctx, prov)
            assert "لا توجد ملفات" in n, n
            # ملف جذر جديد يُرتَّب بلا تحذير «ملفات ظاهرة فقط»
            (ws / "new.pdf").write_text("p", encoding="utf-8")
            a = analyze("رتب الملفات حسب النوع", ctx, prov)
            assert [(s["action_type"], s["target"]) for s in a.steps if s["action_type"] == "move"] == [("move", "new.pdf")], (a.steps, a.note, a.question)
            assert not a.note or "مقتطعة" not in a.note, a.note
            # جذر أكبر من الحد: السؤال القديم (لا نعمّم)
            R._FULL_SCAN_LIMIT = 1
            prov2 = RuleProvider(workspace=ws)
            e2 = prov2._full_ents()
            assert e2.partial and not e2.root_complete
            a2 = analyze("رتب الملفات حسب النوع", ctx, prov2)
            assert (a2.question and "مقتطعة" in a2.question) or (a2.steps and a2.note and "مقتطعة" in a2.note), (a2.question, a2.note)
        finally:
            R._FULL_SCAN_LIMIT = old
    # قائمة النموذج المقتطعة وحدها (بلا مسح كامل) تبقى ناقصة الجذر
    q = asks("رتب الملفات حسب النوع", ["… (القائمة مقتطعة)"])
    assert "مقتطعة" in q

    # ================================================================== 5. «احذف مكررات» بلا أداة التعريف مع مجلد بهذا الاسم
    W5 = ["مكررات/", "مكررات/u.txt", "a.txt", "b.txt"]
    for req in ("احذف مكررات", "اعرض مكررات"):
        q = asks(req, W5)
        assert "مكررات" in q and "تنصيص" in q, (req, q)
    assert steps_of("احذف مجلد مكررات", W5) == [("delete", "مكررات", None)]
    assert steps_of("احذف «مكررات»", W5) == [("delete", "مكررات", None)]
    assert steps_of("انقل a.txt إلى مكررات", W5) == [("move", "a.txt", "مكررات/a.txt")]
    assert "تنصيص" not in (analyze("احذف المكررات", W5).question or "")   # المعنى الأصلي (ملفات مكررة المحتوى) محفوظ: لا التباس مع المجلد

    # ================================================================== 6. مساعد الجمع المشترك
    assert R._n is n_units and R._UNITS
    for n, kind, want in ((1, "ملف", "ملف واحد"), (2, "عنصر", "عنصران"), (5, "عملية", "5 عمليات"), (12, "خطوة", "12 خطوة"), (3, "مجموعة", "3 مجموعات")):
        assert R._n(n, kind) == want == n_units(n, kind), (n, kind)

    print("PASS: test_b39_rules completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b39_rules", file=sys.stderr)
    sys.exit(1)
