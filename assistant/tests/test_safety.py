"""اختبارات الأمان: جدول صريح، فشل مغلق، موافقة لكل خطوة، وزر إيقاف مشترك بين الخيوط."""
import sys
import threading
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.safety import (
        SafetyGuard, SafetyLevel, KillSwitch, EmergencyStopTriggered,
        ActionForbiddenError, ApprovalRequiredError, ACTION_LEVELS,
    )

    guard = SafetyGuard()
    # 1. التصنيف الصريح
    assert guard.classify_action("inspect") == SafetyLevel.READ
    assert guard.classify_action("move") == SafetyLevel.REVERSIBLE
    assert guard.classify_action(" MOVE ") == SafetyLevel.REVERSIBLE
    assert guard.classify_action("winget_upgrade") == SafetyLevel.DANGEROUS
    assert guard.classify_action("permanent_delete") == SafetyLevel.FORBIDDEN

    # 2. R15: لا مطابقة جزئية، والمجهول محظور (كان يرجع READ سابقاً)
    for name in ("read_and_wipe", "move_and_install", "totally_unknown", "", "rm_rf", "format", "moveX", "delete_all_files", None, 5):
        assert guard.classify_action(name) == SafetyLevel.FORBIDDEN, name
        try:
            guard.check_action(name, user_approved=True)
            raise AssertionError(f"كان يجب رفض {name!r}")
        except ActionForbiddenError:
            pass

    # 3. الجدول لا يحوي READ لأي اسم خطير
    assert all(v != SafetyLevel.READ for k, v in ACTION_LEVELS.items() if k != "inspect")

    # 4. الإجراء المحظور ولو بموافقة
    try:
        guard.check_action("permanent_delete", user_approved=True)
        raise AssertionError("المحظور يجب ألا يُسمح به حتى بموافقة")
    except ActionForbiddenError:
        pass

    # 5. الإجراء الخطر يحتاج موافقة
    try:
        guard.check_action("winget_upgrade", user_approved=False)
        raise AssertionError("كان يجب طلب الموافقة")
    except ApprovalRequiredError:
        pass
    guard.check_action("winget_upgrade", user_approved=True)

    # 6. زر الإيقاف
    ks = KillSwitch()
    assert not ks.is_triggered
    ks.check()
    ks.trigger(reason="إيقاف طارئ لاختبار النظام")
    assert ks.is_triggered and "إيقاف طارئ" in ks.reason
    try:
        ks.check()
        raise AssertionError("كان يجب رفع الاستثناء")
    except EmergencyStopTriggered:
        pass
    g2 = SafetyGuard(kill_switch=ks)
    try:
        g2.check_action("move")
        raise AssertionError("الحارس يجب أن يحترم زر الإيقاف")
    except EmergencyStopTriggered:
        pass
    ks.reset()
    assert not ks.is_triggered
    g2.check_action("move")

    # 7. R23: التفعيل من خيط آخر يُرى فوراً
    ks2 = KillSwitch()
    t = threading.Thread(target=lambda: ks2.trigger("من خيط آخر"))
    t.start(); t.join()
    assert ks2.is_triggered and ks2.reason == "من خيط آخر"

    print("PASS: test_safety completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_safety", file=sys.stderr)
    sys.exit(1)
