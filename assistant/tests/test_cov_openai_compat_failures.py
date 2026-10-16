"""تغطية مزوّد OpenAI المتوافق: خادم يرفض المعاملات إلى ما لا نهاية لا يُدخلنا في حلقة ولا يُفوتر بلا حدّ، رفض الاتصال بأنواعه، نموذج محلي غير موجود
يعرض البدائل، أجزاء المحتوى النصية، وفشل جلب قائمة النماذج لا يكسر رسالة الخطأ."""
import json
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.openai_compat import OpenAICompatProvider, _default_get
    from assistant.models.http_base import ProviderError

    KEY = "sk" + "-FAKEFAKEFAKEFAKEFAKEFAKEFAKE1234"

    class Net:
        def __init__(self, fn):
            self.fn, self.calls = fn, 0

        def __call__(self, url, headers, body, timeout):
            self.calls += 1
            return self.fn(self.calls, json.loads(body.decode("utf-8")))

    def ok(content):
        return 200, json.dumps({"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}).encode()

    def raises(fn, fragment=None):
        try:
            fn()
        except ProviderError as exc:
            if fragment:
                assert fragment in str(exc), (fragment, str(exc))
            assert KEY not in str(exc)
            return exc
        raise AssertionError("كان يجب رفع ProviderError")

    # 1. خادم يرفض temperature/max_tokens/response_format دائماً: عدد النداءات محدود ثم خطأ واضح (لا حلقة لا نهائية)
    net = Net(lambda n, body: (400, b'{"error":"unsupported parameter: temperature and max_completion_tokens and response_format"}'))
    p = OpenAICompatProvider(model="gpt-x", base_url="https://api.openai.com/v1", api_key=KEY, transport=net, sleep=lambda s: None, max_retries=0)
    raises(lambda: p.generate("x"))
    assert net.calls <= 4 * 2, net.calls
    assert p.usage.calls == 0 and p.usage.usd == 0.0     # 400 لا يُفوتر

    # 2. محتوى على شكل أجزاء نصية
    net = Net(lambda n, body: ok([{"type": "text", "text": '{"steps":'}, {"type": "text", "text": " []}"}, "ignored", 5]))
    p = OpenAICompatProvider(model="m", base_url="https://api.openai.com/v1", api_key=KEY, transport=net, sleep=lambda s: None)
    assert json.loads(p.generate("x")) == {"steps": []}

    # 3. نموذج غير موجود على خادم محلي عام: تُعرض النماذج المتاحة؛ وفشل جلب القائمة لا يكسر الرسالة
    def get_ok(url, headers, timeout):
        return 200, json.dumps({"data": [{"id": "llama3"}, {"id": "qwen"}, {"nope": 1}, "x"]}).encode()

    def get_boom(url, headers, timeout):
        raise OSError("down")

    def get_500(url, headers, timeout):
        return 500, b""
    for getter, expect_models in ((get_ok, True), (get_boom, False), (get_500, False)):
        p = OpenAICompatProvider(model="missing-m", base_url="http://127.0.0.1:1234/v1", local=True, transport=Net(lambda n, b: (404, b"nf")),
                                 get_transport=getter, sleep=lambda s: None, max_retries=0)
        e = raises(lambda: p.generate("x"), "غير موجود على الخادم المحلي")
        assert ("llama3" in str(e)) is expect_models, str(e)

    # 4. رفض الاتصال: خادم بعيد يبلّغ "محجوب"، محلي يقول "لا يعمل"، وكلاهما يحمل علامة refused
    def refuse(n, body):
        raise ConnectionRefusedError("no")
    p = OpenAICompatProvider(model="m", base_url="https://llm.example.com/v1", api_key=KEY, transport=Net(refuse), sleep=lambda s: None, max_retries=0)
    e = raises(lambda: p.generate("x"), "رفض الخادم")
    assert getattr(e, "refused", False)
    p = OpenAICompatProvider(model="m", base_url="http://127.0.0.1:9/v1", local=True, transport=Net(refuse), sleep=lambda s: None, max_retries=0)
    e = raises(lambda: p.generate("x"), "لا يعمل")
    assert getattr(e, "refused", False)

    # 5. جلب افتراضي لخادم غير موجود: (0, b"") لا استثناء
    assert _default_get("http://127.0.0.1:9/models", {}, 1.0) == (0, b"")

    print("PASS: test_cov_openai_compat_failures completed successfully")
    sys.exit(0)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    print("FAIL: test_cov_openai_compat_failures", file=sys.stderr)
    sys.exit(1)
