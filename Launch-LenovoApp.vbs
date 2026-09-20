' =============================================================================
'  Launch-LenovoApp.vbs  —  double-click to open the GUI launcher
'  No console window appears.
' =============================================================================
Dim appDir, psScript, cmd
appDir   = "C:\Users\MiftahAhmadChoiri\Deploy-App\develop-lenovo-app"
psScript = appDir & "\Launch-LenovoApp.ps1"
cmd      = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & psScript & """"
CreateObject("WScript.Shell").Run cmd, 0, False
