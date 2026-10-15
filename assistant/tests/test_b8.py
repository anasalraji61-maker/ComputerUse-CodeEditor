"""اختبارات B8: إصلاحات المراجعة الأمنية المستقلة (كل بند له اختبار يعيد السيناريو)."""
import json
import os
import sys
import tempfile
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant import config as cfg
    from assistant.models.rules import RuleProvider, mentions, norm
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger
    from assistant.planner import PlanStep
    from assistant.tools.files import FileManager, PathOutOfBoundsError, ProtectedPathError
    from assistant.ui.controller import AssistantController, IDLE, PLANNING, PLAN_READY
    from assistant.models.base import ModelProvider
    import threading, time

    rp = RuleProvider()

    def mk(tmp, files=None):
        base = pathlib.Path(tmp).resolve()
        root = base / "ws"
        root.mkdir()
        for f, c in (files or {}).items():
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            (root / f).write_text(c, encoding="utf-8")
        return root, SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500)

    def raises(exc, fn, *a, **kw):
        try:
            fn(*a, **kw)
        except exc:
            return True
        return False

    # 1. المجلد الافتراضي يُتحقق منه (COS_WORKSPACE=/etc لا يمر) + أب مجلد المستخدم مرفوض
    old = {k: os.environ.get(k) for k in ("COS_WORKSPACE", "COS_DATA_DIR", "HOME", "USERPROFILE")}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.environ["COS_DATA_DIR"] = str(pathlib.Path(tmp) / "data")
            os.environ["HOME"] = os.environ["USERPROFILE"] = str(pathlib.Path(tmp) / "home")
            (pathlib.Path(tmp) / "home").mkdir()
            # قيمة بيئة مرفوضة (جذر المستخدم نفسه): لا تُقبل ويعود لمجلد آمن داخل المنزل (لا نستعمل مسارات نظام حقيقية أبداً)
            os.environ["COS_WORKSPACE"] = str(pathlib.Path(tmp) / "home")
            assert cfg.current_workspace().name == "COS_Workspace"
            os.environ["COS_WORKSPACE"] = str(pathlib.Path(tmp) / "okws")
            assert cfg.current_workspace().name == "okws"
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    assert raises(cfg.WorkspaceError, cfg.validate_workspace, str(pathlib.Path.home().parent))

    # 2. مجلدات حساسة وتيارات NTFS والأسماء المحجوزة الجديدة مرفوضة في كل أدوات الملفات
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = mk(tmp, {".git/config": "x", "a.txt": "a"})
        fm = FileManager(root)
        assert raises(ProtectedPathError, fm.delete, ".git")
        assert raises(ProtectedPathError, fm.move, ".git/config", "x/config")
        for bad in ("a.txt:stream", ".trash:x", ".undo_journal.json:x", "CONIN$", "COM¹", "LPT²"):
            assert raises(PathOutOfBoundsError, fm.create_dir, bad), bad
        assert (root / ".git" / "config").exists()

    # 3. روابط رمزية داخل الجذر مرفوضة (الموافقة تعرض اسماً والفعل على غيره)
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = mk(tmp, {"real.txt": "r"})
        try:
            os.symlink(root / "real.txt", root / "ln")
            fm = FileManager(root)
            assert raises(PathOutOfBoundsError, fm.delete, "ln") and (root / "real.txt").exists()
        except (OSError, NotImplementedError):
            print("NOTE: اختبار الروابط الرمزية لم يُنفَّذ هنا (ويندوز بلا صلاحية إنشاء رابط)")

    # 4. undo_task لا يلغي مهام أحدث، وبالترتيب الصحيح يعمل
    with tempfile.TemporaryDirectory() as tmp:
        root, led = mk(tmp, {"a.txt": "a", "b.txt": "b"})
        r = TaskRunner(root, rp, led)
        assert r.run("A", "انقل a.txt إلى DA").success and r.run("B", "انقل b.txt إلى DB").success
        assert r.undo_task("A") == 0 and (root / "DB" / "b.txt").exists() and (root / "DA" / "a.txt").exists()
        assert r.undo_task("B") >= 1 and (root / "b.txt").exists()
        assert r.undo_task("A") >= 1 and (root / "a.txt").exists()

    # 5. تراجع «ناجح» بلا استعادة: يعيد False/يتخطى الناتج المفقود ولا يعطّل ما قبله
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = mk(tmp, {"x.txt": "x", "y.txt": "y"})
        fm = FileManager(root)
        fm.move("x.txt", "D/x.txt")
        fm.move("y.txt", "D/y.txt")
        (root / "D" / "y.txt").unlink()  # اختفى ناتج آخر عملية
        assert len(fm.journal) == 3  # create_dir D + نقلتان
        assert fm.undo_last() is False and len(fm.journal) == 2  # أُزيلت العملية ولم يُدَّعَ استعادة
        assert fm.undo_all() == 2 and (root / "x.txt").exists() and not fm.journal and not (root / "D").exists()

    # 6. نقل/نسخ إلى مجلد موجود يضع العنصر بداخله (لا «Photos (1)») ويقبله المدقق
    with tempfile.TemporaryDirectory() as tmp:
        root, led = mk(tmp, {"cat.jpg": "c", "Photos/old.jpg": "o"})
        fm = FileManager(root)
        assert fm.move("cat.jpg", "Photos") == (root / "Photos" / "cat.jpg").resolve()
        assert not (root / "Photos (1)").exists()
        runner = TaskRunner(root, rp, led)
        step = PlanStep(1, "move", "Photos/cat.jpg", {"dest": "Photos/cat.jpg"})
        (root / "Dst").mkdir()
        runner2 = TaskRunner(root, rp, led)
        rep = runner2.run("m", "x", plan=[PlanStep(1, "move", "Photos/cat.jpg", {"dest": "Dst"})])
        assert rep.success and (root / "Dst" / "cat.jpg").exists(), rep.failures

    # 7. خطوة بإجراء « Delete » تُطبَّع (لا تنفيذ ثم فشل تدقيق)
    assert PlanStep(1, " Delete ", "f").action_type == "delete"

    # 8. أسماء غير مرئية/طويلة لا تُعرض للمخطط، ومطابقة الاسم كاملة لا جزئية
    with tempfile.TemporaryDirectory() as tmp:
        root, led = mk(tmp, {"invoice.pdf": "1", "invoice​.pdf": "2", "report.docx": "r", "a": "a", "a.tmp": "t"})
        lst = TaskRunner(root, rp, led).workspace_listing().splitlines()
        # الاسم بمحرف خفي لا يُعرض لكنه يُبلَّغ عنه (سطر «…») فلا يُسقَط بصمت
        assert "invoice.pdf" in lst and len(lst) == 5 and lst[-1].startswith("…") and "1" in lst[-1], lst
    assert mentions("report.docx", norm("احذف report.docx")) and not mentions("report.docx", norm("احذف my_report.docx"))
    assert not mentions("a", norm("احذف a.tmp")) and mentions("a.tmp", norm("احذف a.tmp"))

    # 9. run_command: الأمر المعروض يجب أن يطابق الفعلي
    with tempfile.TemporaryDirectory() as tmp:
        root, led = mk(tmp)
        r = TaskRunner(root, rp, led, approver=lambda s: True)
        rep = r.run("c", "x", plan=[PlanStep(1, "run_command", "whoami", {"cmd": ["where", "git"]})])
        assert not rep.success

    # 10. الإيقاف أثناء التخطيط يلغي الطلب (لا خطة جاهزة ولا إعادة تفعيل)
    class Slow(ModelProvider):
        def __init__(self):
            self.go = threading.Event()
        def generate(self, prompt, system_prompt=""):
            if "CLARIFY" in system_prompt:
                return json.dumps({"questions": []})
            self.go.wait(5)
            return json.dumps({"steps": [{"step_id": 1, "action_type": "inspect", "target": ".", "params": {}}]})
        def name(self):
            return "slow"
    with tempfile.TemporaryDirectory() as tmp:
        root, led = mk(tmp)
        prov = Slow()
        base_threads = threading.active_count()
        c = AssistantController(prov, root, led)
        c.submit_request("افحص")
        end = time.time() + 3
        while c.state != PLANNING and time.time() < end:
            time.sleep(0.01)
        time.sleep(0.1)
        c.stop()
        prov.go.set()
        end = time.time() + 5
        while c.state != IDLE and time.time() < end:
            time.sleep(0.02)
        assert c.state == IDLE and c._plan is None
        # العامل القديم قد يبقى لحظات يكتب في المجلد؛ ننتظر انتهاءه قبل حذف المجلد المؤقت
        end = time.time() + 5
        while threading.active_count() > base_threads and time.time() < end:
            time.sleep(0.02)
        c.start()  # لا خطة جاهزة
        assert any(e["type"] == "error" for e in c.drain_events())

    # 11. السجل: عنصر غير قاموس يُتجاهل ولا ينهار، ومسار سلة خارج .trash يُرفض
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = mk(tmp, {"important.docx": "i"})
        (root / ".undo_journal.json").write_text(json.dumps([
            "junk", {"action": "delete", "original_path": str(root / "x (1).txt"), "trash_path": str(root / "important.docx")}]), encoding="utf-8")
        fm = FileManager(root)
        assert len(fm.journal) == 1
        assert fm.undo_all() == 0 and (root / "important.docx").exists()

    # 12. المجلدات الآباء الضمنية تُسجَّل وتُزال عند التراجع (R37)
    with tempfile.TemporaryDirectory() as tmp:
        root, _ = mk(tmp, {"f.txt": "f"})
        fm = FileManager(root)
        fm.create_dir("a/b/c")
        fm.move("f.txt", "x/y/f.txt")
        assert fm.undo_all() == 6 and sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) == ["f.txt"]

    # 13. ردّ موافقة متأخر على خطوة أخرى يُتجاهل
    with tempfile.TemporaryDirectory() as tmp:
        root, led = mk(tmp)
        c = AssistantController(rp, root, led)
        c._pending_approval = 2
        c._approval_result = False
        c.respond_approval(True, approval_id=1)
        assert c._approval_result is False and not c._approval_event.is_set()
        c.respond_approval(True, approval_id=2)
        assert c._approval_result is True and c._approval_event.is_set()

    # 14. مرادفات عربية شائعة للنقل والحذف
    ctx = ["a.pdf", "b.PDF", "c.jpg", "notes.tmp", "x.tmp", "docs/"]
    def kinds(q):
        a = rp.analyze(q, ctx)
        return [(s["action_type"], s["target"]) for s in a.steps or []]
    assert kinds("ضع ملفات pdf في مستندات") == [("create_dir", "مستندات"), ("move", "a.pdf"), ("move", "b.PDF")]
    assert kinds("حط الصور داخل docs") == [("move", "c.jpg")]
    assert kinds("شيل x.tmp") == [("delete", "x.tmp")]
    assert kinds("تخلص من ملفات tmp") == [("delete", "notes.tmp"), ("delete", "x.tmp")]
    assert kinds("ضع شيء") == []

    # 15. فحص مسبق للمسارات: خطوة سيئة في آخر الخطة لا تترك تنفيذاً جزئياً
    with tempfile.TemporaryDirectory() as tmp:
        root, led = mk(tmp, {"a.txt": "a"})
        r = TaskRunner(root, rp, led)
        rep = r.run("pf", "x", plan=[PlanStep(1, "create_dir", "OK"), PlanStep(2, "move", "a.txt", {"dest": "OK/CON"})])
        assert not rep.success and "لم يُنفَّذ شيء" in rep.failures[0]
        assert not (root / "OK").exists() and (root / "a.txt").exists()

    print("PASS: test_b8 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b8", file=sys.stderr)
    sys.exit(1)
