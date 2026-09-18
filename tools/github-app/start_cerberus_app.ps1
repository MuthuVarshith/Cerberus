# Runs the Cerberus GitHub App on this machine, with GitHub deliveries relayed through smee.io.
# Windows PowerShell. See "Use Cerberus on your own GitHub repository" in the README.
#
#   powershell -ExecutionPolicy Bypass -File tools\github-app\start_cerberus_app.ps1
#
# It asks for your smee URL, App ID, private key file and webhook secret. The secret is read
# as hidden input and only placed in this process's environment; nothing is written to disk.
# Press Ctrl+C to stop both the forwarder and the service.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cerberus = (Resolve-Path (Join-Path $here "..\..\autonomous-pr-fixer")).Path
$logs = Join-Path $here "logs"
New-Item -ItemType Directory -Force $logs | Out-Null

Write-Host "`n== Cerberus GitHub App - live test ==`n" -ForegroundColor Cyan

# 1. Docker must be up: the App refuses to run repository code without the sandbox.
docker version --format "{{.Server.Version}}" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker is not running. Start Docker Desktop, wait for it to be ready, and run this again." -ForegroundColor Red
    exit 1
}
Write-Host "[ok] Docker is running"

# Python: set CERBERUS_PYTHON to choose one. Otherwise the py launcher, then `python` on PATH. The Microsoft Store
# "python" alias is skipped: Start-Process cannot launch it.
$python = $null; $pyArgs = @()
if ($env:CERBERUS_PYTHON -and (Test-Path $env:CERBERUS_PYTHON)) { $python = $env:CERBERUS_PYTHON }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $python = (Get-Command py).Source; $pyArgs = @("-3") }
elseif ((Get-Command python -ErrorAction SilentlyContinue) -and (Get-Command python).Source -notlike "*WindowsApps*") { $python = (Get-Command python).Source }
if (-not $python) { Write-Host "Python was not found. Install Python 3.11+ and run this again." -ForegroundColor Red; exit 1 }
& $python @pyArgs -c "import uvicorn, fastapi" *> $null
if ($LASTEXITCODE -ne 0) { Write-Host "This Python lacks uvicorn/fastapi: run  $python -m pip install -r $cerberus\requirements.txt" -ForegroundColor Red; exit 1 }
Write-Host "[ok] Python: $python $pyArgs"

# 2. Details from the GitHub App settings page.
$smee = Read-Host "smee URL (https://smee.io/...)"
if ($smee -notmatch '^https://smee\.io/\S+$') { Write-Host "That is not a smee.io URL." -ForegroundColor Red; exit 1 }
$appId = Read-Host "GitHub App ID (a number on the App's settings page)"
if ($appId -notmatch '^\d+$') { Write-Host "The App ID is a number." -ForegroundColor Red; exit 1 }

$newestKey = Get-ChildItem "$env:USERPROFILE\Downloads" -Filter "*.private-key.pem" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
$prompt = "Private key file"
if ($newestKey) { $prompt += " [Enter for $($newestKey.FullName)]" }
$keyPath = Read-Host $prompt
if (-not $keyPath -and $newestKey) { $keyPath = $newestKey.FullName }
$keyPath = $keyPath.Trim('"')
if (-not (Test-Path $keyPath)) { Write-Host "Key file not found: $keyPath" -ForegroundColor Red; exit 1 }

$secure = Read-Host "Webhook secret (the text you typed in the App's 'Webhook secret' box; hidden)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $secret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if (-not $secret) { Write-Host "The webhook secret cannot be empty." -ForegroundColor Red; exit 1 }

# 3. Configure this process only.
$env:GITHUB_APP_ID = $appId
$env:GITHUB_APP_PRIVATE_KEY_PATH = $keyPath
$env:GITHUB_WEBHOOK_SECRET = $secret
$env:CERBERUS_SANDBOX = "docker"
Remove-Variable secret

# 4. Forwarder and Cerberus service in the background; this window becomes a live view.
$ErrorActionPreference = "Continue"
$forwarder = Start-Process -FilePath $python -ArgumentList ($pyArgs + @("`"$here\smee_forward.py`"", $smee, "http://127.0.0.1:8000/webhook")) `
    -RedirectStandardOutput "$logs\forwarder.log" -RedirectStandardError "$logs\forwarder.err.log" -PassThru -WindowStyle Hidden
$service = Start-Process -FilePath $python -ArgumentList ($pyArgs + @("-m", "uvicorn", "github.webhook_handler:app", "--host", "127.0.0.1", "--port", "8000")) `
    -WorkingDirectory $cerberus -RedirectStandardOutput "$logs\service.log" -RedirectStandardError "$logs\service.err.log" -PassThru -WindowStyle Hidden

$ready = $false
for ($i = 0; $i -lt 40 -and -not $ready; $i++) {
    Start-Sleep -Milliseconds 500
    try { $ready = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/health" -TimeoutSec 2).StatusCode -eq 200 } catch { }
}
if (-not $ready) {
    Write-Host "Cerberus did not start. Details: $logs\service.err.log" -ForegroundColor Red
    foreach ($p in @($forwarder, $service)) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force } }
    exit 1
}
$connected = $false
for ($i = 0; $i -lt 20 -and -not $connected; $i++) {
    $connected = [bool](Select-String -Path "$logs\forwarder.log" -Pattern "connected to" -Quiet -ErrorAction SilentlyContinue)
    if (-not $connected) { Start-Sleep -Milliseconds 500 }
}
if ($connected) { Write-Host "[ok] Forwarder connected to $smee" }
else { Write-Host "[!!] Forwarder has not connected to smee.io yet; check $logs\forwarder.log" -ForegroundColor DarkYellow }
Write-Host "[ok] Cerberus is running on http://127.0.0.1:8000 (a service, not a web page)"
Write-Host "`nWaiting for GitHub. Comment  /cerberus verify  on a pull request. Ctrl+C to stop.`n" -ForegroundColor Yellow

# Live view: new GitHub deliveries, then every line of each verification run as it is written.
$runDir = Join-Path $cerberus ".cerberus-app\work"
$seen = @{}
Get-ChildItem "$runDir\*\cli.log" -ErrorAction SilentlyContinue | ForEach-Object { $seen[$_.FullName] = @(Get-Content $_.FullName).Count }
$fwdSeen = @(Get-Content "$logs\forwarder.log" -ErrorAction SilentlyContinue).Count

function Show-RunLine([string]$line) {
    if (-not $line.Trim() -or $line -like "INFO:*") { return }
    $color = "Gray"
    if ($line -match '^\[\d/9\]') { $color = "White" }
    elseif ($line -match '\[OK\]|GATE \d: PASS') { $color = "Green" }
    elseif ($line -match 'REFUSED|FAIL|ERROR|regression') { $color = "Red" }
    elseif ($line -match '^Final state|^Reason') { $color = "Yellow" }
    Write-Host "  $line" -ForegroundColor $color
}

try {
    while ($true) {
        if ($service.HasExited) { Write-Host "Cerberus stopped unexpectedly. Details: $logs\service.err.log" -ForegroundColor Red; break }

        $fwd = @(Get-Content "$logs\forwarder.log" -ErrorAction SilentlyContinue)
        for ($i = $fwdSeen; $i -lt $fwd.Count; $i++) {
            $l = $fwd[$i]
            if ($l -match 'forwarded (\S+) -> (\d+) (.*)$') {
                $evt, $code, $reply = $Matches[1], $Matches[2], $Matches[3]
                if ($reply -match '"status":"queued","run_id":"([^"]+)"') {
                    Write-Host "`n>> GitHub: $evt  ->  check $($Matches[1]) started" -ForegroundColor Cyan
                } elseif ($reply -match '"reason":"([^"]+)"') {
                    Write-Host ">> GitHub: $evt  ->  not a check request ($($Matches[1]))" -ForegroundColor DarkGray
                } else {
                    Write-Host ">> GitHub: $evt  ->  HTTP $code $reply" -ForegroundColor DarkGray
                }
            } elseif ($l -match 'could not reach|reconnecting') {
                Write-Host ">> $l" -ForegroundColor DarkYellow
            }
        }
        $fwdSeen = $fwd.Count

        Get-ChildItem "$runDir\*\cli.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | ForEach-Object {
            $key = $_.FullName
            $lines = @(Get-Content $key -ErrorAction SilentlyContinue)
            if (-not $seen.ContainsKey($key)) { $seen[$key] = 0; Write-Host "`n=== Cerberus run $($_.Directory.Name) ===" -ForegroundColor Cyan }
            for ($i = $seen[$key]; $i -lt $lines.Count; $i++) { Show-RunLine $lines[$i] }
            $seen[$key] = $lines.Count
        }
        Start-Sleep -Milliseconds 700
    }
}
finally {
    foreach ($p in @($forwarder, $service)) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force } }
    Write-Host "`nStopped Cerberus and the forwarder." -ForegroundColor Cyan
}
