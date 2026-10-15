"""اختبارات B25: تنبيه اقتطاع الخطة، رفض قراءة ملف سحابي كبير غير محمّل (OneDrive)."""
import json
import os
import pathlib
import sys
import tempfile
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.ledger import SQLiteLedger
    from assistant.models.rules import RuleProvider
    from assistant.tools import files as F
    from assistant.tools.files import FileManager, CloudFileNotLocalError, check_not_big_cloud_file
    from assistant.verifier import Verifier
    from assistant.ui.controller import AssistantController

    # 1. تنبيه الاقتطاع يصل مع الخطة
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"; ws.mkdir()
        for i in range(260):
            (ws / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
        c = AssistantController(RuleProvider(), ws, SQLiteLedger(base / "l.db", initial_balance=500, spending_limit=500))
        c.submit_request("رتب الملفات حسب النوع")
        plan = c.wait_for("plan")
        assert plan, c.drain_events()
        assert plan["note"] and "اقتُصرت" in plan["note"], plan.get("note")
        # خطة كاملة بلا تنبيه
        c.cancel_plan()
        (ws / "ws2").mkdir()
        small = base / "small"; small.mkdir()
        for i in range(5):
            (small / f"g{i}.txt").write_text("x", encoding="utf-8")
        c2 = AssistantController(RuleProvider(), small, SQLiteLedger(base / "l2.db", initial_balance=500, spending_limit=500))
        c2.submit_request("رتب الملفات حسب النوع")
        p2 = c2.wait_for("plan"); assert p2 and p2["note"] is None, p2

        # 2. ملف سحابي كبير غير محمّل: يُرفض بدل تنزيله بصمت
        big = ws / "f00.txt"
        class FakeStat:
            st_file_attributes = 0x400000
            st_size = 500 * 1024 * 1024
        class SmallStat(FakeStat):
            st_size = 1024
        try:
            check_not_big_cloud_file(big, _lstat=lambda p: FakeStat())
            raise AssertionError("لم يُرفض")
        except CloudFileNotLocalError as e:
            assert "الاحتفاظ دائماً" in str(e)
        check_not_big_cloud_file(big, _lstat=lambda p: SmallStat())   # الصغير يمر
        class Plain:
            st_file_attributes = 0
            st_size = 10**10
        check_not_big_cloud_file(big, _lstat=lambda p: Plain())        # كبير لكنه محلي عادي يمر
        # المدقق والنسخ يستعملان الفحص
        orig = os.lstat
        def fake_lstat(path, *a, **k):
            r = orig(path, *a, **k)
            if str(path).endswith("f01.txt"):
                class W:
                    def __getattr__(self, n): return getattr(r, n)
                    st_file_attributes = 0x400000
                    st_size = 500 * 1024 * 1024
                return W()
            return r
        with mock.patch("os.lstat", fake_lstat):
            try:
                Verifier(ws).calculate_hash(ws / "f01.txt"); raise AssertionError("المدقق نزّل الملف")
            except CloudFileNotLocalError:
                pass
            fm = FileManager(ws)
            try:
                fm.copy("f01.txt", "copy_of_f01.txt"); raise AssertionError("النسخ نزّل الملف")
            except CloudFileNotLocalError:
                pass
            assert not (ws / "copy_of_f01.txt").exists()

    print("PASS: test_b25 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b25", file=sys.stderr)
    sys.exit(1)
