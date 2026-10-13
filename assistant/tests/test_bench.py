"""اختبار سكربت القياس بمزوّد وهمي: التقرير، بوابة 90%، حدّ الإنفاق، واكتشاف الفشل."""
import hashlib
import json
import sys
import tempfile
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.fake import FakeProvider
    from assistant.models.router import ModelRouter
    from bench.run_bench import run_bench, render_markdown, PROJECT_FILE_CONTENT

    def S(i, a, t, **p):
        return {"step_id": i, "action_type": a, "target": t, "params": p}

    h = hashlib.sha256(PROJECT_FILE_CONTENT.encode()).hexdigest()
    GOOD = {
        "رتّب الملفات": [S(1, "create_dir", "documents"), S(2, "create_dir", "images"),
                         S(3, "move", "file1.pdf", dest="documents/file1.pdf"), S(4, "move", "photo.png", dest="images/photo.png")],
        "انسخ الملف": [S(1, "create_dir", "Backup"), S(2, "copy", "Work/project.txt", dest="Backup/project.txt", expected_hash=h)],
        "أعد تسمية": [S(1, "rename", "DCIM/img1.jpg", new_name="2026-10-09_img1.jpg"),
                      S(2, "rename", "DCIM/img2.jpg", new_name="2026-10-09_img2.jpg")],
        "حدّث برنامج Git": [S(1, "winget_upgrade", "Git.Git")],
        "أنشئ هيكل مشروع": [S(1, "create_dir", "my_project/src"), S(2, "create_dir", "my_project/docs"), S(3, "create_dir", "my_project/tests")],
    }

    def fake(plans):
        return FakeProvider(responses={k: json.dumps({"steps": v}) for k, v in plans.items()})

    # 1. كل شيء صحيح: 100% والبوابة تتحقق
    res = run_bench(fake(GOOD), runs=3)
    assert all(r["ok"] == 3 and r["runs"] == 3 for r in res["results"].values()), res["results"]
    md = render_markdown(res)
    assert "تحققت" in md and "لم تتحقق" not in md and "100%" in md and "لا أعطال" in md

    # 2. خطة خاطئة لمهمة واحدة (تنقص docs وtests): تُسجَّل فاشلة ولو قال المنفّذ «تم»
    bad = dict(GOOD)
    bad["أنشئ هيكل مشروع"] = [S(1, "create_dir", "my_project/src")]
    res = run_bench(fake(bad), runs=2)
    assert res["results"]["t5"]["ok"] == 0 and res["results"]["t1"]["ok"] == 2
    md = render_markdown(res)
    assert "لم تتحقق" in md and "my_project/docs" in md

    # 3. رد غير صالح من النموذج = فشل بلا أي نجاح مزيّف
    res = run_bench(FakeProvider(default_response="لا أعرف"), runs=1)
    assert all(r["ok"] == 0 for r in res["results"].values())

    # 4. حدّ الإنفاق يوقف القياس مبكراً
    class Costly(FakeProvider):
        model = "claude-haiku-5-5"
        last_usage = (1_000_000, 0)  # 0.10$ لكل استدعاء
    inner = Costly(responses={k: json.dumps({"steps": v}) for k, v in GOOD.items()})
    router = ModelRouter(inner, cache_size=0)
    res = run_bench(router, runs=5, max_usd=0.25)
    assert res["stopped_for_budget"] is True
    ran = sum(r["runs"] for r in res["results"].values())
    assert 2 <= ran <= 4, ran
    assert "توقف القياس مبكراً" in render_markdown(res)

    print("PASS: test_bench completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_bench", file=sys.stderr)
    sys.exit(1)
