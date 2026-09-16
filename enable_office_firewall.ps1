# Run once from an elevated PowerShell. Restrict to one office NIC and its subnet.
param([string]$Address = '')
$ErrorActionPreference = 'Stop'
if (-not $Address) {
    $settings = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'comfy.local.json') -Raw | ConvertFrom-Json
    $Address = [string]$settings.lan_address
}
if (-not $Address -or $Address -eq '0.0.0.0' -or $Address -like '127.*') { throw 'An explicit office NIC IPv4 address is required.' }
$nic = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $Address -ErrorAction Stop
$prefix = [int]$nic.PrefixLength
if ($prefix -lt 16 -or $prefix -gt 30) { throw 'Unexpected subnet size. Review the office network configuration.' }
$octets = [System.Net.IPAddress]::Parse($Address).GetAddressBytes()
for ($i = 0; $i -lt 4; $i++) {
    $bits = [Math]::Min(8, [Math]::Max(0, $prefix - $i * 8))
    $mask = if ($bits -eq 0) { 0 } else { (255 -shl (8 - $bits)) -band 255 }
    $octets[$i] = $octets[$i] -band $mask
}
$subnet = ([System.Net.IPAddress]::new($octets)).ToString() + '/' + $prefix
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$program = (& $pythonPath -c 'import sys; print(sys._base_executable)').Trim()
$ruleName = 'FilmAssetWorkbench-Office-' + $Address
$existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "Rule already exists: $ruleName"
} else {
    New-NetFirewallRule -Name $ruleName -DisplayName "Film Asset Workbench Office $Address" `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 `
        -LocalAddress $Address -RemoteAddress $subnet -Program $program -Profile Any | Out-Null
    Write-Output "Allowed $subnet -> ${Address}:8765 for $program"
}
