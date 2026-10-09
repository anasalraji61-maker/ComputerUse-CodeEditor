"""اختبارات أداة تشغيل الأوامر shell.py وأداة winget.py."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.tools.shell import (
        ShellTool,
        CommandNotAllowedError,
        CommandInjectionError,
    )
    from assistant.tools.winget import WingetTool

    # 1. اختبار تشغيل أمر مسموح في القائمة البيضاء
    shell = ShellTool(allowed_commands={"python", "python3", "echo"})
    code, out, err = shell.run([sys.executable, "--version"])
    assert code == 0, f"فشل تشغيل أمر بايثون: {err}"
    assert "Python" in (out + err), "مخرج أمر بايثون غير متوقع"

    # 2. اختبار رفض أمر غير مدرج في القائمة البيضاء
    try:
        shell.run(["powershell", "-Command", "Get-Process"])
        print("FAIL: كان يجب رفض أمر powershell غير المسموح!", file=sys.stderr)
        sys.exit(1)
    except CommandNotAllowedError:
        pass  # سلوك سليم ومطلوب

    # 3. اختبار رفض رموز حقن الأوامر (&, |, >, ;)
    dangerous_commands = [
        ["echo", "hello & dir"],
        ["echo", "hello | type file"],
        ["echo", "test > out.txt"],
        ["echo", "test; whoami"],
    ]
    for d_cmd in dangerous_commands:
        try:
            shell.run(d_cmd)
            print(f"FAIL: كان يجب رفض محاولة الحقن في الأمر: {d_cmd}", file=sys.stderr)
            sys.exit(1)
        except CommandInjectionError:
            pass  # سلوك سليم ومطلوب

    # 4. اختبار أداة winget بمحاكي (mock runner) دون المساس بالنظام
    mock_calls = []

    def fake_winget_runner(cmd):
        mock_calls.append(cmd)
        if "list" in cmd:
            return 0, "Git.Git 2.40.0\nPython.Python.3.11 3.11.0", ""
        if "upgrade" in cmd:
            return 0, "Successfully installed Git.Git", ""
        return 1, "", "Unknown command"

    winget = WingetTool(runner=fake_winget_runner)

    # فحص العرض
    code, out, err = winget.list_packages(query="Git")
    assert code == 0 and "Git" in out, "فشل محاكاة winget list"
    assert mock_calls[0] == ["winget", "list", "-q", "Git"], "أمر winget list غير مطابق"

    # فحص التحديث
    code, out, err = winget.upgrade_package(package_id="Git.Git")
    assert code == 0 and "Successfully" in out, "فشل محاكاة winget upgrade"
    assert "Git.Git" in mock_calls[1], "أمر winget upgrade غير مطابق"

    print("PASS: test_shell completed successfully")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
