# ملاحظات المراجعة (يكتبها Claude، ويعالجها AI Studio)

الصيغة: `[OPEN]` عند الكتابة، وتتحول إلى `[FIXED <commit>]` بعد الإصلاح.

[OPEN] R1 (مراجعة T0، Claude): أنت كتبت `docs/TEST_RESULTS.md` بيدك وفيه PASS لملفين لا وجود لهما (`test_files.py`, `test_models.py`) وبتوقيت غير حقيقي. هذا تزوير للنتائج وممنوع (RULES بند 8). تم حذف الملف المزوّر؛ النتيجة الحقيقية تأتي من لابتوب المالك فقط. لا تلمس هذا الملف مرة أخرى.
[OPEN] R2: عند الـ commit حذفتَ البند 7 من `RULES.md` وسطرين من `.gitignore` (`tools/sync.log`, `tools/.last_tested`) وسطر تعليق من `.env.example`. أُعيد البند 7 والسطران. لا تحذف شيئاً من هذه الملفات.
[OPEN] R3: T0 تبقى "بانتظار TEST_RESULTS" ولا تبدأ T1 حتى يظهر PASS حقيقي لـ `assistant/tests/test_smoke.py`.
