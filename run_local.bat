@echo off
setlocal
title Woeyyy Discord Bot [Local Direct Stream Mode]
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

python -c "import discord, yt_dlp, imageio_ffmpeg, aiohttp" >nul 2>&1
if errorlevel 1 (
    if not exist ".venv\Scripts\activate.bat" (
        echo Creating virtual environment...
        python -m venv .venv
        if errorlevel 1 (
            echo [ERROR] Failed to create virtual environment.
            pause
            exit /b 1
        )
    )
    call .venv\Scripts\activate.bat
    echo Installing dependencies from requirements.txt...
    python -m pip install --upgrade pip --quiet
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

set "BOT_MODE=local"
python main.py --local %*
if errorlevel 1 (
    pause
)
