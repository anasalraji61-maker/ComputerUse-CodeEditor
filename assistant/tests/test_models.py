"""اختبار الواجهة الموحدة ModelProvider والمزوّد الوهمي FakeProvider."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from assistant.models.base import ModelProvider
    from assistant.models.fake import FakeProvider

    # التحقق من الوراثة
    assert issubclass(FakeProvider, ModelProvider), "FakeProvider يجب أن يرث من ModelProvider"

    # التحقق من السلوك الحتمي
    fake = FakeProvider(
        responses={
            "ترتيب": "خطة ترتيب الملفات",
            "نسخ": "خطة نسخ المجلد",
        },
        default_response="خطة افتراضية",
    )

    # 1. مطابقة كلمة مفتاحية
    res1 = fake.generate("يرجى ترتيب مجلد التنزيلات", system_prompt="أنت مساعد مكتب")
    assert res1 == "خطة ترتيب الملفات", f"فشل الرد الحتمي: {res1}"

    # 2. مطابقة كلمة مفتاحية أخرى
    res2 = fake.generate("نسخ الملفات إلى القرص الخارجي")
    assert res2 == "خطة نسخ المجلد", f"فشل الرد الحتمي الثاني: {res2}"

    # 3. الرد الافتراضي
    res3 = fake.generate("طلب غير معروف")
    assert res3 == "خطة افتراضية", f"فشل الرد الافتراضي: {res3}"

    # 4. سجل الاستدعاءات
    assert len(fake.call_history) == 3, f"عدد الاستدعاءات غير متطابق: {len(fake.call_history)}"
    assert fake.call_history[0]["system_prompt"] == "أنت مساعد مكتب", "فشل تسجيل system_prompt"

    # 5. الاسم
    assert "FakeProvider" in fake.name(), "اسم المزود غير متطابق"

    print("PASS: FakeProvider and ModelProvider verified successfully")
    sys.exit(0)
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
