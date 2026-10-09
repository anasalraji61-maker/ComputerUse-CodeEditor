"""اختبارات أداة تشغيل الأوامر shell.py وأداة winget.py مع تغطية الثغرات R4 وR5 وR6 وR13 وR14."""
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

    shell = ShellTool()

    # 1. اختبار تشغيل نمط مسموح صريح (python/python3 --version)
    py_cmd = "python3" if sys.platform != "win32" else "python"
    try:
        code, out, err = shell.run([py_cmd, "--version"])
        assert code == 0, f"فشل تشغيل أمر بايثون: {err}"
        assert "Python" in (out + err), "مخرج أمر بايثون غير متوقع"
    except CommandNotAllowedError:
        # تجربة البديل py أو python إن كان أحدهما متاحاً
        code, out, err = shell.run(["python", "--version"])
        assert code == 0

    # 2. R14 (أ): رفض تشغيل python -c أو git -c أو أي وسائط حرة
    for forbidden_cmd in [
        ["python", "-c", "import os; print('forbidden')"],
        ["python3", "-c", "print('hack')"],
        ["py", "-c", "print(1)"],
        ["git", "-c", "alias.x=!calc", "status"],
        ["git", "status"],  # git الحرة غير مدرجة في الأنماط المسموحة
    ]:
        try:
            shell.run(forbidden_cmd)
            print(f"FAIL: كان يجب رفض النمط غير المصرح به: {forbidden_cmd}", file=sys.stderr)
            sys.exit(1)
        except CommandNotAllowedError:
            pass  # صحيح ومطلوب (R4)

    # 3. R14 (أ) و R5: رفض أي cmd[0] يحتوي على فواصل مسار مثل C:\x\python.exe أو ./script.sh
    for path_cmd in [
        ["C:\\x\\python.exe", "--version"],
        ["/usr/bin/python3", "--version"],
        ["./python", "--version"],
        ["../bin/python", "--version"],
    ]:
        try:
            shell.run(path_cmd)
            print(f"FAIL: كان يجب رفض وجود فواصل مسار في اسم البرنامج: {path_cmd}", file=sys.stderr)
            sys.exit(1)
        except CommandNotAllowedError:
            pass  # صحيح ومطلوب (R5)

    # 4. R6: التأكد من رفض dir و echo و type
    for builtin_cmd in [["dir"], ["echo", "test"], ["type", "file.txt"]]:
        try:
            shell.run(builtin_cmd)
            print(f"FAIL: كان يجب رفض الأوامر المدمجة في cmd: {builtin_cmd}", file=sys.stderr)
            sys.exit(1)
        except CommandNotAllowedError:
            pass  # صحيح ومطلوب (R6)

    # 5. اختبار رفض رموز حقن الأوامر (&, |, >, ;, %, ^, $, ", ')
    dangerous_commands = [
        ["python", "--version", "&", "dir"],
        ["python", "--version|type"],
        ["python", "--version>out.txt"],
        ["python", "--version;whoami"],
        ["python", "%VAR%"],
        ["python", "^escape"],
    ]
    for d_cmd in dangerous_commands:
        try:
            shell.run(d_cmd)
            print(f"FAIL: كان يجب رفض محاولة الحقن في الأمر: {d_cmd}", file=sys.stderr)
            sys.exit(1)
        except (CommandInjectionError, CommandNotAllowedError):
            pass  # صحيح ومطلوب

    # 6. R13: اختبار أداة winget مع فحص النمط الصريح ورفض الخيارات التي تبدأ بشرطة
    mock_calls = []

    def fake_winget_runner(cmd):
        mock_calls.append(cmd)
        if "list" in cmd:
            return 0, "Git.Git 2.40.0", ""
        if "upgrade" in cmd:
            return 0, "Successfully installed Git.Git", ""
        return 1, "", "Unknown"

    winget = WingetTool(runner=fake_winget_runner)

    # رفض استعلام أو معرف يبدأ بشرطة -
    for bad_id in ["-id", "--all", "-v", "package;calc", "package name"]:
        try:
            winget.upgrade_package(bad_id)
            print(f"FAIL: كان يجب رفض معرف الحزمة الخبيث: {bad_id}", file=sys.stderr)
            sys.exit(1)
        except ValueError:
            pass  # صحيح ومطلوب (R13)

    for bad_query in ["-all", "--silent", "query;whoami"]:
        try:
            winget.list_packages(bad_query)
            print(f"FAIL: كان يجب رفض استعلام winget الخبيث: {bad_query}", file=sys.stderr)
            sys.exit(1)
        except ValueError:
            pass  # صحيح ومطلوب (R13)

    # قبول معرف واستعلام صالحين
    code, out, _ = winget.list_packages("Git")
    assert code == 0 and "Git" in out
    code, out, _ = winget.upgrade_package("Git.Git")
    assert code == 0 and "Successfully" in out

    print("PASS: test_shell completed successfully")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
