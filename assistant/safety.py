"""وحدة الأمان (Safety) — جدول صريح للإجراءات، فشل مغلق (fail-closed)، وزر إيقاف مشترك."""
import threading
from enum import Enum
from typing import Dict, Optional


class SafetyLevel(str, Enum):
    """مستويات الصلاحية الأربعة الصارمة لمساعد المهام المكتبية."""
    READ = "READ"                    # قراءة فقط: تنفيذ تلقائي
    REVERSIBLE = "REVERSIBLE"        # تعديل قابل للتراجع: تنفيذ تلقائي مع توثيق السجل
    DANGEROUS = "DANGEROUS"          # خطر: يتطلب موافقة صريحة من المستخدم لكل خطوة
    FORBIDDEN = "FORBIDDEN"          # محظور تماماً: ممنوع منعاً باتاً ولا يمكن تجاوزه


class EmergencyStopTriggered(Exception):
    """استثناء فوري عند تفعيل زر الإيقاف (Kill Switch)."""
    pass


class ActionForbiddenError(Exception):
    """استثناء عند محاولة تنفيذ إجراء محظور أو غير معروف."""
    pass


class ApprovalRequiredError(Exception):
    """استثناء عند محاولة تنفيذ إجراء خطر دون موافقة صريحة."""
    pass


# جدول الإجراءات الصريح (R15): تطابق تام بالاسم، لا مطابقة جزئية إطلاقاً.
# أي اسم غير موجود هنا يُعامل كـ FORBIDDEN (الفشل المغلق).
ACTION_LEVELS: Dict[str, SafetyLevel] = {
    # قراءة
    "inspect": SafetyLevel.READ,
    # تعديل قابل للتراجع
    "create_dir": SafetyLevel.REVERSIBLE,
    "move": SafetyLevel.REVERSIBLE,
    "copy": SafetyLevel.REVERSIBLE,
    "rename": SafetyLevel.REVERSIBLE,
    # الحذف = نقل إلى .trash (قابل للتراجع)، لكن المواصفات تعدّه خطراً فيحتاج موافقة لكل خطوة
    "delete": SafetyLevel.DANGEROUS,
    "trash": SafetyLevel.DANGEROUS,
    # خطر: يحتاج موافقة لكل خطوة
    "winget_upgrade": SafetyLevel.DANGEROUS,
    "run_command": SafetyLevel.DANGEROUS,
    # محظور صراحةً (موثّق للوضوح؛ الأسماء المجهولة محظورة أيضاً)
    "permanent_delete": SafetyLevel.FORBIDDEN,
    "delete_forever": SafetyLevel.FORBIDDEN,
    "format_disk": SafetyLevel.FORBIDDEN,
    "registry_edit": SafetyLevel.FORBIDDEN,
    "system_setting": SafetyLevel.FORBIDDEN,
    "financial_transaction": SafetyLevel.FORBIDDEN,
    "enter_password": SafetyLevel.FORBIDDEN,
}

MUTATING_ACTIONS = frozenset({"create_dir", "move", "copy", "rename", "delete", "trash"})


class KillSwitch:
    """زر إيقاف فوري مشترك بين الخيوط (threading.Event) يُفحص قبل كل خطوة وداخل الحلقات."""

    def __init__(self):
        self._event = threading.Event()
        self._reason: str = ""

    def trigger(self, reason: str = "تم تفعيل زر الإيقاف من قبل المستخدم"):
        """تفعيل زر الإيقاف وقطع العمليات فوراً (آمن للاستدعاء من خيط الواجهة)."""
        self._reason = reason
        self._event.set()

    def reset(self):
        """إعادة ضبط زر الإيقاف."""
        self._event.clear()
        self._reason = ""

    @property
    def is_triggered(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def check(self):
        """التحقق من حالة الزر ورفع استثناء فوري إذا كان مفعّلاً."""
        if self._event.is_set():
            raise EmergencyStopTriggered(self._reason or "تم تفعيل زر الإيقاف الفوري.")


class SafetyGuard:
    """حارس الأمان لفحص العمليات وتصنيفها والتحقق من زر الإيقاف والموافقات."""

    def __init__(self, kill_switch: Optional[KillSwitch] = None):
        self.kill_switch = kill_switch or KillSwitch()

    @staticmethod
    def normalize(action_type) -> str:
        return action_type.strip().lower() if isinstance(action_type, str) else ""

    def classify_action(self, action_type) -> SafetyLevel:
        """تصنيف الإجراء بمطابقة تامة مع الجدول؛ المجهول = FORBIDDEN."""
        return ACTION_LEVELS.get(self.normalize(action_type), SafetyLevel.FORBIDDEN)

    def is_known(self, action_type) -> bool:
        return self.normalize(action_type) in ACTION_LEVELS

    def check_action(self, action_type, user_approved: bool = False) -> SafetyLevel:
        """فحص أمني قبل تنفيذ أي خطوة: زر الإيقاف + مستوى الصلاحية + الموافقة (تخص هذه الخطوة فقط)."""
        self.kill_switch.check()

        level = self.classify_action(action_type)
        if level == SafetyLevel.FORBIDDEN:
            raise ActionForbiddenError(
                f"الإجراء '{action_type}' محظور أو غير معروف ولا يمكن تنفيذه بموجب قواعد الأمان."
            )
        if level == SafetyLevel.DANGEROUS and not user_approved:
            raise ApprovalRequiredError(
                f"الإجراء '{action_type}' يتطلب موافقة صريحة من المستخدم قبل التنفيذ."
            )
        return level
