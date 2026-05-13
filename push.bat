@echo off
echo ========================================
echo BSC Inspection - Push to GitHub
echo ========================================
echo.

cd /d "%~dp0"

git add .
echo.

set /p msg="Enter commit message (or press Enter for 'update'): "
if "%msg%"=="" set msg=update

git commit -m "%msg%"
echo.

git push origin main
echo.

echo ========================================
echo Done! Render will auto-deploy in 2-3 mins
echo ========================================
pause
