$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistDir = Join-Path $ProjectDir "dist"

python -m pip install --disable-pip-version-check pyinstaller
python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onefile `
    --name AudioAtelier `
    --icon (Join-Path $ProjectDir "assets\audio_atelier.ico") `
    --version-file (Join-Path $ProjectDir "version_info.txt") `
    --add-data "$(Join-Path $ProjectDir 'assets\audio_atelier.ico');assets" `
    (Join-Path $ProjectDir "app.py")

$ExePath = Join-Path $DistDir "AudioAtelier.exe"
Write-Host "Built: $ExePath"
