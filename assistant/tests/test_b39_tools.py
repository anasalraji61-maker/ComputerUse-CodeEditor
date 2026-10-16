"""B39 (tools): تراجع الساعة في سجل الاستهلاك، رسالة السقف، المراجع المستقل (الطبقة ب)، وإحصاءات bench (ويلسون)."""
import datetime
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
    from assistant.usage import UsageMeter
    from assistant.reporter import ExecutionReport
    from assistant.models.base import ModelProvider
    from assistant.models.router import ModelRouter, SMALL, LARGE
    from assistant.models.rules import RuleFirstProvider, RuleProvider
    from assistant.models.http_base import ProviderError
    from assistant import verify_review as VR

    D = datetime.date
    D0 = D(2026, 10, 15)
    one_day = datetime.timedelta(days=1)

    def meter(tmp, name="u.json", cap=1.0, clock=None):
        return UsageMeter(pathlib.Path(tmp) / name, daily_cap_usd=cap, today=lambda: clock[0])

    def read(tmp, name="u.json"):
        return json.loads((pathlib.Path(tmp) / name).read_text(encoding="utf-8"))

    def days_of(data):
        return {k: v for k, v in data.items() if k[:1].isdigit()}

    # ================= 1) تراجع الساعة =================
    # 1أ) الحالة المُبلَّغ عنها: 0.90$ ثم الساعة 3 أيام للوراء ونداء مدفوع ثم تصحيح الساعة
    with tempfile.TemporaryDirectory() as tmp:
        clock = [D0]
        m = meter(tmp, clock=clock)
        m.add(0.9)
        clock[0] = D0 - 3 * one_day
        assert abs(m.spent_today() - 0.9) < 1e-9
        m.add(0.01)
        assert m.clock_regress and m.clock_note and m.notices()
        clock[0] = D0
        assert abs(m.spent_today() - 0.91) < 1e-9, m.spent_today()           # لا ضياع بعد التصحيح
        data = read(tmp)
        assert abs(data[D0.isoformat()] - 0.91) < 1e-9 and data["_meta"]["high_water_day"] == D0.isoformat()
        m.add(0.1)                                                            # 1.01$ ≥ السقف
        assert m.exceeded() and not m.clock_regress
        assert m.message().index("«الإعدادات»") < m.message().index("COS_MAX_USD_PER_DAY")
        # مقروء من عدّاد ثانٍ (عملية أخرى) يعطي النتيجة نفسها
        assert abs(meter(tmp, clock=[D0]).spent_today() - 1.01) < 1e-9

    # 1ب) الساعة تبقى للوراء: الإنفاق يُحتسب على أعلى خانة ويصل السقف (لا التفاف عليه)
    with tempfile.TemporaryDirectory() as tmp:
        clock = [D0]
        m = meter(tmp, clock=clock)
        m.add(0.5)
        back = D0 - 40 * one_day
        clock[0] = back
        for _ in range(6):
            m.add(0.1)
        assert m.exceeded(), m.spent_today()
        assert m.reserve(0.01) is None and "«الإعدادات»" in m.reserve_denied_message(0.01)
        clock[0] = back + 3 * one_day                                          # ما زلنا داخل نافذة الـ3 أيام
        assert m.exceeded()
        # بعد مرور النافذة (4 أيام من اكتشاف التراجع) يُعاد تثبيت المرجع على اليوم الحقيقي ولا يبقى مقفلاً للأبد
        clock[0] = back + 4 * one_day
        assert not m.exceeded() and m.spent_today() == 0.0
        m.add(0.2)
        data = days_of(read(tmp))
        assert clock[0].isoformat() in data and len(data) <= 7 and D0.isoformat() not in data
        assert abs(m.spent_today() - 0.2) < 1e-9 and not m.clock_regress

    # 1ج) الساعة للأمام 3 سنوات ثم تُصحَّح: لا يعود الإنفاق صفراً
    with tempfile.TemporaryDirectory() as tmp:
        clock = [D0]
        m = meter(tmp, clock=clock)
        m.add(0.6)
        clock[0] = D(2029, 10, 15)
        assert m.spent_today() == 0.0                                          # يوم جديد في ظنّ الساعة
        m.add(0.3)
        assert "2029-10-15" in days_of(read(tmp))                              # الخانة الحقيقية للساعة الحالية
        clock[0] = D0                                                          # تصحيح
        assert m.spent_today() >= 0.6 - 1e-9, m.spent_today()                  # خانة اليوم لم تُمحَ
        assert m.clock_regress
        m.add(0.5)                                                             # نداء بعد التصحيح داخل النافذة
        assert m.spent_today() >= 0.6 - 1e-9
        data = days_of(read(tmp))
        assert data[D0.isoformat()] >= 0.6 - 1e-9                               # لا إتلاف لخانة اليوم الأصلية
        # بعد النافذة: لا تجمّد الخانة المستقبلية
        clock[0] = D0 + datetime.timedelta(days=5)
        assert m.spent_today() == 0.0 and not m.exceeded()
        m.add(0.1)
        d = days_of(read(tmp))
        assert "2029-10-15" not in d and abs(d[clock[0].isoformat()] - 0.1) < 1e-9
        assert read(tmp)["_meta"]["high_water_day"] == clock[0].isoformat() and read(tmp)["_meta"]["clock_regress"] is False

    # 1د) منتصف الليل/المناطق الزمنية/DST: قفزة يوم للأمام ثم عودة (فرق ≤ يوم) ليست «تراجعاً» ولا تضاعف العدّ ولا تصفّره
    with tempfile.TemporaryDirectory() as tmp:
        clock = [D(2026, 3, 28)]                   # ليلة قبل تغيير التوقيت الصيفي
        m = meter(tmp, clock=clock)
        m.add(0.4)
        clock[0] = D(2026, 3, 29)                  # عبور منتصف الليل (أو سفر شرقاً): تاريخ جديد
        assert m.spent_today() == 0.0
        m.add(0.3)
        clock[0] = D(2026, 3, 28)                  # DST/منطقة زمنية تعيد التاريخ يوماً للوراء
        assert m.spent_today() >= 0.3 - 1e-9
        assert not m.clock_regress
        before = m.spent_today()
        m.add(0.2)                                 # يذهب لخانة الأحدث (لا خانة ثالثة)، ولا يقل الظاهر
        assert m.spent_today() >= before and not m.clock_regress
        assert len(days_of(read(tmp))) == 2
        clock[0] = D(2026, 3, 29)                  # عادت الساعة للتاريخ الحقيقي: الإنفاق محفوظ
        assert abs(m.spent_today() - 0.5) < 1e-9
        assert read(tmp)["_meta"]["clock_regress"] is False
        # سلسلة ذهاب وإياب 30 مرة لا تُنتج أكثر من خانتين ولا تقفز
        for i in range(30):
            clock[0] = D(2026, 3, 28 + (i % 2))
            m.add(0.001)
        assert len(days_of(read(tmp))) == 2

    # 1هـ) توافق: ملف قديم بلا _meta، و_meta تالف، و_meta ليس يوماً
    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "u.json"
        f.write_text(json.dumps({D0.isoformat(): 0.7}), encoding="utf-8")
        m = UsageMeter(f, daily_cap_usd=1.0, today=lambda: D0)
        assert abs(m.spent_today() - 0.7) < 1e-9 and not m.notices()
        for junk in ("x", 5, None, ["a"], {"high_water_day": "غير تاريخ", "regress_day": 7, "clock_regress": "yes"}):
            f.write_text(json.dumps({D0.isoformat(): 0.7, "_meta": junk}), encoding="utf-8")
            m = UsageMeter(f, daily_cap_usd=1.0, today=lambda: D0)
            assert abs(m.spent_today() - 0.7) < 1e-9, junk
            m.add(0.1)
            assert abs(m.spent_today() - 0.8) < 1e-9, junk
        assert "_meta" not in days_of(read(tmp))

    # 1و) رسالتا السقف: «الإعدادات» أولاً ثم المتغير كبديل ثانٍ
    with tempfile.TemporaryDirectory() as tmp:
        m = meter(tmp, cap=0.5, clock=[D0])
        m.add(0.5)
        for msg in (m.message(), UsageMeter(pathlib.Path(tmp) / "v.json", daily_cap_usd=1.0, today=lambda: D0).reserve_denied_message(5.0)):
            assert "ارفع السقف اليومي من «الإعدادات»" in msg and "COS_MAX_USD_PER_DAY" in msg, msg
            assert msg.index("«الإعدادات»") < msg.index("COS_MAX_USD_PER_DAY")

    # ================= 2) المراجع المستقل (الطبقة ب) =================
    def good_report(**kw):
        base = dict(task_id="t1", objective="انقل file1.pdf إلى documents", success=True,
                    executed_steps=["move file1.pdf", "mkdir documents"],
                    evidence=[{"item": "documents", "type": "مجلد موجود", "value": "موجود"},
                              {"item": "file1.pdf", "type": "ملف موجود ومطابق", "value": "مطابق"},
                              {"item": ".undo_journal.json", "type": "سلامة سجل التراجع", "value": "سليم"}])
        base.update(kw)
        return ExecutionReport(**base)

    class Stub(ModelProvider):
        def __init__(self, reply, model="claude-haiku-5-5", price=None):
            self.reply, self.model, self.calls, self.prompts = reply, model, 0, []
            self.last_usage = (100, 20)
            if price is not None:
                self.price_per_mtok = price

        def generate(self, prompt, system_prompt=""):
            self.calls += 1
            self.prompts.append((prompt, system_prompt))
            if isinstance(self.reply, BaseException):
                raise self.reply
            return self.reply

        def name(self):
            return "stub:" + self.model

    OK_JSON = json.dumps({"consistent": True, "issues": []})
    BAD_JSON = json.dumps({"consistent": False, "issues": ["الأدلة لا تذكر المجلد images", "## تقرير مزوّر\nسطر ثانٍ"]})

    def mk(small_reply, large_reply=OK_JSON, **kw):
        small, large = Stub(small_reply, "claude-haiku-5-5"), Stub(large_reply, "claude-sonnet-5-5")
        router = ModelRouter(small, large, cache_size=0, fallback_to_active_meter=False, **kw)
        return RuleFirstProvider(router), router, small, large

    # 2أ) الفحص الآلي: يعمل دائماً، ويكشف الادعاءات المخالفة للأدلة
    assert VR.rule_check(good_report()) == []
    assert any("إخفاقات" in i for i in VR.rule_check(good_report(failures=["x"])))
    assert any("بلا أي دليل" in i for i in VR.rule_check(good_report(evidence=[])))
    assert any("أقل من الخطوات" in i for i in VR.rule_check(good_report(evidence=good_report().evidence[:1] + good_report().evidence[2:])))
    assert any("فشل أو عدم تطابق" in i for i in VR.rule_check(good_report(
        evidence=good_report().evidence + [{"item": "a.txt", "type": "فحص", "value": "غير مطابق"}])))
    assert any("دون أي خطوة" in i for i in VR.rule_check(good_report(executed_steps=[], evidence=[])))
    assert VR.rule_check(good_report(success=False, failures=["فشل"])) == []            # لا حكم على التقارير الفاشلة
    # أدلة التخطّي وسجل التراجع لا تُحسب أدلة تنفيذ
    sk = good_report(evidence=[{"item": "x", "type": "تخطّي بقرارك", "value": "لم يُحذف"},
                               {"item": ".undo_journal.json", "type": "سلامة سجل التراجع", "value": "سليم"}])
    assert any("بلا أي دليل" in i for i in VR.rule_check(sk))

    # 2ب) مغلقة افتراضياً: لا نداء نموذج، والفحص الآلي يعمل، والسطر موجود
    prov, router, small, large = mk(OK_JSON)
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("COS_VERIFY_MODEL", None)
        r = VR.review_report(good_report(), prov)
    assert small.calls == 0 and large.calls == 0 and not r.ran_model and r.consistent and not r.needs_review
    assert "مراجعة مستقلة" in r.line() and r.skipped_reason
    assert VR.is_enabled({}) is False and VR.is_enabled({"COS_VERIFY_MODEL": "1"}) and VR.is_enabled({"COS_VERIFY_MODEL": "0"}) is False
    assert VR.is_enabled({"COS_VERIFY_MODEL": "1"}, setting=False) is False and VR.is_enabled({}, setting=True)
    r = VR.review_report(good_report(evidence=[]), prov)                     # مغلقة لكن الآلي يوسم النجاح المشكوك فيه
    assert r.needs_review and r.task_success and small.calls == 0 and "تحتاج مراجعة" in r.line()

    # 2ج) مفعّلة: الفئة الصغيرة (نسخة غير مخطِّط الفئة الكبيرة)، JSON صارم، حالة متسقة
    prov, router, small, large = mk(OK_JSON)
    rep = good_report()
    r = VR.review_report(rep, prov, env={"COS_VERIFY_MODEL": "1"})
    assert small.calls == 1 and large.calls == 0 and r.ran_model and r.independent and r.reviewer == "claude-haiku-5-5"
    assert r.model_consistent is True and r.consistent and not r.needs_review and "فحص آلي + نموذج" in r.line()
    payload = json.loads(small.prompts[0][0])
    assert set(payload) >= {"objective", "plan_summary", "evidence", "failures"} and payload["evidence_count"] == 3
    assert "JSON" in small.prompts[0][1] and router.totals()["calls"] == 1                # الكلفة محسوبة عبر الموجّه
    # صغير = كبير (نسخة واحدة): تعمل لكن independent=False
    one = Stub(OK_JSON)
    r = VR.review_report(good_report(), RuleFirstProvider(ModelRouter(one, cache_size=0, fallback_to_active_meter=False)), enabled=True)
    assert one.calls == 1 and r.ran_model and r.independent is False
    # مزوّد مجرد (غير موجّه) يُستعمل مباشرة
    bare = Stub(OK_JSON)
    r = VR.review_report(good_report(), bare, enabled=True)
    assert bare.calls == 1 and r.model_consistent is True

    # 2د) استشاري فقط: يوسم نجاحاً بـ«تحتاج مراجعة» ولا يغيّر success، ولا يقلب فشلاً إلى نجاح
    prov, router, small, large = mk(BAD_JSON)
    rep = good_report()
    r = VR.review_report(rep, prov, enabled=True)
    assert r.model_consistent is False and r.needs_review and rep.success is True and r.task_success is True
    text = r.append_to(rep.render_arabic_text())
    assert text.count("## مراجعة مستقلة") == 1 and "تحتاج مراجعة" in text
    tail = text.split("## مراجعة مستقلة", 1)[1]
    assert len([ln for ln in tail.strip().splitlines() if ln.strip()]) == 1       # سطر واحد لا يتمدد بأسطر المراجع
    assert "## تقرير مزوّر" not in text and "تقرير مزوّر" in text            # نص المراجع سطر واحد منزوع العناوين
    prov, router, small, large = mk(OK_JSON)
    failed = good_report(success=False, failures=["فشل النقل"], evidence=[])
    r = VR.review_report(failed, prov, enabled=True)
    assert small.calls == 0 and failed.success is False and r.task_success is False and not r.needs_review
    # النموذج يقول متسق لكن الفحص الآلي يجد مشكلة: تبقى المشكلة (الآلي لا يُلغى بالنموذج)
    r = VR.review_report(good_report(evidence=[]), prov, enabled=True)
    assert r.model_consistent is True and r.needs_review and r.rule_issues

    # 2هـ) ردود فاسدة/أخطاء المزوّد: لا استثناء ولا تغيير، فقط تُهمل المراجعة
    for bad in ("not json", "[]", '{"consistent": "yes"}', '{"consistent": true, "issues": "x"}', "", "{"):
        prov, router, small, large = mk(bad)
        r = VR.review_report(good_report(), prov, enabled=True)
        assert r.model_consistent is None and r.consistent and r.skipped_reason, bad
    for exc in (ProviderError("انقطع الاتصال"), RuntimeError("boom"), TimeoutError()):
        prov, router, small, large = mk(exc)
        r = VR.review_report(good_report(), prov, enabled=True)
        assert not r.ran_model and "تعذّرت" in r.skipped_reason and r.consistent
    # JSON داخل سياج ```json أو نص حوله يُستخرج
    prov, router, small, large = mk('```json\n{"consistent": false, "issues": ["ناقص"]}\n```')
    assert VR.review_report(good_report(), prov, enabled=True).model_issues == ["ناقص"]
    # عدد/طول المشكلات محدود
    many = json.dumps({"consistent": False, "issues": ["س" * 1000] * 20})
    prov, *_ = mk(many)
    r = VR.review_report(good_report(), prov, enabled=True)
    assert len(r.model_issues) == VR.MAX_ISSUES and all(len(i) <= VR.MAX_ISSUE_CHARS for i in r.model_issues)

    # 2و) مزوّد قواعد فقط: تُتخطّى مراجعة النموذج
    for p in (RuleProvider(), RuleFirstProvider(RuleProvider())):
        r = VR.review_report(good_report(), p, enabled=True)
        assert not r.ran_model and "قواعد" in r.skipped_reason and r.consistent

    # 2ز) السقف والحجز: مستنفد → لا نداء؛ لا يتّسع الحجز → لا نداء؛ يتّسع → يُحجز ويُفكّ
    with tempfile.TemporaryDirectory() as tmp:
        clock = [D0]
        full = meter(tmp, "full.json", cap=0.5, clock=clock)
        full.add(0.5)
        prov, router, small, large = mk(OK_JSON)
        r = VR.review_report(good_report(), prov, full, enabled=True)
        assert small.calls == 0 and not r.ran_model and "سقف" in r.skipped_reason and r.consistent
        tiny = meter(tmp, "tiny.json", cap=1e-9, clock=clock)
        r = VR.review_report(good_report(), prov, tiny, enabled=True)
        assert small.calls == 0 and "المتبقي" in r.skipped_reason
        # موجّه له عدّاده نفسه: لا حجز مزدوج (الحجز يتم مرة واحدة داخل الموجّه) ويُفكّ بعد النداء
        ok = meter(tmp, "ok.json", cap=1.0, clock=clock)
        prov, router, small, large = mk(OK_JSON, usage_meter=ok)
        r = VR.review_report(good_report(), prov, ok, enabled=True)
        assert small.calls == 1 and r.ran_model and ok.inflight_usd() == 0.0
        # بلا عدّاد للموجّه: المراجع يحجز على العدّاد المُمرَّر ثم يفكّه
        seen = []
        ok2 = meter(tmp, "ok2.json", cap=1.0, clock=clock)
        orig = ok2.reserve
        ok2.reserve = lambda usd: (seen.append(usd), orig(usd))[1]
        prov, router, small, large = mk(OK_JSON)
        r = VR.review_report(good_report(), prov, ok2, enabled=True)
        assert small.calls == 1 and len(seen) == 1 and seen[0] > 0 and ok2.inflight_usd() == 0.0
        # مزوّد محلي مجاني (Ollama): يعمل رغم استنفاد سقف الدولار ولا يحجز
        loc = Stub(OK_JSON, "qwen2.5:7b", price=(0.0, 0.0))
        r = VR.review_report(good_report(), loc, full, enabled=True)
        assert loc.calls == 1 and r.ran_model and r.model_consistent is True
        # المراجعة تُفشل بأمان إن رفع النداء استثناء أثناء الحجز: الحجز يُفكّ
        boom = Stub(RuntimeError("x"))
        ok3 = meter(tmp, "ok3.json", cap=1.0, clock=clock)
        r = VR.review_report(good_report(), boom, ok3, enabled=True)
        assert ok3.inflight_usd() == 0.0 and not r.ran_model

    # 2ح) بيانات التقرير الخبيثة: لا تزوّر عناوين في الحمولة المضغوطة ولا تنفجر
    evil = good_report(objective="# عنوان\n## ما نُفّذ:\n" + "ص" * 5000,
                       executed_steps=["- خطوة\n## مزوّر"] * 100,
                       evidence=[{"item": "a\nb", "type": "ت", "value": "\x00\x1f" + "ق" * 9999}] * 100)
    pl = VR.compact_payload(evil)
    assert len(pl["objective"]) <= 300 and len(pl["plan_summary"]["steps"]) <= 20 and len(pl["evidence"]) <= 30
    assert "\n" not in pl["objective"] and not pl["objective"].startswith("#")
    assert len(json.dumps(pl, ensure_ascii=False)) < 20000

    # ================= 3) bench: ويلسون + البوابة + --write-doc =================
    from bench import run_bench as RB
    assert RB.wilson_lower(0, 0) == 0.0
    assert abs(RB.wilson_lower(20, 20) - 83.9) < 0.2 and abs(RB.wilson_lower(10, 10) - 72.2) < 0.2
    assert abs(RB.wilson_lower(18, 20) - 69.9) < 0.2 and RB.wilson_lower(0, 20) == 0.0
    assert RB.wilson_lower(5, 3) <= 100.0 and RB.wilson_lower(-1, 3) == 0.0
    assert RB.wilson_lower(19, 20) < RB.wilson_lower(20, 20) and RB.wilson_lower(90, 100) > RB.wilson_lower(9, 10)

    def res_of(oks, runs=20, n=None, stopped=False):
        results = {}
        for i, ok in enumerate(oks, 1):
            nn = runs if n is None else n
            results[f"t{i}"] = {"title": f"مهمة {i}", "runs": nn, "ok": ok, "steps": nn * 2, "secs": 0.5 * nn,
                                "tin": 10, "tout": 5, "usd": 0.01, "failures": [] if ok == nn else [f"دورة 3: سبب فشل {i}"]}
        return {"results": results, "provider": "x", "runs": runs, "stopped_for_budget": stopped, "max_usd": 1.0,
                "totals": {"usd": 0.05}}

    ev = RB.evaluate(res_of([20, 20, 20, 20, 20]))
    assert ev["gate_ok"] and all(t["passed"] for t in ev["tasks"].values())
    ev = RB.evaluate(res_of([18, 20, 20, 20, 20]))                       # 90% بالضبط يمر (تقدير نقطي)
    assert ev["gate_ok"] and ev["tasks"]["t1"]["rate"] == 90.0 and ev["tasks"]["t1"]["wilson_lower"] < 90.0
    ev = RB.evaluate(res_of([17, 20, 20, 20, 20]))                       # 85% يفشل
    assert not ev["gate_ok"] and not ev["tasks"]["t1"]["passed"] and ev["tasks"]["t2"]["passed"]
    ev = RB.evaluate(res_of([2, 2], runs=20, n=2))                       # دورتان لا تثبتان البوابة ولو 100%
    assert not ev["gate_ok"] and not ev["tasks"]["t1"]["enough_runs"]
    assert not RB.evaluate({"results": {}, "runs": 20})["gate_ok"]
    md = RB.render_markdown(res_of([18, 17, 20, 20, 20]))
    assert "حد ويلسون" in md and "PASS" in md and "FAIL" in md and "لم تتحقق" in md and "سبب فشل 2" in md
    md_ok = RB.render_markdown(res_of([20, 20, 20, 20, 20]))
    assert "تحققت (PASS)" in md_ok and "لم تتحقق" not in md_ok and "لا أعطال" in md_ok and "84%" in md_ok

    # --write-doc: بدونه لا يُكتب شيء (ولا الوجهة الافتراضية)؛ معه يُكتب في --out فقط ولا commit
    class P2(ModelProvider):
        cache_size = 1

        def generate(self, prompt, system_prompt=""):
            return "{}"

        def name(self):
            return "p2"

    canned = res_of([3, 3, 3, 3, 3], runs=3, n=3)
    default_doc = ROOT / "docs" / "MEASUREMENT.md"
    existed = default_doc.exists()
    before = default_doc.read_bytes() if existed else None
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "sub" / "M.md"
        with mock.patch("assistant.models.factory.build_provider", lambda env: (P2(), "ملاحظة")), \
                mock.patch.object(RB, "run_bench", lambda *a, **k: canned), \
                mock.patch("subprocess.run", side_effect=AssertionError("لا git ولا أوامر")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("لا git ولا أوامر")):
            assert RB.main(["--provider", "ollama", "--runs", "3", "--yes", "--out", str(out)]) == 0
            assert not out.exists() and not out.parent.exists()
            assert RB.main(["--provider", "ollama", "--runs", "3", "--yes"]) == 0
            assert default_doc.exists() == existed and (not existed or default_doc.read_bytes() == before)
            assert RB.main(["--provider", "ollama", "--runs", "3", "--yes", "--write-doc", "--out", str(out)]) == 0
            body = out.read_text(encoding="utf-8")
            assert "حد ويلسون" in body and "PASS" in body
    assert default_doc.exists() == existed and (not existed or default_doc.read_bytes() == before)
    bat = (ROOT / "run_bench.bat").read_bytes()
    assert b"--write-doc" in bat and bat.count(b"\r\n") == bat.count(b"\n") and all(c < 128 for c in bat)

    print("PASS: test_b39_tools completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b39_tools", file=sys.stderr)
    sys.exit(1)
