"""وحدة إعداد التقارير بالعربية (Reporter) — توثيق الإنجاز بالأدلة وطريقة التراجع."""
from typing import List, Dict, Any, Optional


class ExecutionReport:
    """تقرير إنجاز المهمة باللغة العربية متضمناً الأدلة القاطعة وسجل التراجع."""

    def __init__(
        self,
        task_id: str,
        objective: str,
        success: bool,
        executed_steps: Optional[List[str]] = None,
        evidence: Optional[List[Dict[str, Any]]] = None,
        failures: Optional[List[str]] = None,
        points_charged: int = 0,
        undo_available: bool = True,
        undo_journal_path: Optional[str] = None,
    ):
        self.task_id = task_id
        self.objective = objective
        self.success = success
        self.executed_steps = executed_steps or []
        self.evidence = evidence or []
        self.failures = failures or []
        self.points_charged = points_charged
        self.undo_available = undo_available
        self.undo_journal_path = undo_journal_path

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": "تم" if self.success else "فشل",
            "objective": self.objective,
            "executed_steps": self.executed_steps,
            "evidence": self.evidence,
            "failures": self.failures,
            "points_charged": self.points_charged,
            "undo_available": self.undo_available,
        }

    def render_arabic_text(self) -> str:
        """توليد النص النهائي للتقرير باللغة العربية الفصحى."""
        status_text = "تم بنجاح" if self.success else "فشل"
        lines = [
            f"# تقرير المهمة: {status_text}",
            f"- معرف المهمة: {self.task_id}",
            f"- المطلوب: {self.objective}",
            "",
            "## ما نُفّذ:",
        ]
        if self.executed_steps:
            for i, step in enumerate(self.executed_steps, 1):
                lines.append(f"{i}. {step}")
        else:
            lines.append("- لم تُنفّذ أي خطوات.")

        lines.append("")
        lines.append("## الأدلة القاطعة:")
        if self.evidence:
            for ev in self.evidence:
                item_desc = ev.get("item", "عنصر")
                proof_type = ev.get("type", "تحقق")
                proof_val = ev.get("value", "مطابق")
                lines.append(f"- [{proof_type}] {item_desc}: {proof_val}")
        else:
            lines.append("- لا تتوفر أدلة مسجلة.")

        lines.append("")
        lines.append("## ما فشل:")
        if self.failures:
            for fail in self.failures:
                lines.append(f"- ⚠️ {fail}")
        else:
            lines.append("- لا يوجد أي فشل.")

        lines.append("")
        lines.append(f"## الكلفة بالنقاط: {self.points_charged} نقطة.")

        lines.append("")
        lines.append("## طريقة التراجع:")
        if self.undo_available:
            lines.append(
                "- التراجع متاح عبر استدعاء FileManager.undo_last() أو undo_all()."
            )
            if self.undo_journal_path:
                lines.append(f"- سجل التراجع موثق في الملف: {self.undo_journal_path}")
        else:
            lines.append("- لا تتوفر عمليات قابلة للتراجع لهذه المهمة.")

        return "\n".join(lines)
