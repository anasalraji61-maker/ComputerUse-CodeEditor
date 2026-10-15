"""اختبارات B34 (نواة): قنبلة النسخ/ميزانية القرص، make_plan_with_note الذرّية، نظافة العرض والطول،
JSON عميق/ضخم، حشو الكلفة، ونسخة مكررة."""
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.planner import (PlanParseError, Planner, validate_step, sanitize_display, _extract_json)
    from assistant.models.base import ModelProvider
    from assistant.models.fake import FakeProvider
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.tools.files import FileManager, CopyBudgetExceededError

    def raises(exc, fn, *a, **k):
        try:
            fn(*a, **k)
        except exc:
            return True
        raise AssertionError(f"توقعت {exc.__name__}")

    def st(i, a, t, **p):
        return {"step_id": i, "action_type": a, "target": t, "params": p}

    def mk(tmp, steps, balance=100):
        root = pathlib.Path(tmp).resolve() / "work"
        root.mkdir(parents=True, exist_ok=True)
        led = SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db", initial_balance=balance, spending_limit=balance)
        return root, TaskRunner(root, FakeProvider(default_response=json.dumps({"steps": steps})), led)

    # ---------- 1. قنبلة النسخ ----------
    with tempfile.TemporaryDirectory() as tmp:
        steps = []
        for i in range(12):
            steps.append(st(2 * i + 1, "copy", "X", dest=f"Z{i}"))
            steps.append(st(2 * i + 2, "copy", f"Z{i}", dest=f"X/Zc{i}"))
        root, r = mk(tmp, steps)
        (root / "X").mkdir()
        (root / "X" / "a.bin").write_bytes(b"A" * 1024)
        plan = r.make_plan("rtb")
        rep = r.run("bomb", "x", plan=plan)
        assert not rep.success and rep.executed_steps == [], rep
        assert any("داخل شجرة مصدر" in f for f in rep.failures), rep.failures
        total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.name.startswith(".undo_journal"))
        assert total == 1024, total  # لم يُنفَّذ شيء

    # الوجهة داخل مصدر الخطوة نفسها
    with tempfile.TemporaryDirectory() as tmp:
        root, r = mk(tmp, [st(1, "copy", "D", dest="D/sub")])
        (root / "D").mkdir()
        (root / "D" / "f").write_bytes(b"x")
        raises(ValueError, r._preflight_budget, r.make_plan("x"))

    # تضخم خطي: نسخ عدة مرات يتجاوز سقف المهمة (سقف صغير للاختبار) قبل أي تنفيذ
    with tempfile.TemporaryDirectory() as tmp:
        steps = [st(i, "copy", "big.bin", dest=f"c{i}.bin") for i in range(1, 6)]
        root, r = mk(tmp, steps)
        (root / "big.bin").write_bytes(b"B" * 300_000)
        r.file_manager.max_copy_bytes = 1_000_000  # 5 x 300KB = 1.5MB > 1MB
        rep = r.run("lin", "x", plan=r.make_plan("x"))     # بلا approver: يتجاوز السقف فيُرفض
        assert not rep.success and "الحدّ المعتاد" in rep.failures[0] and "رفضتَ" in rep.failures[0], rep.failures
        assert not (root / "c1.bin").exists()
        # بموافقة صريحة يُنفَّذ (حتى فوق السقف)، وبرفضها لا
        asked = []
        r.approver = lambda stp: (asked.append(stp), False)[1]
        rep = r.run("lin_no", "x", plan=r.make_plan("x"))
        assert not rep.success and asked and "warning" in asked[0].params and not (root / "c1.bin").exists()
        assert any("رُفضت" in x for x in rep.approvals), rep.approvals
        r.approver = lambda stp: True
        rep = r.run("lin_yes", "x", plan=r.make_plan("x"))
        assert rep.success and (root / "c5.bin").exists(), rep.failures
        assert any("تمت الموافقة" in x for x in rep.approvals), rep.approvals
        r.undo_task("lin_yes")
        r.approver = None
        # تحت السقف يعمل
        r.file_manager.max_copy_bytes = 2_000_000
        rep = r.run("lin2", "x", plan=r.make_plan("x"))
        assert rep.success, rep.failures
        # التراجع يذكر الحجم
        r.file_manager.undo_notes.clear()
        assert r.undo_task("lin2") >= 5
        assert any("KB" in n or "MB" in n for n in r.file_manager.undo_notes), r.file_manager.undo_notes

    # وجهة نسخة سابقة تُعدّ مصدراً جديداً: سلسلة نسخ A->B->C->D تُحسب 3 أضعاف
    with tempfile.TemporaryDirectory() as tmp:
        steps = [st(1, "copy", "A", dest="B"), st(2, "copy", "B", dest="C"), st(3, "copy", "C", dest="D")]
        root, r = mk(tmp, steps)
        (root / "A").mkdir()
        (root / "A" / "f.bin").write_bytes(b"1" * 100)
        chk = r._preflight_budget(r.make_plan("x"), max_bytes=300)       # بالضبط 3 x 100
        assert chk.total == 300 and not chk.over_cap, (chk.total, chk.over_cap)
        chk = r._preflight_budget(r.make_plan("x"), 299)
        assert chk.over_cap and chk.total == 300

    # مساحة القرص غير كافية
    with tempfile.TemporaryDirectory() as tmp:
        root, r = mk(tmp, [st(1, "copy", "f.bin", dest="g.bin")])
        (root / "f.bin").write_bytes(b"1" * 5000)
        class Usage:  # noqa
            total, used, free = 10 ** 9, 10 ** 9 - 1000, 1000
        with mock.patch("shutil.disk_usage", lambda p: Usage):
            rep = r.run("nospace", "x", plan=r.make_plan("x"))
        assert not rep.success and "المساحة الحرة" in rep.failures[0], rep.failures
        assert not (root / "g.bin").exists()

    # حارس وقت التنفيذ في FileManager.copy (مستدعٍ خارج الخطة)
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp) / "w"
        fm = FileManager(ws)
        (ws / "f.bin").write_bytes(b"1" * 400)
        fm.max_copy_bytes = 1000
        fm.copy("f.bin", "a.bin")
        fm.copy("f.bin", "b.bin")
        raises(CopyBudgetExceededError, fm.copy, "f.bin", "c.bin")  # 1200 > 1000
        assert not (ws / "c.bin").exists()
        fm.reset_copy_budget()
        fm.copy("f.bin", "c.bin")
        assert (ws / "c.bin").exists()
        # ميزانية لكل مهمة
        fm2 = FileManager(ws)
        fm2.max_copy_bytes = 500
        fm2.current_task = "t1"; fm2.copy("f.bin", "d1.bin")
        fm2.current_task = "t2"; fm2.copy("f.bin", "d2.bin")
        fm2.current_task = "t1"; raises(CopyBudgetExceededError, fm2.copy, "f.bin", "d3.bin")
        # مجلد: تقاس الشجرة كلها
        (ws / "tree").mkdir(); (ws / "tree" / "x").write_bytes(b"1" * 300); (ws / "tree" / "y").write_bytes(b"1" * 300)
        fm3 = FileManager(ws); fm3.max_copy_bytes = 500
        raises(CopyBudgetExceededError, fm3.copy, "tree", "tree2")
        assert not (ws / "tree2").exists()

    # الخطط العادية لا تتأثر
    with tempfile.TemporaryDirectory() as tmp:
        steps = [st(1, "create_dir", "Arc"), st(2, "copy", "a.txt", dest="Arc/a.txt"), st(3, "copy", "d", dest="Arc/d")]
        root, r = mk(tmp, steps)
        (root / "a.txt").write_text("hi"); (root / "d").mkdir(); (root / "d" / "q").write_text("q")
        rep = r.run("ok", "x", plan=r.make_plan("x"))
        assert rep.success, rep.failures

    # ---------- 2. make_plan_with_note ذرّية ----------
    class Notes(ModelProvider):
        def __init__(self):
            self.gate = threading.Event()
        def generate(self, prompt, system_prompt=""):
            if "OLD" in prompt:
                self.gate.wait(5)
                return json.dumps({"steps": [st(1, "inspect", "a.txt")], "note": "NOTE-OLD"})
            return json.dumps({"steps": [st(1, "inspect", "b.txt")], "note": None})
        def name(self):
            return "notes"

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve() / "w"; root.mkdir()
        led = SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db")
        prov = Notes()
        r = TaskRunner(root, prov, led)
        out = {}
        t = threading.Thread(target=lambda: out.update(old=r.make_plan_with_note("OLD")))
        t.start()
        plan_new, note_new = r.make_plan_with_note("new")   # يكتمل أولاً
        prov.gate.set(); t.join(5)
        assert note_new is None, note_new                   # ملاحظة الطلب القديم لم تتسرب
        assert out["old"][1] == "NOTE-OLD" and out["old"][0][0].target == "a.txt"
        # الغلاف القديم ما زال يعمل
        r2 = Planner(FakeProvider(default_response=json.dumps({"steps": [st(1, "inspect", "a")], "note": "n"})))
        assert len(r2.plan("x")) == 1 and r2.last_note == "n"
        steps2, note2 = r2.plan_with_note("x")
        assert note2 == "n" and steps2[0].target == "a"
        assert r.make_plan("new")[0].target == "b.txt"

    # ---------- 3. نظافة العرض والطول ----------
    def vs(target=None, **kw):
        item = {"action_type": kw.pop("action", "delete"), "target": target or "a.txt"}
        if kw:
            item["params"] = kw
        return validate_step(item, 1)

    raises(PlanParseError, vs, "report.pdf️")
    raises(PlanParseError, vs, "report.pdf\U000e0100")
    raises(PlanParseError, vs, "r" + "̶" * 200 + ".pdf")
    raises(PlanParseError, vs, "r̶.pdf")                      # علامة دمج غير عربية
    for slash in "∕⁄⧸／＼":
        raises(PlanParseError, vs, f"Taxes{slash}2024.pdf")
        raises(PlanParseError, vs, action="move", target="a", dest=f"d{slash}x")
        raises(PlanParseError, vs, action="rename", target="a", new_name=f"n{slash}x")
    raises(PlanParseError, vs, "a/" * 1500 + "x.pdf")
    raises(PlanParseError, vs, "x" * 261)
    ok260 = ("x" * 129 + "/") * 2            # 260 محرفاً بمقطعين
    assert len(ok260) == 260 and vs(ok260).target == ok260
    raises(PlanParseError, vs, "x" * 256)   # مقطع واحد أطول من 255 بايتاً
    raises(PlanParseError, vs, action="move", target="a", dest="d" * 261)
    raises(PlanParseError, vs, action="copy", target="a", dest="x" * 300 + "/y")
    from assistant.tools import files as _ft
    if _ft.IS_WINDOWS:   # ويندوز: الحد 255 وحدة UTF-16 (ن = وحدة واحدة)
        raises(PlanParseError, vs, action="rename", target="a", new_name="ن" * 256)
        assert vs(action="rename", target="a", new_name="ن" * 255).params["new_name"] == "ن" * 255
    else:               # غيره: 255 بايت UTF-8 (ن = بايتان)
        raises(PlanParseError, vs, action="rename", target="a", new_name="ن" * 128)   # 256 بايتاً
        assert vs(action="rename", target="a", new_name="ن" * 127).params["new_name"] == "ن" * 127
    raises(PlanParseError, vs, action="rename", target="a", new_name="n" * 256)
    raises(PlanParseError, vs, action="move", target="a", dest=("d" * 200 + "/") + "e" * 256)
    raises(PlanParseError, vs, action="run_command", target="where", cmd=["where", "z" * 121])
    assert vs(action="run_command", target="where", cmd=["where", "z" * 120]).params["cmd"][1] == "z" * 120
    raises(PlanParseError, vs, action="run_command", target="where", cmd=["where"] * 17)
    # أسماء عربية مشكّلة مشروعة تمر (حتى حركتان متتاليتان)
    assert vs("فواتير_٢٠٢٤/مُحَمَّد.pdf").target.endswith("pdf")
    assert vs("كِتَابٌ.txt").target == "كِتَابٌ.txt"
    # (1) أسماء مشروعة بكل اللغات تمر، والدالة الموحّدة نفسها تستعملها القواعد وقائمة المجلد
    from assistant.tools.files import name_problem
    for good in ["cafe\u0301.txt", "ไทย\u0e48.txt", "हिन्दी.txt", "שָׁלוֹם.txt", "❤\ufe0f.txt", "1\ufe0f\u20e3.txt",
                 "نحن\u200cهم.txt", "👨\u200d👩\u200d👧.txt", "Screenshot 10.00.00\u202fAM.png", "10\u00a0000.txt", "1\u20442 cup.txt",
                 "葛\U000e0100.txt", "﷽ قرآن\u06d6.txt"]:
        assert name_problem(good) is None, (good, name_problem(good))
        assert vs(good).target == good
    for bad in ["f\ufe0f", "a\u200b", "a\u200ca\u200d\u200da", "\u200da", "a\u00a0\u00a0b", "\u00a0a", "a\u00a0b", "a\u202fb", "5\u202fMB", "1\u00a0 2", "a\u2215b", "r\u0336x",
                "x" + "\u0301" * 9, "a\u3164b", "a\u202eb", "a\u2044b"]:
        assert name_problem(bad), repr(bad)
    # القواعد وقائمة المجلد تتفقان مع المخطط: اسم يرفضه المخطط لا يظهر ولا تُبنى عليه خطة
    with tempfile.TemporaryDirectory() as tmp:
        from assistant.models.rules import RuleProvider
        root = pathlib.Path(tmp).resolve() / "w"; root.mkdir()
        (root / "ok\u0301.txt").write_text("1")
        (root / "bad\u200b.txt").write_text("1")
        (root / "نحن\u200cهم.txt").write_text("1")
        led = SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db")
        r = TaskRunner(root, RuleProvider(), led)
        listing = r.workspace_listing()
        assert "ok\u0301.txt" in listing and "bad\u200b.txt" not in listing and "نحن\u200cهم.txt" in listing, listing
        plan, note = r.make_plan_with_note("انسخ ملفات txt إلى Bk")
        for stp in plan:
            validate_step({"action_type": stp.action_type, "target": stp.target, "params": stp.params}, 1)
        # خطوات تحمل اسماً مخادعاً من القواعد تُتخطى بملاحظة
        prov = RuleProvider()
        prov.analyze = lambda p, c: type("A", (), {"steps": [st(1, "inspect", "a.txt"), st(2, "inspect", "b\u202e.txt")],
                                                   "question": None, "note": None, "slot": None})()
        out = json.loads(prov.generate("x", ""))
        assert [x["target"] for x in out["steps"]] == ["a.txt"] and "تخطّيتُ 1" in out["note"], out
    assert vs(action="rename", target="a", new_name="الفَاتِحَة.txt").params["new_name"] == "الفَاتِحَة.txt"
    assert vs("ك" + "\u064b\u064c\u064d.txt").target                  # 3 حركات مقبولة الآن (حتى 8)
    raises(PlanParseError, vs, "ك" + "\u064b" * 9 + ".txt")           # 9 مرفوضة
    raises(PlanParseError, vs, "ً.txt")                         # حركة بلا حرف
    # شروط القبول: تُقصّ وتُنظّف
    s = validate_step({"action_type": "inspect", "target": "a", "acceptance_criteria": "x\n‮" + "y" * 5000}, 1)
    assert len(s.acceptance_criteria) <= 301 and "\n" not in s.acceptance_criteria and "‮" not in s.acceptance_criteria
    s = validate_step({"action_type": "inspect", "target": "a", "acceptance_criteria": {"x": 1}}, 1)
    assert isinstance(s.acceptance_criteria, str)
    # sanitize_display
    assert sanitize_display("a️b" + "̶" * 3) == "ab̶̶"
    assert len(sanitize_display("x" * 10 ** 6, 50)) <= 51

    # ---------- 4. JSON عميق / ضخم ----------
    deep = "{\"steps\":" + "[" * 200000 + "]" * 200000 + "}"
    raises(PlanParseError, _extract_json, deep)
    raises(PlanParseError, Planner(FakeProvider(default_response=deep)).plan, "x", "ctx")
    raises(PlanParseError, _extract_json, "[" + "1," * 600000 + "1]")                   # > 1MB
    raises(PlanParseError, _extract_json, " " * 1_100_000 + "{}")
    assert _extract_json("```json\n{\"steps\": []}\n```") == {"steps": []}

    # ---------- 5. حشو الكلفة ----------
    with tempfile.TemporaryDirectory() as tmp:
        pad = [st(i, "inspect", ".") for i in range(1, 51)]
        root, r = mk(tmp, pad)
        rep = r.run("pad", "رتب")
        assert rep.success and rep.points_charged == 1, (rep.success, rep.points_charged)
        pad2 = [st(1 + 2 * i, "inspect", "a") for i in range(25)] + [st(2 + 2 * i, "create_dir", "d") for i in range(25)]
        pad2 = [pad2[i // 2 + (25 if i % 2 else 0)] for i in range(50)]            # تناوب
        pad2 = [dict(s, step_id=i + 1) for i, s in enumerate(pad2)]
        assert SQLiteLedger.effective_step_count([validate_step(x, 1) for x in pad2]) == 2
    # الخطط العادية: نفس الكلفة
    with tempfile.TemporaryDirectory() as tmp:
        led = SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db")
        normal = [validate_step(st(i, "inspect", f"f{i}"), i) for i in range(1, 18)]
        assert led.estimate_cost("x", steps_count=17) == led.estimate_cost("x", steps_count=17, steps=normal) == 3
        moves = [validate_step(st(i, "copy", "a", dest=f"b{i}"), i) for i in range(1, 10)]
        assert led.estimate_cost("x", 9, steps=moves) == led.estimate_cost("x", 9) == 2
        assert led.estimate_cost("take a screen", 1, steps=normal[:1]) == 5

    # ---------- 7. نسخة مكررة ----------
    raises(PlanParseError, Planner(FakeProvider(default_response=json.dumps(
        {"steps": [st(1, "copy", "a", dest="b"), st(2, "copy", "a", dest="b")]}))).plan, "x")
    assert len(Planner(FakeProvider(default_response=json.dumps(
        {"steps": [st(1, "copy", "a", dest="b"), st(2, "copy", "a", dest="c")]}))).plan("x")) == 2

    # ---------- R2-3: لا رفض كاذب لـ «تغذية ذاتية» ----------
    with tempfile.TemporaryDirectory() as tmp:
        steps = [st(1, "copy", "docs", dest="archive"), st(2, "copy", "r.pdf", dest="docs/r.pdf")]
        root, r = mk(tmp, steps)
        (root / "docs").mkdir(); (root / "docs" / "a.txt").write_text("a"); (root / "r.pdf").write_bytes(b"1" * 10)
        chk = r._preflight_budget(r.make_plan("x"))
        assert chk.total == 1 + 10 and not chk.over_cap, chk.total
        rep = r.run("fp", "x", plan=r.make_plan("x"))
        assert rep.success, rep.failures
    # لكن حلقة التغذية الفعلية (نسخ ثم صبّ داخل المصدر ثم إعادة نسخ المصدر) مرفوضة
    with tempfile.TemporaryDirectory() as tmp:
        steps = [st(1, "copy", "X", dest="Z0"), st(2, "copy", "Z0", dest="X/c0"), st(3, "copy", "X", dest="Z1")]
        root, r = mk(tmp, steps)
        (root / "X").mkdir(); (root / "X" / "a").write_bytes(b"1")
        raises(ValueError, r._preflight_budget, r.make_plan("x"))
        # الصبّ مرة واحدة بلا إعادة نسخ للمصدر: مسموح (تضاعف مرة واحدة فقط ومحسوب في الميزانية)
        steps = [st(1, "copy", "X", dest="Z0"), st(2, "copy", "Z0", dest="X/c0")]
        root, r = mk(tmp + "/b", steps)
        (root / "X").mkdir(); (root / "X" / "a").write_bytes(b"1")
        assert r._preflight_budget(r.make_plan("x")).total == 2
        # إعادة النسخ عبر اسم أب
        steps = [st(1, "copy", "P/X", dest="Z0"), st(2, "copy", "Z0", dest="P/X/c0"), st(3, "copy", "P", dest="Q")]
        root, r = mk(tmp + "/c", steps)
        (root / "P" / "X").mkdir(parents=True); (root / "P" / "X" / "a").write_bytes(b"1")
        raises(ValueError, r._preflight_budget, r.make_plan("x"))

    # ---------- R2-4: هامش المساحة الحرة ----------
    from assistant.tools.files import free_space_short, free_space_margin
    assert not free_space_short(1024, 10 * 1024 * 1024)              # 1KB تكفيها 10MB (لا 16MB)
    assert not free_space_short(1024, 0) and not free_space_short(1024, None)   # 0 = غير معروف
    assert free_space_short(10 ** 6, 10 ** 6) and not free_space_short(10 ** 6, 10 ** 6 + free_space_margin(10 ** 6))
    assert free_space_margin(10 ** 12) == 64 * 1024 ** 2 and free_space_margin(100) == 64 * 1024

    # ---------- R2-5: طول الاسم بوحدات UTF-16 على ويندوز ----------
    import assistant.tools.files as ft
    name = "😀" * 100          # 100 رمز = 400 بايت UTF-8 = 200 وحدة UTF-16
    with mock.patch.object(ft, "IS_WINDOWS", True):
        assert ft.name_len(name) == 200
        assert vs(action="rename", target="a", new_name=name).params["new_name"] == name
        assert ft.name_len("ن" * 255) == 255
    with mock.patch.object(ft, "IS_WINDOWS", False):
        assert ft.name_len(name) == 400
        raises(PlanParseError, vs, action="rename", target="a", new_name=name)

    # ---------- R2-7: النمذجة (نقل/حذف/آباء ضمنية) ----------
    with tempfile.TemporaryDirectory() as tmp:
        # نقل جزء ثم نسخ الأب: الحجم يُخصم
        steps = [st(1, "move", "P/big.bin", dest="Q/big.bin"), st(2, "copy", "P", dest="P2")]
        root, r = mk(tmp, steps)
        (root / "P").mkdir(); (root / "P" / "big.bin").write_bytes(b"1" * 1000); (root / "P" / "s").write_bytes(b"1" * 5)
        assert r._preflight_budget(r.make_plan("x")).total == 5
        # نسخ ثم نقل الناتج ثم نسخ مكان النقل
        steps = [st(1, "copy", "f.bin", dest="g.bin"), st(2, "move", "g.bin", dest="M/g.bin"), st(3, "copy", "M/g.bin", dest="h.bin")]
        root, r = mk(tmp + "/b", steps)
        (root / "f.bin").write_bytes(b"1" * 100)
        assert r._preflight_budget(r.make_plan("x")).total == 200
        # حذف ثم نسخ الأب
        steps = [st(1, "delete", "P/big.bin"), st(2, "copy", "P", dest="P2")]
        root, r = mk(tmp + "/c", steps)
        (root / "P").mkdir(); (root / "P" / "big.bin").write_bytes(b"1" * 1000); (root / "P" / "s").write_bytes(b"1" * 5)
        assert r._preflight_budget(r.make_plan("x")).total == 5
        # وجهة مجلد أنشأته خطوة سابقة: النسخة تدخله
        steps = [st(1, "create_dir", "D"), st(2, "copy", "f.bin", dest="D")]
        root, r = mk(tmp + "/d", steps)
        (root / "f.bin").write_bytes(b"1" * 7)
        assert r._preflight_budget(r.make_plan("x")).total == 7
        rep = r.run("cd", "x", plan=r.make_plan("x"))
        assert rep.success and (root / "D" / "f.bin").exists(), rep.failures

    # ---------- R2-8: سقف السلة ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp) / "w"
        fm = FileManager(ws)
        (ws / "f.bin").write_bytes(b"1" * 400)
        fm.copy("f.bin", "a.bin")
        fm.undo_all()
        assert any("B" in n for n in fm.undo_notes), fm.undo_notes
        fm.trash_cap_bytes = 100          # السلة (400 بايت) فوق السقف: لا نسخ جديد ولا حذف نهائي آلي
        raises(CopyBudgetExceededError, fm.copy, "f.bin", "b.bin")
        assert (ws / ".trash").exists() and not (ws / "b.bin").exists()
        fm.trash_cap_bytes = None
        fm.copy("f.bin", "b.bin")

    # ---------- R2-9: تدبير العدّاد ----------
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp) / "w"
        fm = FileManager(ws)
        (ws / "f.bin").write_bytes(b"1" * 300)
        fm.max_copy_bytes = 500
        fm.copy("f.bin", "a.bin")
        assert None in fm._copy_spent
        import assistant.tools.files as ft2
        with mock.patch.object(ft2, "COPY_BUDGET_TTL_SECONDS", -1.0):   # منتهي الصلاحية: يُهمل فلا قفل دائم
            fm.copy("f.bin", "b.bin")
        assert fm._copy_spent[None][0] == 300
        # يُصفَّر عند انتهاء المهمة ولا يُمسح بمعرّف مهمة مكرر مرفوض من الدفتر
        root_dir = pathlib.Path(tmp) / "r"; root_dir.mkdir()
        led = SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db")
        rr = TaskRunner(root_dir, FakeProvider(default_response=json.dumps({"steps": [st(1, "copy", "f", dest="g")]})), led)
        (root_dir / "f").write_bytes(b"1" * 10)
        assert rr.run("T1", "x").success
        assert "T1" not in rr.file_manager._copy_spent
        rr.file_manager._copy_spent["T2"] = (50, time.monotonic())
        led.record_task_start("T2", "x", 1)
        rep = rr.run("T2", "x")          # مكرر: لم يبدأ ولم يمسح ميزانية T2
        assert not rep.success and rr.file_manager._copy_spent["T2"][0] == 50

    # ---------- R2-10: النسخ المكرر ----------
    P = lambda steps: Planner(FakeProvider(default_response=json.dumps({"steps": steps}))).plan("x")
    assert len(P([st(1, "copy", "a", dest="b"), st(2, "delete", "b"), st(3, "copy", "a", dest="b")])) == 3   # حذف الوجهة يسمح بنسخ جديد
    raises(PlanParseError, P, [st(1, "copy", "a", dest="b"), st(2, "copy", "a", dest="b")])
    assert len(P([st(1, "copy", "a", dest="b"), st(2, "copy", "a", dest="c"), st(3, "copy", "d", dest="b")])) == 3

    # ---------- قواعد: temp / أداة التنكير ----------
    with tempfile.TemporaryDirectory() as tmp:
        from assistant.models.rules import RuleProvider
        root = pathlib.Path(tmp).resolve() / "w"; root.mkdir()
        for n in ["photo (1).tmp", "photo.jpg", "~$cv.docx", "cv.docx", "x.tmp", "a", "a.txt", "keep.pdf"]:
            (root / n).write_text("x")
        (root / "Docs").mkdir()
        r = TaskRunner(root, RuleProvider(), SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db"))
        plan, note = r.make_plan_with_note("احذف الملفات المؤقتة")
        assert [x.target for x in plan] == ["x.tmp"], [x.target for x in plan]
        (root / "cv.docx").unlink(); (root / "photo.jpg").unlink()    # بلا مستند/صورة أصلية: القفل اليتيم والنسخة اليتيمة يُحذفان كالمعتاد
        plan2, _ = r.make_plan_with_note("احذف الملفات المؤقتة")
        assert sorted(x.target for x in plan2) == ["photo (1).tmp", "x.tmp", "~$cv.docx"], [x.target for x in plan2]
        (root / "cv.docx").write_text("x"); (root / "photo.jpg").write_text("x")
        assert "~$cv.docx" in note and "photo (1).tmp" in note, note
        plan, note = r.make_plan_with_note("create a backup of Docs")
        assert [(x.action_type, x.target) for x in plan] == [("create_dir", "نسخة_احتياطية"), ("copy", "Docs")], plan
        try:
            r.make_plan_with_note("create a copy")
            raise AssertionError("يجب السؤال")
        except PlanParseError:
            pass
        plan, _ = r.make_plan_with_note("copy «a» to Docs")     # الاسم المنصَّص حرفي
        assert [(x.target, x.params["dest"]) for x in plan] == [("a", "Docs/a")]

    # ---------- R2b: دقة المحاكاة ----------
    from assistant.models.fake import FakeProvider as _FP
    # (1) المجموع كامل لا مبتور بسقف المسح: الموافقة تُعرض بالرقم الحقيقي والتنفيذ ينجح
    with tempfile.TemporaryDirectory() as tmp:
        root, r = mk(tmp, [st(1, "create_dir", "pre"), st(2, "copy", "big", dest="big2")])
        (root / "big").mkdir()
        for i in range(300):
            (root / "big" / f"f{i}").write_bytes(b"x" * 300)
        r.file_manager.max_copy_bytes = 1000
        seen = []
        r.approver = lambda stp: (seen.append(stp.params.get("warning")), True)[1]
        chk = r._preflight_budget(r.make_plan("x"))
        assert chk.total == 90000 and chk.over_cap and chk.exact, chk.total
        rep = r.run("big", "x", plan=r.make_plan("x"))
        assert rep.success, rep.failures
        assert "87.9 KB" in seen[0], seen
    # مسح يتجاوز الحدّ: يُعلَن غير دقيق ويطلب موافقة
    import assistant.runner as rn
    with tempfile.TemporaryDirectory() as tmp:
        root, r = mk(tmp, [st(1, "copy", "big", dest="big2")])
        (root / "big").mkdir()
        for i in range(30):
            (root / "big" / f"f{i}").write_bytes(b"x" * 10)
        with mock.patch.object(rn, "_SIM_MAX_ENTRIES", 10):
            chk = r._preflight_budget(r.make_plan("x"))
        assert not chk.exact and chk.over_cap
    # (2) مجلدات فرعية بعد إعادة التسمية والنقل
    with tempfile.TemporaryDirectory() as tmp:
        steps = [st(1, "rename", "Docs", new_name="Archive"), st(2, "move", "big.bin", dest="Archive/sub"),
                 st(3, "copy", "Archive/sub/big.bin", dest="o1.bin"), st(4, "copy", "Archive/sub/big.bin", dest="o2.bin")]
        root, r = mk(tmp, steps)
        (root / "Docs" / "sub").mkdir(parents=True); (root / "big.bin").write_bytes(b"x" * 1000)
        assert r._preflight_budget(r.make_plan("x")).total == 2000
        r.file_manager.max_copy_bytes = 1500
        r.approver = lambda stp: True
        rep = r.run("sub", "x", plan=r.make_plan("x"))
        assert rep.success, rep.failures
    # محاكاة مقابل الواقع على خطط عشوائية (قطعية)
    import random, shutil
    from assistant.planner import PlanStep
    mism = 0
    for seed in range(40):
        rnd = random.Random(seed)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp).resolve()
            w1 = tmp / "w1"
            for d in ("A", "B", "C/sub"):
                (w1 / d).mkdir(parents=True)
            for i in range(rnd.randint(3, 7)):
                (w1 / rnd.choice(["A", "B", "C", "C/sub", "."]) / f"f{i}.bin").write_bytes(b"x" * rnd.randint(1, 50))
            shutil.copytree(w1, tmp / "w2")
            fm = FileManager(w1); fm.max_copy_bytes = None; fm.trash_cap_bytes = None; fm.current_task = "t"
            plan, sid, newn = [], 0, 0
            for _ in range(rnd.randint(6, 14)):
                items = [q.relative_to(w1) for q in sorted(w1.rglob("*")) if not q.relative_to(w1).parts[0].startswith(".")]
                if not items:
                    break
                sid += 1
                a = rnd.choice(["copy", "copy", "move", "rename", "delete", "create_dir"])
                src = rnd.choice(items).as_posix()
                try:
                    if a == "create_dir":
                        newn += 1; t = f"N{newn}/x"; fm.create_dir(t); plan.append(PlanStep(sid, a, t, {}))
                    elif a == "delete":
                        fm.delete(src); plan.append(PlanStep(sid, a, src, {}))
                    elif a == "rename":
                        newn += 1; nn = f"R{newn}"; fm.rename(src, nn); plan.append(PlanStep(sid, a, src, {"new_name": nn}))
                    else:
                        dirs = [q for q in items if (w1 / q).is_dir()]
                        if rnd.random() < 0.5 and dirs:
                            d = rnd.choice(dirs)
                            if (w1 / d / pathlib.PurePosixPath(src).name).exists():
                                sid -= 1; continue
                            dest = d.as_posix()
                        else:
                            newn += 1; dest = f"D{newn}/{pathlib.PurePosixPath(src).name}" if rnd.random() < 0.5 else f"D{newn}"
                        (fm.copy if a == "copy" else fm.move)(src, dest)
                        plan.append(PlanStep(sid, a, src, {"dest": dest}))
                except Exception:
                    sid -= 1
            real = fm._copy_spent.get("t", (0, 0))[0]
            rr = TaskRunner(tmp / "w2", _FP(default_response="{}"), SQLiteLedger(db_path=tmp / "l.db"))
            rr.file_manager.max_copy_bytes = None
            try:
                got = rr._preflight_budget(plan).total
            except ValueError:
                continue
            if got != real:
                mism += 1
    assert mism == 0, mism
    # (6) نقل مجلد إلى داخل نفسه يُكشف قبل التنفيذ؛ وسقوط الصبّ بعد نقله/حذفه يمنع الرفض الكاذب
    with tempfile.TemporaryDirectory() as tmp:
        root, r = mk(tmp, [st(1, "create_dir", "New"), st(2, "move", "D", dest="D/sub")])
        (root / "D").mkdir(); (root / "D" / "f").write_text("1")
        rep = r.run("mv", "x", plan=r.make_plan("x"))
        assert not rep.success and rep.executed_steps == [] and not (root / "New").exists(), rep.failures
    for variant in (st(3, "delete", "X/c0"), st(3, "move", "X/c0", dest="Y/c0")):
        with tempfile.TemporaryDirectory() as tmp:
            steps = [st(1, "copy", "X", dest="Z0"), st(2, "copy", "Z0", dest="X/c0"), variant, st(4, "copy", "X", dest="Z1")]
            root, r = mk(tmp, steps)
            (root / "X").mkdir(); (root / "X" / "a").write_bytes(b"1")
            r._preflight_budget(r.make_plan("x"))     # لا رفض: الصبّ أُزيل قبل إعادة النسخ
    # (7) رفض/مهلة/إيقاف برسائل مختلفة
    from assistant.executor import ApprovalTimedOut
    for appr, word in ((lambda stp: False, "رفضتَ"), (lambda stp: (_ for _ in ()).throw(ApprovalTimedOut()), "مهلة")):
        with tempfile.TemporaryDirectory() as tmp:
            root, r = mk(tmp, [st(1, "copy", "f.bin", dest="g.bin")])
            (root / "f.bin").write_bytes(b"1" * 100)
            r.file_manager.max_copy_bytes = 10
            r.approver = appr
            rep = r.run("ap", "x", plan=r.make_plan("x"))
            assert not rep.success and word in rep.failures[0] and rep.approvals, rep.failures

    # ---------- R2b: أسماء ----------
    from assistant.tools.files import name_problem as npb
    for good in ["می\u200cخواهم.txt", "क्\u200dष.txt", "葛\U000e0100.txt", "葛\ufe00.txt", "1\ufe0f\u20e3.txt", "10\u00a0000.txt",
                 "Screenshot 10.00.00\u202fAM.png", "\u1820\u180b.txt", "❤\ufe0f.txt", "👨\u200d👩.txt"]:
        assert npb(good) is None, (good, npb(good))
    for bad in ["تقرير\ufe00.txt", "تقرير\ufe0f.txt", "1\ufe0f.txt", "a\u200cb", "1\u200c2", "a\u200db", "report\u202f2024.pdf",
                "report\u00a02024.pdf", "a\u180bb", "a\u2216b", "a\u29f5b", "a\u29f9b", "a\u2571b", "a\u2572b", "a\ufe68b",
                "a\u27cbb", "a\u27cdb", "a\u1735b", "a\u2f03b", "a\u31d3b"]:
        assert npb(bad), repr(bad)

    print("PASS: test_b34_core completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b34_core", file=sys.stderr)
    sys.exit(1)
