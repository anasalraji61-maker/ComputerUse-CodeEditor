"""قياس المهام النموذجية الخمس بمزوّد (وهمي للاختبار أو حقيقي يُشغَّله المالك يدوياً).

التشغيل اليدوي (يستهلك رصيد API فعلياً):
    set ANTHROPIC_API_KEY=...        (أو GEMINI_API_KEY)
    python -m bench.run_bench --provider anthropic --runs 5 --max-usd 1.0

أمان: winget يُحاكى دائماً (لا تحديث حقيقي لأي برنامج)، وكل مهمة في مجلد مؤقت معزول، ولا ذاكرة مؤقتة (cache)
حتى لا تُقاس نتيجة مخزّنة، ويتوقف القياس عند بلوغ --max-usd.
"""
import argparse
import hashlib
import sys
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


def _setup4(r):
    pass


def _check4(r, ctx):
    calls = ctx.get("winget_calls", [])
    ok = any(c[:2] == ["winget", "upgrade"] and "Git.Git" in c for c in calls)
    return [] if ok else ["لم يُستدعَ أمر ترقية Git.Git"]


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


def _totals(provider: ModelProvider) -> Dict[str, Any]:
    fn = getattr(provider, "totals", None)
    return fn() if fn else {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "points": 0.0}


def run_bench(provider: ModelProvider, runs: int = 5, tasks: Optional[List[BenchTask]] = None,
              max_usd: float = 1.0, progress: Callable[[str], None] = lambda s: None) -> Dict[str, Any]:
    tasks = tasks or TASKS
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
                ctx: Dict[str, Any] = {"winget_calls": []}

                def mock_winget(cmd, _ctx=ctx):
                    _ctx["winget_calls"].append(list(cmd))
                    return 0, "Successfully upgraded Git.Git", ""

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
                    r["failures"].append(f"دورة {n}: " + "; ".join(report.failures + check_failures))
                progress(f"{t.key} دورة {n}: {'نجاح' if success else 'فشل'}")
        if stopped_for_budget:
            break
    return {"results": results, "provider": provider.name(), "runs": runs,
            "stopped_for_budget": stopped_for_budget, "max_usd": max_usd, "totals": _totals(provider)}


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
        "| المهمة | الدورات | نسبة النجاح | متوسط الخطوات | متوسط الزمن (ث) | توكنات إدخال | توكنات إخراج | الكلفة ($) | الكلفة (نقطة) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    gate_ok = True
    tot_usd = 0.0
    for key, r in res["results"].items():
        n = max(1, r["runs"])
        rate = 100.0 * r["ok"] / n if r["runs"] else 0.0
        gate_ok &= (r["runs"] > 0 and rate >= 90.0)
        tot_usd += r["usd"]
        L.append(f"| {r['title']} | {r['runs']} | {rate:.0f}% ({r['ok']}/{r['runs']}) | {r['steps'] / n:.1f} | "
                 f"{r['secs'] / n:.2f} | {r['tin']} | {r['tout']} | {r['usd']:.4f} | {r['usd'] / COST_USD_PER_POINT:.2f} |")
    L += ["", f"**إجمالي الكلفة:** {tot_usd:.4f}$ ≈ {tot_usd / COST_USD_PER_POINT:.2f} نقطة.", ""]
    if res["stopped_for_budget"]:
        L.append(f"> توقف القياس مبكراً عند بلوغ سقف الإنفاق {res['max_usd']}$؛ النتائج جزئية.")
    L.append(f"**بوابة المرحلة 1 (≥90% لكل مهمة): {'تحققت' if gate_ok else 'لم تتحقق بعد'}.**")
    fails = [(r["title"], f) for r in res["results"].values() for f in r["failures"]]
    L += ["", "## الأعطال"]
    if fails:
        L += [f"- {title} — {f}" for title, f in fails[:40]]
    else:
        L.append("- لا أعطال.")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="قياس المهام الخمس بمزوّد حقيقي")
    ap.add_argument("--provider", choices=["anthropic", "gemini"], required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--max-usd", type=float, default=1.0)
    ap.add_argument("--out", default=str(ROOT / "docs" / "MEASUREMENT.md"))
    args = ap.parse_args(argv)

    from assistant.models.factory import build_provider
    from assistant.models.http_base import ProviderConfigError
    try:
        provider, note = build_provider({**__import__("os").environ, "COS_PROVIDER": args.provider})
    except ProviderConfigError as exc:
        print(f"خطأ: {exc}", file=sys.stderr)
        return 2
    provider.cache_size = 0  # لا ذاكرة مؤقتة أثناء القياس
    print(note)
    print(f"سيُشغَّل القياس: {args.runs} دورات × 5 مهام، بسقف إنفاق {args.max_usd}$.")
    res = run_bench(provider, runs=args.runs, max_usd=args.max_usd, progress=print)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(res), encoding="utf-8")
    print(f"كُتب التقرير: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
