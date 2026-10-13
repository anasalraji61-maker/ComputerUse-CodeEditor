"""اختبارات TaskRunner: الفحص المسبق، الدفتر، الموافقة لكل خطوة، الإيقاف، والتراجع الخاص بالمهمة."""
import json
import sys
import tempfile
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.safety import KillSwitch
    from assistant.models.fake import FakeProvider
    from assistant.tools.files import FileManager

    def prov(*steps):
        return FakeProvider(default_response=json.dumps({"steps": list(steps)}))

    def st(i, action, target, **params):
        return {"step_id": i, "action_type": action, "target": target, "params": params}

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve() / "work"
        root.mkdir()
        ledger = SQLiteLedger(db_path=pathlib.Path(tmp) / "l.db", initial_balance=100, spending_limit=100)

        # 1. نجاح كامل مع أدلة وكلفة مخصومة
        r = TaskRunner(root, prov(st(1, "create_dir", "x"), st(2, "inspect", "x")), ledger)
        rep = r.run("ok1", "مهمة")
        assert rep.success, rep.failures
        assert rep.points_charged == 1 and ledger.get_balance() == 99

        # 2. الخطة غير الصالحة: فشل بلا خصم ولا تنفيذ
        bal = ledger.get_balance()
        rep = TaskRunner(root, FakeProvider(default_response="OK"), ledger).run("bad_plan", "x")
        assert not rep.success and any("خطة غير صالحة" in f for f in rep.failures)
        assert ledger.get_balance() == bal and rep.executed_steps == []

        # 3. فحص مسبق: خطوة محظورة في آخر الخطة يمنع تنفيذ كل الخطوات السابقة
        rep = TaskRunner(root, prov(st(1, "create_dir", "should_not_exist"), st(2, "permanent_delete", "x")), ledger).run("forb", "x")
        assert not rep.success and not (root / "should_not_exist").exists()
        assert rep.executed_steps == []

        # 4. سقف الميزانية قبل أي تنفيذ
        many = [st(i, "create_dir", f"m{i}") for i in range(1, 15)]
        rep = TaskRunner(root, prov(*many), ledger, max_steps=12).run("budget", "x")
        assert not rep.success and not (root / "m1").exists()

        # 5. R22: لا يبدأ إن كان الرصيد أو السقف لا يكفي
        poor = SQLiteLedger(db_path=pathlib.Path(tmp) / "poor.db", initial_balance=0, spending_limit=10)
        rep = TaskRunner(root, prov(st(1, "create_dir", "poor_dir")), poor).run("poor", "x")
        assert not rep.success and not (root / "poor_dir").exists()
        limited = SQLiteLedger(db_path=pathlib.Path(tmp) / "lim.db", initial_balance=50, spending_limit=1)
        steps9 = [st(i, "create_dir", f"lim{i}") for i in range(1, 10)]  # 9 خطوات = 2 نقطة > السقف 1
        rep = TaskRunner(root, prov(*steps9), limited).run("lim", "x")
        assert not rep.success and not (root / "lim1").exists(), rep.failures
        assert limited.get_balance() == 50

        # 6. R16: الموافقة لكل خطوة (تُرفض الثانية فقط) وتُسجَّل
        decisions = []
        def approver(step):
            decisions.append(step.step_id)
            return step.step_id == 2
        (root / "src.txt").write_text("data", encoding="utf-8")
        rep = TaskRunner(
            root,
            prov(st(1, "run_command", "whoami", cmd=["whoami"]), st(2, "run_command", "whoami", cmd=["whoami"]), st(3, "create_dir", "after")),
            ledger, approver=approver,
        ).run("appr", "x")
        assert not rep.success and decisions == [1], decisions  # الأولى رُفضت فتوقف التنفيذ
        assert not (root / "after").exists()
        assert any("رُفضت" in a for a in rep.approvals)

        # 7. الإيقاف أثناء التنفيذ + R22 undo_task يتراجع عن عمليات المهمة فقط
        ks = KillSwitch()
        runner = TaskRunner(
            root,
            prov(st(1, "create_dir", "k1"), st(2, "move", "src.txt", dest="k1/src.txt"), st(3, "run_command", "whoami", cmd=["whoami"]), st(4, "create_dir", "k2")),
            ledger, kill_switch=ks, approver=lambda s: (ks.trigger("ضغط الإيقاف") or True),
        )
        pre_ops = len(runner.file_manager.journal)
        rep = runner.run("kill", "x")
        assert not rep.success and any("الإيقاف" in f for f in rep.failures), rep.failures
        assert (root / "k1" / "src.txt").exists() and not (root / "k2").exists()
        assert rep.undo_available
        assert runner.undo_task("kill") == 2
        assert (root / "src.txt").exists() and not (root / "k1").exists()
        assert len(runner.file_manager.journal) == pre_ops  # عمليات المهام السابقة محفوظة

        # 8. الموافقة الصحيحة لخطوة خطرة تنجح وتُدقَّق
        rep = TaskRunner(root, prov(st(1, "run_command", "whoami", cmd=["whoami"])), ledger, approver=lambda s: True).run("whoami_ok", "x")
        assert rep.success, rep.failures

        # 9. تلف سجل التراجع بعد التنفيذ يُكتشف
        runner2 = TaskRunner(root, prov(st(1, "create_dir", "j1")), ledger)
        runner2.verifier.verify_journal  # موجود
        rep = runner2.run("jr", "x")
        assert rep.success
        (root / ".undo_journal.json").write_text("{corrupt", encoding="utf-8")
        ok, _, err = runner2.verifier.verify_journal(
            [type("S", (), {"step_id": 1, "action_type": "create_dir"})()], {1: {"exists": False}}, 0)
        assert not ok

    # 10. R23: نسخ مجلد كبير يتوقف عند الإيقاف ولا يترك ناتجاً جزئياً في المسار الهدف
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve()
        ks = KillSwitch()
        fm = FileManager(root, kill_switch=ks)
        big = root / "big"
        big.mkdir()
        for i in range(30):
            (big / f"f{i}.txt").write_text(str(i), encoding="utf-8")
        ks.trigger("stop")
        try:
            fm.copy("big", "big_copy")
            raise AssertionError("كان يجب أن يتوقف النسخ")
        except Exception as exc:
            assert type(exc).__name__ == "EmergencyStopTriggered", exc
        assert not (root / "big_copy").exists()
        assert len(fm.journal) == 0
        ks.reset()
        out = fm.copy("big", "big_copy")
        assert len(list(out.iterdir())) == 30

    print("PASS: test_runner completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_runner", file=sys.stderr)
    sys.exit(1)
