"""أداة إدارة حزم winget المقتصرة على العرض (list) والتحديث (upgrade) فقط مع فحص دقيق للمدخلات."""
import os
import re
import subprocess
from typing import Callable, List, Optional, Tuple

from assistant.tools.shell import minimal_env, neutral_cwd, resolve_exe

PACKAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
DEFAULT_SOURCE = "winget"
QUERY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\- ]*$")


WINGET_NOT_FOUND_CODE = 127
WINGET_NOT_FOUND_MSG = ("أداة winget غير مثبّتة أو غير موجودة على هذا الجهاز (أو وُجدت داخل مجلد العمل فرُفضت). "
                        "ثبّت «App Installer» من Microsoft Store أو حدّث ويندوز، ثم أعد المحاولة.")


class WingetTool:
    """إدارة برامج ويندوز عبر winget مع حصر العمليات في list وupgrade وفحص الأنماط لمنع تمرير خيارات عشوائية."""

    def __init__(self, runner: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
                 stop_check: Optional[Callable[[], bool]] = None, timeout: float = 900.0, reject_roots=(),
                 source: Optional[str] = "env"):
        self.stop_check = stop_check
        # مصدر الترقية: الافتراضي "winget" (المستودع الرسمي)؛ حزم msstore لا تظهر فيه. لتضمينها اضبط COS_WINGET_SOURCE=msstore
        # أو مرّر source=؛ القيم الفارغة/"all"/None تحذف الخيار (كل المصادر). القيمة تُفحص بنمط صارم.
        from_env = source == "env"
        if from_env:
            source = os.environ.get("COS_WINGET_SOURCE", DEFAULT_SOURCE)
        src = (source or "").strip()
        if src.lower() in ("", "all", "none"):
            src = ""
        elif not SOURCE_PATTERN.match(src):
            if not from_env:
                raise ValueError(f"مصدر winget غير صالح: '{source}'")
            src = DEFAULT_SOURCE   # قيمة بيئة تالفة لا توقف التطبيق
        self.source = src
        self.reject_roots = tuple(reject_roots)  # مجلد العمل: أي winget داخله مرفوض
        self.timeout = timeout
        self.runner = runner or self._default_runner

    def _default_runner(self, cmd: List[str]) -> Tuple[int, str, str]:
        """المشغل الافتراضي الفعلي لـ winget: يفحص زر الإيقاف كل نصف ثانية ويُنهي العملية فوراً عند الضغط."""
        if cmd and cmd[0] == "winget":  # الاسم المجرد يُحلّ صراحةً إلى مسار مطلق خارج المجلد الحالي ومجلد العمل
            exe = self._resolve_winget()
            if exe is None:
                return WINGET_NOT_FOUND_CODE, "", WINGET_NOT_FOUND_MSG
            cmd = [exe] + list(cmd[1:])
        proc = subprocess.Popen(cmd, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=minimal_env(), cwd=neutral_cwd(), text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # exe بلا نافذة: لا وميض كونسول
        waited = 0.0
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                return proc.returncode, out, err
            except subprocess.TimeoutExpired:
                waited += 0.5
                if (self.stop_check is not None and self.stop_check()) or waited >= self.timeout:
                    self._kill_tree(proc)
                    try:
                        out, err = proc.communicate(timeout=5)  # حفيد يمسك الأنبوب قد يعلّق الانتظار للأبد
                    except subprocess.TimeoutExpired:
                        out, err = "", ""
                    why = "أُوقفت العملية بزر الإيقاف" if waited < self.timeout else "انتهت مهلة الأمر"
                    return -1, out or "", (err or "") + why

    def is_available(self) -> bool:
        """هل يوجد winget صالح للتشغيل؟ (للتحقق قبل طلب الموافقة إن رغب المتحكم/المنسّق)."""
        return self._resolve_winget() is not None

    def _resolve_winget(self) -> Optional[str]:
        """مسار winget المطلق من PATH (اسم WindowsApps المستعار)، مع رفض المجلد الحالي ومجلد العمل."""
        return resolve_exe("winget", reject_roots=self.reject_roots)

    @staticmethod
    def _kill_tree(proc) -> None:
        """يُنهي winget وعمليات المثبّت التابعة له (على ويندوز taskkill /T) لا العملية الأب فقط."""
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            try:
                tk = resolve_exe("taskkill")  # System32 أولاً؛ لا اسم مجرد
                if tk:
                    subprocess.run([tk, "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10,
                                   env=minimal_env(), cwd=neutral_cwd(), creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass

    def list_packages(self, query: Optional[str] = None) -> Tuple[int, str, str]:
        """عرض البرامج المثبتة أو البحث عن برنامج محدد بنمط آمن يمنع الخيارات."""
        cmd = ["winget", "list", "--accept-source-agreements", "--disable-interactivity"]
        if query is not None:
            clean_query = query.strip()
            if clean_query:
                if not QUERY_PATTERN.match(clean_query) or clean_query.startswith("-"):
                    raise ValueError(f"استعلام winget غير صالح أو يبدأ بشرطة: '{query}'")
                cmd.extend(["-q", clean_query])
        return self.runner(cmd)

    def upgrade_package(self, package_id: str) -> Tuple[int, str, str]:
        """تحديث برنامج محدد عبر معرف الحزمة (package_id) بنمط صريح يمنع الخيارات."""
        if not package_id or not isinstance(package_id, str):
            raise ValueError("معرّف الحزمة مطلوب لتحديث البرنامج.")
        clean_id = package_id.strip()
        if not PACKAGE_ID_PATTERN.match(clean_id) or clean_id.startswith("-"):
            raise ValueError(f"معرّف الحزمة غير صالح أو يبدأ بشرطة: '{package_id}'")

        cmd = ["winget", "upgrade", "--id", clean_id, "--exact"]
        if self.source:
            cmd += ["--source", self.source]
        cmd += ["--accept-source-agreements", "--accept-package-agreements", "--silent", "--disable-interactivity"]
        return self.runner(cmd)
