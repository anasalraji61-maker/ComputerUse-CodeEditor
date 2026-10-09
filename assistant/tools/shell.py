"""أداة تشغيل أوامر النظام الآمنة مع قائمة بيضاء صريحة ومنع الحقن وحظر shell=True."""
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union


class CommandNotAllowedError(Exception):
    """استثناء عند محاولة تشغيل أمر غير موجود في القائمة البيضاء المسموحة."""
    pass


class CommandInjectionError(Exception):
    """استثناء عند وجود رموز خطيرة قد تسمح بالحقن أو تجاوز الأوامر."""
    pass


# قائمة الرموز المحظورة لمنع حقن الأوامر
FORBIDDEN_CHARS = {"&", "|", ">", "<", ";", "`", "$", "\n", "\r"}

# القائمة البيضاء للأوامر الصريحة المسموح بتشغيلها فقط
DEFAULT_ALLOWED_COMMANDS = {
    "dir",
    "where",
    "python",
    "python3",
    "py",
    "git",
    "echo",
    "type",
    "whoami",
}


class ShellTool:
    """تشغيل أوامر النظام بأمان تام عبر subprocess دون shell=True وبقائمة بيضاء محددة."""

    def __init__(self, allowed_commands: Optional[set] = None):
        self.allowed_commands = set(allowed_commands) if allowed_commands else set(DEFAULT_ALLOWED_COMMANDS)

    def validate_command(self, cmd: List[str]):
        """التحقق من صحة الأمر وخلوه من الأحرف المحظورة وانتمائه للقائمة البيضاء."""
        if not cmd or not isinstance(cmd, list):
            raise ValueError("يجب تمرير الأمر كمصفوفة نصوص غير فارغة.")

        # فحص وجود أي رموز محظورة في أي وسيط
        for arg in cmd:
            if not isinstance(arg, str):
                raise ValueError("يجب أن تكون جميع وسائط الأمر نصوصاً.")
            for char in FORBIDDEN_CHARS:
                if char in arg:
                    raise CommandInjectionError(f"تم اكتشاف رمز محظور '{char}' في الأمر.")

        # استخراج اسم البرنامج الأساسي
        executable = Path(cmd[0]).name.lower()
        # إزالة .exe إن وجدت للمقارنة
        if executable.endswith(".exe"):
            executable = executable[:-4]

        if executable not in {c.lower() for c in self.allowed_commands}:
            raise CommandNotAllowedError(f"الأمر '{cmd[0]}' غير مدرج في القائمة البيضاء المسموحة.")

    def run(self, cmd: List[str], cwd: Optional[Union[str, Path]] = None, timeout: int = 30) -> Tuple[int, str, str]:
        """تشغيل الأمر بأمان بلا shell=True."""
        self.validate_command(cmd)

        working_dir = str(cwd) if cwd else None

        result = subprocess.run(
            cmd,
            cwd=working_dir,
            shell=False,  # محظور تشغيل shell=True إطلاقاً
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

        return result.returncode, result.stdout, result.stderr
