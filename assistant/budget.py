"""حارس الميزانية (Budget Guard) — متابعة سقوف الخطوات والتوكنات والمحاولات لكل مهمة."""
from typing import Dict, Any


class BudgetExceededError(Exception):
    """استثناء عند تجاوز أي سقف من سقوف الميزانية المحددة."""
    pass


class BudgetTracker:
    """إدارة ومراقبة استهلاك الموارد لمنع الحلقات اللانهائية والهدر."""

    def __init__(
        self,
        max_steps: int = 10,
        max_tokens: int = 4000,
        max_retries: int = 3,
        max_points: int = 10,
    ):
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.max_points = max_points

        self.current_steps = 0
        self.current_tokens = 0
        self.current_retries = 0
        self.current_points = 0

    def charge(self, steps: int = 1, tokens: int = 0, retries: int = 0, points: int = 0):
        """خصم الموارد وفحص السقوف؛ يرفع BudgetExceededError فوراً عند التجاوز."""
        if self.current_steps + steps > self.max_steps:
            raise BudgetExceededError(
                f"تم تجاوز سقف الخطوات ({self.current_steps + steps} > {self.max_steps})."
            )
        if self.current_tokens + tokens > self.max_tokens:
            raise BudgetExceededError(
                f"تم تجاوز سقف التوكنات ({self.current_tokens + tokens} > {self.max_tokens})."
            )
        if self.current_retries + retries > self.max_retries:
            raise BudgetExceededError(
                f"تم تجاوز سقف المحاولات ({self.current_retries + retries} > {self.max_retries})."
            )
        if self.current_points + points > self.max_points:
            raise BudgetExceededError(
                f"تم تجاوز سقف النقاط ({self.current_points + points} > {self.max_points})."
            )

        self.current_steps += steps
        self.current_tokens += tokens
        self.current_retries += retries
        self.current_points += points

    def record_step(self):
        """تسجيل خطوة واحدة."""
        self.charge(steps=1)

    def record_tokens(self, tokens: int):
        """تسجيل استهلاك توكنات."""
        self.charge(steps=0, tokens=tokens)

    def record_retry(self):
        """تسجيل محاولة إعادة."""
        self.charge(steps=0, retries=1)

    def get_summary(self) -> Dict[str, Any]:
        """ملخص الاستهلاك الحالي والمتبقي."""
        return {
            "steps": f"{self.current_steps}/{self.max_steps}",
            "tokens": f"{self.current_tokens}/{self.max_tokens}",
            "retries": f"{self.current_retries}/{self.max_retries}",
            "points": f"{self.current_points}/{self.max_points}",
            "steps_count": self.current_steps,
            "tokens_count": self.current_tokens,
            "points_count": self.current_points,
        }
