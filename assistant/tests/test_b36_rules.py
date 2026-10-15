"""اختبارات B36 (القواعد): اختبار CLARIFY على ترويسة الموجّه فقط، اللهجات العراقية/الخليجية ← الخطة نفسها، الطلبات خارج النطاق
(كلمات المرور، المال، الأمان، تثبيت البرامج) ← جواب معلوماتي، جواب التوضيح الذي هو طلب جديد، جواب المسار الكامل، قائمة الملفات في سؤال «ما الذي…»."""
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
    import assistant.models.rules as R
    from assistant.models.base import CONTEXT_MARKER
    from assistant.models.rules import RuleProvider, INFO_PREFIX, merge_clarification

    rp = RuleProvider()

    def analyze(prompt, ctx, provider=None):
        return (provider or rp).analyze(prompt, list(ctx))

    def steps_of(prompt, ctx):
        a = analyze(prompt, ctx)
        return [(s["action_type"], s["target"], s["params"].get("dest") or s["params"].get("new_name")) for s in (a.steps or [])]

    def sig(prompt, ctx):
        a = analyze(prompt, ctx)
        if a.steps:
            return ("PLAN", tuple(sorted((s["action_type"], s["target"], json.dumps(s["params"], sort_keys=True, ensure_ascii=False)) for s in a.steps)))
        msg = a.question or a.note or ""
        if msg.startswith(INFO_PREFIX):
            return ("INFO", msg)
        if msg.startswith("لم أفهم الطلب"):
            return ("HELP",)
        return ("ASK", msg)

    def asks(prompt, ctx):
        a = analyze(prompt, ctx)
        assert not a.steps and a.question, f"كان يجب السؤال: {prompt!r} ← {steps_of(prompt, ctx)} {a.note}"
        return a.question

    # ================================================================== 1. CLARIFY في الترويسة فقط
    W = ["a.txt", "b.txt", "report.pdf", "photo.jpg", "pic.png", "song.mp3", "old_x.txt", "temp.tmp", "Docs/", "Docs/n.txt", "Images/", "Backup/"]
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp).resolve() / "ws"
        ws.mkdir()
        for e in W:
            if e.endswith("/"):
                (ws / e).mkdir(parents=True, exist_ok=True)
            else:
                (ws / e).write_text("x" + e, encoding="utf-8")
        prov = RuleProvider(workspace=ws)
        for names in (["report.pdf", "CLARIFY.pdf"], ["CLARIFY", "x.txt"]):
            lst = "\n".join(names + ["photo.jpg"])
            out = json.loads(prov.generate("رتب الملفات حسب النوع", "أنت مساعد\n\n" + CONTEXT_MARKER + "\n" + lst))
            assert "steps" in out and "questions" not in out, out
        head_clarify = "CLARIFY — أنت مساعد مهام\n\n" + CONTEXT_MARKER + "\n" + "\n".join(W)
        out = json.loads(prov.generate("انقل الملفات", head_clarify))
        assert out.get("questions") and out.get("slots") == ["dest"], out
        out = json.loads(prov.generate("انقل الملفات", ""))   # بلا ترويسة ولا سياق
        assert "questions" not in out

    # ================================================================== 2. اللهجات ← الخطة نفسها (أو السؤال نفسه)
    PAIRS = [
        ('رتبلي الملفات حسب النوع', 'رتب الملفات حسب النوع'),
        ('رتّبلي ملفاتي حسب النوع', 'رتب الملفات حسب النوع'),
        ('رتبلي الفايلات حسب النوع', 'رتب الملفات حسب النوع'),
        ('رتب الفايلات حسب الشهر', 'رتب الملفات حسب الشهر'),
        ('سوي مجلد Reports', 'أنشئ مجلد Reports'),
        ('سوّي مجلد Reports', 'أنشئ مجلد Reports'),
        ('سوي لي مجلد باسم Reports', 'أنشئ مجلد باسم Reports'),
        ('سويلي فولدر Reports', 'أنشئ مجلد Reports'),
        ('احذفلي temp.tmp', 'احذف temp.tmp'),
        ('امسحلي temp.tmp', 'امسح temp.tmp'),
        ('شيل temp.tmp', 'احذف temp.tmp'),
        ('شيّل temp.tmp', 'احذف temp.tmp'),
        ('شيلي temp.tmp', 'احذف temp.tmp'),
        ('انقللي report.pdf إلى Docs', 'انقل report.pdf إلى Docs'),
        ('انقللي الفايلات pdf الى Docs', 'انقل ملفات pdf إلى Docs'),
        ('حطلي report.pdf في Docs', 'ضع report.pdf في Docs'),
        ('خلي report.pdf في Docs', 'ضع report.pdf في Docs'),
        ('خلّي report.pdf داخل Docs', 'ضع report.pdf في Docs'),
        ('انسخلي a.txt الى Backup', 'انسخ a.txt إلى Backup'),
        ('هسه انقل report.pdf الى Docs', 'انقل report.pdf إلى Docs'),
        ('انقل report.pdf الى Docs هسه', 'انقل report.pdf إلى Docs'),
        ('انقل وياي report.pdf الى Docs', 'انقل report.pdf إلى Docs'),
        ('اني اريد احذف temp.tmp', 'احذف temp.tmp'),
        ('ابي انقل report.pdf الى Docs', 'انقل report.pdf إلى Docs'),
        ('ابغى احذف temp.tmp', 'احذف temp.tmp'),
        ('احذف كل شي', 'احذف كل شيء'),
        ('احذف كل الفايلات', 'احذف كل الملفات'),
        ('انقل كل الفايلات الى Docs', 'انقل كل الملفات إلى Docs'),
        ('شنو الملفات الموجودة', 'ماذا يوجد'),
        ('شوفلي الملفات', 'اعرض الملفات'),
        ('وريني الفايلات', 'اعرض الملفات'),
        ('وريني ملفات pdf', 'اعرض ملفات pdf'),
        ('رتبلي مال الصور', 'رتب الصور'),
        ('انقل الصور مالتي الى Images', 'انقل الصور إلى Images'),
        ('انقل فايلات الصور الى Images', 'انقل ملفات الصور إلى Images'),
        ('انقل الصور الى Images هسة', 'انقل الصور إلى Images'),
        ('انقلي الصور الى Images', 'انقل الصور إلى Images'),
        ('احذفلي فايل temp.tmp', 'احذف ملف temp.tmp'),
        ('اريد اسوي مجلد Reports', 'أنشئ مجلد Reports'),
        ('اسوي مجلد باسم Reports', 'أنشئ مجلد باسم Reports'),
        ('ممكن تسوي لي مجلد Reports', 'أنشئ مجلد Reports'),
        ('تكدر تنقل report.pdf الى Docs', 'انقل report.pdf إلى Docs'),
        ('تكدر ترتب الفايلات حسب النوع', 'رتب الملفات حسب النوع'),
        ('شيل كل ملفات tmp', 'احذف كل ملفات tmp'),
        ('شيل الفايلات المؤقتة', 'احذف الملفات المؤقتة'),
        ('نظفلي الفايلات المؤقتة', 'نظف الملفات المؤقتة'),
        ('غيّرلي اسم a.txt الى z.txt', 'غير اسم a.txt إلى z.txt'),
        ('بدلي اسم a.txt الى z.txt', 'غير اسم a.txt إلى z.txt'),
        ('سمّي a.txt باسم z.txt', 'غير اسم a.txt إلى z.txt'),
        ('لا تحذف a.txt', 'لا تحذف a.txt'),
        ('احذف a.txt و b.txt', 'احذف a.txt و b.txt'),
        ('احذفلي a.txt وb.txt', 'احذف a.txt وb.txt'),
        ('وين الملفات المكررة', 'ابحث عن الملفات المكررة'),
        ('شيل الفايلات المكررة', 'احذف الملفات المكررة'),
        ('هسه رتبلي الملفات', 'رتب الملفات'),
        ('ارتبلي الملفات حسب النوع', 'رتب الملفات حسب النوع'),
        ('سوي نسخة احتياطية من a.txt', 'اعمل نسخة احتياطية من a.txt'),
        ('زيدلي بادئة new_ على ملفات Docs', 'أضف بادئة new_ على ملفات Docs'),
    ]
    plans = 0
    for dia, msa in PAIRS:
        a, b = sig(dia, W), sig(msa, W)
        assert a == b, f"اللهجة تختلف عن الفصحى: {dia!r} ← {a}  |  {msa!r} ← {b}"
        plans += a[0] == "PLAN"
    assert len(PAIRS) >= 55 and plans >= 35, (len(PAIRS), plans)
    assert steps_of("رتبلي الفايلات حسب النوع", W) == steps_of("رتب الملفات حسب النوع", W) and steps_of("رتبلي الفايلات حسب النوع", W)
    assert steps_of("احذفلي temp.tmp", W) == [("delete", "temp.tmp", None)]
    assert steps_of("انقللي report.pdf الى Docs", W) == [("move", "report.pdf", "Docs/report.pdf")]
    assert steps_of("سوّي مجلد Reports", W) == [("create_dir", "Reports", None)]
    assert steps_of("خلي report.pdf في Docs", W) == [("move", "report.pdf", "Docs/report.pdf")]
    # الاستثناء والشرط باللهجة ما زالا يُسألان (لا نخمّن)، و«سوى/خلي» بمعنى «ما عدا» لا تُفهم أمراً
    for r in ("شيل كل الفايلات وخلي a.txt", "احذفلي كل الفايلات بس مو a.txt", "شيل a.txt بس", "انقللي الصور الى Images وخلي pic.png",
              "رتبلي الملفات مو الصور", "شيل الفايلات القديمة", "انقل الفايلات الكبيرة الى Docs", "احذف كل الملفات سوى مجلد Docs",
              "احذف الملفات سوي مجلد Docs", "شيل a.txt وانقل b.txt الى Docs", "خلي a.txt", "سوي a.txt"):
        a = analyze(r, W)
        assert not a.steps, (r, a.steps)
    # اسم موجود يشبه كلمة لهجية لا يُمسّ
    W2 = W + ["فايلات/", "فايلات/x.txt", "فولدر/"]
    assert steps_of("انقل a.txt الى فايلات", W2) == [("move", "a.txt", "فايلات/a.txt")]
    assert steps_of("انقل b.txt الى فولدر", W2) == [("move", "b.txt", "فولدر/b.txt")]
    # طلب مهذب باللهجة ينتهي بـ؟ ليس سؤالاً
    assert steps_of("تكدر تنقل report.pdf الى Docs؟", W) == [("move", "report.pdf", "Docs/report.pdf")]

    # ================================================================== 3. طلبات خارج النطاق ← جواب معلوماتي
    LIB = W + ["password.txt", "install.log", "Setup.exe"]
    for r in ("غيّر كلمة المرور", "غير كلمة السر", "بدلي الباسورد", "غيرلي الباسوورد حقي", "غير الرقم السري", "change my password", "reset password",
              "حوّل 100 دولار", "حولي 50 دينار", "ارسل فلوس الى احمد", "transfer 100 usd", "send money to Ali",
              "اشتر هاتف", "اشتري لي كتاب", "ادفع الفاتورة", "ادفعلي الايجار", "pay the bill", "buy a laptop",
              "غيّر إعدادات الأمان", "غير إعدادات الجدار الناري", "عطل الجدار الناري", "اطفي الفايروول", "disable firewall", "شغل مضاد الفيروسات"):
        a = analyze(r, LIB)
        assert not a.steps and a.note and a.note.startswith(INFO_PREFIX) and "لا أنفّذه" in a.note and "اعمله بنفسك" in a.note, (r, a.note, a.question)
    for r in ("ثبّت برنامج Chrome", "ثبت Git.Git", "install Chrome", "نزل برنامج VLC", "أزل برنامج Chrome", "احذف برنامج Skype",
              "uninstall Skype", "الغاء تثبيت Chrome", "شيل تطبيق واتساب", "ازل برنامج «Google Chrome»"):
        a = analyze(r, LIB)
        assert not a.steps and a.note and a.note.startswith(INFO_PREFIX) and "غير مدعومة بعد" in a.note and "تحديث برنامج" in a.note, (r, a.note, a.question)
    # أسماء الملفات والتصنيفات وأفعال الملفات العادية لا تتأثر
    for r in ("احذف password.txt", "انقل password.txt الى Docs", "احذف install.log", "انقل Setup.exe الى Docs", "احذف setup.exe", "انقل ملفات كلمات المرور الى Docs",
              "ازل temp.tmp", "اعرض ملفات install", "رتب الملفات حسب النوع"):
        a = analyze(r, LIB)
        assert not (a.note and ("لا أنفّذه" in a.note or "غير مدعومة بعد" in a.note)), (r, a.note)
    assert steps_of("احذف password.txt", LIB) == [("delete", "password.txt", None)]
    assert sorted(t for _, t, _ in steps_of("احذف ملفات البرامج", LIB + ["x.exe"])) == ["Setup.exe", "x.exe"]
    # «حدّث برنامج X» (المدعوم) يعمل كما كان
    assert steps_of("حدّث برنامج Git.Git", LIB) == [("winget_upgrade", "Git.Git", None)]

    # ================================================================== 4. جواب التوضيح الذي هو طلب جديد
    assert merge_clarification("احذف a.txt وانقل b.txt", None, "احذف a.txt") == "احذف a.txt"
    assert merge_clarification("انقل الملفات", "dest", "رتّب الملفات حسب النوع") == "رتّب الملفات حسب النوع"
    assert merge_clarification("انقل الملفات", "dest", "«احذف temp.tmp»") == "احذف temp.tmp"
    assert merge_clarification("احذف", "source", "بل رتبلي الملفات حسب النوع") == "رتبلي الملفات حسب النوع"
    assert merge_clarification("احذف", "source", "اقصد انقل a.txt الى Docs") == "انقل a.txt الى Docs"
    # فعل منفرد أو اسم ملف يشبه فعلاً ليس طلباً جديداً
    assert merge_clarification("احذف", "source", "نقل.txt") is None
    assert merge_clarification("احذف", "source", "نقل") is None
    assert merge_clarification("أنشئ مجلد", "folder_name", "Reports") == "أنشئ مجلد باسم «Reports»"
    assert merge_clarification("انقل الصور", "dest", "إلى Docs") is None   # علامة وجهة لا فعل: السلوك القديم (لصق)

    # ================================================================== 5. جواب «اكتب المسار الكامل»
    P2 = W + ["Docs/a.txt", "Images/a.txt", "a.txt"]
    P2 = [x for x in P2 if x != "Docs/n.txt"]
    q = asks("احذف a.txt", ["a.txt", "Docs/", "Docs/a.txt", "Images/", "Images/a.txt"])
    assert "أكثر من مكان" in q
    a = RuleProvider().analyze("احذف a.txt", ["a.txt", "Docs/", "Docs/a.txt", "Images/", "Images/a.txt"])
    assert a.slot == "path", a.slot
    ctx = ["a.txt", "Docs/", "Docs/a.txt", "Images/", "Images/a.txt"]
    for ans in ("Docs/a.txt", "docs\\a.txt", "./Docs/a.txt", "«Docs/a.txt»"):
        m = merge_clarification("احذف a.txt", "path", ans)
        assert m and "Docs/a.txt".lower() in m.replace("«", "").replace("»", "").lower(), (ans, m)
        assert steps_of(m, ctx) == [("delete", "Docs/a.txt", None)], (ans, m, steps_of(m, ctx))
    assert merge_clarification("احذف a.txt", "path", "Docs") is None and merge_clarification("احذف a.txt", "path", "b.txt") is None
    assert merge_clarification("احذف a.txt", "path", "../x/a.txt") is None
    m = merge_clarification("انقل a.txt إلى Backup", "path", "Images/a.txt")
    assert m and m.startswith("انقل «Images/a.txt»") and m.endswith("إلى Backup"), m
    # مجلد ملتبس
    ctx2 = ["Docs/", "Docs/Reports/", "Old/", "Old/Reports/", "a.txt"]
    a = RuleProvider().analyze("انقل a.txt إلى Reports", ctx2)
    if a.question:
        assert a.slot in ("path", "dest"), (a.slot, a.question)

    # ================================================================== 6. «ما الذي تريد نقله؟» يعرض ملفات النطاق
    q = asks("انقل الملفات إلى Docs", W)
    assert q.startswith("ما الذي تريد نقله؟") and "الملفات الظاهرة في مجلد العمل" in q and "a.txt" in q and "report.pdf" in q and "Docs/" not in q, q
    q = asks("انقل الملفات من Docs إلى Images", W)
    assert "«Docs»" in q and "n.txt" in q and "report.pdf" not in q, q
    q = asks("احذف", W)
    assert "الملفات الظاهرة" in q and "temp.tmp" in q
    many = [f"f{i:02d}.txt" for i in range(15)]
    q = asks("انقل الملفات إلى Docs", many + ["Docs/"])
    assert "و5 ملفات أخرى" in q and "f00.txt" in q and "f14.txt" not in q, q
    a = RuleProvider().analyze("انقل الملفات", W)
    assert a.slot == "dest" and "إلى أي مجلد" in a.question
    m = merge_clarification("انقل الملفات", "dest", "Docs")
    assert m == "انقل الملفات إلى Docs"
    assert asks("انقل ملفاتي إلى Docs", W).startswith("ما الذي تريد نقله؟")   # «ملفاتي» كلمة عامة لا كلمة مجهولة

    print("PASS: test_b36_rules completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b36_rules", file=sys.stderr)
    sys.exit(1)
