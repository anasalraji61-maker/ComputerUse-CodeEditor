@echo off
rem مزامنة المستودع مع GitHub تلقائيا: سحب ثم تشغيل الاختبارات (عند تغير الكود) ثم رفع.
cd /d "%~dp0.."
git rev-parse --is-inside-work-tree >nul 2>&1 || exit /b 1

rem حماية: لا مزامنة اذا كان ملف .env غير محمي بـ gitignore
if exist ".env" (
  git check-ignore -q .env || (echo STOP: .env is not ignored >> tools\sync.log & exit /b 2)
)

rem ملفات السجل لا ترفع
findstr /x /c:"tools/sync.log" .gitignore >nul 2>&1 || echo tools/sync.log>>.gitignore
findstr /x /c:"tools/.last_tested" .gitignore >nul 2>&1 || echo tools/.last_tested>>.gitignore

for /f %%b in ('git rev-parse --abbrev-ref HEAD') do set BR=%%b

git pull --rebase --autostash origin %BR% >> tools\sync.log 2>&1
if errorlevel 1 (
  echo PULL FAILED - rebase aborted, nothing pushed >> tools\sync.log
  git rebase --abort >> tools\sync.log 2>&1
  exit /b 4
)
git diff --name-only --diff-filter=U 2>nul | findstr . >nul && (echo CONFLICT MARKERS - nothing pushed >> tools\sync.log & exit /b 6)
if exist ".git\rebase-merge" exit /b 5
if exist ".git\rebase-apply" exit /b 5

rem اختبارات فعلية (تعيد التشغيل فقط عند تغير ملفات py). عند الفشل: لا يرفع الكود، يرفع ملف النتائج فقط لتراه
set PYEXE=py
where python >nul 2>&1 && set PYEXE=python
%PYEXE% tools\run_tests.py >> tools\sync.log 2>&1
if errorlevel 1 (
  echo TESTS FAILING - code not pushed >> tools\sync.log
  git reset -q
  git add docs/TEST_RESULTS.md
  git diff --cached --quiet || git commit -m "sync: test results only (tests failing, code not pushed)" >> tools\sync.log 2>&1
  git push origin %BR% >> tools\sync.log 2>&1
  exit /b 3
)

git add -A
git diff --cached --quiet || git commit -m "sync: local changes %DATE% %TIME%" >> tools\sync.log 2>&1
git push origin %BR% >> tools\sync.log 2>&1
exit /b 0
