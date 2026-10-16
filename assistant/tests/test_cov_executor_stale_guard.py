"""تغطية: حراسة الخطوة بعد التخطيط (executor.stale_reason) — فشل مغلق عند اختفاء الهدف/النسخة المُبقاة أو تغيّر الحجم/الوقت/المحتوى، وغياب أداتي shell/winget."""
import os
import sys
import tempfile
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.executor import Executor, StaleStepError, STALE_REASON, STALE_CONTENT_REASON, STALE_KEEP_REASON
    from assistant.planner import PlanStep
    from assistant.safety import SafetyGuard
    from assistant.budget import BudgetTracker
    from assistant.tools.files import FileManager, content_fingerprint

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        root = base / "ws"
        root.mkdir()
        fm = FileManager(root, key_dir=base / "keys")
        ex = Executor(fm, SafetyGuard(), BudgetTracker(max_steps=50, max_points=50), approver=lambda s: True)

        v, k = root / "victim.txt", root / "keep.txt"
        v.write_text("same", encoding="utf-8")
        k.write_text("same", encoding="utf-8")
        st = os.lstat(str(v))
        good = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "hash": content_fingerprint(v),
                "keep": "keep.txt", "keep_size": os.lstat(str(k)).st_size, "keep_hash": content_fingerprint(k)}

        def step(guard=None, target="victim.txt", action="delete"):
            return PlanStep(1, action, target, {"guard": guard} if guard is not None else {})

        # الحارس السليم: لا سبب للتخطي
        assert ex.stale_reason(step(dict(good))) is None
        # مسار مرفوض (خارج الجذر): يعالجه المدير لا الحارس
        assert ex.stale_reason(step(dict(good), target="../outside.txt")) is None
        # خطوة بلا حارس ولا بصمة
        assert ex.stale_reason(step(None)) is None
        # إجراء غير مُغيِّر لا يُفحص
        assert ex.stale_reason(PlanStep(2, "inspect", "victim.txt", {"guard": dict(good)})) is None

        # الهدف اختفى: ليس ملفاً
        assert ex.stale_reason(step(dict(good), target="gone.txt")) == STALE_REASON
        # الهدف صار مجلداً
        (root / "adir").mkdir()
        assert ex.stale_reason(step(dict(good), target="adir")) == STALE_REASON
        # حجم مختلف / وقت تعديل مختلف / بصمة محتوى مختلفة
        assert ex.stale_reason(step(dict(good, size=good["size"] + 1))) == STALE_CONTENT_REASON
        assert ex.stale_reason(step(dict(good, mtime_ns=good["mtime_ns"] + 1))) == STALE_CONTENT_REASON
        assert ex.stale_reason(step(dict(good, hash="f:deadbeef"))) == STALE_CONTENT_REASON

        # النسخة المُبقاة: اختفت / هي نفس الهدف / مجلد / حجم أو بصمة مختلفة / مسار خارج الجذر
        assert ex.stale_reason(step(dict(good, keep="missing.txt"))) == STALE_KEEP_REASON
        assert ex.stale_reason(step(dict(good, keep="victim.txt"))) == STALE_KEEP_REASON
        assert ex.stale_reason(step(dict(good, keep="adir"))) == STALE_KEEP_REASON
        assert ex.stale_reason(step(dict(good, keep_size=good["keep_size"] + 5))) == STALE_KEEP_REASON
        assert ex.stale_reason(step(dict(good, keep_hash="f:00"))) == STALE_KEEP_REASON
        assert ex.stale_reason(step(dict(good, keep="../escape.txt"))) == STALE_KEEP_REASON

        # بصمة المنسّق (sig) نوع العنصر: ملف صار مجلداً -> تخطٍّ
        sg = PlanStep(3, "move", "victim.txt", {"dest": "m.txt"})
        sg.sig = {"k": "dir"}
        assert ex.stale_reason(sg) == STALE_REASON

        # execute_step يحوّل السبب إلى StaleStepError ولا ينفّذ شيئاً ولا يسأل المستخدم ولا يخصم ميزانية
        asked = []
        ex2 = Executor(fm, SafetyGuard(), BudgetTracker(max_steps=5, max_points=5), approver=lambda s: asked.append(1) or True)
        try:
            ex2.execute_step(step(dict(good, hash="f:deadbeef")))
            raise AssertionError("كان يجب أن يُتخطّى")
        except StaleStepError as exc:
            assert exc.reason == STALE_CONTENT_REASON
        assert v.exists() and not asked and ex2.budget_tracker.current_steps == 0

        # أدوات غير مهيأة: فشل صريح (لا تنفيذ صامت) — بعد الموافقة
        for act, tgt in (("winget_upgrade", "Some.Pkg"), ("run_command", "whoami")):
            ex3 = Executor(fm, SafetyGuard(), BudgetTracker(max_steps=5, max_points=5), approver=lambda s: True)
            try:
                ex3.execute_step(PlanStep(9, act, tgt, {"cmd": ["whoami"]} if act == "run_command" else {}))
                raise AssertionError("كان يجب أن يفشل")
            except RuntimeError as exc:
                assert "غير مهيأة" in str(exc), exc
            assert ex3.executed_log == [] and ex3.collected_evidence == []

    print("PASS: test_cov_executor_stale_guard completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_executor_stale_guard", file=sys.stderr)
    sys.exit(1)
