param([string]$Address = '', [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = Join-Path $projectRoot 'config.toml'
if (-not $Address) {
    $localConfig = Get-Content -LiteralPath (Join-Path $projectRoot 'comfy.local.json') -Raw | ConvertFrom-Json
    $Address = [string]$localConfig.lan_address
}
if (-not $Address -or $Address -eq '0.0.0.0' -or $Address -like '127.*') { throw 'Set an explicit office NIC address in comfy.local.json: lan_address.' }
$nic = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $Address -ErrorAction Stop
if (-not $nic) { throw 'This IP is not assigned to this computer.' }
$logDirectory = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
foreach ($listenAddress in @('127.0.0.1', $Address)) {
    $existing = Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -eq $listenAddress }
    if ($existing) { continue }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    Start-Process -FilePath $pythonPath -WorkingDirectory $projectRoot -WindowStyle Hidden `
        -ArgumentList "-m film_asset_pipeline.web --config `"$configPath`" --host $listenAddress --port 8765" `
        -RedirectStandardOutput (Join-Path $logDirectory "office-$listenAddress-$stamp.stdout.log") `
        -RedirectStandardError (Join-Path $logDirectory "office-$listenAddress-$stamp.stderr.log") | Out-Null
}
Write-Output "Office members: http://${Address}:8765/objects"
Write-Output 'Administrator: http://127.0.0.1:8765/objects'
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8765/objects' }
