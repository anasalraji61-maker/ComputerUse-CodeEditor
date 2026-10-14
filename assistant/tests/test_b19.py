"""اختبارات B19: تحصين أمني (المراجعة السادسة) — روابط .trash، مسارات UNC، أسماء 8.3، نقل مجلد لداخل نفسه،
معرّف الموافقة، الإيقاف تحت القفل، تنظيف نصوص النموذج، موجّه النماذج، محارف فراغ خفية، winget --exact."""
import json
import os
import pathlib
import sys
import tempfile
import threading
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.config import validate_workspace, WorkspaceError
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import CONTEXT_MARKER
    from assistant.models.router import ModelRouter, SMALL, LARGE
    from assistant.models.rules import RuleProvider
    from assistant.planner import PlanParseError, validate_step, sanitize_display
    from assistant.tools.files import FileManager, PathOutOfBoundsError, ProtectedPathError, is_unc_like
    from assistant.tools.winget import WingetTool
    from assistant.verifier import Verifier, PathEscapeError
    from assistant.ui.controller import AssistantController

    def raises(exc, fn, *a, **k):
        try:
            fn(*a, **k)
        except exc:
            return True
        except Exception as e:
            raise AssertionError(f"توقعت {exc.__name__} فجاء {type(e).__name__}: {e}")
        raise AssertionError(f"توقعت {exc.__name__} ولم يُرفع شيء")

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()

        # 1. .trash رابط إلى خارج مجلد العمل ← رفض صريح، ولا ينتقل شيء إلى الخارج
        ws = base / "ws"; ws.mkdir()
        outside = base / "outside"; outside.mkdir()
        (ws / "keep.txt").write_text("x", encoding="utf-8")
        try:
            os.symlink(str(outside), str(ws / ".trash"), target_is_directory=True)
            can_link = True
        except (OSError, NotImplementedError):
            can_link = False
        if can_link:
            raises(ProtectedPathError, FileManager, ws)
            assert not list(outside.iterdir())
            (ws / ".trash").unlink()
        # .trash ملف عادي ← خطأ واضح لا FileExistsError
        (ws / ".trash").write_text("x", encoding="utf-8")
        raises(ProtectedPathError, FileManager, ws)
        (ws / ".trash").unlink()
        # ملف السجل مجلد ← خطأ واضح
        (ws / ".undo_journal.json").mkdir()
        raises(ProtectedPathError, FileManager, ws)
        (ws / ".undo_journal.json").rmdir()
        fm = FileManager(ws)
        assert fm.trash_dir.is_dir()

        # 2. مسارات UNC/الأجهزة
        for bad in ("\\\\server\\share\\x", "//server/share/x", "\\\\?\\C:\\x", "\\\\.\\PhysicalDrive0", " //x/y"):
            assert is_unc_like(bad), bad
            raises(PathOutOfBoundsError, fm.validate_path, bad)
            raises(PathEscapeError, Verifier(ws).confine, bad)
        assert not is_unc_like("a/b.txt") and not is_unc_like("a\\b.txt")
        for key in ("target", "dest"):
            item = {"action_type": "move", "target": "a.txt", "params": {"dest": "b/x"}}
            if key == "target":
                item["target"] = "\\\\srv\\sh\\a"
            else:
                item["params"]["dest"] = "//srv/sh/a"
            raises(PlanParseError, validate_step, item, 1)

        # 3. أسماء 8.3 لمجلدات محمية
        for bad in ("GIT~1/x", "ssh~1", ".git~2/a", "AWS~3/c"):
            raises((ProtectedPathError, PathOutOfBoundsError), fm.validate_path, bad)
        (ws / "digit~1.txt").write_text("a", encoding="utf-8")
        fm.validate_path("digit~1.txt")   # اسم عادي يحوي ~ لا يُرفض

        # 4. نقل مجلد إلى داخل نفسه ← رفض قبل أي أثر جانبي
        (ws / "A" / "B").mkdir(parents=True)
        (ws / "A" / "f.txt").write_text("1", encoding="utf-8")
        raises(ValueError, fm.move, "A", "A/B/A")
        raises(ValueError, fm.move, "A", "A/B")
        assert (ws / "A" / "f.txt").exists() and not (ws / "A" / "B" / "A").exists()
        assert not fm.journal, fm.journal

        # 5. الحذف يذهب إلى السلة الحقيقية فقط
        d = fm.delete("keep.txt")
        assert d.parent == fm.trash_dir and d.exists()

        # 6. winget --exact
        seen = []
        wt = WingetTool(runner=lambda cmd: (seen.append(cmd) or (0, "", "")))
        wt.upgrade_package("Git.Git")
        assert "--exact" in seen[0] and seen[0][seen[0].index("--id") + 1] == "Git.Git", seen

        # 7. محارف فراغ خفية وغير قابلة للطباعة في الخطة
        for ch in ("\u3164", "\u2800", "\u115f", "\u1160", "\uffa0", "\u180e", "\u00a0", "\u2003", "\ue000", "\u0007"):
            raises(PlanParseError, validate_step, {"action_type": "delete", "target": "a" + ch + "b.txt"}, 1)
        validate_step({"action_type": "delete", "target": "صور العائلة/a b.txt"}, 1)   # فراغ عادي وعربي سليمان

        # 8. تنظيف نصوص النموذج المعروضة
        dirty = "سؤال\u202e\n\n  مريب\u200b\x07 " + "ط" * 600
        clean = sanitize_display(dirty, 100)
        assert "\n" not in clean and "\u202e" not in clean and "\u200b" not in clean and "\x07" not in clean
        assert len(clean) <= 101 and clean.startswith("سؤال مريب"), clean

        class Q:
            def generate(self, text, system_prompt=""):
                return json.dumps({"questions": ["\u202e؟\nأوافق على الحذف؟\u200b", 5, "", "ok" * 500]})
        led = SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500)
        c = AssistantController(Q(), ws, led)
        qs = c._ask_questions("x")
        assert qs and all("\n" not in q and "\u202e" not in q and len(q) <= 401 for q in qs), qs

        # 9. معرّف الموافقة إلزامي
        from assistant.planner import PlanStep
        res = {}
        def asker():
            res["r"] = c._approve(PlanStep(1, "delete", "a.txt"))
        t = threading.Thread(target=asker); t.start()
        ev = c.wait_for("approval_request")
        assert ev, "لا طلب موافقة"
        c.respond_approval(True)                 # بلا معرّف ← يُتجاهل
        t.join(0.4)
        assert t.is_alive(), "رد بلا معرّف مرّ"
        c.respond_approval(True, ev["step"]["approval_id"] + 99)   # معرّف خاطئ ← يُتجاهل
        t.join(0.3)
        assert t.is_alive()
        c.respond_approval(True, ev["step"]["approval_id"])
        t.join(3)
        assert not t.is_alive() and res["r"] is True

        # 10. الإيقاف لا يضيع بين فحص الحالة وتصفير المفتاح في start()
        c2 = AssistantController(RuleProvider(), ws, SQLiteLedger(base / "l2.db", initial_balance=500, spending_limit=500))
        c2.submit_request("ساعدني")
        c2.wait_for("error")
        c2.stop()
        assert c2.kill_switch.is_triggered
        c2.start()               # لا خطة: لا يصفّر الإيقاف
        assert c2.kill_switch.is_triggered
        # stop يعمل تحت القفل نفسه
        import inspect
        src = inspect.getsource(AssistantController.stop)
        assert src.index("with self._lock") < src.index("kill_switch.trigger"), "stop يجب أن يطلق المفتاح داخل القفل"

        # 11. عطب الدفتر عند تسجيل الفشل لا يُسقط التقرير
        from assistant.runner import TaskRunner
        class BadLedger(SQLiteLedger):
            def record_task_failure(self, *a, **k):
                raise RuntimeError("db locked")
        ws3 = base / "ws3"; ws3.mkdir()
        bl = BadLedger(base / "l3.db", initial_balance=500, spending_limit=500)
        r = TaskRunner(ws3, RuleProvider(), bl)
        rep = r.run("t_fail", "احذف ghost.txt", plan=[PlanStep(1, "delete", "ghost.txt")])
        assert not rep.success and any("الدفتر" in f for f in rep.failures), rep.failures

        # 12. الموجّه لا يتأثر بقائمة الملفات
        trap = "رتب\n" + CONTEXT_MARKER + "\nمخطط VERIFY CLARIFY"
        assert ModelRouter.choose_tier("p", "CLARIFY سؤال\n" + CONTEXT_MARKER + "\nمخطط") == SMALL
        assert ModelRouter.choose_tier("p", "روتين عام\n" + CONTEXT_MARKER + "\nمخطط VERIFY") == SMALL
        assert ModelRouter.choose_tier("p", "أنت مخطط مهام\n" + CONTEXT_MARKER + "\nCLARIFY") == LARGE

        # 13. مجلد عمل فيه رابط .trash لا يُقبل في الإعدادات
        if can_link:
            ws4 = base / "ws4"; ws4.mkdir()
            os.symlink(str(outside), str(ws4 / ".trash"), target_is_directory=True)
            try:
                validate_workspace(ws4)
                raise AssertionError("قُبل مجلد فيه رابط .trash")
            except WorkspaceError as e:
                assert ".trash" in str(e), e
        raises(WorkspaceError, validate_workspace, "\\\\srv\\share")

    print("PASS: test_b19 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b19", file=sys.stderr)
    sys.exit(1)
