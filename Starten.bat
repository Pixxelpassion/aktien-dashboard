@echo off
title Parqet Dashboard
cd /d "%~dp0"

echo ========================================
echo  Parqet Portfolio Dashboard
echo ========================================
echo.

:: Prüfe ob Python installiert ist
python --version >nul 2>&1
if errorlevel 1 (
    echo FEHLER: Python nicht gefunden. Bitte Python 3.10+ installieren.
    pause
    exit /b 1
)

:: Erstelle config.json falls nicht vorhanden
if not exist config.json (
    echo Erstelle config.json aus Vorlage...
    copy config.example.json config.json >nul
)

:: Installiere Abhängigkeiten falls nötig
if not exist .venv (
    echo Erstelle virtuelle Umgebung...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installiere/prüfe Abhängigkeiten...
pip install -q -r requirements.txt

echo.
echo Starte Server auf http://localhost:5000
echo Drücke Ctrl+C zum Beenden
echo.
python server.py
pause
