@echo off
rem تشغيل مرة واحدة: يسجل مهمة مجدولة تشغل sync_repo.bat كل 5 دقائق
schtasks /Create /F /SC MINUTE /MO 5 /TN "COS_RepoSync" /TR "\"%~dp0sync_repo.bat\""
echo.
echo Done. To remove: schtasks /Delete /TN COS_RepoSync /F
pause
