[CmdletBinding()]
param(
    [switch]$SkipPythonInstall,
    [switch]$SkipDependencyInstall,
    [switch]$SkipVerification
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPath = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-Python311([string]$Executable, [string[]]$PrefixArguments = @()) {
    try {
        $version = & $Executable @PrefixArguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        return ($LASTEXITCODE -eq 0 -and ($version | Select-Object -Last 1) -eq "3.11")
    }
    catch {
        return $false
    }
}

function Find-Python311 {
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        if (Test-Python311 "py.exe" @("-3.11")) {
            return [pscustomobject]@{ Executable = "py.exe"; Prefix = @("-3.11") }
        }
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Python311\python.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

    foreach ($candidate in $candidates) {
        if (Test-Python311 $candidate) {
            return [pscustomobject]@{ Executable = $candidate; Prefix = @() }
        }
    }

    if (Get-Command python.exe -ErrorAction SilentlyContinue) {
        if (Test-Python311 "python.exe") {
            return [pscustomobject]@{ Executable = "python.exe"; Prefix = @() }
        }
    }

    return $null
}

function Invoke-SelectedPython([object]$Python, [string[]]$Arguments) {
    & $Python.Executable @($Python.Prefix) @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

Set-Location -LiteralPath $ProjectRoot
Write-Host "Project: $ProjectRoot"

Write-Step "Checking Python 3.11"
$python = Find-Python311
if (-not $python -and -not $SkipPythonInstall) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Python 3.11 is missing and winget is unavailable. Install Python 3.11 from https://www.python.org/downloads/ and run INSTALL.bat again."
    }

    Write-Host "Python 3.11 is missing. Installing it for the current Windows user..."
    & $winget.Source install --id Python.Python.3.11 --exact --scope user --silent --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install Python 3.11 (exit code $LASTEXITCODE)."
    }
    $python = Find-Python311
}

if (-not $python) {
    throw "Python 3.11 was not found. Install it and run INSTALL.bat again."
}
Write-Host "Using Python 3.11: $($python.Executable) $($python.Prefix -join ' ')"

Write-Step "Preparing the virtual environment"
if (Test-Path -LiteralPath $VenvPython) {
    if (-not (Test-Python311 $VenvPython)) {
        $backupName = ".venv.backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        $backupPath = Join-Path $ProjectRoot $backupName
        Write-Warning "The existing .venv is not Python 3.11. Moving it to $backupName."
        Move-Item -LiteralPath $VenvPath -Destination $backupPath
    }
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Invoke-SelectedPython $python @("-m", "venv", $VenvPath)
}

if (-not $SkipDependencyInstall) {
    Write-Step "Installing Python packages and the Friend Poster application"
    & $VenvPython -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) { throw "Could not update pip/setuptools/wheel." }

    & $VenvPython -m pip install --editable $ProjectRoot
    if ($LASTEXITCODE -ne 0) { throw "Could not install project dependencies." }
}

Write-Step "Creating local runtime files"
$secretsPath = Join-Path $ProjectRoot "secrets.json"
$secretsTemplate = Join-Path $ProjectRoot "secrets.TEMPLATE.json"
if (-not (Test-Path -LiteralPath $secretsPath)) {
    Copy-Item -LiteralPath $secretsTemplate -Destination $secretsPath
    Write-Host "Created secrets.json from the safe template. Fill in your own values before using live workflows."
} else {
    Write-Host "Keeping the existing secrets.json unchanged."
}

foreach ($directory in @("logs", "dumps", "posts", "used")) {
    $path = Join-Path $ProjectRoot $directory
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
    }
}

foreach ($file in @("accounts.txt", "Birthdays.txt.txt")) {
    $path = Join-Path $ProjectRoot $file
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType File -Path $path | Out-Null
    }
}

Write-Step "Verifying the installation"
if (-not $SkipVerification) {
    & $VenvPython -c "import friend_poster, paramiko, pymobiledevice3, wda, PIL, requests, socks; print('Python imports: OK')"
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency verification failed. Run INSTALL.bat again while connected to the internet."
    }
} else {
    Write-Host "Dependency verification skipped by command-line option."
}

$appleService = Get-Service -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match "Apple.*Mobile" -or $_.DisplayName -match "Apple Mobile Device"
} | Select-Object -First 1

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " PC installation is complete." -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Before live phone automation:"
Write-Host "  1. Fill secrets.json with YOUR credentials and API keys."
Write-Host "  2. Add YOUR data to accounts.txt and Birthdays.txt.txt."
Write-Host "  3. Connect and unlock the iPhone, then tap Trust on the phone."
Write-Host "  4. The iPhone must already have the required jailbreak setup, Reddit,"
Write-Host "     Ghost/Crane, and XXTouch or WebDriverAgent configured."

if ($appleService) {
    Write-Host "  5. Apple Mobile Device service detected: $($appleService.Status)."
} else {
    Write-Warning "Apple Mobile Device support was not detected. Install the Apple Devices app from Microsoft Store (or desktop iTunes from apple.com), reconnect the iPhone, and tap Trust."
}

Write-Host ""
Write-Host "Start the program with: START CREATOR PANEL.bat" -ForegroundColor Yellow
