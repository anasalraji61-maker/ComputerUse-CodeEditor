"""اختبارات التحقق من مجلد العمل وتغييره في المتحكم."""
import os
import sys
import tempfile
import time
import pathlib
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ):   # كل تعديل لـ os.environ هنا يُستعاد عند الخروج
        base = pathlib.Path(tmp).resolve()
        os.environ["COS_DATA_DIR"] = str(base / "data")
        os.environ["COS_WORKSPACE"] = str(base / "defws")
        from assistant import config
        from assistant.config import validate_workspace, WorkspaceError
        from assistant.ui.controller import AssistantController
        from assistant.ledger import SQLiteLedger
        from assistant.models.rules import RuleProvider

        def rejects(p, why=""):
            try:
                validate_workspace(p)
            except WorkspaceError:
                return True
            raise AssertionError(f"كان يجب رفض {p!r} {why}")

        good = base / "Downloads"
        good.mkdir()
        assert validate_workspace(good) == good
        rejects("", "فارغ")
        rejects(base / "nope", "غير موجود")
        f = base / "file.txt"; f.write_text("x", encoding="utf-8")
        rejects(f, "ملف لا مجلد")
        rejects(pathlib.Path(good.anchor), "جذر القرص")
        rejects(pathlib.Path.home(), "جذر المستخدم")
        for blocked in ("/etc", "/usr", "/bin"):
            if os.name != "nt" and os.path.isdir(blocked):
                rejects(blocked)
        (base / "data").mkdir()
        rejects(base / "data", "بيانات التطبيق")
        ssh = base / "u" / ".ssh"; ssh.mkdir(parents=True)
        rejects(ssh, "حساس")
        rejects(base / "u" / ".ssh", "حساس")
        gitrepo = base / "proj" / ".git"; gitrepo.mkdir(parents=True)
        rejects(gitrepo)
        with mock.patch.dict(os.environ, {"ProgramFiles": str(base / "PF")}):   # يُستعاد القديم (على ويندوز يوجد أصلاً) حتى لو فشل التأكيد
            (base / "PF" / "App").mkdir(parents=True)
            rejects(base / "PF" / "App", "برامج")

        # AppData مرفوض إلا Temp (حيث تعمل الاختبارات)
        fake_home = base / "fakehome"
        (fake_home / "AppData" / "Roaming" / "x").mkdir(parents=True)
        (fake_home / "AppData" / "Local" / "Temp" / "y").mkdir(parents=True)
        old = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(fake_home)
        try:
            rejects(fake_home / "AppData" / "Roaming" / "x", "AppData")
            assert validate_workspace(fake_home / "AppData" / "Local" / "Temp" / "y")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        # الحفظ والاستعادة
        assert config.current_workspace() == (base / "defws").resolve()
        config.save_config({"workspace": str(good)})
        assert config.current_workspace() == good
        good.rmdir()
        assert config.current_workspace() == (base / "defws").resolve()  # المحفوظ لم يعد صالحاً
        good.mkdir()

        # المتحكم: التغيير فقط أثناء الخمول، والرفض يصدر خطأ
        ws1 = base / "ws1"; ws1.mkdir()
        ws2 = base / "ws2"; ws2.mkdir()
        (ws2 / "a.txt").write_text("a", encoding="utf-8")
        led = SQLiteLedger(base / "l.db", initial_balance=100, spending_limit=100)
        c = AssistantController(RuleProvider(), ws1, led)
        assert c.workspace == ws1
        assert c.set_workspace(ws2) and c.workspace == ws2
        ev = c.wait_for("workspace")
        assert ev and ev["path"] == str(ws2)
        assert config.load_config()["workspace"] == str(ws2)
        assert c.set_workspace(base / "missing") is False
        e = c.wait_for("error")
        assert e and "مجلد غير مقبول" in e["text"]
        # المهمة تعمل داخل المجلد الجديد فقط
        c.submit_request("رتب الملفات")
        assert c.wait_for("plan")
        c.start()
        rep = c.wait_for("report", 10)
        assert rep and rep["success"]
        assert (ws2 / "مستندات" / "a.txt").exists() and not (ws1 / "مستندات").exists()
        # أثناء خطة جاهزة (غير خامل) يُرفض التغيير
        (ws2 / "b.txt").write_text("b", encoding="utf-8")
        time.sleep(0.2)
        c.submit_request("رتب الملفات")
        assert c.wait_for("plan")
        assert c.set_workspace(ws1) is False and c.workspace == ws2
        c.cancel_plan()

    print("PASS: test_config completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_config", file=sys.stderr)
    sys.exit(1)
