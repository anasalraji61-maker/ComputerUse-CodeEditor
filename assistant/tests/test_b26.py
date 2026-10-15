"""اختبارات B26: نتائج المراجعة الثامنة (انحدارات B19–B25 + fuzz الثوابت): ربط الإيقاف عبر RuleFirst، عدّاد استهلاك، تراجع بلا ناتج،
فحص مسبق للمحمي/السحابي، بصمة سحابية وصفية، مكررات، سقف للمجاني، '..' في الأسماء، مسافة بادئة، سجل .tmp، ترميز شاذ، إعادة معرّف مهمة."""
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
    from assistant.ledger import SQLiteLedger
    from assistant.models.base import ModelProvider
    from assistant.models.http_base import HttpProvider
    from assistant.models.router import ModelRouter
    from assistant.models.rules import RuleProvider, RuleFirstProvider, parse_context
    from assistant.models.base import CONTEXT_MARKER
    from assistant.planner import PlanStep, PlanParseError, validate_step
    from assistant.runner import TaskRunner
    from assistant.safety import KillSwitch
    from assistant.tools.files import FileManager
    from assistant.ui.controller import AssistantController
    from assistant.usage import UsageMeter
    from assistant.verifier import Verifier

    class P(HttpProvider):
        ENV_KEYS = ("X",)
        def _build(self, prompt, system_prompt): return "http://x", {}, {}

    def mkc(tmp, provider=None, meter=None, name="ws"):
        base = pathlib.Path(tmp).resolve()
        ws = base / name; ws.mkdir(exist_ok=True)
        led = SQLiteLedger(base / f"l_{name}.db", initial_balance=500, spending_limit=500)
        return base, ws, led, AssistantController(provider or RuleProvider(), ws, led, usage_meter=meter)

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()

        # 1. الإيقاف مربوط بمزوّدي الراوتر حتى داخل RuleFirstProvider (التركيب الفعلي في الإنتاج)
        small, large = P("s", api_key="k"), P("l", api_key="k")
        rf = RuleFirstProvider(ModelRouter(small, large))
        _, ws, led, c = mkc(tmp, rf)
        assert small.cancel_check is not None and large.cancel_check is not None

        # 2. عدّاد الاستهلاك: قراءة فاشلة لحظية لا تُضخّم الإنفاق
        m = UsageMeter(base / "u.json", daily_cap_usd=1.0, today=datetime.date.today)
        m.add(0.01)
        real = pathlib.Path.read_text
        def flaky(self, *a, **k):
            if self.name == "u.json" and flaky.n < 1:
                flaky.n += 1
                raise PermissionError("locked")
            return real(self, *a, **k)
        flaky.n = 0
        with mock.patch.object(pathlib.Path, "read_text", flaky), mock.patch("time.sleep", lambda s: None):
            m.add(0.02)
        assert abs(m.spent_today() - 0.03) < 1e-9, m.spent_today()

        # 3. تراجع: مهمة بلا ناتج لا يعلق عليها التراجع التالي
        _, ws3, led3, c3 = mkc(tmp, name="ws3")
        for n in ("Alpha", "Beta"):
            c3.submit_request(f"أنشئ مجلد {n}"); assert c3.wait_for("plan"); c3.start(); assert c3.wait_for("report", 10)
        (ws3 / "Beta").rename(ws3 / "Beta_moved")        # المستخدم غيّر ناتج Beta بيده
        c3.undo_last()
        assert c3.wait_for("error")                       # لا ناتج لـ Beta
        c3.undo_last()
        assert c3.wait_for("undo") and not (ws3 / "Alpha").exists(), "Alpha غير قابلة للتراجع"

        # 4. فحص مسبق: مجلد يحوي .git يُرفض قبل أي خطوة
        ws4 = base / "ws4"; ws4.mkdir()
        (ws4 / "a.txt").write_text("a", encoding="utf-8")
        (ws4 / "proj" / ".git").mkdir(parents=True)
        led4 = SQLiteLedger(base / "l4.db", initial_balance=500, spending_limit=500)
        r4 = TaskRunner(ws4, RuleProvider(), led4)
        rep = r4.run("T4", "x", plan=[PlanStep(1, "rename", "a.txt", {"new_name": "a2.txt"}),
                                       PlanStep(2, "move", "proj", {"dest": "moved"})])
        assert not rep.success and (ws4 / "a.txt").exists() and not (ws4 / "a2.txt").exists(), "نُفّذت خطوة قبل الرفض"

        # 5. بصمة وصفية لملف سحابي كبير (نقل/إعادة تسمية تتحقق بلا تنزيل) ونسخه يُرفض مسبقاً
        ws5 = base / "ws5"; ws5.mkdir()
        (ws5 / "big.mp4").write_bytes(b"x" * 100)
        real_lstat = os.lstat
        def cloud_lstat(path, *a, **k):
            r = real_lstat(path, *a, **k)
            if str(path).endswith("big.mp4"):
                class W:
                    def __getattr__(self, n): return getattr(r, n)
                    st_file_attributes = 0x400000
                    st_size = 500 * 1024 * 1024
                return W()
            return r
        led5 = SQLiteLedger(base / "l5.db", initial_balance=500, spending_limit=500)
        with mock.patch("os.lstat", cloud_lstat):
            v = Verifier(ws5)
            d = v.digest(ws5 / "big.mp4")
            assert d.startswith("F:CLOUD:"), d
            r5 = TaskRunner(ws5, RuleProvider(), led5)
            rep = r5.run("T5", "x", plan=[PlanStep(1, "create_dir", "d"), PlanStep(2, "copy", "big.mp4", {"dest": "d/big.mp4"})])
            assert not rep.success and (not (ws5 / "d").exists()), "النسخ السحابي لم يُرفض مسبقاً"

        # 6. المكررات: ملفات سحابية كبيرة لا تُقرأ، والمسح يتكرر مرة واحدة
        ws6 = base / "ws6"; ws6.mkdir()
        for n in ("v1.mp4", "v2.mp4"):
            (ws6 / n).write_bytes(b"same-bytes")
        rp = RuleProvider(workspace=ws6)
        opened = []
        real_open = open
        def spy_open(f, *a, **k):
            if str(f).endswith(".mp4"):
                opened.append(str(f))
            return real_open(f, *a, **k)
        with mock.patch("builtins.open", spy_open):
            a1 = rp.analyze("انقل المكررات", ["v1.mp4", "v2.mp4"])
            n1 = len(opened)
            a2 = rp.analyze("انقل المكررات", ["v1.mp4", "v2.mp4"])
        assert a1.steps and len(opened) == n1, "مسح مكرر لنفس الطلب"

        # 7. الطلب الذي تفهمه القواعد المحلية لا يُحجب بسقف الإنفاق (التركيب الفعلي RuleFirst)
        meter = UsageMeter(base / "u7.json", daily_cap_usd=1.0, today=datetime.date.today); meter.add(1.5)
        rf7 = RuleFirstProvider(ModelRouter(P("s", api_key="k")))
        _, ws7, led7, c7 = mkc(tmp, rf7, meter, name="ws7")
        (ws7 / "a.txt").write_text("a", encoding="utf-8")
        c7.submit_request("رتب الملفات حسب النوع")
        assert c7.wait_for("plan"), "حُجب طلب مجاني بالسقف"
        c7.cancel_plan()
        c7.submit_request("اكتب لي قصيدة عن البحر")
        ev = c7.wait_for("error"); assert ev and "سقف" in ev["text"], ev

        # 8. حساب الإنفاق بعلامة مائية: خيطان متداخلان لا يعدّان الكلفة مرتين
        class Cnt(ModelProvider):
            usd = 0.0
            def name(self): return "c"
            def totals(self): return {"usd": self.usd, "calls": 1, "points": 0}
            def generate(self, p, system_prompt=""): return "{}"
        cp = Cnt(); meter8 = UsageMeter(base / "u8.json", daily_cap_usd=5.0, today=datetime.date.today)
        _, _, _, c8 = mkc(tmp, cp, meter8, name="ws8")
        cp.usd = 0.2
        s1 = c8._record_spend(0.0); s2 = c8._record_spend(0.0)
        assert abs(s1 - 0.2) < 1e-9 and s2 == 0.0 and abs(meter8.spent_today() - 0.2) < 1e-9

        # 9. أسماء بنقطتين متتاليتين تُعاد تسميتها، ومسافة بادئة لا تُستهدف خطأً
        ws9 = base / "ws9"; ws9.mkdir()
        (ws9 / "b.pdf").write_text("b", encoding="utf-8")
        f9 = FileManager(ws9)
        f9.rename("b.pdf", "report..final.pdf")
        assert (ws9 / "report..final.pdf").exists()
        for bad in ("..", ".", "a/b", "a\\b"):
            try:
                f9.rename("report..final.pdf", bad); raise AssertionError(bad)
            except ValueError:
                pass
        ctx = parse_context(CONTEXT_MARKER + "\n a.pdf\nb.pdf\n")
        assert ctx == ["b.pdf"], ctx
        try:
            validate_step({"action_type": "delete", "target": " a.pdf"}, 1); raise AssertionError("قُبل هدف بمسافة")
        except PlanParseError:
            pass

        # 10. سجل .tmp كامل أحدث بعد انهيار يُتبنّى؛ وغير الكامل يُهمَل
        ws10 = base / "ws10"; ws10.mkdir()
        (ws10 / "a.txt").write_text("a", encoding="utf-8")
        f10 = FileManager(ws10)
        f10.move("a.txt", "b.txt")
        jp = ws10 / ".undo_journal.json"
        good = jp.read_text(encoding="utf-8")
        entries = json.loads(good)
        entries.append({"action": "create_dir", "path": "rel:newdir", "task": None})
        entries[-1] = f10.sign_disk_entry(entries[-1])   # كاتب شرعي يوقّع إدخاله (إدخال غير موقّع في سجل موقّع يُرفض)
        (ws10 / "newdir").mkdir()
        tmpj = ws10 / ".undo_journal.json.tmp"
        tmpj.write_text(json.dumps(entries), encoding="utf-8")
        f10b = FileManager(ws10)
        assert len(f10b.journal) == 2 and not tmpj.exists(), f10b.journal
        tmpj.write_text('[{"action": "mo', encoding="utf-8")          # كتابة مقطوعة
        f10c = FileManager(ws10)
        assert len(f10c.journal) == 2 and not tmpj.exists()

        # 11. أسماء بمحرف surrogate لا تكسر السجل ولا الدفتر
        ws11 = base / "ws11"; ws11.mkdir()
        try:
            weird = ws11 / "bad\udcff.txt"
            os.close(os.open(os.fsencode(str(weird)), os.O_CREAT | os.O_WRONLY))
            have = True
        except Exception:
            have = False
        led11 = SQLiteLedger(base / "l11.db", initial_balance=50, spending_limit=50)
        led11.record_task_start("W", "x", 1)
        led11.record_task_failure("W", reason="فشل في bad\udcff.txt", is_platform_failure=False)
        if have:
            f11 = FileManager(ws11)
            f11.rename(os.fsdecode(os.fsencode("bad\udcff.txt")), "ok.txt")
            assert (ws11 / "ok.txt").exists()
            json.loads((ws11 / ".undo_journal.json").read_text(encoding="utf-8"))   # JSON صالح ASCII

        # 12. إعادة معرّف مهمة بعد إيقاف مبكر لا تكسر تراجع المهمة الأولى
        ws12 = base / "ws12"; ws12.mkdir()
        (ws12 / "a.txt").write_text("a", encoding="utf-8")
        led12 = SQLiteLedger(base / "l12.db", initial_balance=500, spending_limit=500)
        ks = KillSwitch()
        r12 = TaskRunner(ws12, RuleProvider(), led12, kill_switch=ks)
        assert r12.run("A", "x", plan=[PlanStep(1, "move", "a.txt", {"dest": "b.txt"})]).success
        ks.trigger("t")
        assert not r12.run("A", "x", plan=[PlanStep(1, "create_dir", "d")]).success
        ks.reset()
        assert r12.undo_task("A") == 1 and (ws12 / "a.txt").exists()

        # 13. دليل الكلفة: مجلد شبكي بعد الحلّ يُرفض
        import assistant.config as cfg
        with mock.patch.object(cfg, "is_unc_like", lambda v: True):
            try:
                cfg.validate_workspace(str(ws12)); raise AssertionError("قُبل مجلد شبكي")
            except cfg.WorkspaceError:
                pass

    # 14. سكربت المزامنة يفحص التعارضات
    raw = (ROOT / "tools" / "sync_repo.bat").read_bytes()
    assert b"--diff-filter=U" in raw and raw.count(b"\r\n") == raw.count(b"\n")

    print("PASS: test_b26 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b26", file=sys.stderr)
    sys.exit(1)
