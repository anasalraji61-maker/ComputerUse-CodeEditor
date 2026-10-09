"""اختبار المهام النموذجية الخمس في 20/20 تشغيلاً حتمياً داخل مجلد مؤقت."""
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.ledger import SQLiteLedger
    from assistant.tools.files import FileManager
    from assistant.benchmarks import (
        run_benchmark_pipeline,
        get_task_1_provider,
        get_task_2_provider,
        get_task_3_provider,
        get_task_4_provider,
        get_task_5_provider,
    )

    # تشغيل المهام الـ 5 في 20 دورة حتمية متتالية
    NUM_RUNS = 20

    for run_idx in range(1, NUM_RUNS + 1):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir).resolve()
            db_path = root / "ledger.db"
            ledger = SQLiteLedger(db_path=db_path, initial_balance=500, spending_limit=500)

            # --- المهمة 1: ترتيب التنزيلات ---
            (root / "file1.pdf").write_text("PDF 1 Content", encoding="utf-8")
            (root / "photo.png").write_text("PNG Image Content", encoding="utf-8")
            p1 = get_task_1_provider(root)
            rep1 = run_benchmark_pipeline(f"t1_run_{run_idx}", "رتّب التنزيلات", root, p1, ledger)
            assert rep1.success is True, f"فشلت المهمة 1 في الدورة {run_idx}: {rep1.failures}"
            assert (root / "documents" / "file1.pdf").exists()
            assert (root / "images" / "photo.png").exists()

            # --- المهمة 2: نسخ مجلد مع مطابقة الـ Hash ---
            work_dir = root / "Work"
            work_dir.mkdir(parents=True, exist_ok=True)
            proj_file = work_dir / "project.txt"
            proj_file.write_text("Secret Project Code", encoding="utf-8")
            h2 = FileManager.file_hash(proj_file)
            p2 = get_task_2_provider(root, h2)
            rep2 = run_benchmark_pipeline(f"t2_run_{run_idx}", "انسخ مع الهاش", root, p2, ledger)
            assert rep2.success is True, f"فشلت المهمة 2 في الدورة {run_idx}: {rep2.failures}"
            assert (root / "Backup" / "project.txt").exists()
            assert FileManager.file_hash(root / "Backup" / "project.txt") == h2

            # --- المهمة 3: إعادة تسمية دفعة ملفات ---
            dcim = root / "DCIM"
            dcim.mkdir(parents=True, exist_ok=True)
            (dcim / "img1.jpg").write_text("Photo 1", encoding="utf-8")
            (dcim / "img2.jpg").write_text("Photo 2", encoding="utf-8")
            p3 = get_task_3_provider()
            rep3 = run_benchmark_pipeline(f"t3_run_{run_idx}", "إعادة تسمية الصور", root, p3, ledger)
            assert rep3.success is True, f"فشلت المهمة 3 في الدورة {run_idx}: {rep3.failures}"
            assert (dcim / "2026-10-09_img1.jpg").exists()
            assert (dcim / "2026-10-09_img2.jpg").exists()
            assert not (dcim / "img1.jpg").exists()

            # --- المهمة 4: تحديث برنامج عبر winget بمحاكي ---
            def mock_runner(cmd):
                return 0, "Successfully upgraded Git.Git", ""

            p4 = get_task_4_provider()
            rep4 = run_benchmark_pipeline(
                f"t4_run_{run_idx}",
                "تحديث برنامج Git",
                root,
                p4,
                ledger,
                user_approved=True,
                mock_winget_runner=mock_runner,
            )
            assert rep4.success is True, f"فشلت المهمة 4 في الدورة {run_idx}: {rep4.failures}"

            # --- المهمة 5: إنشاء هيكل مشروع جديد ---
            p5 = get_task_5_provider()
            rep5 = run_benchmark_pipeline(f"t5_run_{run_idx}", "إنشاء مشروع بايثون", root, p5, ledger)
            assert rep5.success is True, f"فشلت المهمة 5 في الدورة {run_idx}: {rep5.failures}"
            assert (root / "my_project" / "src").is_dir()
            assert (root / "my_project" / "docs").is_dir()
            assert (root / "my_project" / "tests").is_dir()

    print(f"PASS: 20/20 runs for all 5 benchmark tasks completed successfully ({NUM_RUNS} runs, 100% success)")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
