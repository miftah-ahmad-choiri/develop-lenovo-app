Set WshShell = CreateObject("WScript.Shell")

' Set working directory path
appDir = "C:\Users\MiftahAhmadChoiri\Deploy-App\develop-lenovo-app"

' Run Python App silently (0 = hidden window)
WshShell.Run "cmd /c set PYTHONIOENCODING=utf-8 && cd /d " & Chr(34) & appDir & Chr(34) & " && .venv\Scripts\python.exe run.py", 0, False

' Run Cloudflare Tunnel silently (0 = hidden window)
WshShell.Run "cmd /c cd /d " & Chr(34) & appDir & Chr(34) & " && .\cloudflared\cloudflared.exe tunnel --config cloudflared\config.yml run", 0, False
