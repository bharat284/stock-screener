@echo off
rem Daily screener run (scheduled for 8:00 AM via Windows Task Scheduler).
cd /d E:\screener
if not exist logs mkdir logs
echo ===== %date% %time% ===== >> logs\run.log
.venv\Scripts\python.exe screener.py >> logs\run.log 2>&1
