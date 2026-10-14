$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
    $taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    & $taskPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $taskPython build_icon.py
    $taskIcon = Join-Path $PSScriptRoot 'app.ico'
    & $taskPython -m PyInstaller --noconfirm --onefile --windowed --name YaDiskDownloader --collect-all playwright --icon $taskIcon --add-data "$taskIcon;." downloader.py
    if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
    Write-Host 'Ready: dist\YaDiskDownloader.exe'
} finally { Pop-Location }
