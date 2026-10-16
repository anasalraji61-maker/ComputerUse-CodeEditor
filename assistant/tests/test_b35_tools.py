"""B35: سلامة usage.json بين العمليات والتلف، حجوزات معلّقة تبقى بعد انهيار، تعافي ledger.db التالف، winget لا-تحديث، ترميز الكونسول."""
import datetime
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from unittest import mock
    from assistant.usage import UsageMeter, PENDING_TTL_SEC
    from assistant.ledger import SQLiteLedger
    from assistant.planner import PlanStep
    from assistant.verifier import Verifier
    from assistant.tools import shell as shmod
    from assistant.tools.shell import decode_output

    TODAY = datetime.date(2026, 10, 14)

    # ---- 1) عمليتان تكتبان في usage.json معاً: لا ضياع ولا أخطاء ----
    with tempfile.TemporaryDirectory() as tmp:
        u = pathlib.Path(tmp) / "usage.json"
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from assistant.usage import UsageMeter\n"
                "m = UsageMeter(sys.argv[1], daily_cap_usd=100)\n"
                "[m.add(0.01) for _ in range(40)]\n") % str(ROOT)
        procs = [subprocess.Popen([sys.executable, "-c", code, str(u)]) for _ in range(3)]
        assert all(p.wait(timeout=120) == 0 for p in procs)
        got = UsageMeter(u, daily_cap_usd=100).spent_today()
        assert abs(got - 1.20) < 1e-6, got
        assert not list(pathlib.Path(tmp).glob("*.tmp")), "ملفات مؤقتة متروكة"

    # ---- 2) ساعة تعود للوراء لا تصفّر السقف ولا تمحو الأيام الأخرى ----
    with tempfile.TemporaryDirectory() as tmp:
        day = [TODAY]
        m = UsageMeter(pathlib.Path(tmp) / "u.json", daily_cap_usd=1.0, today=lambda: day[0])
        m.add(0.9)
        day[0] = TODAY - datetime.timedelta(days=1)
        assert not m.exceeded() and abs(m.spent_today() - 0.9) < 1e-9   # اليوم المعتمد = الأحدث المسجّل
        m.add(0.2)
        assert m.exceeded()
        day[0] = TODAY + datetime.timedelta(days=1)                       # الأمام: يوم جديد
        assert not m.exceeded() and m.spent_today() == 0.0
        data = json.loads((pathlib.Path(tmp) / "u.json").read_text(encoding="utf-8"))
        assert TODAY.isoformat() in data, data                              # الأيام السابقة محفوظة
        for i in range(10):
            day[0] = TODAY + datetime.timedelta(days=2 + i)
            m.add(0.01)
        assert len([k for k in json.loads((pathlib.Path(tmp) / "u.json").read_text(encoding="utf-8")) if k[:1].isdigit()]) <= 7

    # ---- 1ب) خانة مستقبلية قديمة (ملف قبل التصميم الجديد/ساعة خاطئة): تُحتسب احتياطاً ثم تنتهي نافذتها دون دمج مدمّر ----
    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "usage.json"
        D = datetime.date
        UsageMeter(f, daily_cap_usd=1.0, today=lambda: D(2030, 1, 1)).add(0.6)
        m = UsageMeter(f, daily_cap_usd=1.0, today=lambda: D(2026, 10, 14))
        assert abs(m.spent_today() - 0.6) < 1e-9          # الأعلى من (اليوم، أحدث يوم) خلال النافذة
        m.add(0.3)
        assert abs(m.spent_today() - 0.9) < 1e-9 and m.clock_regress
        assert abs(json.loads(f.read_text(encoding="utf-8"))["2030-01-01"] - 0.9) < 1e-9      # لا طيّ: الخانة محفوظة كما هي
        m = UsageMeter(f, daily_cap_usd=1.0, today=lambda: D(2026, 10, 20))   # خارج نافذة الـ3 أيام: مرجع جديد
        assert m.spent_today() == 0.0 and not m.exceeded()
        m.add(0.3)
        assert abs(m.spent_today() - 0.3) < 1e-9
        assert "2030-01-01" not in json.loads(f.read_text(encoding="utf-8"))       # خانة المستقبل المنتهية تُهمل (لا تُدمج)
        # غداً (حدود منتصف الليل/المناطق الزمنية) يبقى كأحدث يوم دون علم تراجع
        f2 = pathlib.Path(tmp) / "u2.json"
        UsageMeter(f2, daily_cap_usd=1.0, today=lambda: D(2026, 10, 15)).add(0.2)
        m = UsageMeter(f2, daily_cap_usd=1.0, today=lambda: D(2026, 10, 14))
        assert abs(m.spent_today() - 0.2) < 1e-9 and not m.clock_regress
        # الحماية من الساعة الراجعة للوراء ما زالت تعمل
        f3 = pathlib.Path(tmp) / "u3.json"
        UsageMeter(f3, daily_cap_usd=1.0, today=lambda: D(2026, 10, 14)).add(0.9)
        assert UsageMeter(f3, daily_cap_usd=1.0, today=lambda: D(2026, 10, 13)).spent_today() == 0.9

    # ---- 3) تالف/فارغ/BOM: لا يقفل للأبد ----
    cases = {
        "truncated": b'{"2026-10-1', "binary": os.urandom(50), "list": b"[1,2]", "empty": b"",
        "utf16": json.dumps({TODAY.isoformat(): 0.9}).encode("utf-16"),
    }
    for name, raw in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            f = pathlib.Path(tmp) / "usage.json"
            f.write_bytes(raw)
            m = UsageMeter(f, daily_cap_usd=1.0, today=lambda: TODAY)
            assert not m.exceeded(), name
            assert m.reset_note and "أُعيد ضبطه" in m.reset_note and "أُعيد ضبطه" in m.message(), name
            assert list(pathlib.Path(tmp).glob("usage.json.corrupt.*")), name
            m.add(0.3)
            assert abs(m.spent_today() - 0.3) < 1e-9 and m.notices()
    with tempfile.TemporaryDirectory() as tmp:   # BOM (UTF-8) يُقرأ كما هو
        f = pathlib.Path(tmp) / "usage.json"
        f.write_bytes(b"\xef\xbb\xbf" + json.dumps({TODAY.isoformat(): 0.4}).encode())
        m = UsageMeter(f, daily_cap_usd=1.0, today=lambda: TODAY)
        assert abs(m.spent_today() - 0.4) < 1e-9 and not m.reset_note
        f.write_bytes(json.dumps({TODAY.isoformat(): float("nan")}).replace("NaN", "NaN").encode())
        assert m.spent_today() == 0.0
        f.write_bytes(json.dumps({TODAY.isoformat(): 1e30}).encode())    # عبث مستحيل: يقفل ذلك اليوم فقط
        assert m.exceeded()

    # ---- 3ب) خطأ قراءة عابر مستمر: السقف لا يُعلن مستنفداً، ومعه تحذير ----
    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "usage.json"
        m = UsageMeter(f, daily_cap_usd=1.0, today=lambda: TODAY)
        m.add(0.5)
        real = pathlib.Path.read_text

        def locked(self, *a, **k):
            if self.name == "usage.json":
                raise PermissionError("locked")
            return real(self, *a, **k)
        with mock.patch.object(pathlib.Path, "read_text", locked), mock.patch("time.sleep", lambda s: None):
            assert not m.exceeded() and m.warning
            m.add(0.2)                                                      # لا يكتب فوق ملف لا يقرؤه
        assert abs(m.spent_today() - 0.5) < 1e-9 and not m.warning

    # ---- 5) حجز معلّق يبقى بعد انهيار ----
    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "usage.json"
        a = UsageMeter(f, daily_cap_usd=1.0, today=lambda: TODAY)
        tok = a.reserve(0.6)
        assert tok is not None and a.pending_path.exists()
        data = json.loads(a.pending_path.read_text(encoding="utf-8"))
        assert list(data.values())[0]["pid"] == os.getpid()
        b = UsageMeter(f, daily_cap_usd=1.0, today=lambda: TODAY)           # «إعادة تشغيل» بعد انهيار a (لا release)
        assert abs(b.inflight_usd() - 0.6) < 1e-9
        assert b.reserve(0.6) is None and b.reserve(0.3) is not None
        a.release(tok)                                                      # فكّ الحجز الفعلي يحذف المدخل
        assert b.inflight_usd() < 0.31
        # انتهاء بعد 10 دقائق
        ent = json.loads(b.pending_path.read_text(encoding="utf-8"))
        for v in ent.values():
            v["ts"] = time.time() - PENDING_TTL_SEC - 5
        b.pending_path.write_text(json.dumps(ent), encoding="utf-8")
        c = UsageMeter(f, daily_cap_usd=1.0, today=lambda: TODAY)
        assert c.inflight_usd() == 0.0
        # تلف ملف الحجوزات لا يكسر شيئاً
        c.pending_path.write_text("{ not json", encoding="utf-8")
        assert c.inflight_usd() == 0.0 and c.reserve(0.1) is not None

    # ---- 2) ledger: تلف/فراغ/تعافٍ ----
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        good_db = base / "good.db"
        l0 = SQLiteLedger(good_db)
        l0.record_task_start("t", "x", 1)
        l0.record_task_success("t", 1)
        assert not l0.recovered_from_corruption and l0.recovery_message == ""
        raw = good_db.read_bytes()
        for name, blob in {"garbage": b"\x00\xff" * 4000, "half": raw[: len(raw) // 2], "text": b"hello\n" * 50}.items():
            p = base / f"{name}.db"
            p.write_bytes(blob)
            os.utime(p, (time.time() - 3600, time.time() - 3600))   # ملف قديم (ليس حديث الإنشاء من مُشغِّل متزامن)
            led = SQLiteLedger(p, initial_balance=77, spending_limit=100)
            assert led.recovered_from_corruption and "تالفاً" in led.recovery_message, name
            assert led.get_balance() == 77 and led.quarantined_path and led.quarantined_path.exists(), name
            led.record_task_start("a", "x", 1)      # قابل للاستعمال فعلاً
        p = base / "zero.db"
        p.write_bytes(b"")
        os.utime(p, (time.time() - 3600, time.time() - 3600))
        led = SQLiteLedger(p, initial_balance=50, spending_limit=100)
        assert led.recovered_from_corruption and "فارغاً" in led.recovery_message and led.get_balance() == 50
        # صفري البايت وحديث الإنشاء (مُشغِّل متزامن يهيّئه الآن): لا تحذير تلف
        p = base / "fresh0.db"
        p.write_bytes(b"")
        led = SQLiteLedger(p, initial_balance=50, spending_limit=100)
        assert not led.recovered_from_corruption and led.recovery_message == "" and led.get_balance() == 50
        # ملف تالف حديث جداً يُمهَل ثم يُعزل إن بقي تالفاً
        p = base / "fresh_bad.db"
        p.write_bytes(b"garbage" * 100)
        with mock.patch("time.sleep", lambda s: None):
            led = SQLiteLedger(p, initial_balance=60, spending_limit=100)
        assert led.recovered_from_corruption and led.get_balance() == 60
        # قفل (ليس تلفاً) لا يُعزل
        p = base / "locked.db"
        SQLiteLedger(p)
        with mock.patch.object(SQLiteLedger, "_init_db", side_effect=sqlite3.OperationalError("database is locked")):
            try:
                SQLiteLedger(p)
                raise AssertionError
            except sqlite3.OperationalError:
                pass
        assert p.exists() and not list(base.glob("locked.db.corrupt.*"))
        # مهام STARTED العالقة تُغلق (أقدم من ساعة فقط)
        p = base / "stuck.db"
        led = SQLiteLedger(p)
        led.record_task_start("old", "x", 1)
        led.record_task_start("new", "x", 1)
        c = sqlite3.connect(str(p))
        c.execute("UPDATE transactions SET created_at = datetime('now','-3 hours') WHERE task_id='old'")
        c.commit()
        c.close()
        led2 = SQLiteLedger(p)
        assert led2.reconciled_stuck == 1 and led2.get_balance() == 100 - 0
        c = sqlite3.connect(str(p))
        rows = dict(c.execute("SELECT task_id, status FROM transactions WHERE task_id IN ('old','new')").fetchall())
        c.close()
        assert rows == {"old": "FAILED", "new": "STARTED"}, rows

    # ---- 3) winget: «لا تحديث ينطبق» قبل رسالة الفشل العامة ----
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve()
        v = Verifier(root)
        step = PlanStep(1, "winget_upgrade", "Git.Git")
        before = {"step_id": 1, "action": "winget_upgrade"}
        for code in (0x8A15002B, -1978335189, 0x8A150061):
            ok, ev, msg = v.verify_step(step, {"step_id": 1, "success": False, "exit_code": code, "stdout": "No applicable update  found.", "stderr": ""}, before)
            assert not ok and "لا يوجد تحديث متاح" in msg and "عدم نجاح" not in msg, (code, msg)
            assert " " not in msg and "ناتج winget" in msg
        ok, ev, msg = v.verify_step(step, {"step_id": 1, "success": False, "exit_code": 1, "stdout": "No installed package found matching input criteria.", "stderr": ""}, before)
        assert not ok and "غير مثبّتة" in msg and "لا يوجد تحديث متاح" not in msg
        long_out = "x" * 500 + " END-OF-OUTPUT"
        ok, ev, msg = v.verify_step(step, {"step_id": 1, "success": False, "exit_code": 0x80070005, "stdout": long_out, "stderr": ""}, before)
        assert not ok and "0x80070005" in msg and "END-OF-OUTPUT" in msg and len(msg) < 400, msg
        assert "لا يوجد تحديث متاح" not in msg

    # ---- 4) ترميز ناتج الكونسول ----
    arabic = "المستخدم"
    assert decode_output(arabic.encode("utf-8")) == arabic            # UTF-8 صارم أولاً
    assert decode_output(arabic) == arabic and decode_output(None) == ""
    with mock.patch.object(shmod, "_console_encodings", lambda: ["cp720", "cp1256"]):
        assert decode_output(arabic.encode("cp720")) == arabic         # OEM العربية
    with mock.patch.object(shmod, "_console_encodings", lambda: ["cp1256"]):
        assert decode_output(arabic.encode("cp1256")) == arabic
    with mock.patch.object(shmod, "_console_encodings", lambda: ["no-such-codec", "cp1252"]):
        assert decode_output(b"caf\xe9") == "café"                       # ترميز مجهول يُتجاوز
    with mock.patch.object(shmod, "_console_encodings", lambda: []):
        assert "�" in decode_output(b"\xff\xfe\xfd")                # آخر الحلول: استبدال لا استثناء
    assert isinstance(shmod._console_encodings(), list)
    # ShellTool.run يمرّر البايتات عبر decode_output
    fake = mock.Mock(returncode=0, stdout=arabic.encode("cp1256"), stderr=b"")
    with mock.patch.object(shmod, "_console_encodings", lambda: ["cp1256"]), mock.patch.object(shmod.subprocess, "run", return_value=fake):
        tool = shmod.ShellTool(custom_allowed_patterns=[["whoami"]])
        with mock.patch.object(shmod, "resolve_exe", return_value="/bin/whoami"):
            assert tool.run(["whoami"]) == (0, arabic, "")

    print("PASS: test_b35_tools completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b35_tools", file=sys.stderr)
    sys.exit(1)
