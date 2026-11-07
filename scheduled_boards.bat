@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
"C:\Users\Admin1\AppData\Local\Programs\Python\Python313\python.exe" run.py --once --source remoteok,remotive >> logs\boards.log 2>&1
