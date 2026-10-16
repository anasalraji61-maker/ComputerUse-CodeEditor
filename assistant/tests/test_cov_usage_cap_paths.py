"""تغطية عدّاد الإنفاق الحقيقي: الحجز يمنع تجاوز السقف ويفكّ بعد الانتهاء، حجز عملية منهارة يُحتسب ثم ينقضي، قيم مستحيلة/تالفة تقفل اليوم أو تُعزل،
فشل الكتابة (PermissionError) لا يُسقط التطبيق ويُظهر تحذيراً، وقراءة عابرة فاشلة تُظهر تحذيراً."""
import datetime
import json
import os
import pathlib
import sys
import tempfile
import time
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.usage import UsageMeter, _atomic_write, PENDING_TTL_SEC

    D = datetime.date(2026, 3, 10)
    today = lambda: D   # noqa: E731

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()

        # 1. الحجز: لا يتّسع -> None؛ يتّسع -> رمز؛ الحجوزات المتزامنة تُجمع؛ release يفك
        m = UsageMeter(base / "u1.json", daily_cap_usd=1.0, today=today)
        t1 = m.reserve(0.6)
        assert t1 is not None and abs(m.inflight_usd() - 0.6) < 1e-9
        assert m.reserve(0.6) is None                     # 0.6 + 0.6 > 1.0
        assert "أكثر من المتبقي" in m.reserve_denied_message(0.6)
        t2 = m.reserve(0.3)
        assert t2 is not None and t2 != t1
        m.release(t1)
        m.release(t1)                                      # فك مزدوج لا يضرّ
        m.release(None)
        assert abs(m.inflight_usd() - 0.3) < 1e-9
        assert m.reserve(0.6) is not None
        m.add(0.5)
        assert abs(m.spent_today() - 0.5) < 1e-9 and m.exceeded()      # 0.5 منفق + حجوزات جارية 0.9 >= السقف
        # قيم غير صالحة لا تُحتسب
        before = m.spent_today()
        for bad in (float("nan"), float("inf"), -1, 0, "5", None):
            m.add(bad)
        assert m.spent_today() == before

        # 2. حجز عملية منهارة (pid آخر) يُحتسب طوال المهلة ثم ينقضي؛ تالف/سالب/NaN يُتجاهل
        m2 = UsageMeter(base / "u2.json", daily_cap_usd=1.0, today=today)
        pend = {"999-aaaa:1": {"pid": 999, "ts": time.time(), "usd": 0.7},
                "999-aaaa:2": {"pid": 999, "ts": time.time() - PENDING_TTL_SEC - 5, "usd": 0.9},     # منقضٍ
                "999-aaaa:3": {"pid": 999, "ts": time.time() + 9999, "usd": 0.9},                      # مستقبلي
                "999-aaaa:4": {"pid": 999, "ts": time.time(), "usd": -5},
                "999-aaaa:5": {"pid": 999, "ts": time.time(), "usd": "x"},
                "bad": "not a dict"}
        m2.pending_path.write_text(json.dumps(pend), encoding="utf-8")
        assert abs(m2.inflight_usd() - 0.7) < 1e-9, m2.inflight_usd()
        assert m2.reserve(0.5) is None                     # 0.7 + 0.5 > 1.0
        assert m2.reserve(0.2) is not None
        m2.pending_path.write_text("[1,2]", encoding="utf-8")        # بنية خاطئة = لا حجوزات (لا انهيار)
        assert m2.inflight_usd() >= 0.0

        # 3. قيم يومية تالفة: مستحيلة = قفل ذلك اليوم؛ سالبة/نصية = صفر؛ مفاتيح غريبة تُهمل
        p3 = base / "u3.json"
        p3.write_text(json.dumps({D.isoformat(): 1e15, "2026-03-09": -4, "2026-03-08": "abc", "not-a-day": 5}), encoding="utf-8")
        m3 = UsageMeter(p3, daily_cap_usd=1.0, today=today)
        assert m3.spent_today() > 1e9 and m3.exceeded() and "وصلت إلى سقف" in m3.message()
        m3.add(0.1)
        assert json.loads(p3.read_text(encoding="utf-8")).get("2026-03-09") in (None, 0.0)

        # 4. ملف تالف: يُعزل بنسخة محفوظة ويبدأ العدّ من جديد مع تنبيه؛ وتكرار الاسم يزيد لاحقة
        p4 = base / "u4.json"
        p4.write_text("{{{ not json", encoding="utf-8")
        m4 = UsageMeter(p4, daily_cap_usd=1.0, today=today)
        with mock.patch("time.strftime", return_value="20260310-000000"):
            (base / "u4.json.corrupt.20260310-000000").write_text("old", encoding="utf-8")
            assert m4.spent_today() == 0.0
        assert m4.quarantined_path is not None and m4.quarantined_path.name.endswith(".1"), m4.quarantined_path
        assert m4.reset_note and m4.notices()
        # ملف بترميز غير صالح / بنية غير dict
        for content in (b"\xff\xfe\x00bad", b"[1,2,3]", b""):
            p4.write_bytes(content)
            m4b = UsageMeter(p4, daily_cap_usd=1.0, today=today)
            assert m4b.spent_today() == 0.0 and m4b.reset_note

        # 5. فشل كتابة السجل (قفل دائم) لا يُسقط الإضافة: تحذير ظاهر ولا استثناء
        p5 = base / "u5.json"
        m5 = UsageMeter(p5, daily_cap_usd=1.0, today=today)
        real_replace = os.replace

        def deny(src, dst):
            if str(dst) == str(p5):
                raise PermissionError("locked")
            return real_replace(src, dst)
        with mock.patch("os.replace", deny), mock.patch("time.sleep", lambda s: None):
            m5.add(0.2)
        assert "تعذّرت كتابة" in m5.warning and m5.notices()
        assert not list(base.glob("u5.json.*.tmp")), list(base.glob("u5.json.*.tmp"))      # لا مؤقتات يتيمة
        # _atomic_write نفسها ترفع بعد استنفاد المحاولات ولا تترك مؤقتاً
        p6 = base / "u6.json"
        with mock.patch("os.replace", side_effect=PermissionError("locked")), mock.patch("time.sleep", lambda s: None):
            try:
                _atomic_write(p6, "{}")
                raise AssertionError("كان يجب أن يفشل")
            except PermissionError:
                pass
        assert not list(base.glob("u6.json*"))

        # 6. قراءة عابرة فاشلة (قفل مضاد فيروسات): تحذير ظاهر، ولا كتابة فوق الملف
        p7 = base / "u7.json"
        p7.write_text(json.dumps({D.isoformat(): 0.4}), encoding="utf-8")
        m7 = UsageMeter(p7, daily_cap_usd=1.0, today=today)
        with mock.patch.object(pathlib.Path, "read_text", side_effect=PermissionError("av")), mock.patch("time.sleep", lambda s: None):
            assert m7.spent_today() == 0.0
            assert m7.warning and "مؤقتاً" in m7.warning
            m7.add(0.3)                                  # لا يكتب فوق ملف غير مقروء
        assert abs(json.loads(p7.read_text(encoding="utf-8"))[D.isoformat()] - 0.4) < 1e-9

        # 7. سقف غير صالح من البيئة/المعامل يعود للافتراضي (لا سقف لانهائي ولا سالب)
        for badcap in (float("nan"), float("inf"), -5, "x"):
            assert UsageMeter(base / "u8.json", daily_cap_usd=badcap, today=today).cap == 1.0, badcap
        with mock.patch.dict(os.environ, {"COS_MAX_USD_PER_DAY": "garbage"}):
            assert UsageMeter(base / "u9.json", today=today).cap == 1.0

    print("PASS: test_cov_usage_cap_paths completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_usage_cap_paths", file=sys.stderr)
    sys.exit(1)
