# نتائج الاختبارات الفعلية (آلية)

آخر تشغيل: 2026-10-15 01:01:49 — 48 ملف، فشل: 1

هذه النتائج تُنتَج على لابتوب المالك. لا تدّعِ نجاحاً غير مذكور هنا.

| الملف | النتيجة | الزمن |
|---|---|---|
| `assistant/tests/test_b11.py` | PASS | 1.0s |
| `assistant/tests/test_b15.py` | PASS | 0.2s |
| `assistant/tests/test_b16.py` | PASS | 0.5s |
| `assistant/tests/test_b17.py` | PASS | 0.2s |
| `assistant/tests/test_b18.py` | PASS | 0.9s |
| `assistant/tests/test_b19.py` | PASS | 0.9s |
| `assistant/tests/test_b20.py` | PASS | 0.1s |
| `assistant/tests/test_b21.py` | PASS | 0.1s |
| `assistant/tests/test_b22.py` | PASS | 0.8s |
| `assistant/tests/test_b23.py` | PASS | 3.2s |
| `assistant/tests/test_b24.py` | PASS | 0.9s |
| `assistant/tests/test_b25.py` | PASS | 0.2s |
| `assistant/tests/test_b26.py` | PASS | 1.0s |
| `assistant/tests/test_b28.py` | PASS | 0.3s |
| `assistant/tests/test_b28_rules.py` | PASS | 0.4s |
| `assistant/tests/test_b29_rules.py` | PASS | 1.5s |
| `assistant/tests/test_b30_rules.py` | PASS | 1.4s |
| `assistant/tests/test_b31_ui.py` | PASS | 2.0s |
| `assistant/tests/test_b32_rules.py` | PASS | 0.1s |
| `assistant/tests/test_b34_controller.py` | PASS | 7.1s |
| `assistant/tests/test_b34_core.py` | **FAIL** | 0.4s |
| `assistant/tests/test_b34_tools.py` | PASS | 0.7s |
| `assistant/tests/test_b6.py` | PASS | 0.3s |
| `assistant/tests/test_b8.py` | PASS | 0.4s |
| `assistant/tests/test_bench.py` | PASS | 0.7s |
| `assistant/tests/test_benchmarks_20runs.py` | PASS | 1.6s |
| `assistant/tests/test_budget.py` | PASS | 0.1s |
| `assistant/tests/test_config.py` | PASS | 0.4s |
| `assistant/tests/test_controller.py` | PASS | 0.5s |
| `assistant/tests/test_deliberate_failures.py` | PASS | 8.0s |
| `assistant/tests/test_files.py` | PASS | 0.1s |
| `assistant/tests/test_fuzz.py` | PASS | 4.7s |
| `assistant/tests/test_fuzz_invariants.py` | SKIP (لم يُختبر فعلياً) | 0.1s |
| `assistant/tests/test_ledger.py` | PASS | 0.3s |
| `assistant/tests/test_models.py` | PASS | 0.1s |
| `assistant/tests/test_no_secrets.py` | PASS | 0.1s |
| `assistant/tests/test_planner.py` | PASS | 0.1s |
| `assistant/tests/test_planner_executor_verifier.py` | PASS | 0.1s |
| `assistant/tests/test_providers.py` | PASS | 0.1s |
| `assistant/tests/test_router.py` | PASS | 0.1s |
| `assistant/tests/test_rules.py` | PASS | 0.7s |
| `assistant/tests/test_runner.py` | PASS | 1.0s |
| `assistant/tests/test_safety.py` | PASS | 0.1s |
| `assistant/tests/test_scripts.py` | PASS | 0.0s |
| `assistant/tests/test_shell.py` | PASS | 0.2s |
| `assistant/tests/test_smoke.py` | PASS | 0.1s |
| `assistant/tests/test_ui_widget.py` | PASS | 2.3s |
| `assistant/tests/test_windows.py` | PASS | 0.2s |

### assistant/tests/test_b11.py
```
PASS: test_b11 completed successfully
```
### assistant/tests/test_b15.py
```
PASS: test_b15 completed successfully
```
### assistant/tests/test_b16.py
```
PASS: test_b16 completed successfully
```
### assistant/tests/test_b17.py
```
PASS: test_b17 completed successfully
```
### assistant/tests/test_b18.py
```
PASS: test_b18 completed successfully
```
### assistant/tests/test_b19.py
```
PASS: test_b19 completed successfully
```
### assistant/tests/test_b20.py
```
PASS: test_b20 completed successfully
```
### assistant/tests/test_b21.py
```
PASS: test_b21 completed successfully
```
### assistant/tests/test_b22.py
```
PASS: test_b22 completed successfully
```
### assistant/tests/test_b23.py
```
PASS: test_b23 completed successfully
```
### assistant/tests/test_b24.py
```
PASS: test_b24 completed successfully
```
### assistant/tests/test_b25.py
```
PASS: test_b25 completed successfully
```
### assistant/tests/test_b26.py
```
PASS: test_b26 completed successfully
```
### assistant/tests/test_b28.py
```
PASS: test_b28 completed successfully
```
### assistant/tests/test_b28_rules.py
```
PASS: test_b28_rules completed successfully
```
### assistant/tests/test_b29_rules.py
```
PASS: test_b29_rules completed successfully
```
### assistant/tests/test_b30_rules.py
```
PASS: test_b30_rules completed successfully
```
### assistant/tests/test_b31_ui.py
```
PASS: test_b31_ui completed successfully
```
### assistant/tests/test_b32_rules.py
```
PASS: test_b32_rules completed successfully
```
### assistant/tests/test_b34_controller.py
```
ok R1-3: worker death recovers
ok R1-6/7/8: snapshot, stop outside lock, one-line objective
ok 11a: report/escape helpers
ok 11b: approval dialog
PASS: test_b34_controller completed successfully
```
### assistant/tests/test_b34_core.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b34_core.py", line 202, in <module>
    raises(PlanParseError, vs, action="rename", target="a", new_name="ن" * 128)   # 256 بايتاً
    ~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b34_core.py", line 30, in raises
    raise AssertionError(f"توقعت {exc.__name__}")
AssertionError: توقعت PlanParseError
FAIL: test_b34_core
```
### assistant/tests/test_b34_tools.py
```
PASS: test_b34_tools completed successfully
```
### assistant/tests/test_b6.py
```
PASS: test_b6 completed successfully
```
### assistant/tests/test_b8.py
```
NOTE: اختبار الروابط الرمزية لم يُنفَّذ هنا (ويندوز بلا صلاحية إنشاء رابط)
PASS: test_b8 completed successfully
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
PASS: test_config completed successfully
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
### assistant/tests/test_fuzz.py
```
PASS: test_fuzz completed successfully (120 scenarios, 79 mutating runs, 131 failed runs)
```
### assistant/tests/test_fuzz_invariants.py
```
SKIP: fuzz الثوابت يعمل على لينكس فقط حالياً (python tools/fuzz_invariants.py يدوياً على ويندوز للتجربة)
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
### assistant/tests/test_windows.py
```
PASS: test_windows completed successfully
```
