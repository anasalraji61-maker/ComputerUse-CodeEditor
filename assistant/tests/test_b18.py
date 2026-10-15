"""اختبارات B18: نتائج تدقيق المواصفات — سير التوضيح، الإيقاف أثناء آخر خطوة، التراجع بعد إعادة التشغيل،
تخطّي حذف مرفوض، مهلة الموافقة، تقدير الكلفة، تسعير screen، إصدار winget قبل/بعد."""
import json
import os
import pathlib
import sys
import tempfile
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.executor import ApprovalTimedOut
    from assistant.ledger import SQLiteLedger
    from assistant.models.rules import RuleProvider
    from assistant.planner import PlanStep
    from assistant.runner import TaskRunner
    from assistant.safety import KillSwitch
    from assistant.tools.winget import WingetTool
    from assistant.ui.controller import AssistantController

    def mk(tmp, files=None, dirs=()):
        base = pathlib.Path(tmp).resolve()
        root = base / "ws"
        root.mkdir()
        for d in dirs:
            (root / d).mkdir(parents=True, exist_ok=True)
        for f, c in (files or {}).items():
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            (root / f).write_text(c, encoding="utf-8")
        return base, root, SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500)

    # 1. «لم أفهم الطلب» ليست سؤال توضيح: الرسالة التالية طلب جديد لا يُدمج بالقديم
    with tempfile.TemporaryDirectory() as tmp:
        base, root, led = mk(tmp, {"a.txt": "A", "b.jpg": "B"})
        c = AssistantController(RuleProvider(), root, led)
        c.submit_request("ساعدني")
        err = c.wait_for("error")
        assert err and "رتّب" in err["text"] and "«حدّث البرنامج" in err["text"], err   # نص المساعدة كاملاً بلا قصّ
        assert c.state == "IDLE"
        c.submit_request("رتّب الملفات حسب النوع")
        plan = c.wait_for("plan")
        assert plan and plan["steps"], plan
        # 2. تقدير الكلفة قبل البدء
        est = plan["estimate"]
        assert est["points"] >= 1 and est["balance"] == 500 and est["affordable"] is True, est
        c.cancel_plan()
        # سؤال توضيح حقيقي ثم إجابة تُدمج صحيحاً
        c.submit_request("احذف")
        q = c.wait_for("questions")
        assert q and len(q["questions"]) == 1
        c.answer_questions(["a.txt"])
        plan = c.wait_for("plan")
        assert plan and [s["target"] for s in plan["steps"]] == ["a.txt"], plan
        c.cancel_plan()
        # عجز الرصيد يظهر في التقدير
        led2 = SQLiteLedger(base / "l2.db", initial_balance=0, spending_limit=500)
        c2 = AssistantController(RuleProvider(), root, led2)
        _old_trial = os.environ.get("COS_TRIAL_TOPUP")
        os.environ["COS_TRIAL_TOPUP"] = "0"   # الشحن التجريبي التلقائي يعطّل سيناريو العجز؛ نختبره معطَّلاً
        try:
            c2.submit_request("رتّب الملفات حسب النوع")
            p2 = c2.wait_for("plan")
        finally:
            if _old_trial is None:
                os.environ.pop("COS_TRIAL_TOPUP", None)
            else:
                os.environ["COS_TRIAL_TOPUP"] = _old_trial
        assert p2 and p2["estimate"]["affordable"] is False, p2

    # 3. الإيقاف أثناء آخر خطوة لا يُعلَن نجاحاً صامتاً
    with tempfile.TemporaryDirectory() as tmp:
        base, root, led = mk(tmp)
        ks = KillSwitch()

        def slow_winget(cmd):
            ks.trigger("test")   # المستخدم ضغط «إيقاف» أثناء الخطوة الأخيرة
            return 0, "ok", ""
        r = TaskRunner(root, RuleProvider(), led, kill_switch=ks, approver=lambda s: True, winget_runner=slow_winget,
                       version_probe=lambda pkg: (0, f"{pkg} 2.0", ""))
        rep = r.run("w1", "x", plan=[PlanStep(1, "winget_upgrade", "Git.Git")])
        assert not rep.success and any("الإيقاف" in f for f in rep.failures), rep.failures
        assert rep.points_charged == 0

    # 4. التراجع بعد إعادة التشغيل وتبديل المجلد (وسوم المهمة في السجل)
    with tempfile.TemporaryDirectory() as tmp:
        base, root, led = mk(tmp, {"a.jpg": "J", "b.txt": "T"})
        r1 = TaskRunner(root, RuleProvider(), led)
        assert r1.run("t_one", "رتّب الملفات حسب النوع").success
        assert (root / "صور" / "a.jpg").exists()
        j = json.loads((root / ".undo_journal.json").read_text(encoding="utf-8"))
        assert all(e.get("task") == "t_one" for e in j), j
        c = AssistantController(RuleProvider(), root, led)      # «إعادة تشغيل»: ذاكرة جديدة
        assert c.last_task_id is None and c.runner.last_journaled_task() == "t_one"
        c.undo_last()
        ev = c.wait_for("undo")
        assert ev and ev["count"] >= 1, ev
        assert (root / "a.jpg").exists() and (root / "b.txt").exists() and not (root / "صور").exists()
        # مهمة أقدم لا تُتراجع قبل الأحدث (حدود الوسوم)
        assert r1.run("t_a", "انقل a.jpg إلى P").success and r1.run("t_b", "انقل b.txt إلى Q").success
        r2 = TaskRunner(root, RuleProvider(), led)
        assert r2.undo_task("t_a") == 0 and r2.last_journaled_task() == "t_b"
        assert r2.undo_task("t_b") >= 1 and r2.undo_task("t_a") >= 1
        assert (root / "a.jpg").exists() and (root / "b.txt").exists()

    # 5. screen.png ليس طلب شاشة
    assert SQLiteLedger.estimate_cost("انقل screen.png إلى Pics", 1) == 1
    assert SQLiteLedger.estimate_cost("take a screen capture", 1) == 5

    # 6. رفض حذف واحد يتخطاه فقط؛ المهلة والإيقاف رسائل مستقلة
    with tempfile.TemporaryDirectory() as tmp:
        base, root, led = mk(tmp, {"x1.tmp": "1", "x2.tmp": "2", "x3.tmp": "3"})
        asked = []

        def deny_second(step):
            asked.append(step.target)
            return step.target != "x2.tmp"
        r = TaskRunner(root, RuleProvider(), led, approver=deny_second)
        rep = r.run("d1", "احذف ملفات tmp")
        assert rep.success and len(rep.skipped) == 1 and "x2.tmp" in rep.skipped[0], (rep.failures, rep.skipped)
        assert (root / "x2.tmp").exists() and not (root / "x1.tmp").exists() and not (root / "x3.tmp").exists()
        assert "ما تُخطّي بقرارك" in rep.render_arabic_text() and "FileManager" not in rep.render_arabic_text()
        assert "زر «تراجع»" in rep.render_arabic_text()
        assert 0 < rep.points_charged and r.undo_task("d1") >= 2 and (root / "x1.tmp").exists()
    with tempfile.TemporaryDirectory() as tmp:
        base, root, led = mk(tmp, {"x1.tmp": "1"})
        rep = TaskRunner(root, RuleProvider(), led, approver=lambda s: False).run("d2", "احذف x1.tmp")
        assert not rep.success and any("رفضت كل الخطوات" in f for f in rep.failures) and (root / "x1.tmp").exists()
    with tempfile.TemporaryDirectory() as tmp:
        base, root, led = mk(tmp, {"x1.tmp": "1"})

        def timeout(step):
            raise ApprovalTimedOut()
        rep = TaskRunner(root, RuleProvider(), led, approver=timeout).run("d3", "احذف x1.tmp")
        assert not rep.success and any("انتهت مهلة" in f for f in rep.failures), rep.failures
        assert any("انتهت المهلة" in a for a in rep.approvals) and (root / "x1.tmp").exists()
    with tempfile.TemporaryDirectory() as tmp:
        base, root, led = mk(tmp, {"x1.tmp": "1"})
        ks = KillSwitch()

        def stop_during(step):
            ks.trigger("test")
            return False
        rep = TaskRunner(root, RuleProvider(), led, kill_switch=ks, approver=stop_during).run("d4", "احذف x1.tmp")
        assert not rep.success and any("أُوقفت" in a or "الإيقاف" in a for a in rep.approvals + rep.failures)
        assert not any("رُفضت" in a for a in rep.approvals), rep.approvals

    # 7. إصدار winget قبل/بعد يظهر في الدليل
    with tempfile.TemporaryDirectory() as tmp:
        base, root, led = mk(tmp)
        state = {"v": "1.0"}

        def wg(cmd):
            state["v"] = "2.0"
            return 0, "done", ""
        r = TaskRunner(root, RuleProvider(), led, approver=lambda s: True, winget_runner=wg,
                       version_probe=lambda pkg: (0, f"Name Id Version\nGit Git.Git {state['v']}", ""))
        rep = r.run("g1", "x", plan=[PlanStep(1, "winget_upgrade", "Git.Git")])
        assert rep.success, rep.failures
        assert any("قبل" in str(e.get("value")) and "1.0" in str(e.get("value")) and "2.0" in str(e.get("value")) for e in rep.evidence), rep.evidence

    # 8. winget الافتراضي يُنهى فوراً عند الإيقاف
    t0 = time.time()
    code, out, err = WingetTool(stop_check=lambda: True, timeout=20)._default_runner(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    assert code != 0 and time.time() - t0 < 5 and "الإيقاف" in err, (code, err, time.time() - t0)

    print("PASS: test_b18 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b18", file=sys.stderr)
    sys.exit(1)
