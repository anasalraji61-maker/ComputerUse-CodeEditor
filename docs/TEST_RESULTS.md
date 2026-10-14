# نتائج الاختبارات الفعلية (آلية)

آخر تشغيل: 2026-10-14 09:36:12 — 23 ملف، فشل: 2

هذه النتائج تُنتَج على لابتوب المالك. لا تدّعِ نجاحاً غير مذكور هنا.

| الملف | النتيجة | الزمن |
|---|---|---|
| `assistant/tests/test_b6.py` | PASS | 1.3s |
| `assistant/tests/test_b8.py` | **FAIL** | 0.1s |
| `assistant/tests/test_bench.py` | PASS | 0.9s |
| `assistant/tests/test_benchmarks_20runs.py` | PASS | 1.8s |
| `assistant/tests/test_budget.py` | PASS | 0.1s |
| `assistant/tests/test_config.py` | **FAIL** | 0.1s |
| `assistant/tests/test_controller.py` | PASS | 0.5s |
| `assistant/tests/test_deliberate_failures.py` | PASS | 0.1s |
| `assistant/tests/test_files.py` | PASS | 0.1s |
| `assistant/tests/test_ledger.py` | PASS | 0.5s |
| `assistant/tests/test_models.py` | PASS | 0.1s |
| `assistant/tests/test_no_secrets.py` | PASS | 0.1s |
| `assistant/tests/test_planner.py` | PASS | 0.1s |
| `assistant/tests/test_planner_executor_verifier.py` | PASS | 0.1s |
| `assistant/tests/test_providers.py` | PASS | 0.1s |
| `assistant/tests/test_router.py` | PASS | 0.1s |
| `assistant/tests/test_rules.py` | PASS | 0.7s |
| `assistant/tests/test_runner.py` | PASS | 0.4s |
| `assistant/tests/test_safety.py` | PASS | 0.1s |
| `assistant/tests/test_scripts.py` | PASS | 0.0s |
| `assistant/tests/test_shell.py` | PASS | 0.1s |
| `assistant/tests/test_smoke.py` | PASS | 0.1s |
| `assistant/tests/test_ui_widget.py` | PASS | 2.0s |

### assistant/tests/test_b6.py
```
PASS: test_b6 completed successfully
```
### assistant/tests/test_b8.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b8.py", line 50, in <module>
    assert cfg.current_workspace().name == "COS_Workspace"
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError
FAIL: test_b8
```
### assistant/tests/test_bench.py
```
PASS: test_bench completed successfully
```
### assistant/tests/test_benchmarks_20runs.py
```
PASS: 20/20 runs for all 5 benchmark tasks completed successfully (20 runs, 100% success)
```
### assistant/tests/test_budget.py
```
PASS: test_budget completed successfully
```
### assistant/tests/test_config.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_config.py", line 42, in <module>
    rejects(blocked)
    ~~~~~~~^^^^^^^^^
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_config.py", line 29, in rejects
    raise AssertionError(f"كان يجب رفض {p!r} {why}")
AssertionError: كان يجب رفض '/etc' 
FAIL: test_config
```
### assistant/tests/test_controller.py
```
PASS: test_controller completed successfully
```
### assistant/tests/test_deliberate_failures.py
```
PASS: test_deliberate_failures completed successfully (all deliberate failures correctly reported as 'فشل')
```
### assistant/tests/test_files.py
```
SKIP: symlink creation requires admin/elevated privileges on this OS
PASS: test_files completed successfully
```
### assistant/tests/test_ledger.py
```
PASS: test_ledger completed successfully
```
### assistant/tests/test_models.py
```
PASS: FakeProvider and ModelProvider verified successfully
```
### assistant/tests/test_no_secrets.py
```
PASS: test_no_secrets completed successfully
```
### assistant/tests/test_planner.py
```
PASS: test_planner completed successfully
```
### assistant/tests/test_planner_executor_verifier.py
```
PASS: test_planner_executor_verifier completed successfully
```
### assistant/tests/test_providers.py
```
PASS: test_providers completed successfully
```
### assistant/tests/test_router.py
```
PASS: test_router completed successfully
```
### assistant/tests/test_rules.py
```
PASS: test_rules completed successfully
```
### assistant/tests/test_runner.py
```
PASS: test_runner completed successfully
```
### assistant/tests/test_safety.py
```
PASS: test_safety completed successfully
```
### assistant/tests/test_scripts.py
```
PASS: test_scripts completed successfully
```
### assistant/tests/test_shell.py
```
PASS: test_shell completed successfully
```
### assistant/tests/test_smoke.py
```
PASS: assistant package imported successfully (v0.1.0)
```
### assistant/tests/test_ui_widget.py
```
PASS: test_ui_widget completed successfully
```
