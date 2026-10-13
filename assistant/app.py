"""نقطة تشغيل التطبيق: python -m assistant (أو run_assistant.bat)."""
import os
import sys
from pathlib import Path


def default_paths():
    home = Path.home()
    workspace = Path(os.environ.get("COS_WORKSPACE", home / "COS_Workspace"))
    data_dir = Path(os.environ.get("COS_DATA_DIR", home / ".cos_assistant"))
    return workspace, data_dir


def build_controller(provider=None):
    from assistant.ledger import SQLiteLedger
    from assistant.models.demo import DemoProvider
    from assistant.ui.controller import AssistantController

    workspace, data_dir = default_paths()
    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger = SQLiteLedger(db_path=data_dir / "ledger.db", initial_balance=100, spending_limit=100)
    return AssistantController(provider=provider or DemoProvider(), allowed_root=workspace, ledger=ledger)


def main() -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("tkinter غير متوفر في هذا البايثون. ثبّت بايثون الرسمي من python.org (يتضمن tkinter).", file=sys.stderr)
        return 1
    from assistant.ui.widget import AssistantWidget

    controller = build_controller()
    root = tk.Tk()
    AssistantWidget(root, controller)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
