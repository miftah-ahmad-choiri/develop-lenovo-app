@echo off
set PYTHONIOENCODING=utf-8

:: Change to app directory
cd /d "C:\Users\MiftahAhmadChoiri\Deploy-App\develop-lenovo-app"

:: 1. Start Python App in a new window
start "Lenovo App" cmd /k "cd /d C:\Users\MiftahAhmadChoiri\Deploy-App\develop-lenovo-app && .venv\Scripts\python.exe run.py"

:: 2. Start Cloudflare Tunnel in a separate window
start "Cloudflare Tunnel" cmd /k "cd /d C:\Users\MiftahAhmadChoiri\Deploy-App\develop-lenovo-app && .\cloudflared\cloudflared.exe tunnel --config cloudflared\config.yml run"
