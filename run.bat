@echo off
cd /d "%~dp0"
python kaitori.py
start "" lorcana_kaitori_all.html
pause
