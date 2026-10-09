"""أداة تشغيل أوامر النظام الآمنة مع التحقق الدقيق من الأوامر والوسائط ومنع الحقن وحظر shell=True."""
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union


class CommandNotAllowedError(Exception):
    """استثناء عند محاولة تشغيل أمر أو وسائط غير مسموحة."""
    pass


class CommandInjectionError(Exception):
    """استثناء عند وجود رموز خطيرة قد تسمح بالحقن أو تجاوز الأوامر."""
    pass


# قائمة الرموز المحظورة تماماً في أي وسيط
FORBIDDEN_CHARS = {"&", "|", ">", "<", ";", "`", "$", "%", "^", '"', "'", "\n", "\r", "\0"}

# نمط التحقق من أسماء الحزم أو البرامج البسيطة
SAFE_ARG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class ShellTool:
    """تشغيل أوامر النظام بأمان تام عبر subprocess دون shell=True وبأنماط أوامر صريحة فقط."""

    def __init__(self, custom_allowed_patterns: Optional[List[List[str]]] = None):
        # أنماط الأوامر المسموحة الصريحة (الأمر + وسائطه المحددة بدقة)
        self.custom_allowed_patterns = custom_allowed_patterns

    def is_allowed_pattern(self, cmd: List[str]) -> bool:
        """التحقق من أن الأمر يطابق نمطاً صريحاً مسموحاً به بالكامل."""
        if self.custom_allowed_patterns is not None:
            return cmd in self.custom_allowed_patterns

        cmd_name = cmd[0].lower()

        # 1. أوامر إصدار بايثون فقط (بلا -c أو وسائط حرة)
        if cmd_name in ("python", "python3", "py"):
            return cmd[1:] == ["--version"]

        # 2. أمر whoami بلا أي وسائط
        if cmd_name == "whoami":
            return len(cmd) == 1

        # 3. أمر where للبحث عن موقع ملف تنفيذي آمن
        if cmd_name == "where":
            return len(cmd) == 2 and bool(SAFE_ARG_PATTERN.match(cmd[1]))

        return False

    def validate_command(self, cmd: List[str]) -> str:
        """التحقق الأمني الشامل من الأمر ووسائطه وإرجاع المسار المطلق المحلول للبرنامج."""
        if not cmd or not isinstance(cmd, list):
            raise ValueError("يجب تمرير الأمر كمصفوفة نصوص غير فارغة.")

        for arg in cmd:
            if not isinstance(arg, str):
                raise ValueError("يجب أن تكون جميع وسائط الأمر نصوصاً.")
            for char in FORBIDDEN_CHARS:
                if char in arg:
                    raise CommandInjectionError(f"تم اكتشاف رمز محظور '{char}' في الأمر.")

        # R5: رفض أي cmd[0] يحتوي على فاصل مسار لمنع تشغيل ملفات خارجية عشوائية
        raw_cmd0 = cmd[0]
        if "/" in raw_cmd0 or "\\" in raw_cmd0:
            raise CommandNotAllowedError(f"غير مسموح بوجود فواصل مسار في اسم البرنامج '{raw_cmd0}'.")

        # R4: التحقق من النمط الصريح بالكامل
        if not self.is_allowed_pattern(cmd):
            raise CommandNotAllowedError(f"نمط الأمر {cmd} غير مصرح به في القائمة البيضاء الصريحة.")

        # R5: حل اسم البرنامج إلى مسار مطلق عبر shutil.which
        resolved_exe = shutil.which(raw_cmd0)
        if not resolved_exe:
            raise CommandNotAllowedError(f"البرنامج التنفيذي '{raw_cmd0}' غير متوفر على مسار النظام.")

        return resolved_exe

    def run(self, cmd: List[str], cwd: Optional[Union[str, Path]] = None, timeout: int = 30) -> Tuple[int, str, str]:
        """تشغيل الأمر بعد حل مساره والتأكد من مطابقة النمط الصريح وخلوه من shell=True."""
        resolved_exe = self.validate_command(cmd)

        working_dir = str(cwd) if cwd else None
        exec_args = [resolved_exe] + cmd[1:]

        result = subprocess.run(
            exec_args,
            cwd=working_dir,
            shell=False,  # محظور تشغيل shell=True إطلاقاً
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

        return result.returncode, result.stdout, result.stderr
