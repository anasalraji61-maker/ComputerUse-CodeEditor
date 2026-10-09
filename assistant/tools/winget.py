"""أداة إدارة حزم winget المقتصرة على العرض (list) والتحديث (upgrade) فقط مع فحص دقيق للمدخلات."""
import re
import subprocess
from typing import Callable, List, Optional, Tuple

PACKAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
QUERY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\- ]*$")


class WingetTool:
    """إدارة برامج ويندوز عبر winget مع حصر العمليات في list وupgrade وفحص الأنماط لمنع تمرير خيارات عشوائية."""

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
        """عرض البرامج المثبتة أو البحث عن برنامج محدد بنمط آمن يمنع الخيارات."""
        cmd = ["winget", "list"]
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

        cmd = ["winget", "upgrade", "--id", clean_id, "--accept-source-agreements", "--accept-package-agreements"]
        return self.runner(cmd)
