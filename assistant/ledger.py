"""دفتر النقاط المحلي (Ledger) عبر SQLite — رصيد محلي وتقدير الكلفة وسقف الإنفاق."""
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Dict, Any, List

from assistant.arabic import n_units


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
        # تتحقق الواجهة منها بعد الإنشاء: ملف تالف/فارغ لا يمنع التشغيل، بل يُعزل ويُنشأ دفتر جديد مع تنبيه ظاهر
        self.recovered_from_corruption = False
        self.recovery_message = ""
        self.quarantined_path: Optional[Path] = None
        self.reconciled_stuck = 0
        try:
            st = self.db_path.stat() if self.db_path.exists() else None
            # صفري البايت وأقدم من بضع ثوانٍ فقط: ملف حديث الإنشاء قد يكون لمُشغِّل متزامن يهيّئه الآن
            empty = st is not None and st.st_size == 0 and (time.time() - st.st_mtime) > self.FRESH_FILE_SEC
        except OSError:
            empty = False
        if empty:  # ملف صفري البايت (انقطاع أثناء الإنشاء/تفريغ): لا إعادة ضبط صامتة للرصيد
            self._mark_recovered("ملف النقاط كان فارغاً فأنشأت رصيداً جديداً قد يختلف عن رصيدك السابق.")
        for attempt in range(6):
            try:
                self._init_db(initial_balance, spending_limit)
                self._integrity_check()
                break
            except sqlite3.DatabaseError as exc:
                if isinstance(exc, sqlite3.OperationalError) and not self._looks_corrupt(exc):
                    raise   # قفل/صلاحيات/قرص ممتلئ: ليست تلفاً، لا نعزل شيئاً
                if attempt < 5 and self._is_fresh():
                    time.sleep(0.5)   # ملف حديث (مُشغِّل آخر يهيّئه الآن): نمهله ونعيد قبل أن نحكم بتلفه
                    continue
                self._quarantine(exc)
                self._init_db(initial_balance, spending_limit)
                break
        self._reconcile_stuck()

    FRESH_FILE_SEC = 5.0

    def _is_fresh(self) -> bool:
        try:
            return (time.time() - self.db_path.stat().st_mtime) <= self.FRESH_FILE_SEC
        except OSError:
            return False

    @staticmethod
    def _looks_corrupt(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(k in msg for k in ("malformed", "not a database", "corrupt", "disk image"))

    def _mark_recovered(self, msg: str) -> None:
        self.recovered_from_corruption = True
        self.recovery_message = (self.recovery_message + " " + msg).strip()

    def _integrity_check(self) -> None:
        with self._get_connection() as conn:
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
            if row is not None and str(row[0]).lower() != "ok":
                raise sqlite3.DatabaseError("database disk image is malformed (quick_check)")

    def _quarantine(self, exc: Exception) -> None:
        """يعزل الملف التالف (مع ملفات -wal/-shm) إلى ledger.db.corrupt.<ts> ليُنشأ دفتر جديد."""
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = self.db_path.with_name(f"{self.db_path.name}.corrupt.{ts}")
        n = 0
        while dest.exists() and n < 100:
            n += 1
            dest = self.db_path.with_name(f"{self.db_path.name}.corrupt.{ts}.{n}")
        moved = False
        try:
            os.replace(str(self.db_path), str(dest))
            moved = True
            self.quarantined_path = dest
        except OSError:
            pass
        for suffix in ("-wal", "-shm", "-journal"):
            side = self.db_path.with_name(self.db_path.name + suffix)
            try:
                if side.exists():
                    if moved:
                        os.replace(str(side), str(dest.with_name(dest.name + suffix)))
                    else:
                        side.unlink()
            except OSError:
                pass
        if not moved:
            try:
                self.db_path.unlink()   # لا نقدر على الأرشفة: نفضّل البدء من جديد على عدم التشغيل
            except OSError:
                raise exc
        self._mark_recovered("ملف النقاط كان تالفاً"
                             + (f" (حُفظت نسخته للفحص باسم {dest.name})" if moved else "")
                             + " فأنشأت رصيداً جديداً قد يختلف عن رصيدك السابق.")

    def _reconcile_stuck(self) -> None:
        """مهمة بقيت STARTED أكثر من ساعة (أُغلق التطبيق/انهار أثناءها) تُغلق FAILED بلا خصم، فلا تبقى معلّقة في السجل."""
        try:
            with self._get_connection(immediate=True) as conn:
                cur = conn.execute(
                    "UPDATE transactions SET status = 'FAILED', charged_points = 0, "
                    "reason = 'انقطعت المهمة قبل اكتمالها (أُغلق التطبيق أو انهار) - لا خصم' "
                    "WHERE status = 'STARTED' AND created_at < datetime('now', '-1 hour')")
                self.reconciled_stuck = cur.rowcount or 0
        except sqlite3.Error:
            pass

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

    # ---- مرحلة التجربة: شحن تلقائي صغير بدل أن يتوقف التطبيق للأبد ----
    TRIAL_MIN_POINTS = 20
    TRIAL_TOPUP_POINTS = 100

    @staticmethod
    def trial_phase_enabled() -> bool:
        """علم مرحلة التجربة: مفعّل افتراضياً؛ COS_TRIAL_TOPUP=0 يعطّله (عند الانتقال إلى رصيد حقيقي)."""
        return os.environ.get("COS_TRIAL_TOPUP", "1").strip().lower() not in ("0", "false", "no", "off")

    def ensure_trial_balance(self, min_points: int = TRIAL_MIN_POINTS, topup_points: int = TRIAL_TOPUP_POINTS) -> bool:
        """إن نزل الرصيد دون min_points يشحن topup_points (ويرفع سقف نقاط الدفتر بالقدر نفسه) في معاملة واحدة ذرّية، ويعيد True إن شحن.
        نفس منطق بدء التشغيل؛ يستدعيه المتحكم قبل فحص القدرة على الدفع. مسجَّل كمعاملة TOPUP.
        ملاحظة: هذا رصيد نقاط محلي تجريبي لا مال؛ سقف الإنفاق الحقيقي بالدولار على النماذج المدفوعة يبقى في UsageMeter (usage.py)
        ولا يتأثر بهذا الشحن."""
        if not self.trial_phase_enabled():
            return False
        min_points, topup_points = _points(min_points, "الحد الأدنى"), _points(topup_points, "مقدار الشحن")
        with self._get_connection(immediate=True) as conn:
            row = conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()
            if row is None or int(row["balance"]) >= min_points or topup_points == 0:
                return False
            conn.execute("UPDATE account SET balance = balance + ?, spending_limit = spending_limit + ? WHERE id = 1",
                         (topup_points, topup_points))
            conn.execute("INSERT INTO transactions (task_id, task_type, estimated_points, charged_points, status, reason) "
                         "VALUES ('topup', 'TOPUP', 0, ?, 'TOPUP', 'شحن تجريبي تلقائي')", (-topup_points,))
            return True

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

    # إجراءات قراءة فقط/عديمة الأثر عند تكرارها بنفس الوسائط: لا يجوز تضخيم الكلفة بحشوها
    _IDEMPOTENT_ACTIONS = ("inspect", "create_dir")

    @classmethod
    def effective_step_count(cls, steps) -> int:
        """عدد الخطوات المحسوبة: كل خطوة قراءة/إنشاء-مجلد متطابقة (الإجراء، الهدف، الوسائط) تُحسب مرة واحدة. غيرها تُحسب كلها."""
        seen = set()
        n = 0
        for st in steps or ():
            get = (lambda k, d=None: st.get(k, d)) if isinstance(st, dict) else (lambda k, d=None: getattr(st, k, d))
            action = str(get("action_type", "") or "").strip().lower()
            if action in cls._IDEMPOTENT_ACTIONS:
                try:
                    key = (action, str(get("target", "")).strip().casefold(), repr(sorted((get("params") or {}).items())))
                except Exception:
                    key = (action, repr(get("target")), repr(get("params")))
                if key in seen:
                    continue
                seen.add(key)
            n += 1
        return n

    @classmethod
    def estimate_cost(cls, task_type: str, steps_count: int = 1, steps=None) -> int:
        """تقدير كلفة المهمة قبل التنفيذ: نقطة واحدة لكل ~8 خطوات نصية.
        إن مُرّرت steps (قائمة خطوات) فخطوات القراءة/عديمة الأثر المتطابقة تُحسب مرة واحدة (لا حشو لرفع الكلفة)."""
        if steps is not None:
            steps_count = cls.effective_step_count(steps)
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
                raise InsufficientBalanceError(f"الرصيد غير كافٍ: رصيدك {n_units(acc['balance'], 'نقطة')} والمهمة تحتاج {n_units(estimated_points, 'نقطة')}.")
            if acc["total_spent"] + estimated_points > acc["spending_limit"]:
                raise SpendingLimitExceededError(
                    f"سقف الإنفاق المحدد {n_units(acc['spending_limit'], 'نقطة')} وهذه المهمة ستتجاوزه (المُنفَق حتى الآن {n_units(acc['total_spent'], 'نقطة')}).")
            if conn.execute("SELECT 1 FROM transactions WHERE task_id = ?", (task_id,)).fetchone():
                raise ValueError("سُجّلت هذه المهمة من قبل ولا يمكن بدؤها مرة ثانية.")
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
                raise SpendingLimitExceededError("بلغ الإنفاق سقفه الأقصى فلا يمكن خصم نقاط أخرى.")
            if balance < charged_points:
                raise InsufficientBalanceError("الرصيد غير كافٍ لإتمام الخصم، فلم تُخصم أي نقاط.")

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
                (actual_charge, (f"فشل: {reason}" + (" (خطأ منصة - لا خصم)" if is_platform_failure else "")).encode("utf-8", "replace").decode("utf-8"), task_id),
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
