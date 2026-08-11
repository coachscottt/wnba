@echo off
rem Tier 1: double-click to fetch stats + a fresh odds snapshot, then price
rem today's slate. Output stays on screen until you press a key.
rem Lives in the project root (next to run.py) — run it from anywhere.
cd /d "%~dp0"
echo === update (stats + odds snapshot) ===
.venv\Scripts\python.exe run.py update
echo.
echo === clean (rebuild joins + features) ===
.venv\Scripts\python.exe run.py clean
echo.
echo === project (slate CSV; diagnostic only until CLV proves out) ===
.venv\Scripts\python.exe run.py project
echo.
pause
