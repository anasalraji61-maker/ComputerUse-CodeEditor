"""اختبارات المخطط: التحقق الصارم من رد النموذج وعدم وجود خطة افتراضية ناجحة (R20)."""
import json
import sys
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.planner import Planner, PlanParseError
    from assistant.models.fake import FakeProvider

    def plan(resp):
        return Planner(FakeProvider(default_response=resp)).plan("x")

    def must_fail(resp, label):
        try:
            plan(resp)
        except PlanParseError:
            return
        raise AssertionError(f"كان يجب رفع PlanParseError: {label}")

    def step(**kw):
        d = {"step_id": 1, "action_type": "create_dir", "target": "a", "params": {}}
        d.update(kw)
        return d

    must_fail("هذا ليس JSON", "نص عادي")
    must_fail("OK", "رد افتراضي")
    must_fail(json.dumps({"steps": []}), "خطة فارغة")
    must_fail(json.dumps({"steps": "x"}), "steps ليست قائمة")
    must_fail(json.dumps({"steps": [step(action_type="hack_system")]}), "إجراء مجهول")
    must_fail(json.dumps({"steps": [step(action_type=None)]}), "إجراء None")
    must_fail(json.dumps({"steps": [step(target="")]}), "هدف فارغ")
    must_fail(json.dumps({"steps": [step(target=None)]}), "هدف None")
    must_fail(json.dumps({"steps": [step(action_type="move")]}), "move بلا dest")
    must_fail(json.dumps({"steps": [step(action_type="rename", params={"new_name": " "})]}), "rename اسم فارغ")
    must_fail(json.dumps({"steps": [step(params="x")]}), "params ليست كائناً")
    must_fail(json.dumps({"steps": [step(step_id="a")]}), "step_id نص")
    must_fail(json.dumps({"steps": [step(), step()]}), "step_id مكرر")
    must_fail(json.dumps({"steps": [step(action_type="run_command", params={"cmd": "whoami"})]}), "cmd نص")
    many = [step(step_id=i, target=f"d{i}") for i in range(1, 260)]
    must_fail(json.dumps({"steps": many}), "أكثر من الحد الأقصى")

    # حالات صحيحة
    ok = plan("```json\n" + json.dumps({"steps": [step(), step(step_id=2, action_type="move", target="a", params={"dest": "b"})]}) + "\n```")
    assert [s.step_id for s in ok] == [1, 2] and ok[1].params["dest"] == "b"
    ok2 = plan(json.dumps([step(action_type="INSPECT", target="a")]))
    assert ok2[0].action_type == "inspect"
    # الإجراء المحظور المعروف يُقبل في الخطة ويُرفض لاحقاً في الفحص المسبق
    assert plan(json.dumps({"steps": [step(action_type="permanent_delete")]}))[0].action_type == "permanent_delete"

    print("PASS: test_planner completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_planner", file=sys.stderr)
    sys.exit(1)
