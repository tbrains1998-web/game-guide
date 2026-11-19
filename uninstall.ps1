# Removes Game Guide's shortcuts, autostart and Settings > Apps entry.
# The program folder (with your chat history and notes) is left in place; delete it yourself if you want.
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Stop the running app: the supervisor and the app process both run guide.py.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*$dir\guide.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$links = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Game Guide.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Game Guide.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Startup")) "Game Guide.lnk")
)
foreach ($l in $links) { if (Test-Path $l) { Remove-Item $l -Force } }
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\GameGuide" -Recurse -Force -ErrorAction SilentlyContinue

Add-Type -AssemblyName PresentationFramework
[System.Windows.MessageBox]::Show("Game Guide was uninstalled.`n`nYour chats and notes are still in:`n$dir", "Game Guide") | Out-Null
