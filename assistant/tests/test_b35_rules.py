"""اختبارات B35 (القواعد): إزالة بادئة/لاحقة، الأسماء غير الصالحة والطويلة، الأسماء المتشابهة بحرف عربي، «نظف <مجلد>»، الأسئلة
بصيغة الأمر، صياغة المفرد/المثنى/الجمع، عرض ملفات نوع، «ابدأ» بلا خطة، تواريخ التعديل الشاذة، ودليل المستخدم."""
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
    from assistant.models.rules import RuleProvider, INFO_PREFIX, merge_clarification

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

    def mk_ws(tmp, name, files, dirs=()):
        ws = pathlib.Path(tmp).resolve() / name
        ws.mkdir()
        for d in dirs:
            (ws / d).mkdir(parents=True, exist_ok=True)
        for f, c in files.items():
            (ws / f).parent.mkdir(parents=True, exist_ok=True)
            (ws / f).write_text(c, encoding="utf-8")
        return ws

    def listing(ws):
        out = []
        for p in sorted(ws.rglob("*")):
            rel = p.relative_to(ws).as_posix()
            if not rel.startswith("."):
                out.append(rel + ("/" if p.is_dir() else ""))
        return out

    # ================================================================== 1. إزالة بادئة/لاحقة (لا إضافة)
    W1 = ["old_a.txt", "old_b.txt", "c.txt", "z_bak.txt", "Docs/", "Docs/old_k.txt", "Docs/w_bak.txt"]
    want = [("rename", "old_a.txt", "a.txt"), ("rename", "old_b.txt", "b.txt")]
    for req in ("أعد تسمية الملفات بحذف البادئة old_", "أعد تسمية الملفات بإزالة البادئة old_", "أزل البادئة old_ من أسماء الملفات",
                "شيل البادئة old_", "امسح البادئة «old_»", "احذف البادئة old_ من الملفات", "remove the prefix old_"):
        assert steps_of(req, W1) == want, (req, steps_of(req, W1))
    assert steps_of("أزل البادئة old_ من ملفات Docs", W1) == [("rename", "Docs/old_k.txt", "k.txt")]
    assert steps_of("احذف اللاحقة _bak", W1) == [("rename", "z_bak.txt", "z.txt")]
    assert steps_of("أعد تسمية ملفات Docs بإزالة اللاحقة _bak", W1) == [("rename", "Docs/w_bak.txt", "w.txt")]
    note = info("أزل البادئة zzz_ من الملفات", W1)
    assert "zzz_" in note
    assert steps_of("أضف بادئة new_", ["a.txt"]) == [("rename", "a.txt", "new_a.txt")]                       # الإضافة كما كانت
    q = asks("احذف البادئة من أسماء الملفات", W1)
    assert "ما البادئة" in q
    assert merge_clarification("احذف البادئة من أسماء الملفات", "strip_prefix", "old_") == "احذف البادئة «old_» من أسماء الملفات"
    # تصادم الاسم الناتج: يُتخطى بملاحظة
    a = analyze("أزل البادئة old_ من الملفات", ["old_a.txt", "a.txt", "old_b.txt"])
    assert [s["target"] for s in a.steps] == ["old_b.txt"] and "old_a.txt" in a.note, (a.steps, a.note)

    # ================================================================== 2+3. أسماء طويلة/غير صالحة: ملاحظة، لا «لم أجد»
    with tempfile.TemporaryDirectory() as tmp:
        # أطول من حد 120 محرفاً القديم، وقصير بما يكفي ليبقى المسار الكامل دون 260 محرفاً على ويندوز (بدون مسارات طويلة)
        very_long = "x" * 124 + ".txt"
        ws = mk_ws(tmp, "w2", {"a.txt": "a", very_long: "v", "b.pdf": "b", "h‎.txt": "hidden", "hh‏.pdf": "hidden2",
                               "k‎1.txt": "h3", "k‎2.txt": "h4"})
        prov = RuleProvider(workspace=ws)
        ctx = ["a.txt", very_long, "b.pdf",
               "… (4 عنصراً باسم غير صالح للتعامل الآمن لم يُعرض: h‎.txt، hh‏.pdf، k‎1.txt)"]
        a = analyze("رتب الملفات حسب النوع", ctx, prov)
        tg = {s["target"] for s in a.steps if s["action_type"] == "move"}
        assert very_long in tg and "a.txt" in tg, tg                                   # الاسم الطويل (128 محرفاً) لم يعد يسقط بصمت
        assert "4 عناصر" in a.note and "h" in a.note and "…" in a.note, a.note          # العدد وأول ثلاثة أسماء
        assert steps_of(f"احذف {very_long}", ctx, prov) == [("delete", very_long, None)]
        # الطلب يسمّي ملفاً باسم فيه علامة اتجاه: رسالة محددة لا «لم أجد»
        for req in ("احذف h.txt", "انقل hh.pdf إلى Docs", "احذف «h.txt»"):
            note = info(req, ctx, prov)
            assert "لم أجد" not in note and "U+200E" in note.replace("‹U+200E›", "U+200E") or "U+200F" in note, (req, note)
            assert "غير صالح" in note and "مستكشف" in note, note
        a = analyze("احذف a.txt", ctx, prov)
        assert [s["target"] for s in a.steps] == ["a.txt"] and a.note and "استُثني" in a.note
        # حدود حقيقية: الاسم الطويل جداً (بوحدات النظام) يُسقط مع ملاحظة
        old_limit = R.MAX_NAME_UNITS
        try:
            R.MAX_NAME_UNITS = 20
            p2 = RuleProvider(workspace=ws)
            e = p2._full_ents()
            assert very_long in {r for r, _ in e.skipped}, e.skipped
            a = analyze("رتب الملفات حسب النوع", ctx, p2)
            assert all(s["target"] != very_long for s in a.steps) and "استُثني" in a.note
        finally:
            R.MAX_NAME_UNITS = old_limit

    # ================================================================== 4. «نظف <مجلد>»
    W4 = ["a.txt", "x.tmp", "Docs/", "Docs/y.tmp"]
    for req in ("نظف Docs", "تنظيف Docs", "نظف مجلد Docs"):
        q = asks(req, W4)
        assert "احذف مجلد Docs" in q and "المؤقتة" in q, q
    assert steps_of("نظف الملفات المؤقتة", W4) == [("delete", "x.tmp", None)]                  # المعنى الأصلي محفوظ
    assert steps_of("نظف الملفات المؤقتة من Docs", W4) == [("delete", "Docs/y.tmp", None)]
    assert steps_of("احذف مجلد Docs", W4) == [("delete", "Docs", None)]                         # الصريح ينفَّذ كما كان

    # ================================================================== 5. الأسماء المتشابهة: المطابق تماماً يُفضَّل
    W5 = ["أحمد.txt", "احمد.txt", "فاتورة.pdf", "فاتوره.pdf", "a b c.txt", "a b  c.txt", "علي.txt", "على.txt"]
    for name in W5:
        assert steps_of(f"احذف {name}", W5) == [("delete", name, None)], name
        assert steps_of(f"انقل {name} إلى Out", W5 + ["Out/"]) == [("move", name, f"Out/{name}")], name
    assert sorted(t for _, t, _ in steps_of("احذف أحمد.txt و احمد.txt", W5)) == sorted(["احمد.txt", "أحمد.txt"])
    q = asks("احذف إحمد.txt", W5)                                                              # لا مطابق تام ← سؤال بالأسماء الفعلية
    assert "أحمد.txt" in q and "احمد.txt" in q, q
    assert steps_of("غير اسم أحمد.txt إلى n.txt", W5) == [("rename", "أحمد.txt", "n.txt")]

    # ================================================================== 6. أسئلة بصيغة الأمر
    W6 = ["temp.tmp", "a.txt", "Docs/"]
    for req in ("هل أحذف temp.tmp؟", "احذف temp.tmp؟", "هل احذف temp.tmp", "ماذا لو حذفت temp.tmp", "ماذا لو انقل a.txt إلى Docs",
                "هل أنقل a.txt إلى Docs؟", "should I delete temp.tmp"):
        note = info(req, W6)
        assert "سؤال" in note and ("temp.tmp" in note or "a.txt" in note), (req, note)
    assert steps_of("احذف temp.tmp", W6) == [("delete", "temp.tmp", None)]
    assert steps_of("ممكن تحذف temp.tmp", W6) == [("delete", "temp.tmp", None)]                 # طلب مهذب لا سؤال
    assert steps_of("هل يمكنك حذف temp.tmp", W6) == [("delete", "temp.tmp", None)]
    a = analyze("هل توجد ملفات مكررة؟", W6)
    assert not (a.steps and any(s["action_type"] in ("delete", "move") for s in a.steps))      # القراءة فقط لا تتأثر

    # ================================================================== 7. الصياغة
    assert [R._n(i, "ملف") for i in (1, 2, 3, 10, 11, 25)] == ["ملف واحد", "ملفان", "3 ملفات", "10 ملفات", "11 ملفاً", "25 ملفاً"]
    assert [R._n(i, "عنصر") for i in (1, 2, 5, 12)] == ["عنصر واحد", "عنصران", "5 عناصر", "12 عنصراً"]
    assert [R._n(i, "مجموعة") for i in (1, 2, 4, 20)] == ["مجموعة واحدة", "مجموعتان", "4 مجموعات", "20 مجموعة"]
    assert [R._n(i, "خطوة") for i in (1, 2, 3, 11)] == ["خطوة واحدة", "خطوتان", "3 خطوات", "11 خطوة"]
    assert [R._n(i, "عملية") for i in (1, 2, 3, 11)] == ["عملية واحدة", "عمليتان", "3 عمليات", "11 عملية"]
    big = [f"f{i}.txt" for i in range(260)]
    a = analyze("رتب الملفات حسب النوع", big)
    assert "بقي" in (a.note or "") and "عنصر" in a.note
    assert "11 عنصراً" in R._trunc_note(200, 11) and "عنصر واحد" in R._trunc_note(200, 1) and "عنصران" in R._trunc_note(200, 2)
    q = asks("انقل الملفات إلى مجلد جديد", ["a.txt", "b.pdf"])                                    # «جديد» ليس شرطاً
    assert "شرط" not in q and "المجلد الجديد" in q, q
    assert steps_of("انقل a.txt إلى «مجلد جديد»", ["a.txt"])[-1][2] == "مجلد جديد/a.txt"
    W7 = ["a.txt", "b.pdf", "c.pdf", "Docs/", "Docs/q.pdf"]
    a = analyze("اعرض ملفات pdf", W7)
    assert [s["target"] for s in a.steps] == ["b.pdf", "c.pdf"] and all(s["action_type"] == "inspect" for s in a.steps)
    assert [s["target"] for s in analyze("اعرض ملفات pdf من Docs", W7).steps] == ["Docs/q.pdf"]
    assert "لا توجد" in info("اعرض الصور", W7) and "لا توجد" in info("اعرض ملفات zip", W7)
    assert "لم أجد" not in info("اعرض ملفات zip", W7)
    assert steps_of("افحص Docs", W7) == [("inspect", "Docs", None)]
    for start in ("ابدأ", "ابدا", "start"):
        note = info(start, W7)
        assert "لا توجد خطة" in note and "تعذّر" not in note, note
    note = info("غير اسم a.txt إلى a.txt", W7)
    assert "مطابق" in note
    # قائمة كبيرة مقتطعة: لا «لم أجد» مجردة
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk_ws(tmp, "w7b", {f"f{i}.txt": "x" for i in range(8)})
        old = R._FULL_SCAN_LIMIT
        try:
            R._FULL_SCAN_LIMIT = 4
            prov = RuleProvider(workspace=ws)
            q = asks("احذف zzz.txt", ["f0.txt", "f1.txt", "… (القائمة مقتطعة)"], prov)
            assert "مقتطعة" in q, q
        finally:
            R._FULL_SCAN_LIMIT = old

    # ================================================================== 8. تواريخ تعديل شاذة
    import datetime
    for ts in (-1.0, -5e9, -1e11, 1e18, -1e18, float("nan"), float("inf")):
        d, odd = R._mtime_date(ts)
        assert isinstance(d, datetime.date) and R._DATE_MIN <= d <= R._DATE_MAX, (ts, d)
    # قيمة اعتيادية: التاريخ المحلي كما تحسبه المكتبة (يتبع منطقة الجهاز؛ 1.7e9 = 2023-11-14 22:13 UTC أي 15 في UTC+3)
    assert R._mtime_date(1.7e9) == (datetime.date.fromtimestamp(1.7e9), False)
    assert abs((R._mtime_date(1.7e9)[0] - datetime.date(2023, 11, 14)).days) <= 1
    with tempfile.TemporaryDirectory() as tmp:
        ws = mk_ws(tmp, "w8", {"old.txt": "o", "new.txt": "n", "mid.txt": "m"})
        try:
            os.utime(ws / "old.txt", (-5e9, -5e9))
        except (OSError, OverflowError, ValueError):
            pass
        os.utime(ws / "new.txt", (1.7e9, 1.7e9))
        prov = RuleProvider(workspace=ws)
        a = analyze("رتب الملفات حسب الشهر", listing(ws), prov)
        moved = {s["target"] for s in a.steps if s["action_type"] == "move"}
        assert {"old.txt", "new.txt", "mid.txt"} <= moved, (moved, a.question, a.note)
        # أخفق fromtimestamp (ويندوز): الحساب يمرّ عبر 1970 + timedelta
        orig = R.datetime.date

        class BoomDate(datetime.date):
            @classmethod
            def fromtimestamp(cls, ts):
                raise OSError("[Errno 22] Invalid argument")
        R.datetime = type("dt", (), {"date": BoomDate, "datetime": datetime.datetime, "timedelta": datetime.timedelta})
        try:
            d, odd = R._mtime_date(-5e9)
            assert (d.year, d.month) == (1811, 7) and odd
            a = analyze("رتب الملفات حسب الشهر", listing(ws), prov)
            assert any(s["action_type"] == "move" for s in a.steps), (a.question, a.note)
        finally:
            R.datetime = datetime

    # ================================================================== 9. دليل المستخدم يطابق الكود
    guide = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    assert "tmp وbak" not in guide and "bak" in guide and "لا تُحذف" in guide
    assert R._is_temp("x.tmp") and R._is_temp("y.crdownload") and not R._is_temp("old.bak") and not R._is_temp("p.part")

    print("PASS: test_b35_rules completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b35_rules", file=sys.stderr)
    sys.exit(1)
