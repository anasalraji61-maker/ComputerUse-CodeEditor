# نتائج الاختبارات الفعلية (آلية)

آخر تشغيل: 2026-10-10 01:36:09 — 10 ملف، فشل: 3

هذه النتائج تُنتَج على لابتوب المالك. لا تدّعِ نجاحاً غير مذكور هنا.

| الملف | النتيجة | الزمن |
|---|---|---|
| `assistant/tests/test_benchmarks_20runs.py` | **FAIL** | 0.1s |
| `assistant/tests/test_budget.py` | PASS | 0.1s |
| `assistant/tests/test_deliberate_failures.py` | **FAIL** | 0.1s |
| `assistant/tests/test_files.py` | PASS | 0.1s |
| `assistant/tests/test_ledger.py` | **FAIL** | 0.1s |
| `assistant/tests/test_models.py` | PASS | 0.1s |
| `assistant/tests/test_planner_executor_verifier.py` | PASS | 0.1s |
| `assistant/tests/test_safety.py` | PASS | 0.1s |
| `assistant/tests/test_shell.py` | PASS | 0.1s |
| `assistant/tests/test_smoke.py` | PASS | 0.1s |

### assistant/tests/test_benchmarks_20runs.py
```
FAIL: [WinError 32] The process cannot access the file because it is being used by another process: 'C:\\Users\\AkarTech\\AppData\\Local\\Temp\\tmppi9y5_qm\\ledger.db'
```
### assistant/tests/test_budget.py
```
PASS: test_budget completed successfully
```
### assistant/tests/test_deliberate_failures.py
```
FAIL: [WinError 32] The process cannot access the file because it is being used by another process: 'C:\\Users\\AkarTech\\AppData\\Local\\Temp\\tmp7ro2e2j6\\fail_ledger.db'
```
### assistant/tests/test_files.py
```
SKIP: symlink creation requires admin/elevated privileges on this OS
PASS: test_files completed successfully
```
### assistant/tests/test_ledger.py
```
FAIL: [WinError 32] The process cannot access the file because it is being used by another process: 'C:\\Users\\AkarTech\\AppData\\Local\\Temp\\tmpwro8_laj\\test_ledger.db'
```
### assistant/tests/test_models.py
```
PASS: FakeProvider and ModelProvider verified successfully
```
### assistant/tests/test_planner_executor_verifier.py
```
PASS: test_planner_executor_verifier completed successfully
```
### assistant/tests/test_safety.py
```
PASS: test_safety completed successfully
```
### assistant/tests/test_shell.py
```
PASS: test_shell completed successfully
```
### assistant/tests/test_smoke.py
```
PASS: assistant package imported successfully (v0.1.0)
```
