"""اختبارات B24 (أدوات/مزوّدات): فحص الأسرار لكل أنواع الملفات، بصمة الاختبارات تشمل كل الملفات، نداء مفوتَر فاشل يدخل العدّاد،
خطة غير صالحة لا تُخزَّن، مفتاح بفراغات، بوابة القياس، سكربت المزامنة."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.base import ModelProvider
    from assistant.models.http_base import HttpProvider, ProviderConfigError, ProviderError
    from assistant.models.router import ModelRouter

    KEY = "sk-ant-api03-" + "A" * 40

    def scan(tmp_repo):
        import os
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, str(tmp_repo / "assistant" / "tests" / "test_no_secrets.py")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=tmp_repo, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "r"
        (repo / "assistant" / "tests").mkdir(parents=True)
        shutil.copy(ROOT / "assistant" / "tests" / "test_no_secrets.py", repo / "assistant" / "tests" / "test_no_secrets.py")
        (repo / "ok.md").write_text("لا شيء", encoding="utf-8")
        assert scan(repo)[0] == 0
        # 1. مفتاح في أنواع ملفات كانت تفلت من الفحص (بلا git)
        for name in ("keys.yaml", "run.cmd", "setup.ps1", "conf.sh", "notes.html", "leak.csv"):
            (repo / name).write_text(f"x={KEY}\n", encoding="utf-8")
            rc, out = scan(repo)
            assert rc != 0 and name in out, (name, out)
            (repo / name).unlink()
        # ملف *.env يُرفض بالاسم
        (repo / "prod.env").write_text("A=1", encoding="utf-8")
        assert scan(repo)[0] != 0
        (repo / "prod.env").unlink()
        # 2. مع git: ملف .env محلي متجاهَل لا يُفشل الفحص، لكن مفتاحاً في وثيقة جديدة يفشله
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
        (repo / ".env").write_text("A=1", encoding="utf-8")
        assert scan(repo)[0] == 0, "ملف .env المتجاهَل أسقط الفحص"
        (repo / "docs.md").write_text(KEY, encoding="utf-8")
        assert scan(repo)[0] != 0

    # 3. بصمة run_tests تتغير بتغيّر وثيقة (ليس .py فقط)
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib
    rt = importlib.import_module("run_tests")
    with tempfile.TemporaryDirectory() as tmp:
        old_root = rt.ROOT
        try:
            rt.ROOT = pathlib.Path(tmp)
            (rt.ROOT / "a.py").write_text("x=1", encoding="utf-8")
            (rt.ROOT / "n.md").write_text("one", encoding="utf-8")
            f1 = rt.fingerprint()
            (rt.ROOT / "n.md").write_text("two", encoding="utf-8")
            assert rt.fingerprint() != f1
        finally:
            rt.ROOT = old_root

    # 4. نداء مفوتَر لكنه فاشل يدخل عدّاد الراوتر
    class P(HttpProvider):
        ENV_KEYS = ("X",)
        PROVIDER_LABEL = "t"
        def _build(self, prompt, system_prompt): return "http://x", {}, {}
        def _usage(self, data): return 100, 2000
        def _parse(self, data): raise ProviderError("رد فارغ")
    prov = P("gemini-3.1-pro-preview", api_key="k", transport=lambda *a: (200, b"{}"))
    router = ModelRouter(prov, cache_size=0)
    try:
        router.generate("a")
        raise AssertionError("كان يجب أن يفشل")
    except ProviderError:
        pass
    assert router.totals()["usd"] > 0 and router.totals()["calls"] == 1, router.totals()

    # 5. خطة غير صالحة لا تُخزَّن في الذاكرة المؤقتة
    class Seq(ModelProvider):
        def __init__(self): self.n = 0
        def name(self): return "seq"
        def generate(self, prompt, system_prompt=""):
            self.n += 1
            if self.n == 1:
                return json.dumps({"steps": [{"action_type": "rm_rf", "target": "x"}]})
            return json.dumps({"steps": [{"step_id": 1, "action_type": "create_dir", "target": "d"}]})
    sq = Seq(); r2 = ModelRouter(sq)
    r2.generate("p", "sys"); second = r2.generate("p", "sys")
    assert sq.n == 2 and "create_dir" in second and r2.cache_hits == 0

    # 6. مفتاح بفراغات
    for bad in ("ab cd", "ab\ncd", "عربي"):
        p = P("m", api_key=bad)
        try:
            p._require_key(); raise AssertionError(bad)
        except ProviderConfigError:
            pass
    assert P("m", api_key="  abc123  ")._require_key() == "abc123"

    # 7. بوابة القياس لا تمر بدورة واحدة
    from bench.run_bench import render_markdown
    res = {"provider": "x", "runs": 3, "max_usd": 1, "stopped_for_budget": True,
           "results": {"t1": {"title": "t", "runs": 1, "ok": 1, "steps": 1, "secs": 0.1, "tin": 0, "tout": 0, "usd": 0.0, "failures": []}}}
    md = render_markdown(res)
    assert "لم تتحقق" in md and "تحققت." not in md.replace("لم تتحقق", ""), md

    # 8. سكربت المزامنة: يوقف عند فشل السحب ولا يرفع غير ملف النتائج عند فشل الاختبارات (CRLF محفوظ)
    raw = (ROOT / "tools" / "sync_repo.bat").read_bytes()
    assert b"rebase --abort" in raw and b"git reset -q" in raw and raw.count(b"\r\n") == raw.count(b"\n")

    print("PASS: test_b24 completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_b24", file=sys.stderr)
    sys.exit(1)
