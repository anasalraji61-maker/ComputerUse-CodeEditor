"""تغطية أداتي shell وwinget: رفض المدخلات الخاطئة والحقن، عدم تنفيذ ملف تنفيذي من مجلد العمل أو المجلد الحالي، ترميز المخرجات، وإيقاف عملية طويلة بزر الإيقاف."""
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.tools.shell import ShellTool, CommandNotAllowedError, CommandInjectionError, resolve_exe, decode_output, minimal_env
    from assistant.tools.winget import WingetTool

    def raises(types, fn):
        try:
            fn()
        except types as exc:
            return exc
        raise AssertionError(f"كان يجب أن يرفع {types}")

    sh = ShellTool()
    # مدخلات بنيوية خاطئة
    for bad in (None, [], "whoami", ("whoami",)):
        raises(ValueError, lambda b=bad: sh.validate_command(b))
    raises(ValueError, lambda: sh.validate_command(["whoami", 1]))
    # رموز الحقن
    for ch in ("&", "|", ">", "<", ";", "`", "$", "%", "^", '"', "'", "\n", "\r", "\0"):
        raises(CommandInjectionError, lambda c=ch: sh.validate_command(["where", "a" + c + "b"]))
    # فواصل المسار في اسم البرنامج
    raises(CommandNotAllowedError, lambda: sh.validate_command(["/bin/whoami"]))
    raises(CommandNotAllowedError, lambda: sh.validate_command(["..\\whoami"]))
    # قائمة بيضاء: لا -c ولا وسائط زائدة ولا برامج أخرى
    for bad in (["python", "-c", "print(1)"], ["python", "--version", "-c"], ["whoami", "/all"], ["where"], ["where", "a", "b"],
                ["where", "a b"], ["cmd", "/c", "dir"], ["powershell"], ["rm", "-rf", "x"]):
        raises((CommandNotAllowedError, CommandInjectionError), lambda c=bad: sh.validate_command(c))
    # نمط مخصص: يُقبل فقط ما فيه حرفياً، وبرنامج غير موجود في PATH يُرفض بوضوح
    custom = ShellTool(custom_allowed_patterns=[["definitely_missing_prog_xyz"]])
    exc = raises(CommandNotAllowedError, lambda: custom.validate_command(["definitely_missing_prog_xyz"]))
    assert "غير متوفر" in str(exc), exc
    raises(CommandNotAllowedError, lambda: custom.validate_command(["whoami"]))

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"
        ws.mkdir()
        other = base / "bin"
        other.mkdir()
        for d in (ws, other):
            for nm in ("faketool", "faketool.exe", "faketool.cmd", "faketool.bat", "faketool.com"):
                f = d / nm
                # ويندوز: محتوى باتش (@echo off) يناسب .cmd/.bat ولا يعتمد على إذن التنفيذ؛ posix: chmod
                f.write_bytes(b"@echo off\r\necho hi\r\n" if os.name == "nt" else b"echo hi\n")
                if os.name != "nt":
                    os.chmod(str(f), 0o755)

        # ملف تنفيذي داخل مجلد العمل مرفوض ولو كان أول PATH
        path_env = os.pathsep.join([str(ws), str(other)])
        hit = resolve_exe("faketool", reject_roots=(ws,), path_env=path_env)
        assert hit is not None and pathlib.Path(hit).parent == other, hit
        assert resolve_exe("faketool", reject_roots=(ws, other), path_env=path_env) is None
        # مداخل PATH النسبية/الفارغة تُهمل (لا بحث في المجلد الحالي أبداً)
        old_cwd = os.getcwd()
        os.chdir(str(other))
        try:
            assert resolve_exe("faketool", path_env=os.pathsep.join(["", ".", "relative_dir"])) is None
        finally:
            os.chdir(old_cwd)
        # أسماء بفواصل أو فارغة
        assert resolve_exe("", path_env=path_env) is None and resolve_exe("a/b", path_env=path_env) is None and resolve_exe("a\\b", path_env=path_env) is None

    # ترميز المخرجات: لا استثناء على بايتات غير UTF-8 ولا فقد لـ UTF-8 السليم
    assert decode_output(None) == "" and decode_output(b"") == "" and decode_output("already") == "already"
    assert decode_output("مرحبا".encode("utf-8")) == "مرحبا"
    out = decode_output(bytes([0xE3, 0xD1, 0xCD, 0xC8, 0xC7, 0xFF, 0xFE]))     # cp1256 + بايتات عشوائية
    assert isinstance(out, str) and out
    # البيئة الدنيا لا تحمل المفاتيح
    assert "ANTHROPIC_API_KEY" not in minimal_env({"ANTHROPIC_API_KEY": "sk-xxxxxxxxxxxxxxxxxxxx", "PATH": "/bin"})

    # ---- winget ----
    seen = []
    wg = WingetTool(runner=lambda cmd: (seen.append(cmd) or (0, "ok", "")), source="winget")
    for bad in (None, "", "   ", 5, "-rf", "--id", "a b", "a;b", "a&b", "../x", "pkg\nid"):
        raises(ValueError, lambda b=bad: wg.upgrade_package(b))
    assert seen == []
    assert wg.upgrade_package(" Git.Git ") == (0, "ok", "") and seen[0][:5] == ["winget", "upgrade", "--id", "Git.Git", "--exact"]
    assert "--silent" in seen[0] and "--disable-interactivity" in seen[0] and seen[0][seen[0].index("--source") + 1] == "winget"
    for bad in ("-x", "a;b", "a|b", "$(x)"):
        raises(ValueError, lambda b=bad: wg.list_packages(b))
    seen.clear()
    wg.list_packages("   ")                      # استعلام فارغ: يُحذف الخيار
    assert "-q" not in seen[0]
    raises(ValueError, lambda: WingetTool(runner=lambda c: (0, "", ""), source="bad source!"))
    with mock.patch.dict(os.environ, {"COS_WINGET_SOURCE": "bad source!"}):
        assert WingetTool(runner=lambda c: (0, "", "")).source == "winget"      # قيمة بيئة تالفة لا توقف التطبيق ولا تُمرَّر للأمر

    # زر الإيقاف يقطع عملية طويلة فعلاً (ويقتل شجرتها) بدل انتظار المهلة
    t0 = time.monotonic()
    tool = WingetTool(stop_check=lambda: True, timeout=60.0)
    code, out, err = tool._default_runner([sys.executable, "-c", "import time; time.sleep(120)"])
    assert code != 0 and "أُوقفت" in err and time.monotonic() - t0 < 30, (code, err)
    # المهلة تقطع أيضاً
    t0 = time.monotonic()
    tool2 = WingetTool(timeout=0.5)
    code, out, err = tool2._default_runner([sys.executable, "-c", "import time; time.sleep(120)"])
    assert code != 0 and "انتهت مهلة" in err and time.monotonic() - t0 < 30, (code, err)

    print("PASS: test_cov_shell_winget_guards completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_shell_winget_guards", file=sys.stderr)
    sys.exit(1)
