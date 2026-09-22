# Lenovo ASP - Service Launcher
$AppDir   = "C:\Users\MiftahAhmadChoiri\Deploy-App\develop-lenovo-app"
$IconPath = "$AppDir\app\static\launcher.ico"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# P/Invoke: SendMessage to push custom icon into the taskbar button
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAPI {
    [DllImport("user32.dll", CharSet=CharSet.Auto)]
    public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
    public const int WM_SETICON  = 0x0080;
    public const int ICON_SMALL  = 0;
    public const int ICON_BIG    = 1;
}
"@

$script:FlaskProc       = $null
$script:CloudflaredProc = $null

# Form
$Form = New-Object System.Windows.Forms.Form
$Form.Text            = "Lenovo ASP Launcher"
$Form.Size            = New-Object System.Drawing.Size(440, 320)
$Form.StartPosition   = "CenterScreen"
$Form.FormBorderStyle = "FixedSingle"
$Form.MaximizeBox     = $false
$Form.BackColor       = [System.Drawing.Color]::FromArgb(245,245,245)

# Apply icon: form title bar
$script:AppIcon = $null
if (Test-Path $IconPath) {
    $script:AppIcon = New-Object System.Drawing.Icon($IconPath)
    $Form.Icon = $script:AppIcon
}

# Title label
$lblTitle           = New-Object System.Windows.Forms.Label
$lblTitle.Text      = "Lenovo ASP - Service Launcher"
$lblTitle.Font      = New-Object System.Drawing.Font("Segoe UI",13,[System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = [System.Drawing.Color]::FromArgb(31,35,40)
$lblTitle.AutoSize  = $true
$lblTitle.Location  = New-Object System.Drawing.Point(20,18)
$Form.Controls.Add($lblTitle)

$sep             = New-Object System.Windows.Forms.Label
$sep.BorderStyle = "Fixed3D"
$sep.Size        = New-Object System.Drawing.Size(400,2)
$sep.Location    = New-Object System.Drawing.Point(20,52)
$Form.Controls.Add($sep)

# Flask row
$lblFlaskTitle          = New-Object System.Windows.Forms.Label
$lblFlaskTitle.Text     = "Flask App  (port 5050)"
$lblFlaskTitle.Font     = New-Object System.Drawing.Font("Segoe UI",10)
$lblFlaskTitle.AutoSize = $true
$lblFlaskTitle.Location = New-Object System.Drawing.Point(20,70)
$Form.Controls.Add($lblFlaskTitle)

$script:dotFlask           = New-Object System.Windows.Forms.Label
$script:dotFlask.Text      = "[ ] Stopped"
$script:dotFlask.Font      = New-Object System.Drawing.Font("Segoe UI",10,[System.Drawing.FontStyle]::Bold)
$script:dotFlask.ForeColor = [System.Drawing.Color]::Gray
$script:dotFlask.AutoSize  = $true
$script:dotFlask.Location  = New-Object System.Drawing.Point(240,70)
$Form.Controls.Add($script:dotFlask)

# Cloudflare row
$lblCfTitle          = New-Object System.Windows.Forms.Label
$lblCfTitle.Text     = "Cloudflare Tunnel"
$lblCfTitle.Font     = New-Object System.Drawing.Font("Segoe UI",10)
$lblCfTitle.AutoSize = $true
$lblCfTitle.Location = New-Object System.Drawing.Point(20,102)
$Form.Controls.Add($lblCfTitle)

$script:dotCf           = New-Object System.Windows.Forms.Label
$script:dotCf.Text      = "[ ] Stopped"
$script:dotCf.Font      = New-Object System.Drawing.Font("Segoe UI",10,[System.Drawing.FontStyle]::Bold)
$script:dotCf.ForeColor = [System.Drawing.Color]::Gray
$script:dotCf.AutoSize  = $true
$script:dotCf.Location  = New-Object System.Drawing.Point(240,102)
$Form.Controls.Add($script:dotCf)

# Links
$lblUrl          = New-Object System.Windows.Forms.LinkLabel
$lblUrl.Text     = "https://app.ticket-asp.my.id"
$lblUrl.Font     = New-Object System.Drawing.Font("Segoe UI",9)
$lblUrl.Location = New-Object System.Drawing.Point(20,136)
$lblUrl.AutoSize = $true
$lblUrl.Enabled  = $false
$lblUrl.Add_LinkClicked({ Start-Process "https://app.ticket-asp.my.id" })
$Form.Controls.Add($lblUrl)

$lblLocal          = New-Object System.Windows.Forms.LinkLabel
$lblLocal.Text     = "http://localhost:5050"
$lblLocal.Font     = New-Object System.Drawing.Font("Segoe UI",9)
$lblLocal.Location = New-Object System.Drawing.Point(20,158)
$lblLocal.AutoSize = $true
$lblLocal.Enabled  = $false
$lblLocal.Add_LinkClicked({ Start-Process "http://localhost:5050" })
$Form.Controls.Add($lblLocal)

# Log box
$logBox            = New-Object System.Windows.Forms.TextBox
$logBox.Multiline  = $true
$logBox.ScrollBars = "Vertical"
$logBox.ReadOnly   = $true
$logBox.Size       = New-Object System.Drawing.Size(400,58)
$logBox.Location   = New-Object System.Drawing.Point(20,184)
$logBox.BackColor  = [System.Drawing.Color]::FromArgb(247,248,250)
$logBox.Font       = New-Object System.Drawing.Font("Consolas",8)
$Form.Controls.Add($logBox)

function Write-Log([string]$msg) {
    $ts = (Get-Date).ToString("HH:mm:ss")
    $logBox.AppendText("[$ts] $msg`r`n")
}

# Buttons
$btnStart           = New-Object System.Windows.Forms.Button
$btnStart.Text      = "Start All"
$btnStart.Size      = New-Object System.Drawing.Size(125,36)
$btnStart.Location  = New-Object System.Drawing.Point(20,252)
$btnStart.BackColor = [System.Drawing.Color]::FromArgb(59,130,212)
$btnStart.ForeColor = [System.Drawing.Color]::White
$btnStart.FlatStyle = "Flat"
$btnStart.Font      = New-Object System.Drawing.Font("Segoe UI",10,[System.Drawing.FontStyle]::Bold)
$Form.Controls.Add($btnStart)

$btnStop           = New-Object System.Windows.Forms.Button
$btnStop.Text      = "Stop All"
$btnStop.Size      = New-Object System.Drawing.Size(125,36)
$btnStop.Location  = New-Object System.Drawing.Point(158,252)
$btnStop.BackColor = [System.Drawing.Color]::FromArgb(220,53,69)
$btnStop.ForeColor = [System.Drawing.Color]::White
$btnStop.FlatStyle = "Flat"
$btnStop.Font      = New-Object System.Drawing.Font("Segoe UI",10,[System.Drawing.FontStyle]::Bold)
$btnStop.Enabled   = $false
$Form.Controls.Add($btnStop)

$btnBrowser           = New-Object System.Windows.Forms.Button
$btnBrowser.Text      = "Open App"
$btnBrowser.Size      = New-Object System.Drawing.Size(115,36)
$btnBrowser.Location  = New-Object System.Drawing.Point(305,252)
$btnBrowser.BackColor = [System.Drawing.Color]::FromArgb(40,167,69)
$btnBrowser.ForeColor = [System.Drawing.Color]::White
$btnBrowser.FlatStyle = "Flat"
$btnBrowser.Font      = New-Object System.Drawing.Font("Segoe UI",10,[System.Drawing.FontStyle]::Bold)
$btnBrowser.Enabled   = $false
$Form.Controls.Add($btnBrowser)

# Timer: poll process liveness every 2 s
$timer          = New-Object System.Windows.Forms.Timer
$timer.Interval = 2000
$timer.Add_Tick({
    if ($script:FlaskProc -and -not $script:FlaskProc.HasExited) {
        $script:dotFlask.Text      = "[OK] Running"
        $script:dotFlask.ForeColor = [System.Drawing.Color]::FromArgb(40,167,69)
    } else {
        $script:dotFlask.Text      = "[ ] Stopped"
        $script:dotFlask.ForeColor = [System.Drawing.Color]::Gray
    }
    if ($script:CloudflaredProc -and -not $script:CloudflaredProc.HasExited) {
        $script:dotCf.Text      = "[OK] Running"
        $script:dotCf.ForeColor = [System.Drawing.Color]::FromArgb(40,167,69)
    } else {
        $script:dotCf.Text      = "[ ] Stopped"
        $script:dotCf.ForeColor = [System.Drawing.Color]::Gray
    }
})

# Start click
$btnStart.Add_Click({
    if (-not $script:FlaskProc -or $script:FlaskProc.HasExited) {
        Write-Log "Starting Flask app..."
        $psi                  = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName         = "$AppDir\.venv\Scripts\python.exe"
        $psi.Arguments        = "run.py"
        $psi.WorkingDirectory = $AppDir
        $psi.UseShellExecute  = $false
        $psi.CreateNoWindow   = $false
        $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
        $script:FlaskProc = [System.Diagnostics.Process]::Start($psi)
        Write-Log "Flask PID: $($script:FlaskProc.Id)"
    }
    if (-not $script:CloudflaredProc -or $script:CloudflaredProc.HasExited) {
        Write-Log "Starting Cloudflare Tunnel..."
        $psi2                  = New-Object System.Diagnostics.ProcessStartInfo
        $psi2.FileName         = "$AppDir\cloudflared\cloudflared.exe"
        $psi2.Arguments        = "tunnel --config cloudflared\config.yml run"
        $psi2.WorkingDirectory = $AppDir
        $psi2.UseShellExecute  = $false
        $psi2.CreateNoWindow   = $false
        $script:CloudflaredProc = [System.Diagnostics.Process]::Start($psi2)
        Write-Log "Cloudflared PID: $($script:CloudflaredProc.Id)"
    }
    $timer.Start()
    $btnStart.Enabled   = $false
    $btnStop.Enabled    = $true
    $btnBrowser.Enabled = $true
    $lblUrl.Enabled     = $true
    $lblLocal.Enabled   = $true
    Write-Log "Both services started."
})

# Stop click â€” null refs immediately so timer shows Stopped at once
$btnStop.Add_Click({
    Write-Log "Stopping services..."
    if ($script:FlaskProc -and -not $script:FlaskProc.HasExited) {
        $script:FlaskProc.Kill()
        $script:FlaskProc.WaitForExit(3000)
        Write-Log "Flask stopped."
    }
    $script:FlaskProc = $null

    if ($script:CloudflaredProc -and -not $script:CloudflaredProc.HasExited) {
        $script:CloudflaredProc.Kill()
        $script:CloudflaredProc.WaitForExit(3000)
        Write-Log "Cloudflare Tunnel stopped."
    }
    $script:CloudflaredProc = $null

    $script:dotFlask.Text      = "[ ] Stopped"
    $script:dotFlask.ForeColor = [System.Drawing.Color]::Gray
    $script:dotCf.Text         = "[ ] Stopped"
    $script:dotCf.ForeColor    = [System.Drawing.Color]::Gray

    $timer.Stop()
    $btnStart.Enabled   = $true
    $btnStop.Enabled    = $false
    $btnBrowser.Enabled = $false
    $lblUrl.Enabled     = $false
    $lblLocal.Enabled   = $false
})

# Open App click
$btnBrowser.Add_Click({ Start-Process "http://localhost:5050" })

# Form close: kill children
$Form.Add_FormClosing({
    $timer.Stop()
    if ($script:FlaskProc -and -not $script:FlaskProc.HasExited)             { $script:FlaskProc.Kill() }
    if ($script:CloudflaredProc -and -not $script:CloudflaredProc.HasExited) { $script:CloudflaredProc.Kill() }
    if ($script:AppIcon) { $script:AppIcon.Dispose() }
})

# Push custom icon to taskbar button via SendMessage (overrides PowerShell host icon)
$Form.Add_Shown({
    if ($script:AppIcon) {
        $hwnd = $Form.Handle
        [WinAPI]::SendMessage($hwnd, [WinAPI]::WM_SETICON, [IntPtr][WinAPI]::ICON_BIG,   $script:AppIcon.Handle) | Out-Null
        [WinAPI]::SendMessage($hwnd, [WinAPI]::WM_SETICON, [IntPtr][WinAPI]::ICON_SMALL, $script:AppIcon.Handle) | Out-Null
    }
    # Auto-start services on open
    $btnStart.PerformClick()
})

[System.Windows.Forms.Application]::Run($Form)