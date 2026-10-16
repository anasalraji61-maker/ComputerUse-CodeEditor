"""قياس المهام النموذجية الخمس بمزوّد (وهمي للاختبار أو حقيقي يُشغَّله المالك يدوياً).

التشغيل اليدوي (يستهلك رصيد API فعلياً):
    set ANTHROPIC_API_KEY=...        (أو GEMINI_API_KEY)
    python -m bench.run_bench --provider anthropic            (الافتراضي: 20 دورة = بوابة SPEC، والسقف يُحسب من التقدير)
    python -m bench.run_bench --provider gemini --runs 5 --max-usd 1.0
يطبع تقدير الكلفة والسقف قبل البدء ويطلب تأكيداً (أو --yes).

أمان: winget يُحاكى دائماً (لا تحديث حقيقي لأي برنامج)، وكل مهمة في مجلد مؤقت معزول، ولا ذاكرة مؤقتة (cache)
حتى لا تُقاس نتيجة مخزّنة، ويتوقف القياس عند بلوغ --max-usd.
"""
import argparse
import hashlib
import sys
import math
import tempfile
import time
import pathlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assistant.ledger import SQLiteLedger
from assistant.models.base import ModelProvider
from assistant.models.pricing import PRICES_VERIFIED_ON, COST_USD_PER_POINT
from assistant.runner import TaskRunner

PROJECT_FILE_CONTENT = "Secret Project Code"


@dataclass
class BenchTask:
    key: str
    title: str
    objective: str
    setup: Callable[[pathlib.Path], None]
    check: Callable[[pathlib.Path, Dict[str, Any]], List[str]]  # يعيد قائمة أسباب الفشل (فارغة = نجاح)
    approve: bool = False


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup1(r):
    (r / "file1.pdf").write_text("PDF 1 Content", encoding="utf-8")
    (r / "photo.png").write_text("PNG Image Content", encoding="utf-8")


def _check1(r, ctx):
    f = []
    if not (r / "documents" / "file1.pdf").is_file() or (r / "documents" / "file1.pdf").read_text(encoding="utf-8") != "PDF 1 Content":
        f.append("file1.pdf ليس في documents بمحتواه الأصلي")
    if not (r / "images" / "photo.png").is_file() or (r / "images" / "photo.png").read_text(encoding="utf-8") != "PNG Image Content":
        f.append("photo.png ليس في images بمحتواه الأصلي")
    if (r / "file1.pdf").exists() or (r / "photo.png").exists():
        f.append("بقيت نسخ في الجذر")
    return f


def _setup2(r):
    (r / "Work").mkdir()
    (r / "Work" / "project.txt").write_text(PROJECT_FILE_CONTENT, encoding="utf-8")


def _check2(r, ctx):
    f = []
    src, dst = r / "Work" / "project.txt", r / "Backup" / "project.txt"
    if not src.is_file():
        f.append("اختفى الأصل")
    if not dst.is_file() or _sha(dst) != hashlib.sha256(PROJECT_FILE_CONTENT.encode()).hexdigest():
        f.append("النسخة غير موجودة أو بصمتها لا تطابق")
    return f


def _setup3(r):
    (r / "DCIM").mkdir()
    (r / "DCIM" / "img1.jpg").write_text("Photo 1", encoding="utf-8")
    (r / "DCIM" / "img2.jpg").write_text("Photo 2", encoding="utf-8")


def _check3(r, ctx):
    f = []
    for i in (1, 2):
        p = r / "DCIM" / f"2026-10-09_img{i}.jpg"
        if not p.is_file() or p.read_text(encoding="utf-8") != f"Photo {i}":
            f.append(f"2026-10-09_img{i}.jpg غير موجود أو محتواه تغيّر")
        if (r / "DCIM" / f"img{i}.jpg").exists():
            f.append(f"img{i}.jpg ما زال بالاسم القديم")
    return f


class WingetSim:
    """محاكي winget بحالة: حزمة Git.Git مثبّتة بإصدار قديم ولها إصدار أحدث متاح. يعيد جداول وأكواد خروج بشكل winget الحقيقي
    (الأكواد من returnCodes.md: 0x8A150014 غير مثبّتة، 0x8A15002B لا ترقية)، وتغيّر الترقية الصحيحة إصدار الحزمة فعلاً.
    خلافاً للمحاكاة القديمة (0 و«Successfully upgraded» لكل أمر) يمكن أن يفشل الفحص حين يكون السلوك خاطئاً.
    lying=True: يدّعي النجاح (0) دون تغيير الإصدار، لاختبار أن الفحص يكتشف الكذب."""
    NOT_FOUND = 0x8A150014
    NOT_APPLICABLE = 0x8A15002B

    def __init__(self, lying: bool = False, installed=None):
        # id -> [name, version, latest]
        self.pkgs = {k: list(v) for k, v in (installed or {"Git.Git": ("Git", "2.45.0", "2.46.0")}).items()}
        self.lying = lying
        self.calls: List[List[str]] = []

    def _table(self, ids) -> str:
        rows = [(self.pkgs[i][0], i, self.pkgs[i][1], self.pkgs[i][2] if self.pkgs[i][2] != self.pkgs[i][1] else "") for i in ids]
        head = ["Name", "Id", "Version", "Available", "Source"]
        body = [[n, i, v, a, "winget"] for n, i, v, a in rows]
        w = [max(len(h), *(len(r[k]) for r in body)) for k, h in enumerate(head)]
        fmt = lambda r: " ".join(c.ljust(w[k]) for k, c in enumerate(r)).rstrip()
        return fmt(head) + "\n" + "-" * (sum(w) + 4) + "\n" + "\n".join(fmt(r) for r in body) + "\n"

    def version(self, pkg_id: str) -> Optional[str]:
        p = self.pkgs.get(pkg_id)
        return p[1] if p else None

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if cmd[:2] == ["winget", "list"] and "-q" in cmd:
            q = cmd[cmd.index("-q") + 1].lower()
            ids = [i for i in self.pkgs if q in i.lower()]
            if not ids:
                return self.NOT_FOUND, "No installed package found matching input criteria.\n", ""
            return 0, self._table(ids), ""
        if cmd[:2] == ["winget", "upgrade"] and "--id" in cmd:
            pid = cmd[cmd.index("--id") + 1]
            if "--exact" not in cmd or pid not in self.pkgs:
                return self.NOT_FOUND, "No installed package found matching input criteria.\n", ""
            p = self.pkgs[pid]
            if p[1] == p[2]:
                return self.NOT_APPLICABLE, "No available upgrade found.\n", ""
            if not self.lying:
                p[1] = p[2]
            return 0, f"Found {p[0]} [{pid}] Version {p[2]}\nSuccessfully installed\n", ""
        return 1, "", "unsupported in simulator: " + " ".join(cmd)


def _setup4(r):
    pass


def _check4(r, ctx):
    """يفحص حالة المحاكي لا نية الخطة: الإصدار تغيّر فعلاً، والأوامر هي ترقية Git.Git بالمعرّف الدقيق وفحص list فقط."""
    sim = ctx.get("winget")
    if sim is None:
        return ["لا محاكي winget في السياق"]
    f = []
    if sim.version("Git.Git") != "2.46.0":
        f.append(f"إصدار Git.Git بعد التنفيذ {sim.version('Git.Git')} لا 2.46.0 (لم تحدث ترقية فعلية)")
    ups = [c for c in sim.calls if c[:2] == ["winget", "upgrade"]]
    if not ups:
        f.append("لم يُستدعَ أمر ترقية")
    for c in ups:
        if c[c.index("--id") + 1:c.index("--id") + 2] != ["Git.Git"] or "--exact" not in c:
            f.append("أمر ترقية لحزمة غير Git.Git أو بلا --exact: " + " ".join(c[:6]))
    for c in sim.calls:
        if c[:2] not in (["winget", "upgrade"], ["winget", "list"]):
            f.append("أمر winget غير متوقع: " + " ".join(c[:3]))
    return f


def _setup5(r):
    pass


def _check5(r, ctx):
    return [f"المجلد my_project/{d} غير موجود" for d in ("src", "docs", "tests") if not (r / "my_project" / d).is_dir()]


TASKS: List[BenchTask] = [
    BenchTask("t1", "ترتيب التنزيلات حسب النوع",
              "رتّب الملفات في مجلد العمل حسب النوع: انقل file1.pdf إلى مجلد documents وphoto.png إلى مجلد images (أنشئ المجلدين).",
              _setup1, _check1),
    BenchTask("t2", "نسخ مجلد مع تحقق الهاش",
              "انسخ الملف Work/project.txt إلى Backup/project.txt وتحقق من بصمته (أنشئ مجلد Backup).",
              _setup2, _check2),
    BenchTask("t3", "إعادة تسمية دفعة ملفات",
              "أعد تسمية الملفين img1.jpg وimg2.jpg داخل DCIM بإضافة البادئة 2026-10-09_ إلى بداية كل اسم.",
              _setup3, _check3),
    BenchTask("t4", "تحديث برنامج عبر winget",
              "حدّث برنامج Git (المعرّف Git.Git) باستعمال winget.",
              _setup4, _check4, approve=True),
    BenchTask("t5", "هيكل مجلدات مشروع",
              "أنشئ هيكل مشروع جديد باسم my_project يحتوي المجلدات src وdocs وtests.",
              _setup5, _check5),
]


DEFAULT_RUNS = 20          # بوابة SPEC: 20 دورة لكل مهمة
# تقدير متحفظ لنداءات المهمة الواحدة: نداء توضيح صغير + نداء تخطيط كبير (توكنات إدخال/إخراج)
_EST_SMALL = (3000, 300)
_EST_LARGE = (6000, 1200)


def estimate_cost(provider: str, runs: int, env: Optional[Dict[str, str]] = None) -> Dict[str, float]:
    """تقدير كلفة القياس بالدولار من أسعار النماذج الافتراضية (أو COS_SMALL_MODEL/COS_LARGE_MODEL): per_task وtotal وcap المقترح."""
    from assistant.models.factory import DEFAULTS
    from assistant.models.pricing import cost_usd
    env = env or {}
    small = env.get("COS_SMALL_MODEL") or DEFAULTS[provider][0]
    large = env.get("COS_LARGE_MODEL") or DEFAULTS[provider][1] or small
    if provider == "ollama":   # محلي: بلا كلفة؛ السقف رمزي (يُتجاهل فعلياً)
        return {"per_task": 0.0, "total": 0.0, "cap": 1.0}
    per_task = cost_usd(small, *_EST_SMALL) + cost_usd(large, *_EST_LARGE)
    total = per_task * len(TASKS) * max(0, runs)
    cap = max(1.0, math.ceil(total * 2 * 2) / 2)   # ضعف التقدير مقرَّباً لأعلى نصف دولار، بحد أدنى 1$
    return {"per_task": per_task, "total": total, "cap": cap}


def _scrub(text: str, tmp: str) -> str:
    """يحذف المسارات المحلية (المؤقتة ومجلد المستخدم) من نص يُكتب في تقرير يُرفع إلى مستودع عام."""
    for real, shown in ((str(pathlib.Path(tmp).resolve()), "<tmp>"), (str(pathlib.Path(tmp)), "<tmp>"),
                        (str(pathlib.Path.home()), "~")):
        if real:
            text = text.replace(real, shown)
    return text


def _totals(provider: ModelProvider) -> Dict[str, Any]:
    fn = getattr(provider, "totals", None)
    return fn() if fn else {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "points": 0.0}


def run_bench(provider: ModelProvider, runs: int = 5, tasks: Optional[List[BenchTask]] = None,
              max_usd: float = 1.0, progress: Callable[[str], None] = lambda s: None,
              winget_factory: Callable[[], "WingetSim"] = WingetSim) -> Dict[str, Any]:
    tasks = tasks or TASKS
    if not (isinstance(max_usd, (int, float)) and math.isfinite(max_usd) and max_usd > 0):
        raise ValueError("--max-usd يجب أن يكون عدداً موجباً محدوداً.")
    results: Dict[str, Dict[str, Any]] = {
        t.key: {"title": t.title, "runs": 0, "ok": 0, "steps": 0, "secs": 0.0,
                "tin": 0, "tout": 0, "usd": 0.0, "failures": []} for t in tasks
    }
    stopped_for_budget = False
    for n in range(1, runs + 1):
        for t in tasks:
            if _totals(provider)["usd"] >= max_usd:
                stopped_for_budget = True
                break
            with tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp).resolve() / "ws"
                root.mkdir()
                t.setup(root)
                sim = winget_factory()
                ctx: Dict[str, Any] = {"winget": sim, "winget_calls": sim.calls}
                mock_winget = sim

                ledger = SQLiteLedger(pathlib.Path(tmp).resolve() / "l.db", initial_balance=1000, spending_limit=1000)
                runner = TaskRunner(root, provider, ledger, approver=(lambda s: True) if t.approve else None,
                                    winget_runner=mock_winget)
                before = _totals(provider)
                t0 = time.time()
                report = runner.run(f"{t.key}_run{n}", t.objective)
                secs = time.time() - t0
                after = _totals(provider)
                check_failures = t.check(root, ctx) if report.success else []
                success = report.success and not check_failures
                r = results[t.key]
                r["runs"] += 1
                r["ok"] += 1 if success else 0
                r["steps"] += len(report.executed_steps)
                r["secs"] += secs
                r["tin"] += after["input_tokens"] - before["input_tokens"]
                r["tout"] += after["output_tokens"] - before["output_tokens"]
                r["usd"] += after["usd"] - before["usd"]
                if not success:
                    r["failures"].append(_scrub(f"دورة {n}: " + "; ".join(report.failures + check_failures), tmp))
                progress(f"{t.key} دورة {n}: {'نجاح' if success else 'فشل'}")
        if stopped_for_budget:
            break
    return {"results": results, "provider": provider.name(), "runs": runs,
            "stopped_for_budget": stopped_for_budget, "max_usd": max_usd, "totals": _totals(provider)}


GATE_RATE = 90.0           # بوابة SPEC: نسبة النجاح (التقدير النقطي) لا تقل عن 90% لكل مهمة
MIN_GATE_RUNS = 3          # دورة أو دورتان لا تثبتان بوابة مهما كانت نتيجتهما


def wilson_lower(ok: int, n: int, z: float = 1.96) -> float:
    """الحد الأدنى لفاصل ويلسون (95% افتراضياً) لنسبة نجاح ok من n، كنسبة مئوية. n=0 → 0."""
    if n <= 0:
        return 0.0
    ok = min(max(0, ok), n)
    p = ok / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return 100.0 * max(0.0, (centre - margin) / denom)


def evaluate(res: Dict[str, Any]) -> Dict[str, Any]:
    """لكل مهمة: نسبة النجاح، حد ويلسون الأدنى 95%، وPASS/FAIL مقابل بوابة SPEC (تقدير نقطي >= 90% مع دورات كافية).
    الحد الأدنى لويلسون معلومة إضافية للقرار لا شرط في البوابة. gate_ok فقط إن نجحت كل المهام."""
    required = max(MIN_GATE_RUNS, int(res.get("runs", MIN_GATE_RUNS)))
    tasks: Dict[str, Dict[str, Any]] = {}
    for key, r in res["results"].items():
        n = r["runs"]
        rate = 100.0 * r["ok"] / n if n else 0.0
        enough = n >= required
        tasks[key] = {"title": r["title"], "runs": n, "ok": r["ok"], "rate": rate, "wilson_lower": wilson_lower(r["ok"], n),
                      "enough_runs": enough, "passed": bool(enough and rate >= GATE_RATE)}
    return {"required_runs": required, "tasks": tasks, "gate_ok": bool(tasks) and all(t["passed"] for t in tasks.values())}


def render_markdown(res: Dict[str, Any]) -> str:
    L = [
        "# تقرير القياس (MEASUREMENT)",
        "",
        f"- التاريخ: {date.today().isoformat()}",
        f"- المزوّد: `{res['provider']}`",
        f"- عدد الدورات لكل مهمة (المطلوب): {res['runs']}",
        f"- أسعار التوكنات مأخوذة من إعدادات المشروع (تحقق منها بتاريخ {PRICES_VERIFIED_ON})، وكلفة النقطة {COST_USD_PER_POINT}$.",
        "- النجاح = تقرير المنظومة «تم» **و**فحص مستقل لحالة القرص بعد التنفيذ. winget محاكى دائماً.",
        "",
        "| المهمة | الدورات | نسبة النجاح | حد ويلسون الأدنى 95% | البوابة | متوسط الخطوات | متوسط الزمن (ث) | توكنات إدخال | توكنات إخراج | الكلفة ($) | الكلفة (نقطة) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    ev = evaluate(res)
    gate_ok = ev["gate_ok"]
    tot_usd = 0.0
    for key, r in res["results"].items():
        n = max(1, r["runs"])
        t = ev["tasks"][key]
        tot_usd += r["usd"]
        L.append(f"| {r['title']} | {r['runs']} | {t['rate']:.0f}% ({r['ok']}/{r['runs']}) | {t['wilson_lower']:.0f}% | "
                 f"{'PASS' if t['passed'] else 'FAIL'}{'' if t['enough_runs'] else ' (دورات غير كافية)'} | {r['steps'] / n:.1f} | "
                 f"{r['secs'] / n:.2f} | {r['tin']} | {r['tout']} | {r['usd']:.4f} | {r['usd'] / COST_USD_PER_POINT:.2f} |")
    L += ["", f"**إجمالي الكلفة:** {tot_usd:.4f}$ ≈ {tot_usd / COST_USD_PER_POINT:.2f} نقطة.", ""]
    if res["stopped_for_budget"]:
        L.append(f"> توقف القياس مبكراً عند بلوغ سقف الإنفاق {res['max_usd']}$؛ النتائج جزئية.")
    L.append(f"**بوابة المرحلة 1 (≥90% لكل مهمة بتقدير نقطي، وبعدد دورات لا يقل عن {ev['required_runs']}): "
             f"{'تحققت (PASS)' if gate_ok else 'لم تتحقق بعد (FAIL)'}.**")
    L.append("حد ويلسون الأدنى يبيّن مدى الاطمئنان إحصائياً (مثلاً 20/20 ≈ 84% و10/10 ≈ 72%) ولا يدخل في شرط البوابة.")
    fails = [(r["title"], f) for r in res["results"].values() for f in r["failures"]]
    L += ["", "## الأعطال"]
    if fails:
        L += [f"- {title} — {f}" for title, f in fails[:40]]
    else:
        L.append("- لا أعطال.")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    for _s in (sys.stdout, sys.stderr):   # أنابيب/كونسول غير UTF-8 (cp1252) أو بلا كونسول: الطباعة العربية لا تُسقط الأداة
        if _s is not None and hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(description="قياس المهام الخمس بمزوّد حقيقي")
    ap.add_argument("--provider", choices=["anthropic", "gemini", "openai", "ollama"], required=True)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="دورات لكل مهمة (الافتراضي 20 = بوابة SPEC)")
    ap.add_argument("--max-usd", type=float, default=None, help="سقف الإنفاق؛ الافتراضي ضعف التقدير (حد أدنى 1$)")
    ap.add_argument("--yes", action="store_true", help="تأكيد صريح بإنفاق الرصيد بلا سؤال تفاعلي")
    ap.add_argument("--ask-key", action="store_true", help="اطلب المفتاح بإدخال مخفي (لا يُعرض ولا يُحفظ)")
    ap.add_argument("--with-rules", action="store_true", help="قياس الوضع المختلط (القواعد أولاً) بدل النموذج وحده")
    ap.add_argument("--write-doc", action="store_true", help="اكتب التقرير في --out (الافتراضي docs/MEASUREMENT.md). بدونه يُطبع فقط ولا يُكتب شيء")
    ap.add_argument("--out", default=str(ROOT / "docs" / "MEASUREMENT.md"), help="وجهة الكتابة عند --write-doc (لا يُنفَّذ git commit أبداً)")
    args = ap.parse_args(argv)

    import os
    from assistant.models.factory import build_provider
    from assistant.models.http_base import ProviderConfigError
    env = {**os.environ, "COS_PROVIDER": args.provider}
    if args.ask_key:
        import getpass
        var = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}.get(args.provider)
        key = getpass.getpass(f"Enter {args.provider} API key (hidden, never saved): ").strip() if var else ""   # ollama: بلا مفتاح
        if key:
            env[var] = key  # يبقى في ذاكرة هذه العملية فقط
    try:
        provider, note = build_provider(env)
    except ProviderConfigError as exc:
        print(f"خطأ: {exc}", file=sys.stderr)
        return 2
    if not args.with_rules and hasattr(provider, "fallback"):
        provider = provider.fallback  # نقيس النموذج وحده لا القواعد المحلية
    provider.cache_size = 0  # لا ذاكرة مؤقتة أثناء القياس
    print(note)
    est = estimate_cost(args.provider, args.runs, env)
    max_usd = args.max_usd if args.max_usd is not None else est["cap"]
    print(f"سيُشغَّل القياس: {args.runs} دورات × {len(TASKS)} مهام بنموذج حقيقي.")
    if args.provider == "ollama":
        print("التقدير: 0$ (نموذج محلي).")
    else:
        print(f"التقدير: ~{est['per_task']:.4f}$ للمهمة الواحدة، ~{est['total']:.2f}$ للقياس كله؛ سقف الإنفاق {max_usd:.2f}$ (يتوقف عنده).")
    print("نموذج محلي: بلا كلفة، لكنه أبطأ بكثير (قد يستغرق القياس ساعات)." if args.provider == "ollama" else "هذا يستهلك رصيد API حقيقياً.")
    if not args.yes:
        if not sys.stdin or not sys.stdin.isatty():
            print("لم يُؤكَّد الإنفاق: أضف --yes للتشغيل بلا تفاعل.", file=sys.stderr)
            return 3
        if input("اكتب y للمتابعة وإنفاق الرصيد (أي شيء آخر يلغي): ").strip().lower() not in ("y", "yes", "ن", "نعم"):
            print("أُلغي القياس ولم يُنفق شيء.")
            return 3
    res = run_bench(provider, runs=args.runs, max_usd=max_usd, progress=print)
    md = render_markdown(res)
    ev = evaluate(res)
    print()
    for key, t in ev["tasks"].items():
        print(f"{key}: {t['ok']}/{t['runs']} = {t['rate']:.0f}% (ويلسون 95% الأدنى {t['wilson_lower']:.0f}%) {'PASS' if t['passed'] else 'FAIL'}")
    for r in res["results"].values():
        for f in r["failures"][:5]:
            print(f"  فشل - {r['title']}: {f}")
    print(f"البوابة الكلية: {'PASS' if ev['gate_ok'] else 'FAIL'}")
    if args.write_doc:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"كُتب التقرير: {out} (لم يُنفَّذ commit؛ راجعه ثم ارفعه بنفسك)")
    else:
        print("لم يُكتب أي ملف (أضف --write-doc لحفظ التقرير في docs/MEASUREMENT.md). التقرير:\n")
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
