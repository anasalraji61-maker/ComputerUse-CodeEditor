"""B40 (tools): وحدتا «مهمة»/«نقطة»، تدقيق winget (معرّف/Available مقصوص، غير مثبّتة، إعادة تشغيل)، فحص bench T4 الذي يمكن أن يفشل،
وربط المراجعة المستقلة الاختيارية بالمنسّق (مغلقة افتراضياً)، وصياغة رسائل المزوّد/السقف/الدفتر."""
import json
import os
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from unittest import mock
    from assistant.arabic import n_units, UNITS
    from assistant.planner import PlanStep
    from assistant.verifier import Verifier
    from assistant.reporter import ExecutionReport
    from assistant import verify_review as VR
    from assistant.models.base import ModelProvider
    from assistant.ledger import SQLiteLedger, InsufficientBalanceError, SpendingLimitExceededError
    from assistant.budget import BudgetTracker, BudgetExceededError
    from assistant.runner import TaskRunner
    from assistant.models.fake import FakeProvider
    from bench import run_bench as RB

    # ================= 1) الوحدتان الجديدتان: مفرد/مثنى/جمع/تمييز × الحالات =================
    for kind in ("مهمة", "نقطة"):
        assert kind in UNITS
    expect = {
        "مهمة": {0: ("0 مهام",) * 3, 1: ("مهمة واحدة",) * 3, 2: ("مهمتان", "مهمتين", "مهمتين"), 3: ("3 مهام",) * 3,
                 10: ("10 مهام",) * 3, 11: ("11 مهمة",) * 3, 100: ("100 مهمة",) * 3},
        "نقطة": {0: ("0 نقاط",) * 3, 1: ("نقطة واحدة",) * 3, 2: ("نقطتان", "نقطتين", "نقطتين"), 3: ("3 نقاط",) * 3,
                 10: ("10 نقاط",) * 3, 11: ("11 نقطة",) * 3, 100: ("100 نقطة",) * 3},
    }
    for kind, table in expect.items():
        for n, (nom, acc, gen) in table.items():
            assert n_units(n, kind, "nom") == nom, (kind, n, n_units(n, kind, "nom"))
            assert n_units(n, kind, "acc") == acc, (kind, n, n_units(n, kind, "acc"))
            assert n_units(n, kind, "gen") == gen, (kind, n, n_units(n, kind, "gen"))
    assert n_units(3, "نقطة") == "3 نقاط" and n_units(25, "مهمة", "gen") == "25 مهمة"

    # ================= 2) winget: D4 (Available مقصوص) / D5 (معرّف مقصوص) / D6 (غير مثبّتة، إعادة تشغيل) =================
    HDR = "Name Id Version Available Source\n----------------------------------------\n"
    UP = "Microsoft Visual Studio Community 2022  Microsoft.VisualStudio.2022.Community  17.9.0  17.10.0  winget\n"
    TRUNC_ID = "Microsoft Visual Studio Com…  Microsoft.VisualStudio.2022.Comm…  17.9.0  17.10.0  winget\n"
    LONG_ID = "Microsoft.VisualStudio.2022.Community"

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp).resolve()

        def verifier(before_out, after_out, code=0):
            outs = iter([(0, before_out, ""), (code, after_out, "")])
            return Verifier(root, version_probe=lambda pkg: next(outs))

        def run(pkg, before_out, after_out, info_extra=None, probe_code=0):
            v = verifier(before_out, after_out, probe_code)
            step = PlanStep(1, "winget_upgrade", pkg)
            before = v.snapshot(step)
            info = {"step_id": 1, "success": True, "exit_code": 0, "stdout": "Successfully installed", "stderr": ""}
            info.update(info_extra or {})
            return v.verify_step(step, info, before)

        # D4: الإصدار الأحدث ظاهر لكنه مقصوص بـ«…» وسطر الحزمة لم يتغير = لم تتم ترقية (لا نجاح كاذب)
        git_trunc = "Git  Git.Git  2.45.0  2.46…  winget\n"
        ok, ev, err = run("Git.Git", HDR + git_trunc, HDR + git_trunc)
        assert not ok and "لم تتم الترقية" in err, (ok, err)
        assert Verifier._still_upgradable("Git  Git.Git  2.45.0  2.46…  winget", "Git.Git")
        assert not Verifier._still_upgradable("Git  Git.Git  2.46.0  winget", "Git.Git")
        assert Verifier._still_upgradable("Git  Git.Git  2.45.0  2.46.0  winget", "Git.Git")
        # ترقية حقيقية (تغيّر السطر) مع Available مقصوص قبلها: نجاح
        ok, ev, err = run("Git.Git", HDR + git_trunc, HDR + "Git  Git.Git  2.46.0  winget\n")
        assert ok and "قبل" in ev["value"], (ok, err)

        # D5: معرّف طويل مقصوص في الجدول: نجاح حقيقي لا يُعدّ فشلاً
        ok, ev, err = run(LONG_ID, HDR + TRUNC_ID, HDR + "Microsoft Visual Studio Com…  Microsoft.VisualStudio.2022.Comm…  17.10.0  winget\n")
        assert ok, err
        # ولا تُطابَق حزمة أخرى ببادئة قصيرة، ولا يُفضَّل المقصوص على سطر بمعرّف كامل
        assert Verifier._package_line("Git LFS  Git.Gi…  1.0  winget\n", "Git.Git") == ""
        both = "A  Microsoft.VisualStudio.2022.Comm…  1.0\nB  Microsoft.VisualStudio.2022.Community  2.0\n"
        assert "2.0" in Verifier._package_line(both, LONG_ID)
        assert Verifier._package_line("Git LFS  Git.GitLFS  1.0\n", "Git.Git") == ""     # لا مطابقة جزئية لمعرّف آخر كامل

        # D6أ: غير مثبّتة ليست «محدّثة أصلاً»
        for code, out in ((0x8A150014, "No installed package found matching input criteria."),
                          (-1978335212, "No installed package found matching input criteria."),
                          (1, "لم يتم العثور على حزمة مثبّتة تطابق معايير الإدخال.")):
            v = Verifier(root, version_probe=lambda pkg: (0, "", ""))
            step = PlanStep(1, "winget_upgrade", "Git.Git")
            ok, ev, err = v.verify_step(step, {"step_id": 1, "success": False, "exit_code": code, "stdout": out, "stderr": ""},
                                        {"step_id": 1, "action": "winget_upgrade"})
            assert not ok and "غير مثبّتة" in err and "محدّثة أصلاً" not in err, (code, err)
        # ولا يزال «لا ترقية متاحة» الحقيقي كما كان
        ok, ev, err = Verifier(root, version_probe=lambda p: (0, "", "")).verify_step(
            PlanStep(1, "winget_upgrade", "Git.Git"),
            {"step_id": 1, "success": False, "exit_code": 0x8A15002B, "stdout": "No available upgrade found.", "stderr": ""},
            {"step_id": 1, "action": "winget_upgrade"})
        assert not ok and "لا يوجد تحديث متاح" in err

        # D6ب: إعادة تشغيل مطلوبة بعد تثبيت اكتمل: نجاح مشروط بإثبات الفحص المستقل لتغيّر الإصدار
        for code in (0x8A150109, -1978334967):
            ok, ev, err = run("Git.Git", HDR + git_trunc, HDR + "Git  Git.Git  2.46.0  winget\n",
                              {"success": False, "exit_code": code, "stdout": "Successfully installed\nRestart your PC to finish installation."})
            assert ok and "إعادة تشغيل" in ev["value"] and "قبل" in ev["value"], (code, ok, err)
        # الرمز وحده لا يكفي: سطر الحزمة لم يتغير = فشل
        ok, ev, err = run("Git.Git", HDR + git_trunc, HDR + git_trunc, {"success": False, "exit_code": 0x8A150109, "stdout": ""})
        assert not ok and "لم يُظهر تغيّر" in err
        # رمز فشل عادي آخر يبقى فشلاً مع ذيل الناتج
        ok, ev, err = run("Git.Git", HDR + git_trunc, HDR + "Git  Git.Git  2.46.0  winget\n",
                          {"success": False, "exit_code": 0x80070005, "stdout": "denied"})
        assert not ok and "0x80070005" in err

    # ================= 3) bench T4: المحاكي يمكن أن يفشل =================
    t4 = next(t for t in RB.TASKS if t.key == "t4")
    sim = RB.WingetSim()
    assert t4.check(pathlib.Path("."), {"winget": sim})            # لم تحدث ترقية: الفحص يفشل
    assert "لم يُستدعَ" in " ".join(t4.check(pathlib.Path("."), {"winget": sim}))
    sim(["winget", "list", "-q", "Git.Git"])
    rc, out, err = sim(["winget", "upgrade", "--id", "Git.Git", "--exact", "--silent"])
    assert rc == 0 and sim.version("Git.Git") == "2.46.0" and "Successfully installed" in out
    assert t4.check(pathlib.Path("."), {"winget": sim}) == []
    assert sim(["winget", "upgrade", "--id", "Git.Git", "--exact"])[0] == RB.WingetSim.NOT_APPLICABLE     # لا ترقية ثانية
    assert RB.WingetSim()(["winget", "upgrade", "--id", "Nope.Pkg", "--exact"])[0] == RB.WingetSim.NOT_FOUND
    assert RB.WingetSim()(["winget", "install", "x"])[0] == 1
    # ترقية حزمة أخرى أو بلا --exact تُكتشف
    bad = RB.WingetSim(installed={"Git.Git": ("Git", "2.45.0", "2.46.0"), "Other.Pkg": ("O", "1.0", "2.0")})
    bad(["winget", "upgrade", "--id", "Other.Pkg", "--exact"])
    bad(["winget", "upgrade", "--id", "Git.Git", "--exact"])
    assert any("غير Git.Git" in f for f in t4.check(pathlib.Path("."), {"winget": bad}))
    assert t4.check(pathlib.Path("."), {}) == ["لا محاكي winget في السياق"]

    # تشغيل فعلي: خطة صحيحة تنجح، ومحاكٍ كاذب (0 بلا تغيير) يفشل رغم أن الخطة صحيحة
    plan = {"steps": [{"step_id": 1, "action_type": "winget_upgrade", "target": "Git.Git", "params": {}}]}
    prov = FakeProvider(default_response=json.dumps(plan))
    res = RB.run_bench(prov, runs=1, tasks=[t4])
    assert res["results"]["t4"]["ok"] == 1, res["results"]["t4"]
    res = RB.run_bench(prov, runs=2, tasks=[t4], winget_factory=lambda: RB.WingetSim(lying=True))
    assert res["results"]["t4"]["ok"] == 0 and len(res["results"]["t4"]["failures"]) == 2, res["results"]["t4"]
    # خطة تستهدف حزمة غير مثبّتة: تفشل
    wrong = {"steps": [{"step_id": 1, "action_type": "winget_upgrade", "target": "Git.GitLFS", "params": {}}]}
    res = RB.run_bench(FakeProvider(default_response=json.dumps(wrong)), runs=1, tasks=[t4])
    assert res["results"]["t4"]["ok"] == 0

    # ================= 4) ربط المراجعة المستقلة بالمنسّق: مغلق افتراضياً، اختياري بمفتاح تهيئة =================
    class Stub(ModelProvider):
        model = "claude-haiku-5-5"
        last_usage = (10, 5)

        def __init__(self, reply):
            self.reply, self.calls = reply, 0

        def generate(self, prompt, system_prompt=""):
            self.calls += 1
            return self.reply

        def name(self):
            return "stub"

    def mkrep(**kw):
        base = dict(task_id="t", objective="o", success=True, executed_steps=["a"],
                    evidence=[{"item": "a", "type": "ملف موجود ومطابق", "value": "ok"}])
        base.update(kw)
        return ExecutionReport(**base)

    # التهيئة: مغلقة افتراضياً؛ المفتاح verify_model؛ المتغير يغلب
    assert VR.configured_enabled({}, {}) is False
    assert VR.configured_enabled({}, {"verify_model": True}) is True and VR.configured_enabled({}, {"verify_model": "1"}) is True
    assert VR.configured_enabled({}, {"verify_model": "false"}) is False and VR.configured_enabled({}, {"verify_model": 1}) is False
    assert VR.configured_enabled({"COS_VERIFY_MODEL": "0"}, {"verify_model": True}) is False
    assert VR.configured_enabled({"COS_VERIFY_MODEL": "1"}, {}) is True

    # attach_review: مغلق = لا شيء؛ مفعّل = سطر في التقرير بلا تغيير النجاح
    rep = mkrep()
    assert VR.attach_review(rep, Stub("{}"), enabled=False) is None and rep.review_line is None
    assert "مراجعة مستقلة" not in rep.render_arabic_text() and rep.to_dict()["review"] is None
    stub = Stub(json.dumps({"consistent": False, "issues": ["ناقص"]}))
    res = VR.attach_review(rep, stub, enabled=True)
    assert res is not None and rep.success is True and rep.review_needs is True and stub.calls == 1
    txt = rep.render_arabic_text()
    assert "## مراجعة مستقلة:" in txt and "تحتاج مراجعة" in txt and txt.count("مراجعة مستقلة") >= 2
    failed = mkrep(success=False, failures=["x"], evidence=[])
    VR.attach_review(failed, stub, enabled=True)
    assert failed.success is False and stub.calls == 1          # فاشل: لا نداء نموذج
    assert VR.attach_review(mkrep(), Stub("{}"), enabled=True, env={}) is not None
    with mock.patch.object(VR, "review_report", side_effect=RuntimeError("boom")):
        assert VR.attach_review(mkrep(), Stub("{}"), enabled=True) is None      # عطب المراجعة لا يسقط التقرير

    # عبر TaskRunner الحقيقي: الافتراضي مغلق (لا سطر ولا نداء)، ومع review_enabled=True يظهر السطر والنجاح كما هو
    plan_json = json.dumps({"steps": [{"step_id": 1, "action_type": "create_dir", "target": "d1", "params": {}}]})
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        ws = base / "ws"
        ws.mkdir()
        reviewer = Stub(json.dumps({"consistent": True, "issues": []}))

        class PlanAndReview(FakeProvider):
            model = "claude-haiku-5-5"

            def generate(self, prompt, system_prompt=""):
                if "consistent" in system_prompt:
                    return reviewer.generate(prompt, system_prompt)
                return plan_json

        prov2 = PlanAndReview(default_response=plan_json)
        with mock.patch.dict(os.environ, {"COS_DATA_DIR": str(base / "data")}):
            os.environ.pop("COS_VERIFY_MODEL", None)
            led = SQLiteLedger(base / "l1.db", initial_balance=100, spending_limit=100)
            r = TaskRunner(ws, prov2, led).run("T-off", "أنشئ المجلد d1")
            assert r.success and r.review_line is None and reviewer.calls == 0 and "مراجعة مستقلة" not in r.render_arabic_text()
            (base / "data").mkdir(exist_ok=True)
            (base / "data" / "config.json").write_text(json.dumps({"verify_model": True}), encoding="utf-8")
            led2 = SQLiteLedger(base / "l2.db", initial_balance=100, spending_limit=100)
            r2 = TaskRunner(ws, prov2, led2).run("T-cfg", "أنشئ المجلد d2")
            assert r2.success and r2.review_line and "مراجعة مستقلة" in r2.render_arabic_text(), r2.review_line
            led3 = SQLiteLedger(base / "l3.db", initial_balance=100, spending_limit=100)
            r3 = TaskRunner(ws, prov2, led3, review_enabled=False).run("T-forced-off", "أنشئ المجلد d3")
            assert r3.success and r3.review_line is None

    # ================= 5) صياغة الرسائل (بلا تقنيات، ومفردات موحّدة) =================
    import ssl, socket, urllib.error
    from assistant.models.http_base import describe_network_error
    from assistant.usage import UsageMeter
    import datetime
    for exc in (urllib.error.URLError(socket.gaierror(-2, "x")), urllib.error.URLError(ssl.SSLError("x")), RuntimeError("weird")):
        m = describe_network_error(exc)
        assert "DNS" not in m and "TLS" not in m and "Error" not in m and "URLError" not in m, m
    with tempfile.TemporaryDirectory() as tmp:
        m = UsageMeter(pathlib.Path(tmp) / "u.json", daily_cap_usd=0.5, today=lambda: datetime.date(2026, 10, 15))
        m.add(0.5)
        msg = m.message()
        assert "$" not in msg and "دولار" in msg and "COS_PROVIDER=demo" not in msg and "«الإعدادات»" in msg
        msg2 = m.reserve_denied_message(5.0)
        assert "$" not in msg2 and "COS_PROVIDER" not in msg2 and msg2.index("«الإعدادات»") < msg2.index("COS_MAX_USD_PER_DAY")
        (pathlib.Path(tmp) / "c.json").write_text("{bad", encoding="utf-8")
        m2 = UsageMeter(pathlib.Path(tmp) / "c.json", daily_cap_usd=1.0, today=lambda: datetime.date(2026, 10, 15))
        m2.spent_today()
        assert "أُعيد ضبطه" in m2.reset_note and "JSON" not in m2.reset_note and "ترميز" not in m2.reset_note
    with tempfile.TemporaryDirectory() as tmp:
        led = SQLiteLedger(pathlib.Path(tmp) / "l.db", initial_balance=10, spending_limit=10)
        try:
            led.record_task_start("big", "x", 20)
            raise AssertionError
        except (InsufficientBalanceError, SpendingLimitExceededError) as exc:
            s = str(exc)
            assert "<" not in s and ">" not in s and "نقاط" in s, s
        led.record_task_start("t1", "x", 3)
        try:
            led.record_task_start("t1", "x", 3)
            raise AssertionError
        except ValueError as exc:
            assert "t1" not in str(exc)
    b = BudgetTracker(max_steps=2, max_tokens=10, max_retries=1, max_points=5)
    try:
        b.charge(steps=3)
        raise AssertionError
    except BudgetExceededError as exc:
        assert ">" not in str(exc) and "3 خطوات" in str(exc) and "خطوتان" in str(exc), str(exc)
    try:
        b.charge(tokens=11)
        raise AssertionError
    except BudgetExceededError as exc:
        assert "التوكنات" not in str(exc) and ">" not in str(exc)

    print("PASS: test_b40_tools completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b40_tools", file=sys.stderr)
    sys.exit(1)
