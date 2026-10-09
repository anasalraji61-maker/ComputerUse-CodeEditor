"""أداة إدارة حزم winget المقتصرة على العرض (list) والتحديث (upgrade) فقط مع دعم المحاكاة للاختبارات."""
import subprocess
from typing import Callable, List, Optional, Tuple


class WingetTool:
    """إدارة برامج ويندوز عبر winget مع حصر العمليات في list وupgrade فقط ودعم دالة تشغيل قابلة للاستبدال."""

    def __init__(self, runner: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None):
        self.runner = runner or self._default_runner

    @staticmethod
    def _default_runner(cmd: List[str]) -> Tuple[int, str, str]:
        """المشغل الافتراضي الفعلي لـ winget."""
        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        return result.returncode, result.stdout, result.stderr

    def list_packages(self, query: Optional[str] = None) -> Tuple[int, str, str]:
        """عرض البرامج المثبتة أو البحث عن برنامج محدد."""
        cmd = ["winget", "list"]
        if query:
            # تنظيف الاستعلام للتأكد من خلوه من الأحرف الخطرة
            clean_query = query.strip()
            if any(c in clean_query for c in {"&", "|", ">", "<", ";", "`", "$"}):
                raise ValueError("استعلام winget يحتوي على رموز محظورة.")
            cmd.extend(["-q", clean_query])
        return self.runner(cmd)

    def upgrade_package(self, package_id: str) -> Tuple[int, str, str]:
        """تحديث برنامج محدد عبر معرف الحزمة (package_id)."""
        clean_id = package_id.strip()
        if not clean_id:
            raise ValueError("معرّف الحزمة مطلوب لتحديث البرنامج.")
        if any(c in clean_id for c in {"&", "|", ">", "<", ";", "`", "$", " "}):
            raise ValueError("معرّف الحزمة يحتوي على رموز أو مسافات غير صالحة.")

        cmd = ["winget", "upgrade", "--id", clean_id, "--accept-source-agreements", "--accept-package-agreements"]
        return self.runner(cmd)
