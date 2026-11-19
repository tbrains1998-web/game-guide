# Installs Game Guide for the current user (no admin needed):
#   desktop + Start Menu shortcuts, and an entry in Settings > Apps so it can be uninstalled normally.
$ErrorActionPreference = "Stop"
$dir     = Split-Path -Parent $MyInvocation.MyCommand.Path
# Find pythonw.exe: the .venv in this folder (see README) first, then the Python launcher (pyw), else PATH.
$pythonw = $null
$venv = Join-Path $dir ".venv\Scripts\pythonw.exe"
$pyw = Get-Command pyw.exe -ErrorAction SilentlyContinue
if (Test-Path $venv) {
    $pythonw = $venv
} elseif ($pyw) {
    # try: with "Stop", a message from py on stderr (e.g. no Python 3 registered) would end the script here
    try { $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null } catch { $exe = $null }
    if ($exe) { $pythonw = Join-Path (Split-Path -Parent $exe) "pythonw.exe" }
}
if (-not $pythonw -or -not (Test-Path $pythonw)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pythonw = $cmd.Source }
}
if (-not $pythonw -or -not (Test-Path $pythonw)) {
    throw "Python 3 was not found. Install Python 3.12 from python.org, then run these two lines in this folder (see README): py -3.12 -m venv .venv   and   .venv\Scripts\python -m pip install -r requirements.txt"
}
$icon    = Join-Path $dir "web\icon.ico"
$shell   = New-Object -ComObject WScript.Shell

function New-Shortcut($path, $arguments) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $pythonw
    $s.Arguments = $arguments
    $s.WorkingDirectory = $dir
    $s.IconLocation = $icon
    $s.Description = "Game Guide - hold F10 in game to ask for help"
    $s.Save()
}

$main = "`"$dir\guide.py`""
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Game Guide.lnk") $main
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "Game Guide.lnk") $main

$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\GameGuide"
New-Item -Path $key -Force | Out-Null
$props = @{
    DisplayName     = "Game Guide"
    DisplayVersion  = "1.0"
    Publisher       = "Game Guide (local)"
    DisplayIcon     = $icon
    InstallLocation = $dir
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$dir\uninstall.ps1`""
    InstallDate     = (Get-Date -Format "yyyyMMdd")
}
foreach ($k in $props.Keys) { Set-ItemProperty -Path $key -Name $k -Value $props[$k] }
Set-ItemProperty -Path $key -Name NoModify -Value 1 -Type DWord
Set-ItemProperty -Path $key -Name NoRepair -Value 1 -Type DWord
Set-ItemProperty -Path $key -Name EstimatedSize -Value 1024 -Type DWord

"Installed Game Guide from $dir"
