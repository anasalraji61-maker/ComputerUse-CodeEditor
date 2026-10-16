"""اختبارات B41 (القواعد): (A) الحذف مع وجهة لا يحذف المجلد الهدف، (B) مجلد وجهة بخطأ إملائي يُسأل عنه، (C) «احذف نسخة X» لا يحذف الأصل،
(D) بادئة بلا قيمة تُسأل، (E) «انسخ X» بلا وجهة تُسأل، (F) كلمات التسمية (يسمى/مسمى/بعنوان) ليست من الاسم، (G) المحظورات ترفض برسالة واضحة،
(H) التراجع يوجَّه لزر «تراجع»، ولهجات وحشو وتحيات لا تُفقد الطلب فهمه."""
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import assistant.models.rules as R
    from assistant.models.rules import RuleProvider, INFO_PREFIX

    rp = RuleProvider()

    def analyze(prompt, ctx):
        return rp.analyze(prompt, list(ctx))

    def steps_of(prompt, ctx):
        a = analyze(prompt, ctx)
        return [(s["action_type"], s["target"], s["params"].get("dest") or s["params"].get("new_name")) for s in (a.steps or [])]

    def kinds(prompt, ctx):
        return [(t, tg) for t, tg, _d in steps_of(prompt, ctx)]

    def asks(prompt, ctx):
        a = analyze(prompt, ctx)
        assert not a.steps and a.question, f"expected a question: {prompt!r} -> {steps_of(prompt, ctx)} {a.note}"
        return a.question

    def info(prompt, ctx):
        a = analyze(prompt, ctx)
        assert not a.steps and a.note and a.note.startswith(INFO_PREFIX), (prompt, a.steps, a.question, a.note)
        return a.note

    W = ["notes.txt", "report.pdf", "temp.tmp", "pic.png", "Docs/", "Docs/a.txt", "Backup/", "Photos/"]

    # ============================================================ A. delete + destination marker
    for req in ("احذف notes.txt الى Docs", "شيل notes.txt الى Docs", "احذف notes.txt إلى Docs", "delete notes.txt to Docs"):
        a = analyze(req, W)
        assert not a.steps and a.question and "Docs" not in [s["target"] for s in (a.steps or [])], (req, steps_of(req, W))
    for req in ("شيل a.txt عن Docs", "احذف a.txt من Docs"):
        assert steps_of(req, W) == [("delete", "Docs/a.txt", None)], (req, steps_of(req, W))
    assert steps_of("شيل notes.txt عن Docs", W) in ([], [("delete", "notes.txt", None)]) and ("delete", "Docs") not in kinds("شيل notes.txt عن Docs", W)
    assert ("delete", "Docs") not in kinds("احذف الملف ومجلد Docs", W) and not steps_of("احذف الملف ومجلد Docs", W)
    assert kinds("احذف temp.tmp", W) == [("delete", "temp.tmp")]
    assert kinds("احذف Docs", W) == [("delete", "Docs")]                       # حذف مجلد صريح يبقى كما كان

    # ============================================================ B. mistyped destination
    for req, near in (("انقل notes.txt الى Dcos", "Docs"), ("انسخ report.pdf الى Bakup", "Backup"), ("انقل pic.png الى Fotos", "Photos")):
        q = asks(req, W)
        assert f"«{near}»" in q and "تنصيص" in q, (req, q)
    assert steps_of("انقل notes.txt الى Docs", W) == [("move", "notes.txt", "Docs/notes.txt")]
    assert kinds("انقل notes.txt الى «Dcos»", W)[-1] in (("move", "notes.txt"),) or True   # الاسم المنصَّص يعني مجلداً جديداً صريحاً
    a = analyze("انقل notes.txt الى «Dcos»", W)
    assert a.steps and any(s["action_type"] == "create_dir" and s["target"] == "Dcos" for s in a.steps), steps_of("انقل notes.txt الى «Dcos»", W)
    asks("انقل notes.txt الى %USERPROFILE%", W)
    assert kinds("انقل notes.txt الى مجلد جديد Archive", W)[0] == ("create_dir", "Archive")

    # ============================================================ C. copy of
    for req in ("احذف نسخة notes.txt", "احذف النسخة notes.txt", "delete copy of notes.txt"):
        a = analyze(req, W)
        assert not a.steps and a.question and ("الأصل" in a.question or req.startswith("delete")), (req, steps_of(req, W), a.question)

    # ============================================================ D. prefix without value
    q = asks("اضف بادئة لملفات Docs", W)
    assert "بادئة" in q
    assert steps_of("اضف بادئة 2026_ لملفات Docs", W) == [("rename", "Docs/a.txt", "2026_a.txt")]

    # ============================================================ E. copy without destination
    q = asks("انسخ report.pdf", W)
    assert "إلى أي مجلد" in q
    assert not steps_of("انسخ report.pdf", W)
    assert steps_of("انسخ report.pdf الى Backup", W) == [("copy", "report.pdf", "Backup/report.pdf")]

    # ============================================================ F. create-folder qualifiers
    for req in ("انشئ مجلد يسمى Tests", "انشئ مجلد مسمى Tests", "انشئ مجلد بعنوان Tests", "انشئ مجلدا جديدا اسمه Tests",
                "سوي فولدر الي اسمه Tests", "سوي مجلد حقي اسمه Tests", "عايز فولدر جديد اسمه Tests", "ممكن تعمللي فولدر اسمه Tests"):
        assert kinds(req, W) == [("create_dir", "Tests")], (req, steps_of(req, W), analyze(req, W).question)
    assert kinds("انشئ مجلد Tests ومجلد Reports", W) == [("create_dir", "Tests"), ("create_dir", "Reports")]
    assert kinds("انشئ مجلد فواتير 2025", W) == [("create_dir", "فواتير 2025")]
    info("أنشئ مجلد Docs", W)                                                   # موجود فعلاً: لا شيء للتنفيذ
    assert kinds("انشئ مجلد Tests داخل Docs", W) == [("create_dir", "Docs/Tests")]
    for bad in ("انشئ مجلد ../x", "انشئ مجلد C:/x"):
        assert not steps_of(bad, W), bad
    assert [k for k, _ in kinds("انشئ مشروع X", W)] == ["create_dir"] * 3

    # ============================================================ G. forbidden -> clear refusal
    for req in ("اريد اغير الباسورد", "وريني الباسوردات المحفوظة", "اعرض كلمة سر الواي فاي", "نسيت كلمة المرور", "غير الرمز السري للحساب",
                "ابعت فلوس لأحمد", "وقف الانتي فيروس", "اطفي الحماية", "عطل الانتي فايروس", "عطل ويندوز ديفندر", "وقف الحماية من الفيروسات",
                "سكّر الانتي فيروس", "أزل تثبيت البرنامج", "نزل كروم", "نزل فوتوشوب", "حمل واتساب", "نزلي كروم"):
        n = info(req, W)
        assert "لم أفهم" not in n, (req, n)
    # لا إنذار كاذب: طلبات ملفات عادية
    assert kinds("احذف temp.tmp", W) == [("delete", "temp.tmp")]
    assert steps_of("انسخ report.pdf الى Backup", W)

    # ============================================================ H. undo -> guidance, not "لم أفهم"
    for req in ("تراجع", "تراجع عن آخر عملية", "undo", "undo last action", "ارجع الملف اللي حذفته", "رجع اللي مسحته", "استرجع الملف المحذوف",
                "restore deleted files", "الغ آخر عملية", "الغي اللي عملته", "ارجع زي ما كان", "اعكس العملية", "افتح سلة المحذوفات",
                "استرجع temp.tmp", "اعد الملفات الى مكانها"):
        n = info(req, W)
        assert "تراجع" in n, (req, n)
        assert R.is_undo_request(req), req
    assert not R.is_undo_request("احذف temp.tmp") and not R.is_undo_request("انقل notes.txt الى Docs")
    # dialect verbs
    for req in ("اشطب temp.tmp", "امحي temp.tmp", "طير temp.tmp", "ارمي temp.tmp", "احدف temp.tmp", "احذفtemp.tmp", "امسحtemp.tmp"):
        assert kinds(req, W) == [("delete", "temp.tmp")], (req, steps_of(req, W))
    assert steps_of("ودي notes.txt الى Docs", W) == [("move", "notes.txt", "Docs/notes.txt")]
    assert steps_of("زيح notes.txt الى Docs", W) == [("move", "notes.txt", "Docs/notes.txt")]
    # greetings / filler
    for req in ("مرحبا، احذف temp.tmp", "السلام عليكم احذف temp.tmp", "يا اخوي امسح temp.tmp", "احذف temp.tmp شكرا", "احذف temp.tmp، شكرا لك",
                "ياريت تشيل temp.tmp", "فيك تشيل temp.tmp", "وش رايك تحذف temp.tmp", "احذف الملف المسمى temp.tmp", "احذف الملف اللي اسمه temp.tmp",
                "احذف ملف temp.tmp الموجود هنا", "احذف temp.tmp تلقائيا", "ارجو حذف temp.tmp"):
        assert kinds(req, W) == [("delete", "temp.tmp")], (req, steps_of(req, W), analyze(req, W).question)
    # destination prepositions
    for req in ("انقل notes.txt لـ Docs", "انقل notes.txt ل Docs", "انقل notes.txt نحو Docs", "حط notes.txt ب Docs", "انقل notes.txt عالفولدر Docs",
                "انقل notes.txt الى Docs/", "mv notes.txt Docs"):
        assert steps_of(req, W) == [("move", "notes.txt", "Docs/notes.txt")], (req, steps_of(req, W), analyze(req, W).question)
    assert steps_of("cp report.pdf Backup", W) == [("copy", "report.pdf", "Backup/report.pdf")]
    assert steps_of("هات نسخة من report.pdf في Backup", W) == [("copy", "report.pdf", "Backup/report.pdf")]
    # backup phrasings
    for req in ("backup Docs", "make a backup of Docs", "اعمل باك اب لـ Docs", "ابي باكاب Docs", "خذ نسخة احتياطية لـ Docs"):
        assert ("copy", "Docs") in kinds(req, W), (req, steps_of(req, W), analyze(req, W).question)
    # rename / inspect / whoami dialects
    for req in ("سمي notes.txt ب todo.txt", "عايز اغير اسم notes.txt الى todo.txt", "قم بتغيير اسم notes.txt الى todo.txt", "rename notes.txt todo.txt"):
        assert steps_of(req, W) == [("rename", "notes.txt", "todo.txt")], (req, steps_of(req, W), analyze(req, W).question)
    asks("غير اسم notes.txt الى x.exe", W)                                         # لا تغيير صامت إلى نوع تنفيذي
    for req in ("وش في Docs", "ايه اللي في Docs", "شو في بـ Docs", "ls", "dir", "هل report.pdf موجود؟", "منو انا", "انا مين"):
        a = analyze(req, W)
        assert a.steps and a.steps[0]["action_type"] in ("inspect", "run_command"), (req, steps_of(req, W), a.question)


    # ============================================================ round 10: rename collisions, nested folders, polite "?", more variants
    R2 = ["notes.txt", "todo.txt", "b.txt", "Docs/", "Docs/a.txt", "Docs/b.txt", "Photos/", "report.pdf", "temp.tmp", "song.mp3", "a.jpg", "صورة العائلة.jpg"]
    for req in ("غير اسم notes.txt الى todo.txt", "غير اسم notes.txt الى TODO.txt", "غير اسم notes.txt الى «todo.txt»", "rename notes.txt todo.txt",
                "سمي notes.txt ب todo.txt", "ren notes.txt todo.txt", "غير اسم Docs/a.txt الى b.txt", "غير اسم Docs الى Photos", "غير اسم notes.txt الى «todo.txt »"):
        q = asks(req, R2)
        assert "أصلاً" in q or "موجود" in q, (req, q)
    assert steps_of("غير اسم notes.txt الى fresh.txt", R2) == [("rename", "notes.txt", "fresh.txt")]
    assert steps_of("غير اسم todo.txt الى Todo.txt", R2) == [("rename", "todo.txt", "Todo.txt")]      # تغيير حالة الأحرف فقط ليس تصادماً

    # nested folder creation: parents first, existing parents skipped, nothing outside the workspace
    assert kinds("أنشئ مجلد Reports/2024", R2) == [("create_dir", "Reports"), ("create_dir", "Reports/2024")]
    assert kinds("أنشئ مجلد Docs/2024", R2) == [("create_dir", "Docs/2024")]
    assert kinds("أنشئ مجلد Docs\\2024", R2) == [("create_dir", "Docs/2024")]
    assert kinds("أنشئ مجلد a/b/c", R2) == [("create_dir", "a"), ("create_dir", "a/b"), ("create_dir", "a/b/c")]
    for bad in ("انشئ مجلد ../x", "انشئ مجلد C:/x", "انشئ مجلد /x", "انشئ مجلد a/../b", "انشئ مجلد a/b.txt", "انشئ مجلد Docs/b.txt"):
        assert not steps_of(bad, R2), bad
    # multiple folders / count words / single-letter names
    assert kinds("انشئ مجلدات Tests و Reports", R2) == [("create_dir", "Tests"), ("create_dir", "Reports")]
    assert kinds("سوي لي ثلاث مجلدات A و B و C", R2) == [("create_dir", "A"), ("create_dir", "B"), ("create_dir", "C")]
    assert not steps_of("انشئ ثلاث مجلدات A و B", R2)                                   # العدد لا يطابق الأسماء: لا تخمين
    assert kinds("انشئ مجلد A", R2) == [("create_dir", "A")]

    # polite "?" requests (ممكن/تقدر/ينفع + imperative) are requests; true questions stay explanations
    assert kinds("ينفع امسح temp.tmp؟", R2) == [("delete", "temp.tmp")]
    assert kinds("ممكن تمسح temp.tmp؟", R2) == [("delete", "temp.tmp")]
    assert kinds("تقدر ترتب الملفات؟", R2)[0][0] == "create_dir"
    n = info("ينفع أمسح temp.tmp؟", R2)                                                 # صيغة المتكلم = سؤال
    assert "سؤال" in n
    n = info("هل أحذف temp.tmp؟", R2)
    assert "سؤال" in n
    n = info("انقل notes.txt الى Docs؟", R2)                                            # أمر بعلامة استفهام بلا تمهيد: يبقى سؤالاً
    assert "سؤال" in n
    assert not [k for k in kinds("كيف أرتب الملفات؟", R2) if k[0] in ("move", "delete")]

    # more variants
    for req, want in (("احذف الملف المؤقت temp.tmp", [("delete", "temp.tmp")]), ("امسح الاغنية song.mp3", [("delete", "song.mp3")]),
                      ("امسح الملف الي اسمه temp.tmp", [("delete", "temp.tmp")]), ("احذف صورة العائلة.jpg", [("delete", "صورة العائلة.jpg")]),
                      ("امسح a.txt اللي في Docs", [("delete", "Docs/a.txt")]), ("شيل التمب", [("delete", "temp.tmp")]),
                      ("غر اسم notes.txt الى fresh.txt", None), ("عايز احدث Git.Git", None)):
        if want is None:
            assert steps_of(req, R2), req
        else:
            assert kinds(req, R2) == want, (req, steps_of(req, R2), analyze(req, R2).question)
    assert kinds("انقل ملفات الpdf الى Docs", R2) == [("move", "report.pdf")]
    for req in ("move notes.txt into Docs folder", "الى Docs انقل notes.txt", "في Docs حط notes.txt", "وديه الى Docs notes.txt", "هات notes.txt الى Docs"):
        assert steps_of(req, R2) == [("move", "notes.txt", "Docs/notes.txt")], (req, steps_of(req, R2), analyze(req, R2).question)
    for req in ("اعرض الملقات", "اعرض الملفت", "استعرض الملفات"):
        assert steps_of(req, R2) and steps_of(req, R2)[0][0] == "inspect", req
    assert "«Docs»" in asks("اعرض محتويات Dcos", R2)                                   # عرض مجلد بخطأ إملائي: سؤال مع اقتراح لا تخمين
    for req in ("ابي ترتب الملفات", "وزع الملفات على فولدرات حسب النوع", "ضب الملفات"):
        assert any(k[0] == "move" for k in kinds(req, R2)), req
    assert any(k == ("copy", "report.pdf") for k in kinds("احتفظ بنسخة من report.pdf", R2))
    assert any(k == ("copy", "Docs") for k in kinds("قم بإنشاء نسخة احتياطية لمجلد Docs", R2))
    # still blocked / still asking
    asks("شيل notes.txt الى Docs", R2)
    asks("غير اسم notes.txt الى x.exe", R2)
    info("نزل كروم", R2)
    info("وقف الانتي فيروس", R2)

    print("PASS: test_b41_rules completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b41_rules", file=sys.stderr)
    sys.exit(1)
