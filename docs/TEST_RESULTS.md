# نتائج الاختبارات الفعلية (آلية)

آخر تشغيل: 2026-10-16 08:51:15 — 59 ملف، فشل: 56

هذه النتائج تُنتَج على لابتوب المالك. لا تدّعِ نجاحاً غير مذكور هنا.

| الملف | النتيجة | الزمن |
|---|---|---|
| `assistant/tests/test_b11.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b15.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b16.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b17.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b18.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b19.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b20.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b21.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b22.py` | **FAIL** | 0.3s |
| `assistant/tests/test_b23.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b24.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b25.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b26.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b28.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b28_rules.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b29_rules.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b30_rules.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b31_ui.py` | **FAIL** | 0.5s |
| `assistant/tests/test_b32_rules.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b34_controller.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b34_core.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b34_tools.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b35_core.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b35_rules.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b35_tools.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b35_ui.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b36_core.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b36_rules.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b36_tools.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b36_ui.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b37_core.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b37_ui.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b38_tools.py` | **FAIL** | 0.2s |
| `assistant/tests/test_b6.py` | **FAIL** | 0.1s |
| `assistant/tests/test_b8.py` | **FAIL** | 0.1s |
| `assistant/tests/test_bench.py` | **FAIL** | 0.1s |
| `assistant/tests/test_benchmarks_20runs.py` | **FAIL** | 0.1s |
| `assistant/tests/test_budget.py` | **FAIL** | 0.1s |
| `assistant/tests/test_config.py` | **FAIL** | 0.1s |
| `assistant/tests/test_controller.py` | **FAIL** | 0.1s |
| `assistant/tests/test_deliberate_failures.py` | **FAIL** | 0.1s |
| `assistant/tests/test_files.py` | **FAIL** | 0.1s |
| `assistant/tests/test_fuzz.py` | **FAIL** | 0.1s |
| `assistant/tests/test_fuzz_invariants.py` | SKIP (لم يُختبر فعلياً) | 0.1s |
| `assistant/tests/test_ledger.py` | **FAIL** | 0.1s |
| `assistant/tests/test_models.py` | **FAIL** | 0.1s |
| `assistant/tests/test_no_secrets.py` | PASS | 0.1s |
| `assistant/tests/test_planner.py` | **FAIL** | 0.1s |
| `assistant/tests/test_planner_executor_verifier.py` | **FAIL** | 0.1s |
| `assistant/tests/test_providers.py` | **FAIL** | 0.1s |
| `assistant/tests/test_router.py` | **FAIL** | 0.1s |
| `assistant/tests/test_rules.py` | **FAIL** | 0.1s |
| `assistant/tests/test_runner.py` | **FAIL** | 0.1s |
| `assistant/tests/test_safety.py` | **FAIL** | 0.1s |
| `assistant/tests/test_scripts.py` | PASS | 0.0s |
| `assistant/tests/test_shell.py` | **FAIL** | 0.1s |
| `assistant/tests/test_smoke.py` | **FAIL** | 0.1s |
| `assistant/tests/test_ui_widget.py` | **FAIL** | 0.2s |
| `assistant/tests/test_windows.py` | **FAIL** | 0.2s |

### assistant/tests/test_b11.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b11.py", line 17, in <module>
    from assistant.models.gemini_provider import GeminiProvider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 17, in <module>
    from .planner import Planner, PlanStep, PlanParseError
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\planner.py", line 5, in <module>
    from assistant.models.base import ModelProvider, CONTEXT_MARKER
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\__init__.py", line 10, in <module>
    from .factory import build_provider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\factory.py", line 11, in <module>
    from .openai_compat import (OpenAICompatProvider, OLLAMA_BASE_URL, OPENAI_BASE_URL, is_loopback_url, normalize_base_url)
ImportError: cannot import name 'is_loopback_url' from 'assistant.models.openai_compat' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\openai_compat.py)
FAIL: test_b11
```
### assistant/tests/test_b15.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b15.py", line 13, in <module>
    from assistant.models.rules import RuleProvider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 17, in <module>
    from .planner import Planner, PlanStep, PlanParseError
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\planner.py", line 5, in <module>
    from assistant.models.base import ModelProvider, CONTEXT_MARKER
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\__init__.py", line 10, in <module>
    from .factory import build_provider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\factory.py", line 11, in <module>
    from .openai_compat import (OpenAICompatProvider, OLLAMA_BASE_URL, OPENAI_BASE_URL, is_loopback_url, normalize_base_url)
ImportError: cannot import name 'is_loopback_url' from 'assistant.models.openai_compat' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\openai_compat.py)
FAIL: test_b15
```
### assistant/tests/test_b16.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b16.py", line 14, in <module>
    from assistant.models.rules import RuleProvider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 17, in <module>
    from .planner import Planner, PlanStep, PlanParseError
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\planner.py", line 5, in <module>
    from assistant.models.base import ModelProvider, CONTEXT_MARKER
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\__init__.py", line 10, in <module>
    from .factory import build_provider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\factory.py", line 11, in <module>
    from .openai_compat import (OpenAICompatProvider, OLLAMA_BASE_URL, OPENAI_BASE_URL, is_loopback_url, normalize_base_url)
ImportError: cannot import name 'is_loopback_url' from 'assistant.models.openai_compat' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\openai_compat.py)
FAIL: test_b16
```
### assistant/tests/test_b17.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b17.py", line 14, in <module>
    from assistant.models.rules import RuleProvider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 17, in <module>
    from .planner import Planner, PlanStep, PlanParseError
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\planner.py", line 5, in <module>
    from assistant.models.base import ModelProvider, CONTEXT_MARKER
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\__init__.py", line 10, in <module>
    from .factory import build_provider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\factory.py", line 11, in <module>
    from .openai_compat import (OpenAICompatProvider, OLLAMA_BASE_URL, OPENAI_BASE_URL, is_loopback_url, normalize_base_url)
ImportError: cannot import name 'is_loopback_url' from 'assistant.models.openai_compat' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\openai_compat.py)
FAIL: test_b17
```
### assistant/tests/test_b18.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b18.py", line 16, in <module>
    from assistant.executor import ApprovalTimedOut
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 17, in <module>
    from .planner import Planner, PlanStep, PlanParseError
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\planner.py", line 5, in <module>
    from assistant.models.base import ModelProvider, CONTEXT_MARKER
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\__init__.py", line 10, in <module>
    from .factory import build_provider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\factory.py", line 11, in <module>
    from .openai_compat import (OpenAICompatProvider, OLLAMA_BASE_URL, OPENAI_BASE_URL, is_loopback_url, normalize_base_url)
ImportError: cannot import name 'is_loopback_url' from 'assistant.models.openai_compat' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\openai_compat.py)
FAIL: test_b18
```
### assistant/tests/test_b19.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b19.py", line 16, in <module>
    from assistant.config import validate_workspace, WorkspaceError
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 17, in <module>
    from .planner import Planner, PlanStep, PlanParseError
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\planner.py", line 5, in <module>
    from assistant.models.base import ModelProvider, CONTEXT_MARKER
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\__init__.py", line 10, in <module>
    from .factory import build_provider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\factory.py", line 11, in <module>
    from .openai_compat import (OpenAICompatProvider, OLLAMA_BASE_URL, OPENAI_BASE_URL, is_loopback_url, normalize_base_url)
ImportError: cannot import name 'is_loopback_url' from 'assistant.models.openai_compat' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\models\openai_compat.py)
FAIL: test_b19
```
### assistant/tests/test_b20.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b20.py", line 12, in <module>
    from assistant.ledger import SQLiteLedger
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b20
```
### assistant/tests/test_b21.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b21.py", line 14, in <module>
    from assistant.tools.files import FileManager
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b21
```
### assistant/tests/test_b22.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b22.py", line 16, in <module>
    from assistant.ledger import SQLiteLedger
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b22
```
### assistant/tests/test_b23.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b23.py", line 19, in <module>
    from assistant.config import validate_workspace, load_config, WorkspaceError
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b23
```
### assistant/tests/test_b24.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b24.py", line 16, in <module>
    from assistant.models.base import ModelProvider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b24
```
### assistant/tests/test_b25.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b25.py", line 15, in <module>
    from assistant.ledger import SQLiteLedger
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b25
```
### assistant/tests/test_b26.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b26.py", line 18, in <module>
    from assistant.ledger import SQLiteLedger
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b26
```
### assistant/tests/test_b28.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b28.py", line 16, in <module>
    from assistant.planner import PlanParseError, Planner
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b28
```
### assistant/tests/test_b28_rules.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b28_rules.py", line 17, in <module>
    from assistant.ledger import SQLiteLedger
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b28_rules
```
### assistant/tests/test_b29_rules.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b29_rules.py", line 18, in <module>
    from assistant.ledger import SQLiteLedger
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b29_rules
```
### assistant/tests/test_b30_rules.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b30_rules.py", line 17, in <module>
    from assistant.ledger import SQLiteLedger
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b30_rules
```
### assistant/tests/test_b31_ui.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b31_ui.py", line 22, in <module>
    from assistant.ui.widget import steps_phrase
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b31_ui
```
### assistant/tests/test_b32_rules.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b32_rules.py", line 14, in <module>
    from assistant.models.rules import RuleProvider, INFO_PREFIX
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b32_rules
```
### assistant/tests/test_b34_controller.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b34_controller.py", line 20, in <module>
    import assistant.ui.controller as C
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b34_controller
```
### assistant/tests/test_b34_core.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b34_core.py", line 18, in <module>
    from assistant.planner import (PlanParseError, Planner, validate_step, sanitize_display, _extract_json)
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b34_core
```
### assistant/tests/test_b34_tools.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b34_tools.py", line 16, in <module>
    from assistant.planner import PlanStep
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b34_tools
```
### assistant/tests/test_b35_core.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b35_core.py", line 19, in <module>
    import assistant.tools.files as ft
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b35_core
```
### assistant/tests/test_b35_rules.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b35_rules.py", line 14, in <module>
    import assistant.models.rules as R
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b35_rules
```
### assistant/tests/test_b35_tools.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b35_tools.py", line 19, in <module>
    from assistant.usage import UsageMeter, PENDING_TTL_SEC
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b35_tools
```
### assistant/tests/test_b35_ui.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b35_ui.py", line 20, in <module>
    import assistant.app as app
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b35_ui
```
### assistant/tests/test_b36_core.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b36_core.py", line 18, in <module>
    import assistant.tools.files as ft
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b36_core
```
### assistant/tests/test_b36_rules.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b36_rules.py", line 15, in <module>
    import assistant.models.rules as R
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b36_rules
```
### assistant/tests/test_b36_tools.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b36_tools.py", line 22, in <module>
    from assistant.models.http_base import (HttpProvider, ProviderError, ProviderConfigError, parse_retry_after,
                                            describe_network_error, default_transport)
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b36_tools
```
### assistant/tests/test_b36_ui.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b36_ui.py", line 19, in <module>
    import assistant.app as app
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b36_ui
```
### assistant/tests/test_b37_core.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b37_core.py", line 19, in <module>
    import assistant.tools.files as ft
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b37_core
```
### assistant/tests/test_b37_ui.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b37_ui.py", line 19, in <module>
    from assistant.ui.controller import AssistantController, IDLE
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b37_ui
```
### assistant/tests/test_b38_tools.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b38_tools.py", line 16, in <module>
    from assistant.models.openai_compat import OpenAICompatProvider, is_local_url, normalize_base_url, OLLAMA_BASE_URL, OPENAI_BASE_URL
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b38_tools
```
### assistant/tests/test_b6.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b6.py", line 15, in <module>
    from assistant.models.rules import RuleProvider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b6
```
### assistant/tests/test_b8.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_b8.py", line 14, in <module>
    from assistant import config as cfg
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_b8
```
### assistant/tests/test_bench.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_bench.py", line 14, in <module>
    from assistant.models.fake import FakeProvider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_bench
```
### assistant/tests/test_benchmarks_20runs.py
```
FAIL: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
```
### assistant/tests/test_budget.py
```
FAIL: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
```
### assistant/tests/test_config.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_config.py", line 18, in <module>
    from assistant import config
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_config
```
### assistant/tests/test_controller.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_controller.py", line 15, in <module>
    from assistant.ui.controller import AssistantController, IDLE, PLAN_READY, CLARIFYING
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_controller
```
### assistant/tests/test_deliberate_failures.py
```
FAIL: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
```
### assistant/tests/test_files.py
```
FAIL: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
```
### assistant/tests/test_fuzz.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_fuzz.py", line 16, in <module>
    from assistant.models.rules import RuleProvider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_fuzz
```
### assistant/tests/test_fuzz_invariants.py
```
SKIP: fuzz الثوابت يعمل على لينكس فقط حالياً (python tools/fuzz_invariants.py يدوياً على ويندوز للتجربة)
```
### assistant/tests/test_ledger.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_ledger.py", line 11, in <module>
    from assistant.ledger import (
    ...<3 lines>...
    )
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: ImportError("cannot import name 'entry_kind' from 'assistant.tools.files' (C:\\Users\\AkarTech\\Documents\\COS_Projects\\ComputerUse-CodeEditor\\assistant\\tools\\files.py)")
```
### assistant/tests/test_models.py
```
FAIL: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
```
### assistant/tests/test_no_secrets.py
```
PASS: test_no_secrets completed successfully
```
### assistant/tests/test_planner.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_planner.py", line 12, in <module>
    from assistant.planner import Planner, PlanParseError
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_planner
```
### assistant/tests/test_planner_executor_verifier.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_planner_executor_verifier.py", line 12, in <module>
    from assistant.planner import Planner, PlanStep
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_planner_executor_verifier
```
### assistant/tests/test_providers.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_providers.py", line 13, in <module>
    from assistant.models.anthropic_provider import AnthropicProvider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_providers
```
### assistant/tests/test_router.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_router.py", line 12, in <module>
    from assistant.models.anthropic_provider import AnthropicProvider
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_router
```
### assistant/tests/test_rules.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_rules.py", line 15, in <module>
    from assistant.models.rules import RuleProvider, RuleFirstProvider, norm, parse_context
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_rules
```
### assistant/tests/test_runner.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_runner.py", line 13, in <module>
    from assistant.runner import TaskRunner
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_runner
```
### assistant/tests/test_safety.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_safety.py", line 12, in <module>
    from assistant.safety import (
    ...<2 lines>...
    )
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_safety
```
### assistant/tests/test_scripts.py
```
PASS: test_scripts completed successfully
```
### assistant/tests/test_shell.py
```
FAIL: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
```
### assistant/tests/test_smoke.py
```
FAIL: فشل استيراد assistant: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
```
### assistant/tests/test_ui_widget.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_ui_widget.py", line 22, in <module>
    from assistant.ui.controller import AssistantController
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_ui_widget
```
### assistant/tests/test_windows.py
```
Traceback (most recent call last):
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tests\test_windows.py", line 18, in <module>
    from assistant.tools.files import FileManager, PathOutOfBoundsError, _is_link
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\__init__.py", line 18, in <module>
    from .executor import Executor
  File "C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\executor.py", line 10, in <module>
    from assistant.tools.files import FileManager, entry_kind, content_fingerprint
ImportError: cannot import name 'entry_kind' from 'assistant.tools.files' (C:\Users\AkarTech\Documents\COS_Projects\ComputerUse-CodeEditor\assistant\tools\files.py)
FAIL: test_windows
```
