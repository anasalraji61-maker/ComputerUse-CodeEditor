"""أداة تشغيل أوامر النظام الآمنة مع التحقق الدقيق من الأوامر والوسائط ومنع الحقن وحظر shell=True."""
import locale
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple, Union


# ويندوز يبحث في المجلد الحالي أولاً عن الملفات التنفيذية؛ نعطّل ذلك حتى لا يُشغَّل exe مزروع في مجلد العمل
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")


# ---------- بيئة الأبناء ومسارات التنفيذ ----------
# الأبناء لا يرثون البيئة كاملة (فيها مفاتيح API)؛ فقط ما يلزم لتشغيل أدوات النظام.
_ENV_KEEP_WIN = ("SystemRoot", "windir", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                 "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "COMSPEC", "SystemDrive",
                 "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432", "ALLUSERSPROFILE", "PUBLIC",
                 "HOMEDRIVE", "HOMEPATH", "USERDOMAIN", "USERDOMAIN_ROAMINGPROFILE", "USERNAME", "COMPUTERNAME", "LOGONSERVER",
                 "OS", "PSModulePath", "SESSIONNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
                 "PROCESSOR_LEVEL", "PROCESSOR_REVISION")
_ENV_KEEP_POSIX = ("PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "TERM", "SHELL")


def minimal_env(source=None) -> dict:
    """بيئة دنيا للعمليات الفرعية: بلا مفاتيح ولا أسرار. على ويندوز تُطابَق الأسماء دون حساسية لحالة الأحرف."""
    src = os.environ if source is None else source
    lower = {str(k).lower(): (k, v) for k, v in src.items()}
    out = {}
    for name in _ENV_KEEP_WIN + _ENV_KEEP_POSIX:
        hit = lower.get(name.lower())
        if hit is not None and isinstance(hit[1], str):
            out[name if os.name == "nt" else hit[0]] = hit[1]
    for k, v in src.items():  # اللغة على يوكس فقط (LANG / LC_*)
        if os.name != "nt" and (k == "LANG" or str(k).startswith("LC_")) and isinstance(v, str):
            out[k] = v
    out["NoDefaultCurrentDirectoryInExePath"] = "1"
    return out


def neutral_cwd() -> str:
    """مجلد عمل محايد للأبناء (ليس مجلد العمل ولا مجلداً يزرع فيه المستخدم/الملفات الواردة)."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if os.name == "nt" and root and os.path.isdir(os.path.join(root, "System32")):
        return os.path.join(root, "System32")
    return tempfile.gettempdir()


def _console_encodings() -> List[str]:
    """ترميزات احتمالية لناتج أدوات الكونسول على ويندوز: صفحة OEM (GetOEMCP) ثم ANSI/المحلي. تُستبدل في الاختبارات."""
    encs: List[str] = []
    if os.name == "nt":
        try:
            import ctypes
            for fn in ("GetOEMCP", "GetACP"):
                cp = int(getattr(ctypes.windll.kernel32, fn)())
                if cp:
                    encs.append(f"cp{cp}")
        except Exception:
            pass
    try:
        pref = locale.getpreferredencoding(False)
        if pref:
            encs.append(pref)
    except Exception:
        pass
    return encs


def decode_output(raw: Optional[bytes]) -> str:
    """يفك ناتج عملية فرعية: UTF-8 إن فُكّ بصرامة، وإلا صفحة OEM/ANSI (where/whoami العربية مثلاً cp720/cp1256)، وأخيراً UTF-8 مع الاستبدال."""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in _console_encodings():
        try:
            return raw.decode(enc, errors="replace")
        except (LookupError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace")


def _inside(path: str, roots) -> bool:
    try:
        rp = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return True
    for r in roots:
        if not r:
            continue
        try:
            rr = os.path.normcase(os.path.realpath(str(r)))
        except (OSError, ValueError):
            continue
        if rp == rr or rp.startswith(rr.rstrip(os.sep) + os.sep):
            return True
    return False


def resolve_exe(name: str, reject_roots=(), path_env: Optional[str] = None) -> Optional[str]:
    """يحلّ اسم برنامج إلى مسار مطلق بلا اعتماد على المجلد الحالي: مداخل PATH النسبية/الفارغة تُهمل (فلا يُبحث في cwd أبداً)،
    وأي ملف يقع داخل reject_roots (مجلد العمل) يُرفض. المجلد الحالي نفسه لا يُحظر (قد يكون / أو System32 أو مجلد المستخدم).
    على ويندوز يُفضَّل System32 لـ whoami/where/taskkill."""
    if not name or "/" in name or "\\" in name:
        return None
    exts = [""]
    if os.name == "nt":
        pathext = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        exts = [e for e in pathext.split(";") if e]
        if os.path.splitext(name)[1].lower() in [e.lower() for e in exts]:
            exts = [""]
    banned = list(reject_roots)
    dirs = []
    if os.name == "nt" and name.lower() in ("whoami", "where", "taskkill"):
        root = os.environ.get("SystemRoot") or os.environ.get("windir")
        if root:
            dirs.append(os.path.join(root, "System32"))
    raw = os.environ.get("PATH", "") if path_env is None else path_env
    for d in raw.split(os.pathsep):
        d = d.strip().strip('"')
        if d and os.path.isabs(d):
            dirs.append(d)
    seen = set()
    for d in dirs:
        key = os.path.normcase(d)
        if key in seen:
            continue
        seen.add(key)
        for e in exts:
            cand = os.path.join(d, name + e)
            if os.path.isfile(cand) and os.access(cand, os.X_OK) and not _inside(cand, banned):
                return cand
    return None


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

    def __init__(self, custom_allowed_patterns: Optional[List[List[str]]] = None, reject_roots=()):
        self.reject_roots = tuple(reject_roots)  # مجلدات (مجلد العمل) لا يُقبل منها أي ملف تنفيذي
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
        resolved_exe = resolve_exe(raw_cmd0, reject_roots=self.reject_roots)
        if not resolved_exe:
            raise CommandNotAllowedError(f"البرنامج التنفيذي '{raw_cmd0}' غير متوفر على مسار النظام.")

        return resolved_exe

    def run(self, cmd: List[str], cwd: Optional[Union[str, Path]] = None, timeout: int = 30) -> Tuple[int, str, str]:
        """تشغيل الأمر بعد حل مساره والتأكد من مطابقة النمط الصريح وخلوه من shell=True."""
        resolved_exe = self.validate_command(cmd)

        working_dir = str(cwd) if cwd else neutral_cwd()
        exec_args = [resolved_exe] + cmd[1:]

        result = subprocess.run(
            exec_args,
            cwd=working_dir,
            stdin=subprocess.DEVNULL,
            env=minimal_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            shell=False,  # محظور تشغيل shell=True إطلاقاً
            capture_output=True,
            timeout=timeout,
        )

        return result.returncode, decode_output(result.stdout), decode_output(result.stderr)
