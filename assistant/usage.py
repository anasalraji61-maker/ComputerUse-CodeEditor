"""عدّاد استهلاك النماذج الحقيقية بالدولار (يومي، محفوظ على القرص) وسقف يمنع الإنفاق المفتوح."""
import datetime
import json
import math
import os
import threading
from pathlib import Path
from typing import Callable, Optional

DEFAULT_DAILY_CAP_USD = 1.0


class UsageMeter:
    def __init__(self, path, daily_cap_usd: Optional[float] = None,
                 today: Optional[Callable[[], datetime.date]] = None):
        self.path = Path(path)
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

    def _load(self) -> dict:
        """ملف غير موجود = لا إنفاق. ملف موجود لكنه تالف/مقفل = نفترض أن السقف استُهلك (الفشل يغلق ولا يفتح)."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {self._today().isoformat(): self.cap}

    def spent_today(self) -> float:
        with self._lock:
            v = self._load().get(self._today().isoformat(), 0.0)
            return float(v) if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 else 0.0

    def add(self, usd: float) -> None:
        if not (isinstance(usd, (int, float)) and math.isfinite(usd) and usd > 0):
            return
        with self._lock:
            key = self._today().isoformat()
            data = None
            if self.path.exists():
                for attempt in range(3):  # قفل لحظي: نعيد القراءة بدل أن نعدّه استهلاكاً كاملاً
                    try:
                        raw = json.loads(self.path.read_text(encoding="utf-8"))
                        if isinstance(raw, dict):
                            data = raw
                            break
                    except Exception:
                        pass
                    if attempt < 2:
                        import time as _t
                        _t.sleep(0.15)
                if data is None:
                    return  # غير قابل للقراءة: لا نكتب فوقه (exceeded() يبقى مغلقاً)
            else:
                data = {}
            data = {key: float(data.get(key, 0.0) or 0.0) + float(usd)}  # يُبقي اليوم الحالي فقط
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(str(tmp), str(self.path))

    def exceeded(self) -> bool:
        return self.spent_today() >= self.cap

    def message(self) -> str:
        return (f"بلغتَ سقف الإنفاق اليومي على النماذج الحقيقية ({self.spent_today():.2f}$ من {self.cap:.2f}$). "
                f"المخطط المحلي المجاني ما زال متاحاً بـ COS_PROVIDER=demo، أو ارفع السقف بمتغير COS_MAX_USD_PER_DAY.")
