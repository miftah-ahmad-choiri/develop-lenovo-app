' =============================================================================
'  Launch-LenovoApp.vbs  —  double-click to open the GUI launcher
'  No console window appears.
' =============================================================================
Dim fso, scriptDir, psScript, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
psScript = scriptDir & "\Launch-LenovoApp.ps1"
cmd      = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & psScript & """"
CreateObject("WScript.Shell").Run cmd, 0, False
