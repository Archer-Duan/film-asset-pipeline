param(
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$userRoot = Join-Path $env:LOCALAPPDATA "FilmAssetPipeline"
$userConfig = Join-Path $userRoot "config.toml"
$exampleConfig = Join-Path $projectRoot "config.example.toml"

function Invoke-SystemPython([string[]]$Arguments) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 @Arguments
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python @Arguments
    }
    else {
        throw "Python was not found. Install Python 3.11 or newer and enable Add Python to PATH."
    }
    if ($LASTEXITCODE -ne 0) { throw "Python failed with exit code $LASTEXITCODE." }
}

Write-Host "[1/4] Checking Python"
Invoke-SystemPython @("-c", "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'; print(sys.version)")

Write-Host "[2/4] Creating the virtual environment"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Invoke-SystemPython @("-m", "venv", (Join-Path $projectRoot ".venv"))
}

Write-Host "[3/4] Installing application dependencies"
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed. Check the network connection and retry." }
& $venvPython -m pip install -e $projectRoot
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check the network connection and retry." }

Write-Host "[4/4] Initializing the user workspace"
New-Item -ItemType Directory -Path $userRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $userConfig -PathType Leaf)) {
    Copy-Item -LiteralPath $exampleConfig -Destination $userConfig
}
foreach ($relative in @("data\input", "data\output", "data\models", "data\state", "logs")) {
    New-Item -ItemType Directory -Path (Join-Path $userRoot $relative) -Force | Out-Null
}

if (-not $NoShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "Film Asset Workbench.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = "powershell.exe"
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $projectRoot 'start_workbench.ps1')`""
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.Description = "Film Asset Workbench"
    $shortcut.Save()
}

Write-Host "Installation completed. Open Film Asset Workbench from the desktop."
Write-Host "The first launch opens Model Settings for your Seedream and Hunyuan credentials."
