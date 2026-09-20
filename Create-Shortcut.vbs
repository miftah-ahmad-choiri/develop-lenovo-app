' =============================================================================
'  Create-Shortcut.vbs  —  run ONCE to place a Desktop shortcut
' =============================================================================
Dim appDir, vbsPath, iconPath, shortcutPath
appDir       = "C:\Users\MiftahAhmadChoiri\Deploy-App\develop-lenovo-app"
vbsPath      = appDir & "\Launch-LenovoApp.vbs"
iconPath     = appDir & "\app\static\launcher.ico"
shortcutPath = CreateObject("WScript.Shell").SpecialFolders("Desktop") & "\Lenovo ASP Launcher.lnk"

Dim oShell, oLink
Set oShell = CreateObject("WScript.Shell")
Set oLink  = oShell.CreateShortcut(shortcutPath)
oLink.TargetPath       = "wscript.exe"
oLink.Arguments        = Chr(34) & vbsPath & Chr(34)
oLink.WorkingDirectory = appDir
oLink.Description      = "Lenovo ASP App Launcher"
oLink.IconLocation     = iconPath
oLink.Save

MsgBox "Shortcut created on your Desktop!" & Chr(10) & shortcutPath, 64, "Done"
