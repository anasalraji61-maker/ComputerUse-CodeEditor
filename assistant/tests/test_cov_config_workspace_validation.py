"""تغطية اختيار مجلد العمل: يُرفض مجلد المستخدم الرئيسي ومن يحويه، مجلدات النظام/البرامج (من متغيرات البيئة)، بيانات التطبيق، AppData، المجلدات الحساسة،
وعناصر .trash/.undo_journal.json بنوع خاطئ؛ والتراجع للافتراضي عند حفظ غير صالح، وخطأ واضح حين لا يصلح أي مجلد."""
import os
import pathlib
import sys
import tempfile
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant import config as C
    from assistant.config import WorkspaceError, validate_workspace, current_workspace, save_config, load_config

    def bad(path, fragment=None):
        try:
            validate_workspace(path)
        except WorkspaceError as exc:
            if fragment:
                assert fragment in str(exc), (fragment, str(exc))
            return
        raise AssertionError(f"كان يجب رفض: {path!r}")

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        home = base / "users" / "me"
        home.mkdir(parents=True)
        data = base / "appdata_cos"
        data.mkdir()
        envs = {"COS_DATA_DIR": str(data)}
        for k in ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "COS_WORKSPACE"):
            envs[k] = ""
        with mock.patch.dict(os.environ, envs), mock.patch("pathlib.Path.home", return_value=home):
            # الأساسيات
            for p in (None, "", "   "):
                bad(p, "لم يُحدَّد")
            bad(base / "nope", "غير موجود")
            f = base / "afile"
            f.write_text("x", encoding="utf-8")
            bad(f, "غير موجود")
            bad(pathlib.Path(base.anchor), "جذر")

            # المستخدم الرئيسي ومن يحويه
            bad(home, "الرئيسي")
            bad(home.parent, "يحوي مجلد المستخدم")
            ok = home / "Downloads"
            ok.mkdir()
            assert validate_workspace(ok) == ok.resolve()

            # بيانات التطبيق نفسها وما فوقها وما تحتها
            bad(data, "بيانات التطبيق")
            (data / "sub").mkdir()
            bad(data / "sub", "بيانات التطبيق")

            # AppData مرفوض إلا Temp
            ad = home / "AppData" / "Roaming"
            ad.mkdir(parents=True)
            bad(ad, "AppData")
            tmpd = home / "AppData" / "Local" / "Temp" / "work"
            tmpd.mkdir(parents=True)
            assert validate_workspace(tmpd) == tmpd.resolve()

            # أجزاء حساسة
            for part in (".ssh", ".git", ".aws", ".gnupg"):
                d = home / "proj" / part / "inner"
                d.mkdir(parents=True)
                bad(d, "حساس")

            # عناصر محجوزة بنوع خاطئ
            w1 = home / "w1"
            w1.mkdir()
            (w1 / ".trash").write_text("file", encoding="utf-8")
            bad(w1, ".trash")
            w2 = home / "w2"
            w2.mkdir()
            (w2 / ".undo_journal.json").mkdir()
            bad(w2, ".undo_journal.json")
            w3 = home / "w3"
            (w3 / ".trash").mkdir(parents=True)
            assert validate_workspace(w3) == w3.resolve()      # سلة سليمة مقبولة

        # مجلدات النظام/البرامج من متغيرات البيئة (ويندوز) تُرفض هي وأبناؤها
        sysroot = base / "FakeWindows"
        (sysroot / "System32").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"COS_DATA_DIR": str(data), "WINDIR": str(sysroot), "SystemRoot": str(sysroot),
                                          "ProgramFiles": "", "ProgramFiles(x86)": "", "ProgramW6432": "", "ProgramData": ""}), \
                mock.patch("pathlib.Path.home", return_value=home):
            bad(sysroot, "النظام")
            bad(sysroot / "System32", "النظام")

        # المجلد المحفوظ غير الصالح يُستبدل بالافتراضي الآمن؛ وحين لا يصلح أي مجلد: خطأ واضح لا انهيار
        good_default = home / "COS_Workspace"
        with mock.patch.dict(os.environ, {"COS_DATA_DIR": str(data), "COS_WORKSPACE": "", "WINDIR": "", "SystemRoot": "", "ProgramFiles": "",
                                          "ProgramFiles(x86)": "", "ProgramW6432": "", "ProgramData": ""}), \
                mock.patch("pathlib.Path.home", return_value=home):
            save_config({"workspace": str(home.parent)})            # غير صالح (يحوي المستخدم)
            assert current_workspace() == good_default.resolve() and good_default.is_dir()
            # الافتراضي نفسه غير صالح (متغير بيئة يشير للمجلد الرئيسي) ولا يمكن إنشاء البديل
            save_config({})
            with mock.patch.dict(os.environ, {"COS_WORKSPACE": str(home)}), mock.patch.object(pathlib.Path, "mkdir", side_effect=OSError("ro")):
                try:
                    current_workspace()
                    raise AssertionError("كان يجب رفع WorkspaceError")
                except WorkspaceError as exc:
                    assert "مجلد عمل آمن" in str(exc)
            # config.json تالف لا يكسر التحميل
            (data / "config.json").write_text("{not json", encoding="utf-8")
            assert load_config() == {}

    print("PASS: test_cov_config_workspace_validation completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_config_workspace_validation", file=sys.stderr)
    sys.exit(1)
