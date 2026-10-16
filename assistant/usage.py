"""عدّاد استهلاك النماذج الحقيقية بالدولار (يومي، محفوظ على القرص) وسقف يمنع الإنفاق المفتوح.

سلامة الملف: الكتابة ذرّية باسم مؤقت فريد لكل كاتب مع fsync وإعادة محاولة os.replace، وقفل ملف بين العمليات (نافذتا تطبيق)،
وإعادة القراءة داخل القفل قبل كل إضافة. الملف التالف يُعزل إلى usage.json.corrupt.<ts> ويبدأ عدّ جديد (مع تنبيه ظاهر) فلا يُقفل
الإنفاق للأبد؛ والخطأ العابر في القراءة (قفل مضاد فيروسات) يُعاد ثم يُفتح السقف مع تحذير بدل «بلغت السقف».
حجوزات النداءات المدفوعة الجارية تُسجَّل في usage.json.pending (pid + وقت) فتُحتسب عند أي عملية/إعادة تشغيل لمدة 10 دقائق."""
import contextlib
import datetime
import json
import math
import os
import re
import threading
import time
import weakref
from pathlib import Path
from typing import Callable, Dict, Optional

DEFAULT_DAILY_CAP_USD = 1.0
REGRESS_WINDOW_DAYS = 3        # بعد تراجع الساعة يُحتسب الأعلى من خانتي (اليوم، أحدث يوم) هذه المدة
KEEP_DAYS = 7                  # أيام تُحفظ في الملف (ساعة تعود للوراء لا تمحو السجل)
PENDING_TTL_SEC = 600.0        # حجز معلّق (نداء قد يكون فُوتر ثم انهار التطبيق) يُحتسب 10 دقائق
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOCKED_USD = 1e12             # قيمة تُكتب (JSON آمن) لتقفل اليوم المعبوث بملفه
_ABSURD_USD = 1e9              # قيمة يومية مستحيلة: تُعتبر العبث بالملف فتُقفل ذلك اليوم فقط

_ACTIVE: "Optional[weakref.ref]" = None


def active_meter() -> "Optional[UsageMeter]":
    """آخر عدّاد أُنشئ في العملية (مرجع ضعيف): الموجّه يحجز عليه كلفة النداء المدفوع قبل إرساله."""
    ref = _ACTIVE
    return ref() if ref is not None else None


def _atomic_write(path: Path, text: str) -> None:
    """كتابة ذرّية: اسم مؤقت فريد (pid+خيط+عدّاد) + flush + fsync + os.replace مع إعادة محاولة عند قفل ويندوز."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        last = None
        for attempt in range(8):
            try:
                os.replace(str(tmp), str(path))
                return
            except PermissionError as exc:   # ويندوز: الهدف مفتوح لحظياً من قارئ آخر/مضاد فيروسات
                last = exc
                time.sleep(0.05 * (attempt + 1))
        raise last  # type: ignore[misc]
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


class _FileLock:
    """قفل استشاري بين العمليات على ملف .lock (fcntl على يونكس، msvcrt على ويندوز). إن تعذّر خلال المهلة نكمل بلا قفل."""

    def __init__(self, path: Path, timeout: float = 5.0):
        self.path, self.timeout, self._fh = path, timeout, None

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a+b")
        except OSError:
            self._fh = None
            return self
        end = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, ImportError):
                if time.monotonic() >= end:
                    return self   # بلا قفل: أفضل جهد
                time.sleep(0.02)

    def __exit__(self, *exc):
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except OSError:
                pass
        return False


class UsageMeter:
    def __init__(self, path, daily_cap_usd: Optional[float] = None,
                 today: Optional[Callable[[], datetime.date]] = None):
        self.path = Path(path)
        self.pending_path = self.path.with_name(self.path.name + ".pending")
        self._flock_path = self.path.with_name(self.path.name + ".lock")
        cap = daily_cap_usd
        if cap is None:
            try:
                cap = float(os.environ.get("COS_MAX_USD_PER_DAY", DEFAULT_DAILY_CAP_USD))
            except ValueError:
                cap = DEFAULT_DAILY_CAP_USD
        if not (isinstance(cap, (int, float)) and math.isfinite(cap) and cap >= 0):
            cap = DEFAULT_DAILY_CAP_USD
        self.cap = float(cap)
        self._today = today or datetime.date.today
        self._lock = threading.Lock()
        self._ilock = threading.Lock()
        self._inflight: Dict[int, float] = {}   # token -> USD محجوز لنداءات مدفوعة قيد الطيران في هذه العملية
        self._next_token = 0
        self._uid = f"{os.getpid()}-{id(self) & 0xFFFFFF:x}"
        self.reset_note = ""     # يُملأ إن عُزل ملف تالف وبدأ العدّ من جديد (تعرضه الواجهة)
        self.warning = ""        # قراءة/كتابة فشلت مؤقتاً فلم يُطبَّق السقف بدقة
        self.quarantined_path: Optional[Path] = None
        self.clock_regress = False   # الساعة تأخرت عن أحدث يوم مسجّل بأكثر من يوم (ما زلنا داخل نافذة الاحتياط)
        self.clock_note = ""
        global _ACTIVE
        _ACTIVE = weakref.ref(self)

    # ---------- قراءة الملف ----------
    def _quarantine(self, why: str) -> None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = self.path.with_name(f"{self.path.name}.corrupt.{ts}")
        n = 0
        while dest.exists() and n < 100:
            n += 1
            dest = self.path.with_name(f"{self.path.name}.corrupt.{ts}.{n}")
        try:
            os.replace(str(self.path), str(dest))
            self.quarantined_path = dest
        except OSError:
            self.quarantined_path = None
        self.reset_note = ("سجل الاستهلاك اليومي كان تالفاً فأُعيد ضبطه وبدأ العدّ من جديد لهذا اليوم، لذلك قد يتجاوز إنفاقك السقف قليلاً."
                           + (f" (حُفظت النسخة التالفة باسم {dest.name})" if self.quarantined_path else ""))

    def _read_raw(self) -> Optional[dict]:
        """dict الأيام، أو {} لملف غير موجود/تالف (بعد العزل)، أو None لخطأ قراءة عابر مستمر (يُملأ self.warning)."""
        if not self.path.exists():
            return {}
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                text = self.path.read_text(encoding="utf-8-sig")
            except OSError as exc:       # PermissionError وغيره: قفل لحظي
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.15)
                continue
            except UnicodeDecodeError:
                self._quarantine("ترميز غير صالح")
                return {}
            try:
                raw = json.loads(text)
            except ValueError:
                self._quarantine("JSON غير مفهوم أو فارغ")
                return {}
            if not isinstance(raw, dict):
                self._quarantine("بنية غير متوقعة")
                return {}
            self.warning = ""
            return raw
        self.warning = "تعذّرت قراءة سجل الاستهلاك مؤقتاً فلم يُطبَّق سقف الإنفاق بدقة هذه المرة."
        return None

    @staticmethod
    def _clean(raw: dict) -> Dict[str, float]:
        """الأيام الصالحة فقط (مفتاح YYYY-MM-DD). قيمة غير عددية/سالبة/NaN = 0؛ مستحيلة الضخامة = قفل ذلك اليوم."""
        out: Dict[str, float] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not _DAY_RE.match(k):
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                f = 0.0
            if math.isnan(f) or f < 0:
                f = 0.0
            elif not math.isfinite(f) or f > _ABSURD_USD:
                f = _LOCKED_USD
            out[k] = f
        return out

    # ---------- منطق اليوم والساعة الخاطئة ----------
    # لا نطوي ولا ندمج خانات الأيام أبداً (الطيّ المدمّر كان يخسر الإنفاق عند تصحيح الساعة). نحتفظ بـ high_water_day رتيب (لا يتراجع)
    # وبعلم clock_regress: إن كانت الساعة خلف high_water_day بأكثر من يوم، يُنسب الإنفاق الجديد إلى خانة high_water_day (ويُنسخ لخانة اليوم الحالي)
    # ويُحتسب spent_today = أكبر خانتين (اليوم الحالي، high_water) طوال REGRESS_WINDOW_DAYS من لحظة اكتشاف التراجع، ثم يُعاد تثبيت المرجع على اليوم الحالي.
    @staticmethod
    def _d(key: str) -> Optional[datetime.date]:
        try:
            return datetime.date.fromisoformat(key)
        except ValueError:
            return None

    def _resolve(self, raw: dict) -> dict:
        days = {k: v for k, v in self._clean(raw).items() if self._d(k) is not None}
        meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
        T = self._today()
        tk = T.isoformat()
        stored_hw = self._d(meta.get("high_water_day")) if isinstance(meta.get("high_water_day"), str) else None
        stored_rd = self._d(meta.get("regress_day")) if isinstance(meta.get("regress_day"), str) else None
        cands = [d for d in (stored_hw, *(self._d(k) for k in days)) if d is not None]
        hw = max(cands) if cands else T
        R = {"days": days, "T": T, "dirty": False, "rebase": False, "regress": False, "protect": [tk],
             "add_keys": [tk], "regress_day": None}
        if T >= hw:                                   # الوضع الطبيعي (أو يوم جديد): الخانة الحقيقية لليوم
            R["hw"], R["read"] = T, days.get(tk, 0.0)
            R["dirty"] = bool(meta.get("clock_regress") or stored_rd)
            return R
        lag = (hw - T).days
        hk = hw.isoformat()
        if lag <= 1:                                  # فرق منطقي (منتصف الليل/منطقة زمنية/DST): نبقى على خانة الأحدث
            R.update(hw=hw, read=max(days.get(tk, 0.0), days.get(hk, 0.0)), add_keys=[hk], protect=[tk, hk],
                     regress=bool(meta.get("clock_regress")), regress_day=stored_rd)
            return R
        rd = stored_rd or T                           # تراجع حقيقي: المرجع أول يوم لوحظ فيه
        if T > rd + datetime.timedelta(days=REGRESS_WINDOW_DAYS):
            R.update(hw=T, read=days.get(tk, 0.0), rebase=True, dirty=True)   # انتهت النافذة: نعيد تثبيت المرجع على اليوم الحقيقي
            return R
        R.update(hw=hw, read=max(days.get(tk, 0.0), days.get(hk, 0.0)), add_keys=[hk, tk], protect=[tk, hk],
                 regress=True, regress_day=rd, dirty=(stored_rd is None or not meta.get("clock_regress")))
        return R

    def _meta_of(self, R: dict) -> dict:
        return {"high_water_day": R["hw"].isoformat(), "clock_regress": bool(R["regress"]),
                "regress_day": R["regress_day"].isoformat() if R["regress_day"] else None}

    def _commit(self, R: dict, usd: float = 0.0) -> None:
        days = dict(R["days"])
        if R["rebase"]:                               # خانات بعد «غداً» من ساعة خاطئة منتهية النافذة تُهمَل (لا تُدمج في غيرها)
            limit = (R["T"] + datetime.timedelta(days=1)).isoformat()
            days = {k: v for k, v in days.items() if k <= limit}
        if usd > 0:
            for k in R["add_keys"]:
                days[k] = days.get(k, 0.0) + usd
        keep = set(sorted(days)[-KEEP_DAYS:]) | {k for k in R["protect"] if k in days}
        data: dict = {k: days[k] for k in sorted(keep) if math.isfinite(days[k])}
        data["_meta"] = self._meta_of(R)
        try:
            _atomic_write(self.path, json.dumps(data))
        except OSError as exc:
            self.warning = "تعذّرت كتابة سجل الاستهلاك؛ قد لا تُحتسب هذه الكلفة."

    def _note_clock(self, R: dict) -> None:
        self.clock_regress = bool(R["regress"])
        self.clock_note = ("ساعة الجهاز تبدو متأخرة عن آخر تاريخ مسجّل؛ صحّح تاريخ الجهاز. يُحتسب الأعلى من إنفاق اليومين احتياطاً لبضعة أيام."
                           if R["regress"] else "")

    def spent_today(self) -> float:
        with self._lock:
            raw = self._read_raw()
            if raw is None:
                return 0.0          # قراءة عابرة فاشلة: تحذير بدل «بلغت السقف»
            R = self._resolve(raw)
            if R["dirty"]:          # اكتشاف تراجع/انتهاء نافذة يُثبَّت في الملف حتى لا ينتظر إضافة قد يمنعها السقف نفسه
                with _FileLock(self._flock_path, timeout=2.0):
                    raw2 = self._read_raw()
                    if raw2 is not None:
                        R = self._resolve(raw2)
                        if R["dirty"]:
                            self._commit(R)
            self._note_clock(R)
            return R["read"]

    def add(self, usd: float) -> None:
        if not (isinstance(usd, (int, float)) and math.isfinite(usd) and usd > 0):
            return
        with self._lock, _FileLock(self._flock_path):
            raw = self._read_raw()   # إعادة القراءة داخل القفل: عملية أخرى قد تكون كتبت
            if raw is None:
                return               # غير قابل للقراءة مؤقتاً: لا نكتب فوقه
            R = self._resolve(raw)
            self._note_clock(R)
            self._commit(R, float(usd))

    # ---------- الحجوزات المعلّقة (تبقى بعد انهيار) ----------
    def _pending_read(self) -> Dict[str, dict]:
        try:
            raw = json.loads(self.pending_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        now, out = time.time(), {}
        for k, v in raw.items():
            if isinstance(v, dict) and isinstance(v.get("ts"), (int, float)) and isinstance(v.get("usd"), (int, float)):
                if 0 <= now - v["ts"] < PENDING_TTL_SEC and math.isfinite(v["usd"]) and v["usd"] > 0:
                    out[str(k)] = v
        return out

    def _pending_write(self, entries: Dict[str, dict]) -> None:
        try:
            if entries:
                _atomic_write(self.pending_path, json.dumps(entries))
            elif self.pending_path.exists():
                self.pending_path.unlink()
        except OSError:
            pass

    def _foreign_pending_usd(self) -> float:
        """حجوزات سجّلتها عملية أخرى أو جلسة سابقة انهارت (ليست حجوزات هذا العدّاد الحيّة)."""
        mine = f"{self._uid}:"
        return float(sum(v["usd"] for k, v in self._pending_read().items() if not k.startswith(mine)))

    # ---------- حجز الكلفة للنداءات الجارية ----------
    def inflight_usd(self) -> float:
        with self._ilock:
            local = float(sum(self._inflight.values()))
        return local + self._foreign_pending_usd()

    def reserve(self, usd: float) -> Optional[int]:
        """يحجز كلفة نداء مدفوع (حدّها الأقصى المقدَّر) من السقف اليومي قبل إرساله؛ يعيد رمز الحجز أو None إن لم يتّسع السقف.
        بدون الحجز كان الإيقاف ثم إعادة الإرسال يطلق نداءات مدفوعة متوازية كلها تمرّ فحص السقف قبل أن تُسجَّل كلفة أي منها."""
        usd = float(usd) if isinstance(usd, (int, float)) and math.isfinite(usd) and usd > 0 else 0.0
        spent = self.spent_today()
        with self._ilock, _FileLock(self._flock_path, timeout=2.0):
            pend = self._pending_read()
            mine = f"{self._uid}:"
            foreign = sum(v["usd"] for k, v in pend.items() if not k.startswith(mine))
            if spent + sum(self._inflight.values()) + foreign + usd > self.cap:
                return None
            self._next_token += 1
            token = self._next_token
            self._inflight[token] = usd
            if usd > 0:
                pend[f"{mine}{token}"] = {"pid": os.getpid(), "ts": time.time(), "usd": usd}
                self._pending_write(pend)
            return token

    def release(self, token: Optional[int]) -> None:
        """يفكّ الحجز بعد انتهاء النداء (الكلفة الفعلية تُسجَّل عبر add من المتحكم)."""
        if token is None:
            return
        with self._ilock, _FileLock(self._flock_path, timeout=2.0):
            usd = self._inflight.pop(token, None)
            if usd:
                pend = self._pending_read()
                pend.pop(f"{self._uid}:{token}", None)
                self._pending_write(pend)

    def exceeded(self) -> bool:
        return self.spent_today() + self.inflight_usd() >= self.cap

    def message(self) -> str:
        base = (f"وصلت إلى سقف الإنفاق اليومي على النموذج الذكي ({self.spent_today():.2f} من {self.cap:.2f} دولار). "
                f"ارفع السقف اليومي من «الإعدادات» (أو بمتغير البيئة COS_MAX_USD_PER_DAY)، أو اختر «الوضع المحلي» المجاني.")
        return base + (f" ({self.reset_note})" if self.reset_note else "")

    def reserve_denied_message(self, usd: float) -> str:
        """سبب رفض الحجز: إن كان السقف مستنفداً (مع الجارية) فالرسالة العامة، وإلا فالكلفة المقدَّرة لهذا النداء لا تتّسع في المتبقي."""
        if self.exceeded():
            return self.message()
        left = max(0.0, self.cap - self.spent_today() - self.inflight_usd())
        return (f"هذا الطلب قد يكلّف أكثر من المتبقي من سقفك اليومي (المتبقي {left:.2f} دولار). "
                f"انتظر انتهاء الطلبات الجارية أو ارفع السقف اليومي من «الإعدادات» (أو بمتغير البيئة COS_MAX_USD_PER_DAY).")

    def notices(self) -> list:
        """رسائل ظاهرة للمستخدم عن حالة السجل (إعادة ضبط/قراءة عابرة فاشلة)؛ فارغة عادةً."""
        return [m for m in (self.reset_note, self.warning, self.clock_note) if m]
