param(
    [string]$Python = (Join-Path $PSScriptRoot '..\.venv-windows\Scripts\python.exe'),
    [switch]$InstallDesktop
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$buildPath = Join-Path $root 'build\windows'
$distPath = Join-Path $root 'dist'

& $pythonPath -m PyInstaller --noconfirm --onefile --console --noupx `
    --name vital --icon (Join-Path $root 'docs\images\vital.ico') `
    --collect-all textual --paths $root --distpath $distPath `
    --workpath (Join-Path $buildPath 'work') --specpath $buildPath (Join-Path $root 'sysmon\__main__.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$executable = Join-Path $distPath 'vital.exe'
if ($InstallDesktop) {
    $desktopExecutable = Join-Path ([Environment]::GetFolderPath('Desktop')) 'vital.exe'
    Copy-Item -LiteralPath $executable -Destination $desktopExecutable -Force
    Write-Output $desktopExecutable
} else {
    Write-Output $executable
}
