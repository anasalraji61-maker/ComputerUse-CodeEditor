"""اختبارات وحدة الأمان (Safety) — المستويات الأربعة وزر الإيقاف المشترك."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.safety import (
        SafetyGuard,
        SafetyLevel,
        KillSwitch,
        EmergencyStopTriggered,
        ActionForbiddenError,
        ApprovalRequiredError,
    )

    # 1. اختبار تصنيف المستويات الأربعة
    guard = SafetyGuard()
    assert guard.classify_action("inspect") == SafetyLevel.READ
    assert guard.classify_action("move") == SafetyLevel.REVERSIBLE
    assert guard.classify_action("winget_upgrade") == SafetyLevel.DANGEROUS
    assert guard.classify_action("permanent_delete") == SafetyLevel.FORBIDDEN

    # 2. اختبار رفض الإجراء المحظور
    try:
        guard.check_action("permanent_delete")
        print("FAIL: كان يجب رفض الإجراء المحظور!", file=sys.stderr)
        sys.exit(1)
    except ActionForbiddenError:
        pass  # صحيح ومطلوب

    # 3. اختبار طلب الموافقة الصريحة للإجراء الخطر
    try:
        guard.check_action("winget_upgrade", user_approved=False)
        print("FAIL: كان يجب طلب الموافقة للإجراء الخطر!", file=sys.stderr)
        sys.exit(1)
    except ApprovalRequiredError:
        pass  # صحيح ومطلوب

    # نجاح الإجراء الخطر عند توفر الموافقة
    guard.check_action("winget_upgrade", user_approved=True)

    # 4. اختبار زر الإيقاف المشترك (Kill Switch)
    ks = KillSwitch()
    assert not ks.is_triggered
    ks.check()  # لا يرفع استثناء عندما يكون غير مفعل

    ks.trigger(reason="إيقاف طارئ لاختبار النظام")
    assert ks.is_triggered
    assert "إيقاف طارئ" in ks.reason

    try:
        ks.check()
        print("FAIL: كان يجب رفع استثناء عند فحص زر الإيقاف المفعل!", file=sys.stderr)
        sys.exit(1)
    except EmergencyStopTriggered:
        pass  # صحيح ومطلوب

    # التحقق من أن SafetyGuard يحترم زر الإيقاف المشترك
    guard_with_ks = SafetyGuard(kill_switch=ks)
    try:
        guard_with_ks.check_action("move")
        print("FAIL: كان يجب أن يمنع الحارس الإجراء عند تفعيل زر الإيقاف!", file=sys.stderr)
        sys.exit(1)
    except EmergencyStopTriggered:
        pass  # صحيح ومطلوب

    # إعادة الضبط
    ks.reset()
    assert not ks.is_triggered
    guard_with_ks.check_action("move")

    print("PASS: test_safety completed successfully")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
