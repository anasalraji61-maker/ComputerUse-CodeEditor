"""دفتر النقاط المحلي (Ledger) عبر SQLite — رصيد محلي وتقدير الكلفة وسقف الإنفاق."""
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Dict, Any, List


class SpendingLimitExceededError(Exception):
    """استثناء عند تجاوز سقف الإنفاق التلقائي."""
    pass


class InsufficientBalanceError(Exception):
    """استثناء عند عدم كفاية الرصيد للمهمة."""
    pass


def _points(value, name: str = "النقاط") -> int:
    """يقبل أعداداً صحيحة غير سالبة فقط (يمنع سكّ نقاط بقيم سالبة أو غير عددية)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} يجب أن تكون عدداً صحيحاً غير سالب (وصلت: {value!r}).")
    return value


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
        self._init_db(initial_balance, spending_limit)

    @contextmanager
    def _get_connection(self, immediate: bool = False):
        """اتصال SQLite يُغلق دائماً (يمنع WinError 32 على ويندوز) مع commit/rollback تلقائي."""
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")  # قفل كتابة فوري: يمنع سباق فحص-ثم-خصم بين خيوط/نسخ
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @property
    def spending_limit(self) -> int:
        """السقف الفعلي المخزَّن في القاعدة (مصدر واحد للحقيقة)."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT spending_limit FROM account WHERE id = 1").fetchone()
            return int(row["spending_limit"]) if row else 0

    def top_up(self, points: int, reason: str = "شحن رصيد", raise_limit: bool = True):
        """إضافة رصيد (ورفع السقف بنفس القدر افتراضياً). مسجَّل كمعاملة."""
        points = _points(points)
        with self._get_connection(immediate=True) as conn:
            conn.execute("UPDATE account SET balance = balance + ?, spending_limit = spending_limit + ? WHERE id = 1",
                         (points, points if raise_limit else 0))
            conn.execute("INSERT INTO transactions (task_id, task_type, estimated_points, charged_points, status, reason) "
                         "VALUES ('topup', 'TOPUP', 0, ?, 'TOPUP', ?)", (-points, reason))

    def _init_db(self, initial_balance: int, spending_limit: int):
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
                    (_points(initial_balance, "الرصيد الابتدائي"), _points(spending_limit, "السقف")),
                )

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

    def get_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """آخر المهام المسجلة (الأحدث أولاً) لعرضها في الواجهة."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT task_id, task_type, status, charged_points, reason, created_at FROM transactions "
                "WHERE status NOT IN ('REFUNDED', 'TOPUP') ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def estimate_cost(task_type: str, steps_count: int = 1) -> int:
        """تقدير كلفة المهمة قبل التنفيذ: نقطة واحدة لكل ~8 خطوات نصية."""
        words = " ".join(t for t in task_type[:4000].lower().split() if not any(c in t for c in "./\\"))  # أسماء الملفات (screen.png) ليست طلب شاشة
        if re.search(r"\bscreen\b", words):
            return 5
        base = max(1, (max(0, int(steps_count)) + 7) // 8)
        return base

    def can_afford(self, points: int) -> bool:
        """التحقق من إمكانية تغطية النقاط دون تجاوز الرصيد أو سقف الإنفاق."""
        points = _points(points)
        balance = self.get_balance()
        total_spent = self.get_total_spent()
        if balance < points:
            return False
        if total_spent + points > self.spending_limit:
            return False
        return True

    def record_task_start(self, task_id: str, task_type: str, estimated_points: int):
        """تسجيل بدء المهمة بالتقدير الأولي (فحص القدرة والإدخال في معاملة واحدة، ومعرّف المهمة فريد)."""
        estimated_points = _points(estimated_points, "التقدير")
        with self._get_connection(immediate=True) as conn:
            acc = conn.execute("SELECT balance, total_spent, spending_limit FROM account WHERE id = 1").fetchone()
            if acc["balance"] < estimated_points:
                raise InsufficientBalanceError(f"الرصيد غير كافٍ ({acc['balance']} < {estimated_points}).")
            if acc["total_spent"] + estimated_points > acc["spending_limit"]:
                raise SpendingLimitExceededError(
                    f"تجاوز سقف الإنفاق المحدد ({acc['total_spent'] + estimated_points} > {acc['spending_limit']}).")
            if conn.execute("SELECT 1 FROM transactions WHERE task_id = ?", (task_id,)).fetchone():
                raise ValueError(f"معرّف المهمة '{task_id}' مستعمل من قبل.")
            conn.execute(
                "INSERT INTO transactions (task_id, task_type, estimated_points, charged_points, status, reason) "
                "VALUES (?, ?, ?, 0, 'STARTED', 'قيد التنفيذ')",
                (task_id, task_type, estimated_points),
            )

    def record_task_success(self, task_id: str, charged_points: int):
        """تسجيل نجاح المهمة وخصم النقاط الفعلية وتحديث سقف الإنفاق."""
        charged_points = _points(charged_points, "النقاط المخصومة")
        with self._get_connection(immediate=True) as conn:
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
            cur_upd = conn.execute(
                """
                UPDATE transactions
                SET charged_points = ?, status = 'SUCCESS', reason = 'اكتملت بنجاح وتدقيق قاطع'
                WHERE task_id = ? AND status = 'STARTED'
                """,
                (charged_points, task_id),
            )
            if cur_upd.rowcount == 0:
                raise ValueError(f"المهمة '{task_id}' غير مسجلة كبادئة أو سُجّلت نتيجتها من قبل.")

    def record_task_failure(
        self,
        task_id: str,
        reason: str,
        is_platform_failure: bool = False,
        charged_points: int = 0,
    ):
        """تسجيل فشل المهمة: لا يُخصم أي شيء إذا كان الفشل بسبب المنصة، ولا يُخصم إلا لمهمة STARTED فعلاً."""
        charged_points = _points(charged_points, "النقاط المخصومة")
        actual_charge = 0 if is_platform_failure else charged_points
        with self._get_connection(immediate=True) as conn:
            upd = conn.execute(
                "UPDATE transactions SET charged_points = ?, status = 'FAILED', reason = ? "
                "WHERE task_id = ? AND status = 'STARTED'",
                (actual_charge, f"فشل: {reason}" + (" (خطأ منصة - لا خصم)" if is_platform_failure else ""), task_id),
            )
            if upd.rowcount and actual_charge > 0:
                conn.execute(
                    "UPDATE account SET balance = balance - ?, total_spent = total_spent + ? WHERE id = 1",
                    (actual_charge, actual_charge),
                )

    def refund(self, task_id: str, points: int, reason: str = "استرجاع نقاط"):
        """استرجاع نقاط مخصومة لمهمة محددة: لا يتجاوز ما خُصم لها فعلاً، ولا يتكرر فوق ذلك."""
        points = _points(points)
        with self._get_connection(immediate=True) as conn:
            charged = conn.execute(
                "SELECT COALESCE(SUM(charged_points), 0) AS c FROM transactions "
                "WHERE task_id = ? AND status IN ('SUCCESS', 'FAILED')", (task_id,)).fetchone()["c"]
            refunded = -conn.execute(
                "SELECT COALESCE(SUM(charged_points), 0) AS c FROM transactions "
                "WHERE task_id = ? AND status = 'REFUNDED'", (task_id,)).fetchone()["c"]
            if points > charged - refunded:
                raise ValueError(f"الاسترجاع ({points}) يتجاوز المتاح للمهمة '{task_id}' ({charged - refunded}).")
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
