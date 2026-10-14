"""نقطة تشغيل التطبيق: python -m assistant (أو run_assistant.bat)."""
import os
import sys
from pathlib import Path


def default_paths():
    from assistant.config import current_workspace, data_dir
    return current_workspace(), data_dir()


def build_controller(provider=None):
    """يعيد (المتحكم، ملاحظة للمستخدم). المزوّد من COS_PROVIDER؛ أي خلل في الإعداد يعود للوضع التجريبي مع تنبيه."""
    from assistant.ledger import SQLiteLedger
    from assistant.models.demo import DemoProvider
    from assistant.models.factory import build_provider
    from assistant.models.http_base import ProviderConfigError
    from assistant.ui.controller import AssistantController

    notice = ""
    if provider is None:
        try:
            provider, notice = build_provider()
        except ProviderConfigError as exc:
            provider, notice = DemoProvider(), f"تنبيه: {exc} — يعمل الآن بالوضع التجريبي."

    workspace, data_dir = default_paths()
    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger = SQLiteLedger(db_path=data_dir / "ledger.db", initial_balance=100, spending_limit=100)
    if ledger.get_balance() < 20:  # مرحلة تجريبية للمالك: شحن تلقائي صغير بدل أن يتوقف التطبيق للأبد
        ledger.top_up(100, "شحن تجريبي تلقائي")
    from assistant.usage import UsageMeter
    meter = UsageMeter(data_dir / "usage.json")
    return AssistantController(provider=provider, allowed_root=workspace, ledger=ledger, usage_meter=meter), notice


def main() -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("tkinter غير متوفر في هذا البايثون. ثبّت بايثون الرسمي من python.org (يتضمن tkinter).", file=sys.stderr)
        return 1
    from assistant.ui.widget import AssistantWidget

    controller, notice = build_controller()
    root = tk.Tk()
    widget = AssistantWidget(root, controller)
    if notice:
        widget.add_message(notice, "assistant")
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
