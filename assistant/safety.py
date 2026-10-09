"""وحدة الأمان (Safety) — مستويات الصلاحية وزر الإيقاف المشترك وحارس الأمان."""
from enum import Enum
from typing import Optional, Set


class SafetyLevel(str, Enum):
    """مستويات الصلاحية الأربعة الصارمة لمساعد المهام المكتبية."""
    READ = "READ"                    # قراءة فقط: تنفيذ تلقائي
    REVERSIBLE = "REVERSIBLE"        # تعديل قابل للتراجع: تنفيذ تلقائي مع توثيق السجل
    DANGEROUS = "DANGEROUS"          # خطر: يتطلب موافقة صريحة من المستخدم
    FORBIDDEN = "FORBIDDEN"          # محظور تماماً: ممنوع منعاً باتاً ولا يمكن تجاوزه


class EmergencyStopTriggered(Exception):
    """استثناء فوري عند تفعيل زر الإيقاف (Kill Switch)."""
    pass


class ActionForbiddenError(Exception):
    """استثناء عند محاولة تنفيذ إجراء محظور أمنياً."""
    pass


class ApprovalRequiredError(Exception):
    """استثناء عند محاولة تنفيذ إجراء خطر دون موافقة صريحة."""
    pass


class KillSwitch:
    """زر إيقاف فوري مشترك (Kill Switch) يُفحص قبل كل خطوة وداخل الحلقات."""

    def __init__(self):
        self._triggered: bool = False
        self._reason: str = ""

    def trigger(self, reason: str = "تم تفعيل زر الإيقاف من قبل المستخدم"):
        """تفعيل زر الإيقاف وقطع العمليات فوراً."""
        self._triggered = True
        self._reason = reason

    def reset(self):
        """إعادة ضبط زر الإيقاف."""
        self._triggered = False
        self._reason = ""

    @property
    def is_triggered(self) -> bool:
        return self._triggered

    @property
    def reason(self) -> str:
        return self._reason

    def check(self):
        """التحقق من حالة الزر ورفع استثناء فوري إذا كان مفعّلاً."""
        if self._triggered:
            raise EmergencyStopTriggered(self._reason or "تم تفعيل زر الإيقاف الفوري.")


class SafetyGuard:
    """حارس الأمان لفحص العمليات وتصنيفها والتحقق من زر الإيقاف والموافقات."""

    # كلمات دلالية للإجراءات المحظورة
    FORBIDDEN_KEYWORDS: Set[str] = {
        "permanent_delete", "delete_forever", "rmdir_force", "format",
        "drop_database", "wipe", "financial", "credit_card", "password",
        "crypto_key", "raw_disk", "kill_system"
    }

    # كلمات دلالية للإجراءات الخطرة التي تتطلب موافقة
    DANGEROUS_KEYWORDS: Set[str] = {
        "install", "uninstall", "upgrade", "winget_upgrade", "system_setting",
        "registry", "kill_process", "service_stop"
    }

    # كلمات دلالية للإجراءات القابلة للتراجع
    REVERSIBLE_KEYWORDS: Set[str] = {
        "move", "copy", "rename", "create_dir", "delete", "trash", "touch"
    }

    def __init__(self, kill_switch: Optional[KillSwitch] = None):
        self.kill_switch = kill_switch or KillSwitch()

    def classify_action(self, action_type: str) -> SafetyLevel:
        """تصنيف الإجراء بناءً على نوعه الصريح."""
        act = action_type.lower().strip()
        for kw in self.FORBIDDEN_KEYWORDS:
            if kw in act:
                return SafetyLevel.FORBIDDEN
        for kw in self.DANGEROUS_KEYWORDS:
            if kw in act:
                return SafetyLevel.DANGEROUS
        for kw in self.REVERSIBLE_KEYWORDS:
            if kw in act:
                return SafetyLevel.REVERSIBLE
        return SafetyLevel.READ

    def check_action(self, action_type: str, user_approved: bool = False):
        """فحص أمني شامل قبل تنفيذ أي خطوة: زر الإيقاف + مستوى الصلاحية والموافقة."""
        self.kill_switch.check()

        level = self.classify_action(action_type)
        if level == SafetyLevel.FORBIDDEN:
            raise ActionForbiddenError(
                f"الإجراء '{action_type}' محظور تماماً بموجب قواعد الأمان الصارمة."
            )
        if level == SafetyLevel.DANGEROUS and not user_approved:
            raise ApprovalRequiredError(
                f"الإجراء '{action_type}' يتطلب موافقة صريحة من المستخدم قبل التنفيذ."
            )
