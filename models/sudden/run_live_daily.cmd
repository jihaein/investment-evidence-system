@echo off
rem Sudden-drop model daily run (Windows Task Scheduler, weekdays 18:30).
rem Log: models\sudden\cache\live\logs\YYYYMMDD.log  Status: models\sudden\cache\live\YYYYMMDD\run_status.json
rem Optional date: run_live_daily.cmd 2026-11-09   (ASCII only: cmd.exe misreads UTF-8 comments)
cd /d "%~dp0..\.."
set D=%1
if "%D%"=="" set D=today
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" models\sudden\live.py --date %D% --log --skip-done
