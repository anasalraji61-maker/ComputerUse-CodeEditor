"""تغطية قواعد المكررات والحذف الشامل: البحث عن المكررات يُقطع بالإيقاف، ملف غير مقروء/سحابي لا يُحذف ولا يكسر البحث، تعارض الأسماء داخل «مكررات»
يُسأل عنه بدل الكتابة فوق أحد، وطلبات الحذف الشاملة لا تنتج أي خطوة حذف."""
import builtins
import pathlib
import sys
import tempfile
import traceback
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import assistant.models.rules as R
    from assistant.models.rules import RuleProvider
    from assistant.safety import EmergencyStopTriggered
    from assistant.tools.files import CloudFileNotLocalError

    def mk(tmp, name, files):
        ws = pathlib.Path(tmp).resolve() / name
        ws.mkdir()
        for k, v in files.items():
            (ws / k).parent.mkdir(parents=True, exist_ok=True)
            (ws / k).write_text(v, encoding="utf-8")
        return ws

    def ctx(ws):
        out = []
        for p in sorted(ws.rglob("*")):
            r = p.relative_to(ws).as_posix()
            out.append(r + "/" if p.is_dir() else r)
        return out

    with tempfile.TemporaryDirectory() as tmp:
        # 1. الإيقاف أثناء البحث عن المكررات يقطع التحليل (لا خطة ناقصة تُنفَّذ)
        ws = mk(tmp, "w1", {"a.txt": "same", "b.txt": "same", "c.txt": "other"})
        rp = RuleProvider(workspace=ws)
        rp.cancel_check = lambda: True
        try:
            rp.analyze("احذف المكررات", ctx(ws))
            raise AssertionError("كان يجب أن يُقطع البحث")
        except EmergencyStopTriggered:
            pass
        rp.cancel_check = None
        assert [s["target"] for s in rp.analyze("احذف المكررات", ctx(ws)).steps] == ["b.txt"]

        # 2. ملف غير مقروء لا يدخل مجموعة المكررات (لا يُحذف بالتخمين) ولا يكسر البحث
        ws = mk(tmp, "w2", {"a.txt": "same", "b.txt": "same", "c.txt": "same"})
        real_open = builtins.open

        def selective_open(f, mode="r", *a, **k):
            if "rb" in mode and str(f).endswith("b.txt"):
                raise PermissionError("locked by antivirus")
            return real_open(f, mode, *a, **k)
        with mock.patch("builtins.open", selective_open):
            steps = RuleProvider(workspace=ws).analyze("احذف المكررات", ctx(ws)).steps
        assert [s["target"] for s in steps] == ["c.txt"], steps

        # 3. ملف سحابي كبير غير محمّل يُتخطى (لا تنزيل صامت)، فلا مكررات حقيقية هنا
        ws = mk(tmp, "w3", {"a.txt": "same", "b.txt": "same"})

        def cloud(p):
            if str(p).endswith("b.txt"):
                raise CloudFileNotLocalError("cloud")
        with mock.patch.object(R, "check_not_big_cloud_file", cloud):
            a = RuleProvider(workspace=ws).analyze("احذف المكررات", ctx(ws))
        assert not a.steps, a.steps

        # 4. نقل المكررات: أسماء متعارضة بعد التسطيح -> سؤال، لا نقل يكتب فوق أحدها
        ws = mk(tmp, "w4", {"k.txt": "same", "a_b.txt": "same", "a/b.txt": "same"})
        a = RuleProvider(workspace=ws).analyze("انقل المكررات", ctx(ws))
        assert not a.steps and a.question and "تتعارض" in a.question, (a.steps, a.question)

        # 5. «مكررات» موجود فيه اسم مطابق لما سيُنقل -> سؤال
        ws = mk(tmp, "w5", {"a.txt": "same", "b.txt": "same", "مكررات/b.txt": "old"})
        a = RuleProvider(workspace=ws).analyze("انقل المكررات", ctx(ws))
        assert not a.steps and a.question and "مكررات" in a.question, (a.steps, a.question)

        # 6. الحذف الشامل/المبهم لا ينتج أي خطوة حذف أبداً
        ws = mk(tmp, "w6", {"a.txt": "1", "b.txt": "2", "Docs/n.txt": "3"})
        rp6 = RuleProvider(workspace=ws)
        for req in ("احذف كل الملفات", "احذف كل شيء", "احذف الكل", "احذف محتويات Docs", "امسح كل شيء", "احذف"):
            a = rp6.analyze(req, ctx(ws))
            assert not a.steps and (a.question or a.note), (req, a.steps)
        # التوليد النهائي بصيغة JSON يحمل نفس النتيجة (steps فارغة)
        import json
        out = json.loads(rp6.generate("احذف كل الملفات", "plan\n" + R.CONTEXT_MARKER + "\n" + "\n".join(ctx(ws))))
        assert out["steps"] == [] and out.get("note"), out

    print("PASS: test_cov_rules_duplicates_safety completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_rules_duplicates_safety", file=sys.stderr)
    sys.exit(1)
