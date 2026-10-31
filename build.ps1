# Builds dist\PhotoTools.exe: a single file that runs without Python installed.
# Run from the project folder after the normal setup (see README).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = '.venv\Scripts\python.exe'
& $python -m pip install --quiet -r requirements.txt -r requirements-build.txt
if ($LASTEXITCODE) { throw 'pip install failed' }

& $python -m PyInstaller run.py `
    --name PhotoTools `
    --onefile `
    --windowed `
    --clean `
    --noconfirm `
    --exclude-module unittest `
    --exclude-module pydoc
if ($LASTEXITCODE) { throw 'PyInstaller failed' }

$exe = Get-Item dist\PhotoTools.exe
Write-Host ("Built {0} ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB))
