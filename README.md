# Computer Use — محرر الأكواد والأتمتة

مساعد أتمتة عربي لأعمال الحاسوب المتعبة (ترتيب ملفات، نسخ، تسمية، تحديث برامج…): تكتب ما تريد بالعربية، فيخطط وينفذ ويتحقق بأدلة ويقدم تقريراً.

**الحالة:** قيد البناء (المرحلة 1، ويندوز). المستودع: `github.com/anasalraji61-maker/ComputerUse-CodeEditor`.

## التشغيل
- المتطلبات: ويندوز + **Python 3.10 أو أحدث** من python.org مع tkinter (خيار tcl/tk عند التثبيت). لا مكتبات خارجية.
- التشغيل: انقر مرتين على `run_assistant.bat` (أو `python -m assistant`). يعمل بلا أي مفتاح بمخطط محلي مجاني.
- نموذج حقيقي اختياري: متغيرات البيئة `GEMINI_API_KEY` أو `ANTHROPIC_API_KEY` مع `COS_PROVIDER=gemini|anthropic|auto` (المفاتيح لا تُحفظ في أي ملف).
- مجلد العمل الافتراضي `~/COS_Workspace` (يتغير من الواجهة أو بـ `COS_WORKSPACE`)، وبيانات التطبيق في `~/.cos_assistant` (أو `COS_DATA_DIR`).
- ملف تنفيذي مستقل: `build_exe.bat` (قد يحذّر SmartScreen لأنه غير موقّع).
- التفاصيل: `docs/USER_GUIDE.md` (دليل المستخدم) · `docs/BENCH_HOWTO.md` (القياس) · `docs/SPEC_PHASE1.md` (المواصفات) · `docs/TASKS.md` (المهام).

- المواصفات: `docs/SPEC_PHASE1.md`
- المهام: `docs/TASKS.md`
- القواعد وبروتوكول العمل: `docs/RULES.md`
- نتائج الاختبارات الفعلية: `docs/TEST_RESULTS.md`

> المستودع عام. لا مفاتيح ولا بيانات شخصية ولا أسرار في أي ملف.
