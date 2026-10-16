"""اختبارات B40 (القواعد): سقف الخطة التلقائية 400 (= MAX_PLAN_STEPS)، اسم المجلد غير المنصَّص لا يبتلع كلمات الموقع («على سطح المكتب»)،
السهم «->» وكلمة «المجلد» الملتصقة باسم لاتيني، وطبقة «الصيغ» الاحتياطية التي تفهم اللهجة والحشو وترتيب الكلمات والخطأ الإملائي بحرف
لخمس مهام (ترتيب/نسخ/بادئة/مشروع/تحديث) دون أن تُسقط عملية ثانية أو استثناءً أو تفتح باب المحظورات."""
import pathlib
import sys
import tempfile
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
        assert not a.steps and a.question, f"كان يجب السؤال: {prompt!r} ← {steps_of(prompt, ctx)} {a.note}"
        return a.question

    def info(prompt, ctx):
        a = analyze(prompt, ctx)
        assert not a.steps and a.note and a.note.startswith(INFO_PREFIX), (prompt, a.steps, a.question, a.note)
        return a.note

    # ================================================================== 1. سقف الخطة 400
    assert R.MAX_AUTO_STEPS == 400 and R.MAX_AUTO_DELETE_STEPS == 48
    big = [f"f{i:03d}.txt" for i in range(460)]
    a = analyze("رتب الملفات حسب النوع", big)
    assert a.steps and len(a.steps) <= R.MAX_AUTO_STEPS and "بقي" in (a.note or ""), (len(a.steps or []), a.note)
    mid = [f"f{i:03d}.txt" for i in range(300)]
    a = analyze("رتب الملفات حسب النوع", mid)
    assert a.steps and len(a.steps) > 200 and not (a.note or "").count("بقي"), (len(a.steps or []), a.note)

    # ================================================================== 2. D1: الاسم غير المنصَّص لا يبتلع كلمات الموقع
    W = ["a.txt", "b.txt", "report.pdf", "pic.png", "Docs/", "Docs/n.txt", "B/"]
    for req in ("أنشئ مجلد تقارير على سطح المكتب", "أنشئ مجلد تقارير في سطح المكتب", "أنشئ مجلد تقارير عند سطح المكتب", "create folder تقارير on the desktop",
                "اعمل مجلد تقارير على سطح المكتب"):
        a = analyze(req, W)
        assert [(s["action_type"], s["target"]) for s in a.steps] == [("create_dir", "تقارير")], (req, steps_of(req, W), a.question)
        assert "سطح مكتب آخر" in (a.note or ""), (req, a.note)
    assert kinds("أنشئ مجلد فواتير 2025", W) == [("create_dir", "فواتير 2025")]          # الاسم المتعدد الكلمات يبقى كما كان
    assert kinds("أنشئ مجلد Reports", W) == [("create_dir", "Reports")]
    assert kinds("أنشئ مجلد src", W) == [("create_dir", "src")]                          # اسم يطابق مجلداً فرعياً افتراضياً يبقى اسماً

    # ================================================================== 3. D2: السهم وكلمة «المجلد» الملتصقة
    for req in ("انسخ a.txt -> B", "انسخ a.txt -> المجلدB", "انسخ a.txt → B", "انسخ a.txt ==> B", "copy a.txt -> B", "copy a.txt->B",
                "انسخ a.txt إلى المجلدB", "انسخ a.txt الى المجلدB", "انسخ a.txt إلى B"):
        assert steps_of(req, W) == [("copy", "a.txt", "B/a.txt")], (req, steps_of(req, W), asks(req, W) if not steps_of(req, W) else "")
    assert steps_of("انقل b.txt -> Docs", W) == [("move", "b.txt", "Docs/b.txt")]
    # اسم ملف فيه شرطة وسهم لا يُفصل
    assert kinds("احذف a-b.txt", ["a-b.txt", "x.txt"]) == [("delete", "a-b.txt")]

    # ================================================================== 4. صيغ الترتيب (لهجة/حشو/خطأ بحرف)
    SORT = [("sort", "p.jpg", None)]
    L = ["a.txt", "p.jpg", "Docs/"]
    ref = steps_of("رتب الملفات حسب النوع", L)
    assert ref and any(t == "move" for t, _a, _b in ref)
    for req in ("ينبغي ترتيب الملفات بحسب النوع", "رتبلي الفايلات حسب نوعها", "ربت الملفات حسب النوع", "رتبب الملفات حسب النوع", "sort the filse by type",
                "organize files by tyep", "I want my files sorted by type", "can you sort these files by type?", "tidy up my files by type",
                "رتب الملفاتحسب النوع", "رتب الملفات حسب النو", "رتب الملفات حسب النوغ", "ممكن ترتبلي الملفات حسب النوع", "خل ترتب الملفات حسب نوعها",
                "الرجاء تصنيف جميع الملفات حسب أنواعها", "افرز الملفات حسب نوعها في مجلدات منفصلة", "ضع كل نوع من الملفات في مجلد خاص به",
                "خلي كل نوع بمجلد", "put each file type in its own folder", "دير الملفات بمجلدات حسب نوعها", "رتّب ملفات التنزيلات حسب نوعها",
                "sort downloads by type", "group files by extension"):
        assert steps_of(req, L) == ref, (req, steps_of(req, L), asks(req, L) if not steps_of(req, L) else "")
    a = analyze("رتّب ملفات التنزيلات حسب نوعها", L)
    assert "التنزيلات" in (a.note or ""), a.note                                          # نرتّب مجلد العمل ونقول ذلك
    with tempfile.TemporaryDirectory() as tmp:   # الترتيب بالتاريخ يحتاج مجلداً مربوطاً
        wsd = pathlib.Path(tmp).resolve() / "ws"
        wsd.mkdir()
        (wsd / "a.txt").write_text("x", encoding="utf-8")
        (wsd / "p.jpg").write_text("y", encoding="utf-8")
        rp2 = RuleProvider()
        rp2.bind_workspace(wsd)
        base = [(s["action_type"], s["target"]) for s in (rp2.analyze("رتب الملفات حسب الشهر", ["a.txt", "p.jpg"]).steps or [])]
        assert base, "كان يجب ترتيب بالشهر"
        for req in ("رتبلي الفايلات حسب الشهر", "ينبغي ترتيب الملفات بحسب الشهر", "sort files by month"):
            assert [(s["action_type"], s["target"]) for s in (rp2.analyze(req, ["a.txt", "p.jpg"]).steps or [])] == base, req
    # ما يبقى سؤالاً: استثناء، عملية ثانية، نوع جزئي، مجلد موجود بالاسم، معيارا ترتيب
    for req in ("رتب الملفات حسب النوع ما عدا الصور", "رتب الملفات حسب النوع بدون a.txt", "رتب الملفات حسب النوع واحذف a.txt", "رتب الملفات حسب النوع ثم انسخ a.txt إلى Docs",
                "رتّب الصور والمستندات في مجلدات", "رتب ملفات Docs حسب النوع", "رتب الملفات حسب النوع والتاريخ", "رتب الملفات حسب الحجم",
                "رتب الملفات حسب النوع وعدل a.txt", "نظف المجلد ورتب الملفات حسب النوع"):
        asks(req, L)

    # ================================================================== 5. صيغ النسخ
    C = ["Work/", "Work/w1.txt", "Backup/", "Reports/", "Reports/r.txt", "عملي/", "عملي/o.txt", "a.txt"]
    for req in ("انسخ Work إلى Backup", "قم بنسخ المجلد Work إلى Backup", "أريد نسخ Work إلى المجلد Backup", "انسخ المجلد Work داخل Backup", "يرجى نسخ Work الى Backup",
                "انسخ لي Work إلى Backup", "نسخ Work إلى Backup", "قم بنسخ Work ووضعه في Backup", "انسخلي المجلد Work لـBackup", "copy Work to Backup",
                "please copy the folder Work into Backup", "make a copy of Work in Backup", "duplicate Work to Backup", "could you copy Work to Backup?",
                "clone folder Work into Backup", "copy Work -> Backup", "copy folder Work to Backup and verify the hash", "انسخ Work إلى Backup وتحقق من سلامة النسخ",
                "انسخ Work إلى Backup مع التحقق من البصمة", "copyy Work to Backup", "cpoy Work to Backup", "copy Work too Backup", "انسخ Work الي Backup",
                "انسخ Work بـ Backup", "كوبي Work الى Backup", "هسه انسخ Work إلى Backup", "انسخ  Work  الى  Backup!!", "انسخ المجلد Work الى المجلدBackup"):
        got = steps_of(req, C)
        assert got and [t for t, _a, _b in got].count("copy") == 1 and got[-1][:2] == ("copy", "Work"), (req, got, asks(req, C) if not got else "")
    a = analyze("انسخ Work إلى Backup وتحقق من سلامة النسخ", C)
    assert "يتحقق المنفّذ" in (a.note or ""), a.note
    # «مجلد» تبقى: لا يُنسخ نوع الملفات بدل المجلد
    asks("انسخ مجلد pdf إلى Backup", C)
    for req in ("انسخ Work إلى Backup وانقل a.txt إلى Reports", "انسخ Work إلى Backup ومن أنا", "ضع a.txt في Backup واعمل نسخة احتياطية", "انسخ محتويات Work إلى Backup",
                "انسخ Work إلى Backup ما عدا w1.txt", "انسخ Work إلى Backup ثم احذف a.txt", "copy Work to Backup and delete a.txt"):
        asks(req, C)

    # مجلد موجود باسم من كلمتين: الصيغ لا تفصله ولا تُسقط كلماته (لا نرتّب مجلد العمل بدل «my files»)
    M = ["my files/", "my files/a.txt", "x.txt"]
    asks("please رتب مجلد my files حسب النوع please", M)
    asks("الآن نظم المجلد my files حسب النوع", M)
    asks("رتب مجلد Reports2 حسب النوع", M)                                              # مجلد مسمّى غير موجود ≠ مجلد العمل
    assert steps_of("رتب محتويات المجلد حسب النوع", M)

    # ================================================================== 6. صيغ البادئة
    P = ["Photos/", "Photos/a.jpg", "Photos/b.jpg", "صور/", "صور/c.jpg", "DCIM/", "DCIM/d.jpg", "x.txt"]
    ref = steps_of("أضف البادئة IMG_ إلى ملفات Photos", P)
    assert ref and all(t == "rename" for t, _a, _b in ref) and len(ref) == 2, ref
    for req in ("ضع IMG_ قبل أسماء الملفات في Photos", "أعد تسمية ملفات Photos بإضافة IMG_ في أولها", "ضيف IMG_ بأول اسم كل ملف بمجلد Photos", "ضيفلي IMG_ لاسماء ملفات Photos",
                "add IMG_ as a prefix to all files in Photos", "prefix every filename in Photos with IMG_", "prepend IMG_ to every file name in Photos",
                "rename the Photos files by adding IMG_ at the front", "batch rename: Photos, prefix IMG_", "add prefix IMG_ to the files in Photos", "put the prefix IMG_ on all files in Photos",
                "اضف البادئه IMG_ الى ملفات Photos", "اضف البادئة IMG_ إلى ملفاتPhotos", "غير اسماء ملفات Photos باضافة بادئة IMG_", "أضف IMG_ كبادئة لكل ملف في Photos",
                "add prefx IMG_ to files in Photos", "ابدأ أسماء ملفات Photos بـ IMG_", "سمِّ ملفات Photos بإضافة IMG_ في أول الاسم"):
        assert steps_of(req, P) == ref, (req, steps_of(req, P), asks(req, P) if not steps_of(req, P) else "")
    assert [t for t, _a, _b in steps_of("ضيف رحلة_ لاسماء ملفات صور", P)] == ["rename"]
    for req in ("أعد تسمية الصور في Photos بإضافة البادئة IMG_", "أضف البادئة IMG_ إلى ملفات Photos ما عدا a.jpg", "أضف البادئة IMG_ إلى ملفات Photos و DCIM",
                "أضف البادئة IMG_ إلى ملفات Photos واحذف x.txt", "add prefix IMG_ to all the pictures in Photos", "لي أعد تسمية ملفات Photos بإضافة البادئة Z- الأول"):
        asks(req, P)

    # ================================================================== 7. صيغ إنشاء المشروع
    E = ["x.txt"]
    proj = [("create_dir", "Shop2026/src"), ("create_dir", "Shop2026/docs"), ("create_dir", "Shop2026/tests")]
    for req in ("أنشئ مشروعاً جديداً باسم Shop2026", "أنشئ مشروع Shop2026", "قم بإنشاء هيكل مشروع Shop2026", "أريد إنشاء مشروع جديد اسمه Shop2026",
                "جهّز مجلد مشروع باسم Shop2026 فيه src وdocs وtests", "أنشئ هيكل مجلدات لمشروع Shop2026", "سوي مشروع جديد اسمه Shop2026", "سويلي مشروع Shop2026",
                "سوي مجلد المشروع Shop2026", "اريد مجلدات مشروع Shop2026", "create a project named Shop2026", "create project Shop2026", "set up a project structure for Shop2026",
                "scaffold project Shop2026", "I want a new project named Shop2026", "create a project Shop2026 with src, docs and tests folders",
                "create a project folder named Shop2026 with src docs tests", "create a project nammed Shop2026", "creat a project Shop2026", "create projcet Shop2026",
                "أنشئ مشرع Shop2026", "انشأ مشروع Shop2026", "هيّئ مشروعاً جديداً Shop2026", "سوي بروجكت جديد Shop2026", "أنشئ مشروعا جديدا باسمShop2026"):
        assert kinds(req, E) == proj, (req, kinds(req, E), asks(req, E) if not kinds(req, E) else "")
    assert kinds("سوي مشروع جديد «App 2026»", E) == [("create_dir", "App 2026/src"), ("create_dir", "App 2026/docs"), ("create_dir", "App 2026/tests")]
    q = asks("أنشئ مجلد مشروع التخرج", E)                                                # مشروع التخرج أم مجلد اسمه «مشروع التخرج»؟ يُسأل ولا يُخمَّن
    assert "معنيين" in q, q
    assert kinds("أنشئ مجلد «مشروع التخرج»", E) == [("create_dir", "مشروع التخرج")]
    assert kinds("اعمل مشروع «مشروع التخرج»", E) == [("create_dir", "مشروع التخرج/src"), ("create_dir", "مشروع التخرج/docs"), ("create_dir", "مشروع التخرج/tests")]
    assert not any(tg == "التخرج" for _t, tg in kinds("جهز folder باسم مشروع التخرج", E))
    for req in ("أنشئ مشروع Shop2026 فيه src وdocs", "أنشئ مشروع Shop2026 ثم احذف x.txt", "أنشئ مشروع Shop2026 وانسخ x.txt إلى Shop2026"):
        asks(req, E)                                                                      # هيكل مختلف أو عملية ثانية

    # ================================================================== 8. صيغ التحديث وبقاء حظر التثبيت
    U = ["x.txt"]
    for req, pkg in (("حدثلي Docker.DockerDesktop", "Docker.DockerDesktop"), ("حدث برنامج Mozilla.Firefox", "Mozilla.Firefox"), ("updte VideoLAN.VLC", "VideoLAN.VLC"),
                     ("updateOpenJS.NodeJS.LTS", "OpenJS.NodeJS.LTS"), ("ممكن تحدثلي Google.Chrome", "Google.Chrome"), ("خلي Microsoft.VisualStudioCode يتحدث", "Microsoft.VisualStudioCode"),
                     ("make sure Google.Chrome is up to date", "Google.Chrome"), ("refresh Python.Python.3.12 to the newest release", "Python.Python.3.12"),
                     ("ثبّت آخر تحديث للبرنامج Git.Git", "Git.Git"), ("حدثلي برنامج Git.Git بالwinget", "Git.Git"), ("قم بترقية Git.Git عبر ويندوز", "Git.Git"),
                     ("winget upgrade Git.Git", "Git.Git"), ("وinget upgrade Git.Git", "Git.Git"), ("حدّث برنامجGit.Git", "Git.Git")):
        got = steps_of(req, U)
        assert got == [("winget_upgrade", pkg, None)], (req, got)
    for req in ("حدثلي برنامج vlc", "حدّث برنامج الكروم", "update chrome", "can you upgrade chrome for me?", "حدث firefox هسه", "حدّث Git.Git و VideoLAN.VLC"):
        q = asks(req, U) if req != "حدّث Git.Git و VideoLAN.VLC" else None
        if q is not None:
            assert "معرّف الحزمة" in q, (req, q)
    for req in ("ثبّت برنامج Git.Git", "install Git.Git", "install the latest version of Git.Git", "ثبّت Git.Git", "احذف برنامج Chrome", "uninstall Chrome"):
        info(req, U)
    # جمل خبرية فيها «حدث/تحديث» ليست أوامر
    for req in ("بعد الحدث التالف", "محدّثة أصلاً", "لم تتم الترقية"):
        a = analyze(req, U)
        assert not a.steps and not (a.question or "").startswith("ما معرّف الحزمة"), (req, a.question)

    # ================================================================== 9. المحظورات تبقى محظورة مهما كانت الصياغة
    for req in ("غيّر كلمة المرور", "ادفع 20 دولار", "عطّل الجدار الناري", "ثبّت برنامج Chrome", "أرسل لي رصيد الحساب", "اشتري ملف", "change my password", "disable the firewall",
                "حوّل 50 دولار"):
        info(req, L)

    print("PASS: test_b40_rules completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b40_rules", file=sys.stderr)
    sys.exit(1)
