param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$projectConfigPath = Join-Path $projectRoot "config.toml"
$userRoot = Join-Path $env:LOCALAPPDATA "FilmAssetPipeline"
$userConfigPath = Join-Path $userRoot "config.toml"
$configPath = if (Test-Path -LiteralPath $projectConfigPath -PathType Leaf) { $projectConfigPath } else { $userConfigPath }
$logDirectory = Join-Path (Split-Path -Parent $configPath) "logs"
$logStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$stdoutPath = Join-Path $logDirectory "web-server-$logStamp.stdout.log"
$stderrPath = Join-Path $logDirectory "web-server-$logStamp.stderr.log"
$healthUrl = "http://127.0.0.1:8765/api/health"
$baseUrl = "http://127.0.0.1:8765/"
$mutex = [System.Threading.Mutex]::new($false, "Local\FilmAssetPipelineLauncher")
$hasMutex = $false

function Get-WorkspaceStatus {
    try {
        return Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    }
    catch {
        return $null
    }
}

function Show-LauncherError([string]$message) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        $message,
        "Film Asset Workbench",
        [System.Windows.MessageBoxButton]::OK,
        [System.Windows.MessageBoxImage]::Error
    ) | Out-Null
}

try {
    try {
        $hasMutex = $mutex.WaitOne([TimeSpan]::FromSeconds(10))
    }
    catch [System.Threading.AbandonedMutexException] {
        $hasMutex = $true
    }
    if (-not $hasMutex) {
        throw "Another launch is still in progress. Please try again shortly."
    }

    $workspace = Get-WorkspaceStatus
    if ($null -eq $workspace) {
        if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
            throw "The project Python environment was not found: $pythonPath"
        }
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
            throw "The project configuration file was not found: $configPath"
        }

        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        $env:FILM_ASSET_CONFIG = $configPath
        $arguments = "-m film_asset_pipeline.web --host 127.0.0.1 --port 8765"
        $serverProcess = Start-Process `
            -FilePath $pythonPath `
            -ArgumentList $arguments `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru

        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            Start-Sleep -Milliseconds 500
            $workspace = Get-WorkspaceStatus
            if ($null -ne $workspace) {
                break
            }
            if ($serverProcess.HasExited) {
                break
            }
        }
    }

    if ($null -eq $workspace) {
        $details = ""
        if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
            $details = (Get-Content -LiteralPath $stderrPath -Tail 8 -ErrorAction SilentlyContinue) -join "`n"
        }
        throw "The local service did not start within 20 seconds.$([Environment]::NewLine)$details"
    }

    $version = [string]$workspace.frontend_version
    $url = if ($version) { "$baseUrl`?version=$version" } else { $baseUrl }
    if (-not $NoBrowser) {
        Start-Process -FilePath $url
    }
    Write-Output $url
}
catch {
    Show-LauncherError $_.Exception.Message
    Write-Error $_.Exception.Message
    exit 1
}
finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
