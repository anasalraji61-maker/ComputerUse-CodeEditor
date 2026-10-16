"""تغطية المنسّق: فشل التخطيط بخطأ عام (بلا خصم)، رفض نقل/نسخ مجلد إلى داخل نفسه قبل أي تنفيذ، امتلاء السلة، فشل تدقيق سجل التراجع،
وعطب الدفتر أو ردّ التقدّم لا يُسقط التقرير."""
import json
import pathlib
import sqlite3
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.models.fake import FakeProvider
    from assistant.models.base import ModelProvider

    def prov(*steps):
        return FakeProvider(default_response=json.dumps({"steps": list(steps)}))

    def st(i, action, target, **params):
        return {"step_id": i, "action_type": action, "target": target, "params": params}

    class Boom(ModelProvider):
        def generate(self, prompt, system_prompt=""):
            raise RuntimeError("network exploded")

        def name(self):
            return "Boom"

    def tx(ledger, task_id):
        with ledger._get_connection() as c:
            r = c.execute("SELECT status, charged_points, reason FROM transactions WHERE task_id = ?", (task_id,)).fetchone()
            return dict(r) if r else None

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        root = base / "work"
        root.mkdir()
        ledger = SQLiteLedger(db_path=base / "l.db", initial_balance=100, spending_limit=100)

        def mk(provider, **kw):
            return TaskRunner(root, provider, ledger, **kw)

        # 1. خطأ عام من المزوّد أثناء التخطيط: فشل بلا خصم ولا أثر على الملفات، والمعاملة FAILED بصفر نقاط
        bal = ledger.get_balance()
        rep = mk(Boom()).run("plan_boom", "x")
        assert not rep.success and any("تعذّر التخطيط" in f for f in rep.failures), rep.failures
        assert rep.executed_steps == [] and rep.points_charged == 0 and ledger.get_balance() == bal
        t = tx(ledger, "plan_boom")
        assert t["status"] == "FAILED" and t["charged_points"] == 0, t

        # 2. نقل/نسخ مجلد إلى داخل نفسه (وجهة غير موجودة) يُرفض في الفحص المسبق قبل تنفيذ الخطوات السابقة له
        (root / "proj").mkdir()
        (root / "proj" / "f.txt").write_text("x", encoding="utf-8")
        for action in ("move", "copy"):
            rep = mk(prov(st(1, "create_dir", "should_not_exist"), st(2, action, "proj", dest="proj/inner"))).run(f"self_{action}", "x")
            assert not rep.success and any("داخل نفسه" in f for f in rep.failures), rep.failures
            assert not (root / "should_not_exist").exists() and not (root / "proj" / "inner").exists()
            assert rep.executed_steps == [] and ledger.get_balance() == bal

        # 3. السلة ممتلئة فوق سقفها: خطة فيها نسخ تُرفض قبل أي تنفيذ
        (root / "src.txt").write_text("payload", encoding="utf-8")
        r3 = mk(prov(st(1, "create_dir", "d3"), st(2, "copy", "src.txt", dest="d3/src.txt")))
        (r3.file_manager.trash_dir / "big.bin").write_bytes(b"0" * 4096)
        r3.file_manager.trash_cap_bytes = 100
        rep = r3.run("trash_full", "x")
        assert not rep.success and any(".trash" in f and "ممتلئة" in f for f in rep.failures), rep.failures
        assert not (root / "d3").exists() and rep.executed_steps == []
        t = tx(ledger, "trash_full")
        assert t["status"] == "FAILED" and t["charged_points"] == 0, t

        # 4. تلف سجل التراجع أثناء التنفيذ يُكتشف: المهمة تفشل بلا خصم (والملفات تغيّرت فعلاً ويبقى التراجع ممكناً من الذاكرة)
        def corrupt_journal(step):
            (root / ".undo_journal.json").write_text("{corrupt", encoding="utf-8")
            return True
        rep = mk(prov(st(1, "create_dir", "j_dir"), st(2, "run_command", "whoami", cmd=["whoami"])), approver=corrupt_journal).run("jr_bad", "x")
        assert not rep.success and any("ملف التراجع" in f for f in rep.failures), rep.failures
        assert (root / "j_dir").exists() and rep.points_charged == 0
        t = tx(ledger, "jr_bad")
        assert t["status"] == "FAILED" and t["charged_points"] == 0, t
        (root / ".undo_journal.json").unlink()

        # 5. عطب ردّ التقدّم (واجهة) لا يكسر التنفيذ
        def bad_progress(ev):
            raise RuntimeError("ui died")
        rep = mk(prov(st(1, "create_dir", "p_dir"))).run("prog_bad", "x", progress=bad_progress)
        assert rep.success and (root / "p_dir").is_dir(), rep.failures

        # 6. فشل تسجيل النجاح في الدفتر: يُعلن الفشل صراحة ولا يُدّعى النجاح، ولو فشل تسجيل الفشل أيضاً لا ينهار التقرير
        r6 = mk(prov(st(1, "create_dir", "l_dir")))

        def fail_success(*a, **k):
            raise sqlite3.OperationalError("db locked")

        def fail_failure(*a, **k):
            raise sqlite3.OperationalError("db locked too")
        r6.ledger = ledger
        orig_s, orig_f = ledger.record_task_success, ledger.record_task_failure
        ledger.record_task_success, ledger.record_task_failure = fail_success, fail_failure
        try:
            rep = r6.run("ledger_down", "x")
        finally:
            ledger.record_task_success, ledger.record_task_failure = orig_s, orig_f
        assert not rep.success and any("تعذّر تسجيل الكلفة" in f for f in rep.failures), rep.failures
        assert (root / "l_dir").exists() and rep.undo_available

    print("PASS: test_cov_runner_failure_paths completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_runner_failure_paths", file=sys.stderr)
    sys.exit(1)
