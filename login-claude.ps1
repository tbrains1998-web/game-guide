# One-time: log the Claude Code CLI (bundled with the Claude desktop app) into your Claude
# account so Game Guide can use your subscription. --claudeai = subscription, never API billing.
# The Claude desktop app is a packaged (MSIX) app, so its files really live under Packages\Claude_*.
# App updates moved it one level deeper (claude-code\<ver>\<hash>\claude.exe), so search recursively.
# The native installer (the "claude" you can run from any terminal) puts claude.exe in ~\.local\bin.
# That copy is preferred when it exists; the desktop-app search below is the fallback.
$exe = $null
$local = "$env:USERPROFILE\.local\bin\claude.exe"
if (Test-Path $local -PathType Leaf) { $exe = $local }
if (-not $exe) {   # installed some other way (for example with npm): use the one on PATH, like the app does
    $cmd = Get-Command claude -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $exe = $cmd.Source }
}
if (-not $exe) {
    $roots = @("$env:APPDATA\Claude\claude-code") + @(Get-ChildItem "$env:LOCALAPPDATA\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code" -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName)
    $found = foreach ($r in $roots) {
        Get-ChildItem $r -Recurse -Filter claude.exe -ErrorAction SilentlyContinue | ForEach-Object {
            $ver = $_.FullName.Substring($r.Length + 1).Split('\')[0]
            if ($ver -match '^\d+(\.\d+)*$') { [pscustomobject]@{ V = [version]$ver; P = $_.FullName } }
        }
    }
    $exe = $found | Sort-Object V | Select-Object -Last 1 -ExpandProperty P
}
if (-not $exe) {
    Write-Host "Could not find claude.exe in $local or under $env:APPDATA\Claude\claude-code" -ForegroundColor Red
    Write-Host "Install Claude Code first: https://claude.com/claude-code"
    Read-Host "Press Enter to close"
    exit 1
}
Write-Host "Using $exe`n"
& $exe auth login --claudeai
Write-Host ""
& $exe auth status
Read-Host "`nPress Enter to close"
