# تشغيل النموذج الحقيقي والقياس (يدوي من المالك)

المفاتيح تُوضع في متغيرات البيئة فقط، ولا تُكتب في أي ملف داخل المستودع.

## تشغيل التطبيق بنموذج حقيقي
في نافذة CMD (المفتاح يبقى لهذه النافذة فقط):

    set ANTHROPIC_API_KEY=ضع_المفتاح_هنا
    set COS_PROVIDER=anthropic
    run_assistant.bat

لـ Gemini: `set GEMINI_API_KEY=...` و`set COS_PROVIDER=gemini`. القيمة `auto` تختار المتاح.
نماذج مخصصة: `COS_SMALL_MODEL` و`COS_LARGE_MODEL`.

## القياس (يستهلك رصيداً فعلياً بسقف حماية)

    python -m bench.run_bench --provider anthropic --runs 5 --max-usd 1.0

يكتب `docs/MEASUREMENT.md`: نسبة النجاح لكل مهمة، الخطوات، التوكنات، الكلفة بالدولار والنقاط. يتوقف تلقائياً عند بلوغ `--max-usd`. تحديث winget محاكى دائماً؛ لا يتغير أي برنامج على جهازك.
