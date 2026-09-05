@echo off
setlocal
title Woeyyy Discord Bot
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python and ensure it is added to PATH.
    pause
    exit /b 1
)

if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

python main.py %*
if errorlevel 1 (
    pause
)
