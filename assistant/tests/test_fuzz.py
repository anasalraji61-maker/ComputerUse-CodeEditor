"""اختبار خصائص (fuzz حتمي): أي طلب عربي عشوائي على مجلد بأسماء غريبة لا يخرج عن الجذر ولا ينهار،
وبعد التراجع عن كل المهام يعود المجلد إلى حالته الأصلية بالضبط (أسماء ومحتوى)."""
import hashlib
import os
import random
import sys
import tempfile
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.rules import RuleProvider
    from assistant.planner import PlanParseError
    from assistant.runner import TaskRunner
    from assistant.ledger import SQLiteLedger

    def tree(root: pathlib.Path):
        out = {}
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if rel.split("/")[0] == ".trash" or rel.startswith(".undo_journal"):
                continue
            out[rel] = ("dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest())
        return out

    NAMES = ["a.txt", "b.pdf", "c.JPG", "d.mp3", "تقرير.docx", "صورة 1.png", "notes.tmp", "x.tmp", "archive.zip", "noext",
             "dup1.txt", "dup2.txt", "report (1).pdf", "my file.xlsx"]
    DIRS = ["docs", "Pics", "مجلد", "Backup"]
    VERBS = ["رتب الملفات حسب النوع", "رتّب الملفات حسب الشهر", "رتب حسب السنة", "انقل ملفات pdf إلى مستندات",
             "انقل الصور إلى Pics", "ضع ملفات tmp في قمامة", "احذف ملفات tmp", "شيل {n}", "انقل {n} إلى {d}",
             "انسخ {n} إلى {d}", "انقل المكررات", "احذف المكررات", "أعد تسمية ملفات {d} بإضافة البادئة 2026_",
             "أعد تسمية الملفات بإضافة التاريخ", "أنشئ مجلد {d}", "أنشئ مشروع demo", "افحص {n}", "افحص المجلد",
             "احذف", "انقل", "../../etc/passwd", "احذف ../x", "انقل {n} إلى ..", "انقل {n} إلى C:\\Windows",
             "انسخ {n} إلى CON", "أنشئ مجلد a:b", "رتب", "ضع {n} في {d}", "تخلص من {n}"]

    rnd = random.Random(20261014)
    changed = 0
    failed_runs = 0
    unexpected = []
    for it in range(120):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp).resolve()
            root = base / "ws"
            root.mkdir()
            sentinel = base / "outside.txt"
            sentinel.write_text("do-not-touch", encoding="utf-8")
            for n in rnd.sample(NAMES, rnd.randint(0, len(NAMES))):
                (root / n).write_text(rnd.choice(["same", "same", n, "x" * rnd.randint(1, 50)]), encoding="utf-8")
            for d in rnd.sample(DIRS, rnd.randint(0, 2)):
                (root / d).mkdir(exist_ok=True)
                (root / d / "inner.txt").write_text("inner " + d, encoding="utf-8")
            before = tree(root)
            led = SQLiteLedger(base / "l.db", initial_balance=10_000, spending_limit=10_000)
            runner = TaskRunner(root, RuleProvider(), led, approver=lambda s: True)
            done = []
            for k in range(rnd.randint(1, 3)):
                text = rnd.choice(VERBS).format(n=rnd.choice(NAMES), d=rnd.choice(DIRS))
                try:
                    rep = runner.run(f"t{it}_{k}", text)
                    done.append(f"t{it}_{k}")
                    changed += 1 if rep.undo_available else 0
                    failed_runs += 0 if rep.success else 1
                    if not rep.success and not (rep.failures and (rep.failures[0].startswith('خطة غير صالحة') or rep.failures[0].startswith('مسار مرفوض قبل التنفيذ'))):
                        unexpected.append((text, rep.failures[:2]))
                except PlanParseError:
                    pass
                assert sentinel.read_text(encoding="utf-8") == "do-not-touch", text
                assert all(p.resolve().is_relative_to(root) for p in root.rglob("*") if not p.is_symlink()), text
            # تراجع كل المهام بالترتيب العكسي ← الحالة الأصلية
            for tid in reversed(done):
                runner.undo_task(tid)
            after = tree(root)
            assert after == before, (it, sorted(set(before) ^ set(after))[:6])

    assert not unexpected, unexpected[:3]  # الفشل الوحيد المقبول: خطة غير صالحة/سؤال توضيحي؛ لا فشل تنفيذ أو تدقيق
    assert changed >= 60, f"الاختبار لا يُحدث تغييرات كافية ليكون ذا معنى: {changed}"
    print(f"PASS: test_fuzz completed successfully (120 scenarios, {changed} mutating runs, {failed_runs} failed runs)")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_fuzz", file=sys.stderr)
    sys.exit(1)
