<#
Builds the JARVIS Windows desktop application and installer.

    pwsh -File jarvis\packaging\build_windows.ps1 [-SkipInstaller]

Requirements: Python 3.10+, PyInstaller, and (optionally) Inno Setup 6 for the
installer. The produced executable is self-contained and needs no Python runtime.
#>
param(
    [switch]$SkipInstaller,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot "..\.."))

Write-Host "==> Creating build environment"
& $Python -m venv .venv-build
& .\.venv-build\Scripts\python.exe -m pip install --upgrade pip
& .\.venv-build\Scripts\python.exe -m pip install -e ".[desktop,computer,voice]" pyinstaller

Write-Host "==> Running the verification suite"
& .\.venv-build\Scripts\python.exe -m pip install pytest
& .\.venv-build\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "tests failed - refusing to build an unverified release" }

Write-Host "==> Building the executable"
& .\.venv-build\Scripts\pyinstaller.exe jarvis\packaging\jarvis.spec --noconfirm --clean

if (-not $SkipInstaller) {
    $iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($iscc) {
        Write-Host "==> Building the installer"
        & iscc.exe jarvis\packaging\installer.iss
    } else {
        Write-Warning "Inno Setup (iscc.exe) not found; skipping the installer."
    }
}

Write-Host "==> Smoke-testing the built application"
$env:JARVIS_HOME = Join-Path $env:TEMP "jarvis-build-check"
& .\dist\JARVIS\JARVIS.exe --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "the built executable did not start" }
Write-Host "Done. Executable: dist\JARVIS\JARVIS.exe"
