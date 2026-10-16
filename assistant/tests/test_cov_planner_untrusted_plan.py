"""تغطية المخطط: مخرجات النموذج غير موثوقة — كل خطوة شاذة (نوع/رقم/هدف/وجهة/اسم/أمر/حارس/محارف اتجاه) تُرفض بـ PlanParseError قبل أي تنفيذ،
والبارامترات الغريبة تُسقَط، والترتيب المستحيل (استهداف ما نُقل/حُذف) يُرفض."""
import json
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.planner import Planner, PlanParseError, validate_step, sanitize_display, MAX_RESPONSE_CHARS
    from assistant.models.fake import FakeProvider

    def bad(item, fragment=None, idx=1):
        try:
            validate_step(item, idx)
        except PlanParseError as exc:
            if fragment is not None:
                assert fragment in str(exc), (fragment, str(exc))
            return
        raise AssertionError(f"كان يجب رفض: {item!r}")

    def step(**kw):
        d = {"step_id": 1, "action_type": "inspect", "target": "a.txt"}
        d.update(kw)
        return d

    # بنية الخطوة
    for it in (None, 5, "inspect", ["inspect"]):
        bad(it, "ليست كائناً")
    for sid in (True, 0, 10000, "1", 1.5, -2):
        bad(step(step_id=sid), "step_id")
    for act in (None, "", "rm -rf", "dir", "Delete_All", 5):
        bad(step(action_type=act), "غير معروف")
    bad(step(target=None), "الهدف")
    bad(step(target="   "), "الهدف")
    bad(step(target=" a.txt"), "مسافة")
    bad(step(target="a.txt "), "مسافة")
    bad(step(params=[1]), "params")
    assert validate_step(step(params=None), 1).params == {}

    # محارف خطرة/خفية في الهدف والوجهة والاسم والأمر
    for ch in ("\u202e", "\u200b", "\x00", "\x07", "\u2028"):
        bad(step(target="a" + ch + "txt"))
        bad(step(action_type="move", params={"dest": "x" + ch + "y"}))
        bad(step(action_type="rename", params={"new_name": "x" + ch + "y"}))
    bad(step(action_type="run_command", target="whoami", params={"cmd": ["who\u200bami"]}), "غير مرئي")
    bad(step(action_type="run_command", target="whoami", params={"cmd": ["who\u00a0ami"]}), "غير مرئي")      # فراغ غير عادي
    bad(step(target="\\\\server\\share\\x"))
    bad(step(target="../x.txt"), "..")
    bad(step(target="a/../../x.txt"), "..")
    bad(step(action_type="copy", params={"dest": "../out.txt"}), "..")

    # حقول الإجراء
    bad(step(action_type="move"), "dest")
    bad(step(action_type="move", params={"dest": "  "}), "dest")
    bad(step(action_type="move", params={"dest": "a.txt"}), "نفس المصدر")
    bad(step(action_type="move", params={"dest": "A.TXT"}), "نفس المصدر")
    bad(step(action_type="rename"), "new_name")
    bad(step(action_type="rename", params={"new_name": "x/y"}))
    bad(step(action_type="rename", params={"new_name": ".."}))
    bad(step(action_type="rename", params={"new_name": " x"}))
    bad(step(action_type="rename", params={"new_name": "x" * 400}), "أطول")
    bad(step(action_type="run_command", target="whoami", params={"cmd": "whoami"}), "cmd")
    bad(step(action_type="run_command", target="whoami", params={"cmd": []}), "cmd")
    bad(step(action_type="run_command", target="whoami", params={"cmd": [1]}), "cmd")
    bad(step(action_type="run_command", target="whoami", params={"cmd": ["whoami"] * 500}), "طويلة")
    bad(step(action_type="copy", params={"dest": "b.txt", "expected_hash": "a" * 5000}), "expected_hash")
    bad(step(target="x" * 5000), "أطول")

    # الحارس params.guard (تضعه القواعد فقط، لكنه يمرّ بالتحقق إن جاء من النموذج)
    for g in ("x", [1], {"size": -1}, {"size": True}, {"size": "5"}, {"mtime_ns": 2 ** 70}, {"hash": ""}, {"hash": "a b"}, {"keep": "/abs/x"},
              {"keep": "C:\\x"}, {"keep": "../x"}, {"keep": "\\\\srv\\x"}, {"keep_hash": "z;rm"}):
        bad(step(action_type="delete", params={"guard": g}), None)
    ok = validate_step(step(action_type="delete", params={"guard": {"size": 3, "hash": "f:ab12", "keep": "k/x.txt", "junk": 1}}), 1)
    assert ok.params["guard"] == {"size": 3, "hash": "f:ab12", "keep": "k/x.txt"}, ok.params
    assert "guard" not in validate_step(step(action_type="inspect", params={"guard": {"size": 1}}), 1).params   # لا يمرّ لإجراء لا يستعمله

    # بارامترات غريبة تُسقَط ولا تمرّ للمنفّذ
    v = validate_step(step(action_type="copy", params={"dest": "b.txt", "force": True, "overwrite": "yes", "shell": "x"}), 1)
    assert v.params == {"dest": "b.txt"}, v.params
    # معيار القبول الفارغ/الخفي يأخذ القيمة الافتراضية
    assert validate_step(step(acceptance_criteria="\u200b\u200b"), 1).acceptance_criteria == "تحقق الحالة الفعلية"
    assert sanitize_display("a\u202eb\x00c\nd", 50) == "abc d", sanitize_display("a\u202eb\x00c\nd", 50)

    # المخطط الكامل
    def plan(obj_or_text):
        text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text)
        return Planner(FakeProvider(default_response=text)).plan_with_note("x")

    def bad_plan(obj_or_text, fragment=None):
        try:
            plan(obj_or_text)
        except PlanParseError as exc:
            if fragment is not None:
                assert fragment in str(exc), (fragment, str(exc))
            return
        raise AssertionError("كان يجب رفض الخطة")

    s = lambda i, a, t, **p: {"step_id": i, "action_type": a, "target": t, "params": p}   # noqa: E731
    bad_plan("not json at all")
    steps, _ = plan("```json\n" + json.dumps({"steps": [s(1, "inspect", ".")]}))
    assert len(steps) == 1
    bad_plan("[" * 100000)                                                       # تداخل مفرط
    bad_plan("x" * (MAX_RESPONSE_CHARS + 1), "كبير")
    bad_plan({"steps": []}, "فارغة")
    bad_plan({"steps": "x"})
    bad_plan({"steps": [s(1, "inspect", "a"), s(1, "inspect", "b")]}, "مكررة")
    bad_plan({"steps": [s(1, "move", "a.txt", dest="b.txt"), s(2, "delete", "a.txt")]}, "نُقل")
    bad_plan({"steps": [s(1, "delete", "d"), s(2, "inspect", "d/f.txt")]}, "نُقل")
    bad_plan({"steps": [s(1, "copy", "a.txt", dest="b.txt"), s(2, "copy", "a.txt", dest="b.txt")]}, "مكررة")
    bad_plan({"steps": [s(i, "create_dir", f"d{i}") for i in range(1, 5000)]}, "الحد الأقصى")
    # قائمة مباشرة بلا غلاف steps مقبولة، والملاحظة تُنقّى
    steps, note = plan([s(1, "inspect", ".")])
    assert len(steps) == 1 and note is None
    steps, note = plan({"steps": [s(1, "inspect", ".")], "note": "ملاحظة\u202e خطرة"})
    assert "\u202e" not in (note or "")

    print("PASS: test_cov_planner_untrusted_plan completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_planner_untrusted_plan", file=sys.stderr)
    sys.exit(1)
