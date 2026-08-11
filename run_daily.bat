@echo off
rem Tier 1: double-click to sync with the cloud collector, fetch stats + a
rem fresh odds snapshot, price today's slate, and push the capture back.
rem The GitHub collector is canonical for data/wnba.db: local db changes are
rem discarded before pulling (everything local is re-derivable; the cloud
rem archive is not).
cd /d "%~dp0"
echo === sync down (cloud collector is canonical) ===
git checkout -- data/wnba.db 2>nul
git pull
echo.
echo === update (stats + odds snapshot) ===
.venv\Scripts\python.exe run.py update
echo.
echo === clean (rebuild joins + features) ===
.venv\Scripts\python.exe run.py clean
echo.
echo === project (slate CSV; diagnostic only until CLV proves out) ===
.venv\Scripts\python.exe run.py project
echo.
echo === sync up (push this capture to the archive) ===
git add data/wnba.db data/raw/odds
git commit -m "local collect + slate" >nul 2>&1
git push
echo.
pause
