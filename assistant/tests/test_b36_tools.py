"""B36: بيئة/إخفاء أسرار run_tests، مفتاح صريح فارغ، شحن تجريبي، رسائل HTTP، Retry-After، القياس، BudgetTracker، winget غير موجود."""
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import traceback
import urllib.error
import socket
import ssl

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from unittest import mock
    from assistant.models.http_base import (HttpProvider, ProviderError, ProviderConfigError, parse_retry_after,
                                            describe_network_error, default_transport)
    from assistant.models.anthropic_provider import AnthropicProvider
    from assistant.models.gemini_provider import GeminiProvider
    from assistant.models.factory import build_provider
    from assistant.ledger import SQLiteLedger
    from assistant.budget import BudgetTracker, BudgetExceededError
    from assistant.tools.winget import WingetTool, WINGET_NOT_FOUND_MSG, WINGET_NOT_FOUND_CODE
    from assistant.verifier import Verifier
    from assistant.planner import PlanStep
    import bench.run_bench as rb

    spec = importlib.util.spec_from_file_location("cos_run_tests", str(ROOT / "tools" / "run_tests.py"))
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)

    # ---- 1) run_tests: بيئة بيضاء + إخفاء الأسرار ----
    canary = "AI" + "zaCANARY_run_tests_leak_1234567890abcdef"   # مجزَّأ كي لا يلتقطه فحص الأسرار
    fake_env = {"GEMINI_API_KEY": canary, "ANTHROPIC_API_KEY": "sk" + "-ant-CANARYKEY1234567890", "GITHUB_TOKEN": "gh" + "p_abcdefghij1234567890",
                "MY_PASSWORD": "hunter2hunter2", "COS_PROVIDER": "auto", "COS_API_KEY": "cosCANARYsecret999", "PATH": "/usr/bin",
                "DISPLAY": ":99", "XAUTHORITY": "/tmp/xauth", "RANDOM_VAR": "x"}
    env = rt.child_env("/tmp/d", fake_env)
    assert not any(canary in v or v.startswith(("sk-", "ghp_", "hunter", "cosCANARY")) for v in env.values()), env
    assert not [k for k in env if any(w in k for w in ("KEY", "TOKEN", "PASSWORD"))], env
    assert env["COS_PROVIDER"] == "auto" and env["COS_TEST_MODE"] == "1" and env["COS_DATA_DIR"] == "/tmp/d"
    assert env["DISPLAY"] == ":99" and env["XAUTHORITY"] == "/tmp/xauth" and "RANDOM_VAR" not in env and env["PATH"] == "/usr/bin"
    text = (f"leak {canary} and " + "sk" + "-ant-abcdef123456 and " + "gh" + "p_abcdefghij1234567890ABCDEFGHIJ12 and Bearer abcdefghijklmnop.qrstuvwx and "
            + "AI" + "zaSyABCDEFGH12345678901234567890123 and hunter2hunter2 and short")
    red = rt.redact(text, fake_env)
    for bad in (canary, "abcdef123456", "abcdefghij1234567890ABCDEF", "abcdefghijklmnop.qrstuvwx", "SyABCDEFGH123456789", "hunter2hunter2"):
        assert bad not in red, (bad, red)
    assert "short" in red
    # لا إيجابيات كاذبة
    from assistant import redact as rd
    ok_texts = ["ok 3: disk-cleanup-plan", "task-ask-me-anything-please-now", "risk-assessment-for-all-files-done", "ask-1234567890123456789012",
                "Bearer token required", "/usr/local/lib/python3", "C:\\Users\\me\\my-sk-folder\\file.txt", "AKIAxyz", "xoxb-1", "github_pat_x",
                "gh" + "p_short", "AI" + "zaShort"]
    for t in ok_texts:
        assert rd.redact(t, {}) == t, (t, rd.redact(t, {}))
    # اسم يحوي KEY لا يعني سراً: قيمته لا تُحجب ولا تُفسد المسارات
    env2 = {"MYKEYBOARD_LAYOUT": "/usr/local", "KEYBOARD_X": "us-international-layout", "MONKEY_NAME": "curiousgeorge99",
            "SOME_TOKEN_PATH": "/etc/tokens/real", "COMPOSE_PROJECT_NAME": "my_project_name_long"}
    t2 = "path /usr/local/lib and us-international-layout and curiousgeorge99 and /etc/tokens/real"
    assert rd.redact(t2, env2) == t2
    # قيمة سرية الاسم لا تُحجب داخل مسار ملف ولا داخل كلمة أطول، وتُحجب مستقلة
    env3 = {"DEPLOY_TOKEN": "abc123def456ghi789", "BACKUP_PASSWORD": "/mnt/pw/file.txt", "MODE_SECRET": "production", "SHORT_TOKEN": "abc"}
    assert rd.redact("log at /var/abc123def456ghi789/out and xabc123def456ghi789y", env3) == "log at /var/abc123def456ghi789/out and xabc123def456ghi789y"
    assert rd.redact("tok=abc123def456ghi789 end", env3) == "tok=*** end"
    assert rd.redact("production abc /mnt/pw/file.txt", env3) == "production abc /mnt/pw/file.txt"
    assert rd.is_secret_name("OPENAI_API_KEY") and rd.is_secret_name("GITHUB_TOKEN") and rd.is_secret_name("x_secret") and rd.is_secret_name("STRIPE_KEY")
    assert not rd.is_secret_name("MYKEYBOARD_LAYOUT") and not rd.is_secret_name("MONKEY") and not rd.is_secret_name("TOKENIZER_PATH")
    assert len(rd.secret_patterns()) >= 8 and all(hasattr(p, "sub") for p in rd.secret_patterns())
    # تشغيل حقيقي لاختبار فرعي: يطبع مفتاحاً من بيئته الموروثة؛ لا يراه ولو طبعه لأُخفي
    with tempfile.TemporaryDirectory() as tmp:
        script = pathlib.Path(tmp) / "test_probe.py"
        script.write_text("import os\nprint('SEEN=' + os.environ.get('GEMINI_API_KEY', 'none'))\nprint('LEAK=' + 'AI' + 'zaCANARYinline12345678901234567890')\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": canary}):
            ok, out, _ = rt.run(script)
        assert ok and "SEEN=none" in out and canary not in out and "CANARYinline" not in out, out

    AMB = "sk" + "-ant-ambient-should-not-be-used"
    # ---- 2) مفتاح صريح فارغ لا يسقط إلى مفتاح البيئة ----
    with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": AMB, "GEMINI_API_KEY": "ambient-g"}):
        assert AnthropicProvider(api_key="")._api_key == ""
        assert AnthropicProvider(api_key=None)._api_key == AMB
        assert AnthropicProvider()._api_key == AMB
        try:
            build_provider({"COS_PROVIDER": "anthropic"})
            raise AssertionError("كان يجب رفض غياب المفتاح")
        except ProviderConfigError:
            pass
        try:
            build_provider({"COS_PROVIDER": "gemini"})
            raise AssertionError
        except ProviderConfigError:
            pass

    # ---- 3) الشحن التجريبي ----
    with tempfile.TemporaryDirectory() as tmp:
        led = SQLiteLedger(pathlib.Path(tmp) / "l.db", initial_balance=5, spending_limit=5)
        assert led.ensure_trial_balance() is True and led.get_balance() == 105 and led.spending_limit == 105
        assert led.ensure_trial_balance() is False and led.get_balance() == 105      # فوق الحد: لا شحن
        led.record_task_start("a", "x", 1)
        led.record_task_success("a", 90)
        assert led.get_balance() == 15 and led.ensure_trial_balance() is True and led.get_balance() == 115
        with mock.patch.dict(os.environ, {"COS_TRIAL_TOPUP": "0"}):
            led2 = SQLiteLedger(pathlib.Path(tmp) / "l2.db", initial_balance=1, spending_limit=1)
            assert led2.ensure_trial_balance() is False and led2.get_balance() == 1
        assert led.ensure_trial_balance(min_points=0) is False
        c = sqlite3.connect(str(pathlib.Path(tmp) / "l.db"))
        assert c.execute("SELECT COUNT(*) FROM transactions WHERE status='TOPUP' AND reason LIKE '%تجريبي%'").fetchone()[0] == 2
        c.close()

    # ---- 4) رسائل HTTP المميّزة ----
    KEY = "test-key-NOT-REAL-123456"

    def err_for(status, body=b"x"):
        p = AnthropicProvider(model="claude-haiku-5-5", api_key=KEY, transport=lambda *a: (status, body), max_retries=0, sleep=lambda s: None)
        try:
            p.generate("x")
        except ProviderError as e:
            return str(e)
        raise AssertionError(status)
    m401, m403, m404, m429, m503 = (err_for(c, b"bad " + KEY.encode()) for c in (401, 403, 404, 429, 503))
    assert "401" in m401 and "«الإعدادات»" in m401
    assert "403" in m403 and "بلدك" in m403 and "الفوترة" in m403
    assert "404" in m404 and "«الإعدادات»" in m404 and "claude-haiku-5-5" in m404
    assert "429" in m429 and "الاستعمال" in m429
    assert "503" in m503 and "مؤقتاً" in m503
    assert len({m401, m403, m404, m429, m503}) == 5
    assert all(KEY not in m and "http" not in m.lower() for m in (m401, m403, m404, m429, m503))
    # أخطاء الشبكة بالعربية دون نص الاستثناء/الرابط
    assert "الإنترنت" in describe_network_error(urllib.error.URLError(socket.gaierror(-2, "Name or service not known: https://x?key=SECRET")))
    assert "مهلة" in describe_network_error(urllib.error.URLError(socket.timeout("timed out")))
    assert "مهلة" in describe_network_error(TimeoutError())
    assert "شهادة" in describe_network_error(urllib.error.URLError(ssl.SSLCertVerificationError("bad cert https://x?key=SECRET")))
    assert "SECRET" not in describe_network_error(urllib.error.URLError(socket.gaierror(-2, "https://x?key=SECRET")))
    assert "رفض" in describe_network_error(ConnectionRefusedError())
    with mock.patch.object(urllib.request.OpenerDirector if hasattr(urllib, "request") else object, "open",
                           side_effect=urllib.error.URLError(socket.gaierror(-2, "https://x?key=SECRET"))):
        try:
            default_transport("https://example.invalid/?key=SECRET", {}, b"", 1)
            raise AssertionError
        except ProviderError as e:
            assert "الإنترنت" in str(e) and "SECRET" not in str(e) and "example.invalid" not in str(e)

    # ---- 5) Retry-After + الإيقاف أثناء الانتظار ----
    assert parse_retry_after("5") == 5.0 and parse_retry_after("9999") == 60.0 and parse_retry_after("-3") == 0.0
    assert parse_retry_after("abc") is None and parse_retry_after(None) is None and parse_retry_after("nan") is None
    http_date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 30))
    assert 20 < parse_retry_after(http_date) <= 31
    sleeps, calls = [], []

    def tr(url, h, b, t):
        calls.append(1)
        if len(calls) == 1:
            return 429, b"slow", {"Retry-After": "7"}
        return 200, json.dumps({"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
    p = AnthropicProvider(api_key=KEY, transport=tr, sleep=sleeps.append)
    assert p.generate("x") == "ok" and sleeps == [7.0], sleeps
    sleeps.clear(); calls.clear()
    p = AnthropicProvider(api_key=KEY, transport=lambda *a: (503, b"b", {"Retry-After": "100000"}), sleep=sleeps.append, max_retries=1)
    try:
        p.generate("x")
    except ProviderError:
        pass
    assert sleeps == [60.0], sleeps                                   # سقف 60ث
    # نوم حقيقي مقطّع: الإيقاف يقطع الانتظار خلال شرائح 0.2ث
    stop = {"v": False}
    import threading
    threading.Timer(0.5, lambda: stop.update(v=True)).start()
    p = AnthropicProvider(api_key=KEY, transport=lambda *a: (429, b"b", {"Retry-After": "30"}), cancel_check=lambda: stop["v"], max_retries=2)
    t0 = time.time()
    try:
        p.generate("x")
        raise AssertionError
    except ProviderError as e:
        assert "الإيقاف" in str(e)
    assert time.time() - t0 < 3, time.time() - t0

    # ---- 6) القياس: تقدير + تأكيد ----
    est = rb.estimate_cost("gemini", 20)
    assert est["total"] > 0 and est["cap"] >= 1.0 and est["cap"] >= est["total"]
    assert rb.estimate_cost("anthropic", 20)["total"] > rb.estimate_cost("anthropic", 5)["total"]
    assert rb.DEFAULT_RUNS == 20
    ran = []
    fake_prov = mock.Mock()
    fake_prov.fallback = fake_prov
    with mock.patch("assistant.models.factory.build_provider", return_value=(fake_prov, "note")), \
            mock.patch.object(rb, "run_bench", side_effect=lambda *a, **k: ran.append((a, k)) or {"results": {}, "provider": "p", "runs": 1,
                                                                                                    "stopped_for_budget": False, "max_usd": 1, "totals": {}}), \
            mock.patch.object(rb, "render_markdown", return_value="x"):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(pathlib.Path(tmp) / "m.md")
            with mock.patch.object(sys, "stdin", io.StringIO("")):       # غير تفاعلي بلا --yes => لا إنفاق
                assert rb.main(["--provider", "gemini", "--out", out]) == 3 and not ran
            class _Tty(io.StringIO):
                def isatty(self):
                    return True
            with mock.patch.object(sys, "stdin", _Tty("")), mock.patch("builtins.input", return_value="n"):
                assert rb.main(["--provider", "gemini", "--out", out]) == 3 and not ran
            assert rb.main(["--provider", "gemini", "--yes", "--out", out]) == 0
            assert ran and ran[0][1]["runs"] == 20 and ran[0][1]["max_usd"] == est["cap"]
    bat = (ROOT / "run_bench.bat").read_bytes()
    assert b"\r\n" in bat and bat.replace(b"\r\n", b"").find(b"\n") == -1 and all(c < 128 for c in bat)
    assert b"--ask-key" in bat and b"gemini" in bat and b"anthropic" in bat and b"--yes" not in bat

    # ---- 7) BudgetTracker يرفض NaN/السالب ----
    for bad in (float("nan"), -1, float("inf"), "3", True, None):
        for field in ("steps", "tokens", "retries", "points"):
            bt = BudgetTracker(max_steps=10, max_tokens=100, max_retries=5, max_points=10)
            try:
                bt.charge(**{field: bad})
                raise AssertionError((field, bad))
            except (BudgetExceededError, ValueError):
                pass
            assert (bt.current_steps, bt.current_tokens, bt.current_retries, bt.current_points) == (0, 0, 0, 0)
    bt = BudgetTracker(max_steps=10, max_tokens=100)
    bt.charge(steps=1, tokens=5)
    assert bt.current_tokens == 5

    # ---- 8) winget غير موجود: رسالة عربية واضحة ----
    with mock.patch.object(WingetTool, "_resolve_winget", lambda self: None):
        code, out, err = WingetTool()._default_runner(["winget", "upgrade", "--id", "Git.Git"])
        assert code == WINGET_NOT_FOUND_CODE and err == WINGET_NOT_FOUND_MSG and not WingetTool().is_available()
    with tempfile.TemporaryDirectory() as tmp:
        v = Verifier(pathlib.Path(tmp).resolve())
        step = PlanStep(1, "winget_upgrade", "Git.Git")
        ok, ev, msg = v.verify_step(step, {"step_id": 1, "success": False, "exit_code": 127, "stdout": "", "stderr": WINGET_NOT_FOUND_MSG},
                                    {"step_id": 1})
        assert not ok and msg == WINGET_NOT_FOUND_MSG and "127" not in msg and "winget" in msg

    # ---- 9) اتفاق العدد والمعدود ----
    from assistant.arabic import n_units
    cases = [(1, "عنصر", "nom", "عنصر واحد"), (1, "عنصر", "acc", "عنصراً واحداً"), (2, "عنصر", "nom", "عنصران"), (2, "عنصر", "acc", "عنصرين"),
             (2, "عنصر", "gen", "عنصرين"), (3, "عنصر", "nom", "3 عناصر"), (10, "ملف", "acc", "10 ملفات"), (11, "ملف", "nom", "11 ملفاً"),
             (60, "عنصر", "acc", "60 عنصراً"), (1, "عملية", "nom", "عملية واحدة"), (1, "عملية", "acc", "عملية واحدة"), (2, "عملية", "nom", "عمليتان"),
             (2, "عملية", "gen", "عمليتين"), (4, "عملية", "acc", "4 عمليات"), (12, "عملية", "nom", "12 عملية"), (2, "خطوة", "gen", "خطوتين"),
             (7, "خطوة", "gen", "7 خطوات"), (0, "مجلد", "nom", "0 مجلدات"), (2, "مجلد", "nom", "مجلدان"), (15, "مجلد", "acc", "15 مجلداً")]
    for n, kind, case, want in cases:
        assert n_units(n, kind, case) == want, (n, kind, case, n_units(n, kind, case), want)
    # الجدول متطابق مع rules.py (حتى يستورد منه لاحقاً)
    from assistant.models import rules as _rules
    for kind, row in _rules._UNITS.items():
        for n in (1, 2, 3, 10, 11, 25):
            assert n_units(n, kind) == _rules._n(n, kind), (kind, n)
    # المدقّق يستعملها
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve()
        (root / "d").mkdir()
        v = Verifier(root)
        assert "يحوي عنصراً واحداً:" in (v._listing(root))
        (root / "e").mkdir()
        assert "يحوي عنصرين:" in v._listing(root)
        for i in range(5):
            (root / f"f{i}").write_text("x", encoding="utf-8")
        assert "يحوي 7 عناصر:" in v._listing(root)
        big = root / "big"
        big.mkdir()
        for i in range(42):
            (big / f"f{i:02d}").write_text("x", encoding="utf-8")
        assert "يحوي 42 عنصراً:" in v._listing(big) and "ومعها عنصران غير معروضة" in v._listing(big)
        for jn, want in ((1, "عملية واحدة"), (2, "عمليتين"), (4, "4 عمليات"), (13, "13 عملية")):
            (root / ".undo_journal.json").write_text(json.dumps([{"action": "create_dir", "path": f"rel:d{i}"} for i in range(jn)]), encoding="utf-8")
            steps = [PlanStep(i + 1, "create_dir", f"d{i}") for i in range(jn)]
            ok, ev, msg = v.verify_journal(steps, {})
            assert ok and ev["value"] == f"صالح ويحوي {want} لهذه المهمة", ev
        (root / ".undo_journal.json").write_text("[]", encoding="utf-8")
        ok, ev, msg = v.verify_journal([PlanStep(1, "create_dir", "x1"), PlanStep(2, "create_dir", "x2")], {})
        assert not ok and "المطلوب عمليتان" in msg, msg

    print("PASS: test_b36_tools completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b36_tools", file=sys.stderr)
    sys.exit(1)
