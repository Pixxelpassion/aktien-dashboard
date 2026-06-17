@echo off
title Parqet Sync
cd /d "%~dp0"

echo ========================================
echo  Parqet Manueller Sync
echo ========================================
echo.

call .venv\Scripts\activate.bat 2>nul
if errorlevel 1 (
    python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -q -r requirements.txt
)

python sync.py
echo.
echo Sync abgeschlossen.
pause
