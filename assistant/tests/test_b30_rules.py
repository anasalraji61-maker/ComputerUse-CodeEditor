"""اختبارات B30: ثغرات مراجعة rt13 على عمل B29: المفرد مع كلمات الفئات (ملف اكسل/مؤقت/وورد)، «المؤقتة» بلا .bak/.part ومع عرض الأسماء،
المكررات (تجاهل ~$ ومجلدي النسخ الاحتياطي/المكررات وصيغة السؤال تعرض فقط)، خانات التوضيح (جيل الطلب، الترتيب مع نطاق، بادئات الوجهة،
«ملفات» داخل الاسم المقتبس، المصدر قبل «في»)، تخطي النسخة المطابقة، استثناء ~$ من الأنواع، حدود المسح والإيقاف."""
import datetime
import pathlib
import sys
import tempfile
import threading
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.ledger import SQLiteLedger
    import assistant.models.rules as R
    from assistant.models.rules import RuleProvider, RuleFirstProvider, merge_clarification, INFO_PREFIX
    from assistant.models.base import ModelProvider
    from assistant.safety import EmergencyStopTriggered
    from assistant.ui.controller import AssistantController

    rp = RuleProvider(today=lambda: datetime.date(2026, 1, 2))

    def analyze(prompt, ctx, provider=None):
        return (provider or rp).analyze(prompt, list(ctx))

    def steps_of(prompt, ctx, provider=None):
        a = analyze(prompt, ctx, provider)
        return [(s["action_type"], s["target"], s["params"].get("dest") or s["params"].get("new_name")) for s in (a.steps or [])]

    def asks(prompt, ctx, provider=None):
        a = analyze(prompt, ctx, provider)
        assert not a.steps and a.question, f"كان يجب السؤال: {prompt!r} ← {steps_of(prompt, ctx, provider)} {a.note}"
        return a.question

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

    with tempfile.TemporaryDirectory() as tmp:
        # ================================================================== 1. «ملف X» بالمفرد مع كلمات الفئات
        L1 = ["budget.xlsx", "sales.xls", "cv.docx", "notes.doc", "x.tmp", "a.txt", "Docs/", "temp"]
        for req in ("احذف ملف اكسل", "احذف ملف excel", "احذف ملف مؤقت", "انقل ملف وورد إلى Docs", "انسخ ملف word إلى Docs", "احذف الملف الاكسل"):
            asks(req, L1)
        assert sorted(s[1] for s in steps_of("احذف ملفات اكسل", L1)) == ["budget.xlsx", "sales.xls"]   # الجمع ما زال يعمل
        assert sorted(s[1] for s in steps_of("احذف الاكسل", L1)) == ["budget.xlsx", "sales.xls"]
        assert steps_of("احذف ملف temp", L1) == [("delete", "temp", None)]                            # ملف اسمه temp فعلاً

        # ================================================================== 2. «المؤقتة»: tmp/temp/crdownload/~$ فقط، وتُعرض أسماؤها
        L2 = ["x.tmp", "y.temp", "z.crdownload", "~$cv.docx", "db.bak", "config.bak", "dl.part", "big.partial", "a.txt", "Docs/", "Docs/keep.bak"]
        a = analyze("احذف الملفات المؤقتة", L2)
        tg = sorted(s["target"] for s in a.steps)
        assert tg == ["x.tmp", "y.temp", "z.crdownload", "~$cv.docx"], tg
        for n in tg:
            assert n in a.note, a.note
        assert "مؤقتة" in a.note
        assert sorted(s[1] for s in steps_of("نظف الملفات المؤقتة", L2)) == tg
        assert sorted(s[1] for s in steps_of("انقل الملفات المؤقتة إلى Docs", L2)) == tg

        # ================================================================== 3. المكررات
        ws = mk_ws(tmp, "w3", {"a.txt": "same", "b.txt": "same", "~$a.txt": "same", "نسخة_احتياطية/a.txt": "same",
                               "مكررات/old.txt": "same", "u.txt": "unique"})
        prov = RuleProvider(workspace=ws)
        ctx3 = listing(ws)
        a = analyze("احذف المكررات", ctx3, prov)
        assert [s["target"] for s in a.steps] == ["b.txt"], a.steps          # لا ~$ ولا نسخة_احتياطية ولا مكررات
        a = analyze("انقل المكررات", ctx3, prov)
        assert [s["target"] for s in a.steps if s["action_type"] == "move"] == ["b.txt"], a.steps
        for q in ("هل أحذف المكررات؟", "هل أحذف المكررات", "احذف المكررات؟"):   # صيغة السؤال تعرض فقط
            a = analyze(q, ctx3, prov)
            assert a.steps and all(s["action_type"] == "inspect" for s in a.steps), (q, a.steps, a.question)
            assert "لم يُحذف" in a.note, a.note
        # لا مكررات حقيقية إذا كانت الأخرى كلها في المستثنى
        ws3b = mk_ws(tmp, "w3b", {"a.txt": "s", "نسخة_احتياطية/a.txt": "s", "~$a.txt": "s"})
        a = analyze("احذف المكررات", listing(ws3b), RuleProvider(workspace=ws3b))
        assert not a.steps and a.note.startswith(INFO_PREFIX), (a.steps, a.note)

        ws3c = mk_ws(tmp, "w3c", {"a.txt": "s", "Backup/a.txt": "s", "b.txt": "t", "Other/b.txt": "t"})
        a = analyze("احذف المكررات", listing(ws3c), RuleProvider(workspace=ws3c))
        assert sorted(s["target"] for s in a.steps) == ["Other/b.txt"], a.steps          # مجلد Backup نسخ متعمدة لا مكررات

        # ================================================================== 4. المتحكم: _slots لا تتلوث بخيط قديم ويُمسح عند الإيقاف/الإلغاء
        ws = mk_ws(tmp, "w4", {"a.txt": "a", "b.txt": "b", "c.pdf": "c"}, dirs=["Docs"])
        led = SQLiteLedger(pathlib.Path(tmp).resolve() / "c4.db", initial_balance=5000, spending_limit=5000)
        gate_in, gate_out = threading.Event(), threading.Event()
        orig_analyze = RuleProvider.analyze

        def slow(self, prompt, context):
            res = orig_analyze(self, prompt, context)
            if prompt.startswith("رتب ملف a.txt"):
                gate_in.set()
                gate_out.wait(5)
            return res
        RuleProvider.analyze = slow
        try:
            c = AssistantController(RuleProvider(), ws, led, approval_timeout=5.0)
            c.submit_request("رتب ملف a.txt")
            assert gate_in.wait(5)
            c.stop()
            assert c._slots == []
            c.submit_request("انسخ الملفات")
            ev = c.wait_for("questions", 5)
            assert ev, "لم يُطرح سؤال الطلب الثاني"
            slots_b = list(c._slots)
            assert slots_b and "source" in slots_b, slots_b
            gate_out.set()
            time.sleep(0.6)   # يكتمل الخيط القديم متأخراً
            assert c._slots == slots_b, (c._slots, slots_b)   # لم يكتب فوق خانات الطلب الجديد
            c.cancel_plan()
            assert c._slots == []
            c.submit_request("انسخ الملفات")
            assert c.wait_for("questions", 5)
            c.stop()
            assert c._slots == []
        finally:
            RuleProvider.analyze = orig_analyze
            gate_out.set()

        # ================================================================== 5. خانة الترتيب: الطلب بنطاق/اسم لا يُسقط
        assert merge_clarification("رتب الملفات", "organize_mode", "النوع") == "رتب الملفات حسب النوع"
        assert merge_clarification("رتب الملفات حسب الحجم", "organize_mode", "حسب النوع") == "رتب الملفات حسب النوع"
        assert merge_clarification("رتب a.txt", "organize_mode", "النوع") is None
        assert merge_clarification("رتب ملفات Docs", "organize_mode", "النوع") is None
        assert merge_clarification("رتب ملفات «صور العائلة»", "organize_mode", "النوع") is None
        assert merge_clarification("رتب الملفات الموجودة في Docs", "organize_mode", "النوع") is None

        # ================================================================== 6. أجوبة الوجهة/الاسم الطبيعية
        for ans in ("مجلد Docs", "المجلد Docs", "في Docs", "داخل Docs", "folder Docs", "في مجلد Docs"):
            assert merge_clarification("انقل a.txt", "dest", ans) == "انقل a.txt إلى Docs", (ans, merge_clarification("انقل a.txt", "dest", ans))
        assert merge_clarification("انقل a.txt", "dest", "مجلد جديد") == "انقل a.txt إلى «مجلد جديد»"   # الاسم الحرفي إن لم يبقَ غيره
        assert merge_clarification("أنشئ مجلد", "folder_name", "مجلد Reports") == "أنشئ مجلد باسم «Reports»"
        assert merge_clarification("أنشئ مجلد", "folder_name", "باسم فواتير 2025") == "أنشئ مجلد باسم «فواتير 2025»"
        assert steps_of(merge_clarification("انقل a.txt", "dest", "مجلد Docs"), ["a.txt", "Docs/", "Docs/n.txt"]) == [("move", "a.txt", "Docs/a.txt")]

        # ================================================================== 7. «ملفات» داخل الاسم المقتبس / اسم الملف تبقى
        m = merge_clarification("انسخ الملفات إلى «ملفات العمل»", "source", "a.txt")
        assert m == "انسخ a.txt إلى «ملفات العمل»", m
        m = merge_clarification("انقل ملفات إلى «files backup»", "source", "a.txt")
        assert m == "انقل a.txt إلى «files backup»", m
        m = merge_clarification("انقل الملفات إلى Docs", "source", "files.txt")
        assert m == "انقل files.txt إلى Docs", m

        # ================================================================== 8. نسخ مكرر لوجهة فيها نسخة مطابقة
        ws = mk_ws(tmp, "w8", {"x.txt": "content", "y.txt": "other", "Backup/x.txt": "content", "Backup/y.txt": "different"})
        prov = RuleProvider(workspace=ws)
        ctx8 = listing(ws)
        a = analyze("انسخ x.txt إلى Backup", ctx8, prov)
        assert not a.steps and a.note.startswith(INFO_PREFIX) and "مطابقة" in a.note, (a.steps, a.note)
        a = analyze("انسخ y.txt إلى Backup", ctx8, prov)              # محتوى مختلف: سلوك (1) محفوظ
        assert [(s["action_type"], s["params"]["dest"]) for s in a.steps] == [("copy", "Backup/y (1).txt")], a.steps
        a = analyze("اعمل نسخة احتياطية من x.txt", ctx8, prov)       # نسخة_احتياطية غير موجودة بعد: نسخ عادي
        assert [s["params"]["dest"] for s in a.steps if s["action_type"] == "copy"] == ["نسخة_احتياطية/x.txt"], a.steps
        # متراكمة سابقاً (x.txt + x (1).txt): النسخة المطابقة الأحدث تُعتبر أيضاً
        (ws / "Backup" / "z (1).txt").write_text("zz", encoding="utf-8")
        (ws / "Backup" / "z.txt").write_text("old", encoding="utf-8")
        (ws / "z.txt").write_text("zz", encoding="utf-8")
        a = analyze("انسخ z.txt إلى Backup", listing(ws), prov)
        assert not a.steps and "مطابقة" in a.note, (a.steps, a.note)
        # مجموعة: يُنسخ المختلف ويُخطى المطابق مع إعلام
        a = analyze("انسخ x.txt و y.txt إلى Backup", listing(ws), prov)
        assert [s["params"]["dest"] for s in a.steps] == ["Backup/y (1).txt"] and "مطابقة" in (a.note or ""), (a.steps, a.note)

        # ================================================================== 9. ~$ لا تدخل نوعاً (نقل المستندات)
        L9 = ["cv.docx", "~$cv.docx", "n.txt", "Docs/"]
        a = analyze("انقل المستندات إلى Docs", L9)
        assert sorted(s["target"] for s in a.steps) == ["cv.docx", "n.txt"], a.steps
        assert "~$cv.docx" in (a.note or "") and "القفل" in a.note, a.note
        a = analyze("احذف ملفات word", L9)
        assert [s["target"] for s in a.steps] == ["cv.docx"], a.steps

        # ================================================================== 10. المصدر قبل علامة «في/داخل»
        assert merge_clarification("ضع الملفات في Docs", "source", "a.txt") == "ضع a.txt في Docs"
        assert merge_clarification("ضع الملفات داخل Docs", "source", "a.txt") == "ضع a.txt داخل Docs"
        assert merge_clarification("ضع ملف في Docs", "source", "a.txt") == "ضع a.txt في Docs"
        assert steps_of("ضع a.txt في Docs", ["a.txt", "Docs/"]) == [("move", "a.txt", "Docs/a.txt")]

        # ================================================================== 11. الترتيب حسب التاريخ ليس تربيعياً
        src = pathlib.Path(R.__file__).read_text(encoding="utf-8")
        body = src[src.index("def _organize_by_date"):]
        body = body[:body.index("\n    def ", 10)]
        loop_at = body.index("for f in sorted(files")
        assert "names_cf" in body[:loop_at], "casefold set يجب أن يُبنى قبل الحلقة"
        big = mk_ws(tmp, "w11", {f"f{i:04d}.txt": "x" for i in range(1500)})
        t0 = time.time()
        a = RuleProvider(workspace=big).analyze("رتب الملفات حسب الشهر", listing(big))
        assert a.steps and time.time() - t0 < 15, time.time() - t0

        # ================================================================== 12. حدود المسح والإيقاف
        ws = mk_ws(tmp, "w12", {"a.txt": "same", "b.txt": "same", "c.txt": "c"})
        prov = RuleProvider(workspace=ws)
        old = R._FULL_SCAN_LIMIT
        try:
            R._FULL_SCAN_LIMIT = 3
            assert prov._full_ents().partial is False       # 3 عناصر بالضبط ليست اقتطاعاً
            R._FULL_SCAN_LIMIT = 2
            assert prov._full_ents().partial is True
            # تقرير المكررات يذكر الجزئية
            R._FULL_SCAN_LIMIT = 2
            ents = prov._full_ents()
            assert ents.partial
            rep = prov._duplicates_report(ents)
            assert rep.note and ("جزئي" in rep.note or "لم أجد" in rep.note or "مقتطعة" in rep.note), rep.note
            ws2 = mk_ws(tmp, "w12b", {"a.txt": "same", "b.txt": "same"})
            p2 = RuleProvider(workspace=ws2)
            R._FULL_SCAN_LIMIT = 5000
            e2 = p2._full_ents()
            e2.partial = True
            rep = p2._duplicates_report(e2)
            assert "جزئي" in rep.note, rep.note
        finally:
            R._FULL_SCAN_LIMIT = old
        # الإيقاف يقطع تجزئة المكررات
        ws3 = mk_ws(tmp, "w12c", {"a.txt": "same", "b.txt": "same"})
        p3 = RuleProvider(workspace=ws3)
        p3.cancel_check = lambda: True
        try:
            p3.analyze("احذف المكررات", listing(ws3))
            raise AssertionError("كان يجب رفع EmergencyStopTriggered")
        except EmergencyStopTriggered:
            pass
        p3.cancel_check = lambda: False
        assert p3.analyze("احذف المكررات", listing(ws3)).steps
        # المتحكم يمرّر زر الإيقاف إلى قواعد RuleFirstProvider
        class Stub(ModelProvider):
            def generate(self, prompt, system_prompt=""):
                return '{"steps": []}'

            def name(self):
                return "stub"
        led2 = SQLiteLedger(pathlib.Path(tmp).resolve() / "c12.db", initial_balance=5000, spending_limit=5000)
        c2 = AssistantController(RuleFirstProvider(Stub()), ws3, led2, approval_timeout=5.0)
        assert c2.provider.rules.cancel_check is not None
        assert c2.provider.rules.cancel_check() is False
        c2.kill_switch.trigger("اختبار")
        assert c2.provider.rules.cancel_check() is True

    print("PASS: test_b30_rules completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b30_rules", file=sys.stderr)
    sys.exit(1)
