"""دفتر النقاط المحلي (Ledger) عبر SQLite — رصيد محلي وتقدير الكلفة وسقف الإنفاق."""
import sqlite3
from pathlib import Path
from typing import Optional, Dict, Any, List


class SpendingLimitExceededError(Exception):
    """استثناء عند تجاوز سقف الإنفاق التلقائي."""
    pass


class InsufficientBalanceError(Exception):
    """استثناء عند عدم كفاية الرصيد للمهمة."""
    pass


class SQLiteLedger:
    """دفتر النقاط المحلي باستخدام SQLite لإدارة الحسابات والمعاملات بأمان."""

    def __init__(
        self,
        db_path: Path,
        initial_balance: int = 100,
        spending_limit: int = 50,
    ):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.spending_limit = spending_limit
        self._init_db(initial_balance)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self, initial_balance: int):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL,
                    spending_limit INTEGER NOT NULL,
                    total_spent INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    estimated_points INTEGER NOT NULL,
                    charged_points INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur = conn.execute("SELECT id FROM account WHERE id = 1")
            if not cur.fetchone():
                conn.execute(
                    "INSERT INTO account (id, balance, spending_limit, total_spent) VALUES (1, ?, ?, 0)",
                    (initial_balance, self.spending_limit),
                )
            conn.commit()

    def get_balance(self) -> int:
        """قراءة الرصيد الحالي."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT balance FROM account WHERE id = 1")
            row = cur.fetchone()
            return int(row["balance"]) if row else 0

    def get_total_spent(self) -> int:
        """إجمالي النقاط المنفقة."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT total_spent FROM account WHERE id = 1")
            row = cur.fetchone()
            return int(row["total_spent"]) if row else 0

    @staticmethod
    def estimate_cost(task_type: str, steps_count: int = 1) -> int:
        """تقدير كلفة المهمة قبل التنفيذ: نقطة واحدة لكل ~8 خطوات نصية."""
        if "screen" in task_type.lower():
            return 5
        base = max(1, (steps_count + 7) // 8)
        return base

    def can_afford(self, points: int) -> bool:
        """التحقق من إمكانية تغطية النقاط دون تجاوز الرصيد أو سقف الإنفاق."""
        balance = self.get_balance()
        total_spent = self.get_total_spent()
        if balance < points:
            return False
        if total_spent + points > self.spending_limit:
            return False
        return True

    def record_task_start(self, task_id: str, task_type: str, estimated_points: int):
        """تسجيل بدء المهمة بالتقدير الأولي."""
        if not self.can_afford(estimated_points):
            balance = self.get_balance()
            total_spent = self.get_total_spent()
            if balance < estimated_points:
                raise InsufficientBalanceError(f"الرصيد غير كافٍ ({balance} < {estimated_points}).")
            raise SpendingLimitExceededError(
                f"تجاوز سقف الإنفاق المحدد ({total_spent + estimated_points} > {self.spending_limit})."
            )

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO transactions (task_id, task_type, estimated_points, charged_points, status, reason)
                VALUES (?, ?, ?, 0, 'STARTED', 'قيد التنفيذ')
                """,
                (task_id, task_type, estimated_points),
            )
            conn.commit()

    def record_task_success(self, task_id: str, charged_points: int):
        """تسجيل نجاح المهمة وخصم النقاط الفعلية وتحديث سقف الإنفاق."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT balance, total_spent, spending_limit FROM account WHERE id = 1")
            acc = cur.fetchone()
            balance, total_spent, limit = acc["balance"], acc["total_spent"], acc["spending_limit"]

            if total_spent + charged_points > limit:
                raise SpendingLimitExceededError("تم بلوغ سقف الإنفاق الأقصى.")
            if balance < charged_points:
                raise InsufficientBalanceError("الرصيد غير كافٍ لإتمام الخصم.")

            conn.execute(
                "UPDATE account SET balance = balance - ?, total_spent = total_spent + ? WHERE id = 1",
                (charged_points, charged_points),
            )
            conn.execute(
                """
                UPDATE transactions
                SET charged_points = ?, status = 'SUCCESS', reason = 'اكتملت بنجاح وتدقيق قاطع'
                WHERE task_id = ?
                """,
                (charged_points, task_id),
            )
            conn.commit()

    def record_task_failure(
        self,
        task_id: str,
        reason: str,
        is_platform_failure: bool = False,
        charged_points: int = 0,
    ):
        """تسجيل فشل المهمة: لا يُخصم أي شيء إذا كان الفشل بسبب المنصة."""
        actual_charge = 0 if is_platform_failure else charged_points

        with self._get_connection() as conn:
            if actual_charge > 0:
                conn.execute(
                    "UPDATE account SET balance = balance - ?, total_spent = total_spent + ? WHERE id = 1",
                    (actual_charge, actual_charge),
                )
            conn.execute(
                """
                UPDATE transactions
                SET charged_points = ?, status = 'FAILED', reason = ?
                WHERE task_id = ?
                """,
                (actual_charge, f"فشل: {reason}" + (" (خطأ منصة - لا خصم)" if is_platform_failure else ""), task_id),
            )
            conn.commit()

    def refund(self, task_id: str, points: int, reason: str = "استرجاع نقاط"):
        """استرجاع نقاط مخصومة لحساب المستخدم."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE account SET balance = balance + ?, total_spent = max(0, total_spent - ?) WHERE id = 1",
                (points, points),
            )
            conn.execute(
                """
                INSERT INTO transactions (task_id, task_type, estimated_points, charged_points, status, reason)
                VALUES (?, 'REFUND', 0, ?, 'REFUNDED', ?)
                """,
                (task_id, -points, reason),
            )
            conn.commit()
