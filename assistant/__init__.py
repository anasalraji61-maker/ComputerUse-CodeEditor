"""حزمة مساعد المهام المكتبية.

مساعد أتمتة عربي لأعمال الحاسوب المتعبة: يخطط، ينفّذ، يتحقق بأدلة، ويقدّم تقريراً.
"""

__version__ = "0.1.0"

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

from .safety import SafetyGuard, SafetyLevel, KillSwitch
from .budget import BudgetTracker, BudgetExceededError
from .ledger import SQLiteLedger
from .reporter import ExecutionReport
from .planner import Planner, PlanStep, PlanParseError
from .executor import Executor
from .verifier import Verifier
from .runner import TaskRunner
from .models.base import ModelProvider
from .models.fake import FakeProvider
from .tools.files import FileManager
from .tools.shell import ShellTool
from .tools.winget import WingetTool

__all__ = [
    "__version__",
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
    "SafetyGuard",
    "SafetyLevel",
    "KillSwitch",
    "BudgetTracker",
    "BudgetExceededError",
    "SQLiteLedger",
    "ExecutionReport",
    "Planner",
    "PlanParseError",
    "TaskRunner",
    "PlanStep",
    "Executor",
    "Verifier",
    "ModelProvider",
    "FakeProvider",
    "FileManager",
    "ShellTool",
    "WingetTool",
]
