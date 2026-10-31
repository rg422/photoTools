# Builds dist\PhotoTools-<version>.exe: a single file that runs without Python installed.
# The version comes from __version__ in imgdupe\__init__.py.
# Run from the project folder after the normal setup (see README).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = '.venv\Scripts\python.exe'
$version = (Select-String -Path imgdupe\__init__.py -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$name = "PhotoTools-$version"
& $python -m pip install --quiet -r requirements.txt -r requirements-build.txt
if ($LASTEXITCODE) { throw 'pip install failed' }

& $python -m PyInstaller run.py `
    --name $name `
    --onefile `
    --windowed `
    --clean `
    --noconfirm `
    --exclude-module unittest `
    --exclude-module pydoc
if ($LASTEXITCODE) { throw 'PyInstaller failed' }

$exe = Get-Item "dist\$name.exe"
Write-Host ("Built {0} ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB))
