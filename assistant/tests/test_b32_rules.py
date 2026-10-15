"""اختبارات B32: ثغرات الاختبار بالتوليد (gen_requests): نطاق «في/من المجلد X»، «مجلد X» غير الموجود لا يصير نوعاً، إلى/إلي/التشكيل،
«من فضلك» وعبارات المجاملة في الإنشاء، اسم عنصر يشبه كلمة وظيفية، الاسم المجرد على أكثر من عمق، نسخ مجلد متكرر، كلمة زائدة في إعادة
تسمية عنصر، علامات ترقيم البادئة، وكلمات الحشو الإضافية."""
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
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

    # ================================================================== 1. «في/من المجلد X» نطاق لا هدف
    W1 = ["a.txt", "docs/", "docs/a.txt"]
    assert steps_of("احذف a.txt في المجلد docs", W1) == [("delete", "docs/a.txt", None)]
    assert steps_of("احذف a.txt من المجلد docs", W1) == [("delete", "docs/a.txt", None)]
    assert steps_of("احذف a.txt داخل مجلد docs", W1) == [("delete", "docs/a.txt", None)]
    assert steps_of("delete a.txt in the folder docs", W1) == [("delete", "docs/a.txt", None)]
    assert steps_of("انقل b.txt من المجلد docs إلى Backup", ["docs/", "docs/b.txt", "Backup/"]) == [("move", "docs/b.txt", "Backup/b.txt")]
    q = asks("احذف a.txt في المجلد nodir", W1)
    assert "nodir" in q

    # ================================================================== 2. «مجلد X» غير موجود ← سؤال لا اختيار بالنوع
    W2 = ["a.zip", "b.zip", "docs/", "a.jpg", "b.png", "r.docx", "s.pdf"]
    for req in ("احذف مجلد zip", "احذف مجلد الصور", "احذف المجلد مستندات", "delete folder zip", "انقل مجلد الصور إلى docs",
                "انسخ مجلد pdf إلى docs", "اعمل نسخة احتياطية من مجلد الصور", "احذف مجلد «zip»"):
        q = asks(req, W2)
        assert "لم أجد" in q, (req, q)
    assert steps_of("احذف مجلد docs", W2) == [("delete", "docs", None)]                  # الموجود يعمل
    assert sorted(t for _, t, _ in steps_of("احذف الصور", W2)) == ["a.jpg", "b.png"]     # بلا كلمة مجلد: النوع كما كان
    assert asks("نظف «فيديو»", W2)                                                        # اسم منصَّص غير موجود لا يصير نوعاً

    # ================================================================== 3. إلى/إلي/التشكيل/التطويل
    W3 = ["a.txt", "docs/", "docs/x.txt"]
    for dest_word in ("إلى", "الى", "إلي", "الي", "إِلَى", "إلــى", "الىِ"):
        assert steps_of(f"انسخ a.txt {dest_word} docs", W3) == [("copy", "a.txt", "docs/a.txt")], dest_word
        assert steps_of(f"انقل a.txt {dest_word} docs", W3) == [("move", "a.txt", "docs/a.txt")], dest_word
    assert steps_of("ضع a.txt فِي docs", W3) == [("move", "a.txt", "docs/a.txt")]
    assert steps_of("ضع a.txt فى docs", W3) == [("move", "a.txt", "docs/a.txt")]
    assert steps_of("غيّر اسم a.txt إلي n.txt", W3) == [("rename", "a.txt", "n.txt")]
    assert steps_of("اِنْسَخْ a.txt إِلَى docs", W3) == [("copy", "a.txt", "docs/a.txt")]
    assert steps_of("أنشئ مجلد اسِمه Reports", W3) == [("create_dir", "Reports", None)]
    assert steps_of("غير اسم a.txt إلى دَفتر.log", W3) == [("rename", "a.txt", "دَفتر.log")]   # الاسم الجديد يبقى كما كُتب
    assert steps_of("انقل a.txt إلى «مَشاريع»", W3)[-1][2] == "مَشاريع/a.txt"                    # المنصَّص حرفي

    # ================================================================== 4. «من فضلك» والمجاملة في الإنشاء
    for req in ("أنشئ مجلد Reports من فضلك", "أنشئ مجلد Reports لو سمحت", "رجاءً أنشئ مجلد Reports", "أنشئ مجلد Reports من فضلك.",
                "أنشئ مجلد 'Reports'", "أنشئ مجلد باسم 'Reports' من فضلك", "can you create folder Reports please"):
        assert steps_of(req, W3) == [("create_dir", "Reports", None)], (req, steps_of(req, W3))
    asks("أنشئ مجلد من فضلك", W3)                                                       # لا اسم ← سؤال (لا مجلد باسم «من»)
    tg = [t for _, t, _ in steps_of("أنشئ مشروع X من فضلك", W3)]
    assert tg and all(t == "X" or t.startswith("X/") for t in tg), tg

    # ================================================================== 5. اسم عنصر يشبه كلمة وظيفية
    q = asks("احذف a.txt بدون b.txt", ["a.txt", "b.txt", "بدون/"])
    assert "بدون" in q and "تنصيص" in q
    asks("احذف a.txt من docs", ["a.txt", "docs/", "docs/a.txt", "من/"])
    assert steps_of("احذف «بدون»", ["a.txt", "بدون/"]) == [("delete", "بدون", None)]            # منصَّصاً: الاسم حرفي
    assert steps_of("انقل a.txt إلى keep", ["a.txt", "keep/"]) == [("move", "a.txt", "keep/a.txt")]
    assert steps_of("لو سمحت امسح a.txt من فضلك", ["a.txt", "من/"]) == [("delete", "a.txt", None)]
    asks("copy folder x.txt", ["folder/", "x.txt"])                                          # مجلد اسمه «folder» قبل اسم ملف
    asks("احذف كل المجلد", ["كل/", "a.txt"])

    # ================================================================== 6. اسم مجرد على أكثر من عمق
    q = asks("احذف a.txt", W1)
    assert "أكثر من مكان" in q
    assert steps_of("احذف docs/a.txt", W1) == [("delete", "docs/a.txt", None)]
    asks("غير اسم a.txt إلى n.txt", W1)
    a = analyze("انقل a.txt إلى Out", W1 + ["Out/"])
    assert [s["target"] for s in a.steps] == ["a.txt"] and "docs/a.txt" in (a.note or ""), (a.steps, a.note)   # النقل: يُذكر المختار صراحة
    assert steps_of("احذف b.txt", ["b.txt", "docs/", "docs/c.txt"]) == [("delete", "b.txt", None)]            # لا التباس إن وُجد في عمق واحد

    # ================================================================== 7. نسخ مجلد متكرر
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp).resolve() / "w7"
        (ws / "docs").mkdir(parents=True)
        (ws / "docs" / "x.txt").write_text("x", encoding="utf-8")
        (ws / "Out").mkdir()
        prov = RuleProvider(workspace=ws)
        ctx = ["Out/", "docs/", "docs/x.txt"]
        assert steps_of("انسخ docs إلى Out", ctx, prov) == [("copy", "docs", "Out/docs")]
        (ws / "Out" / "docs").mkdir()
        (ws / "Out" / "docs" / "x.txt").write_text("x", encoding="utf-8")
        ctx2 = ctx + ["Out/docs/"]
        a = analyze("انسخ docs إلى Out", ctx2, prov)
        assert not a.steps and a.note.startswith(INFO_PREFIX) and "مطابقة" in a.note, (a.steps, a.note)
        a = analyze("اعمل نسخة احتياطية من docs", ctx, prov)
        assert [s["action_type"] for s in a.steps] == ["create_dir", "copy"]
        (ws / "Out" / "docs" / "x.txt").write_text("CHANGED", encoding="utf-8")        # محتوى مختلف: سلوك (1) محفوظ
        assert steps_of("انسخ docs إلى Out", ctx2, prov) == [("copy", "docs", "Out/docs (1)")]
        (ws / "Out" / "docs" / "x.txt").write_text("x", encoding="utf-8")
        (ws / "Out" / "docs" / "extra.txt").write_text("e", encoding="utf-8")           # ملف زائد: ليست مطابقة
        assert steps_of("انسخ docs إلى Out", ctx2 + ["Out/docs/extra.txt"], prov) == [("copy", "docs", "Out/docs (1)")]

    # ================================================================== 8. كلمة زائدة في إعادة تسمية عنصر
    W8 = ["a.txt", "b.txt"]
    for req in ("غير اسم a.txt الباقي إلى n.txt", "غير اسم a.txt تجاهل إلى n.txt"):
        q = asks(req, W8)
        assert "لم أفهم" in q, q
    assert steps_of("غير اسم a.txt إلى n.txt", W8) == [("rename", "a.txt", "n.txt")]
    assert steps_of("غير اسم الملف a.txt إلى n.txt من فضلك", W8) == [("rename", "a.txt", "n.txt")]

    # ================================================================== 9. بادئة غير منصَّصة تقطع !/؟
    for req in ("أضف بادئة x!", "أعد تسمية الملفات بإضافة البادئة x!"):
        assert [n for _, _, n in steps_of(req, W8)] == ["xa.txt", "xb.txt"], req
    assert [n for _, _, n in steps_of("أضف بادئة «x!»", W8)] == ["x!a.txt", "x!b.txt"]       # المنصَّصة حرفية

    # ================================================================== 10. حشو إضافي آمن
    for req in ("هل يمكنك حذف a.txt", "قم بحذف b.txt", "can you delete b.txt", "يا مساعد احذف b.txt رجاءً", "احذف لي b.txt",
                "لو تكرمت احذف b.txt", "احذف b.txt رجاء"):
        assert [t for _, t, _ in steps_of(req, W8)] in (["a.txt"], ["b.txt"]), req
    assert sorted(t for _, t, _ in steps_of("احذف a.txt وb.txt", W8)) == ["a.txt", "b.txt"]
    asks("قم بتشغيل a.txt", W8)                                                         # ما لا نفهمه يبقى سؤالاً
    asks("احذف a.txt ما عدا b.txt", W8)                                                 # الحشو لا يخفي استثناء
    assert steps_of("احذف لي", ["لي/", "a.txt"]) == [("delete", "لي", None)]            # مجلد اسمه «لي» لا يُمحى بالحشو

    # ================================================================== 11. تتمات من جولة التوليد الثانية
    assert steps_of("انقل a.txt إلى 'my docs'", ["a.txt"])[-1][2] == "my docs/a.txt"                 # الاقتباس المفرد كالمزدوج
    assert steps_of("انقل a.txt إلى “my docs”", ["a.txt"])[-1][2] == "my docs/a.txt"
    assert [n for _, _, n in steps_of("أعد تسمية الملفات بادئة '(old)_'", W8)] == ["(old)_a.txt", "(old)_b.txt"]
    asks("نظف folder صور", ["temp/", "temp/صور/", "a.txt"])         # مجلد متداخل بالاسم المجرد
    assert steps_of("احذف temp/صور", ["temp/", "temp/صور/", "a.txt"]) == [("delete", "temp/صور", None)]
    asks("أضف بادئة x_ لملفات docs الملف الحالي", ["docs/", "docs/a.txt"])
    asks("غير اسم a.txt إلى COM١.txt", W8)                                                          # اسم جهاز ويندوز بأرقام شرقية
    asks("اعمل نسخة احتياطية من a.txt وانسخ b.txt إلى docs", ["a.txt", "b.txt", "docs/"])
    asks("انقل all files داخل Archive", ["files/", "Archive/", "a.txt"])
    assert [t for _, t, _ in steps_of("نقل ملفات .temp إلى Out", ["~$cv.docx", "x.temp", "Out/"])] == ["x.temp"]   # الامتداد لا فئة «المؤقتة» ولا ملفات القفل

    print("PASS: test_b32_rules completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b32_rules", file=sys.stderr)
    sys.exit(1)
