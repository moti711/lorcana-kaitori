@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONIOENCODING=utf-8
echo ===== %date% %time% =====>> logs\kaitori.log
"C:\Users\yudai\AppData\Local\Programs\Python\Python312\python.exe" kaitori.py >> logs\kaitori.log 2>&1
