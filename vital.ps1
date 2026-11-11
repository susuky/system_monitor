$pythonPath = Join-Path $PSScriptRoot '.venv-windows\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    [Console]::Error.WriteLine("Run these commands in '$PSScriptRoot' to install Vital.")
    [Console]::Error.WriteLine('python -m venv .venv-windows')
    [Console]::Error.WriteLine('.\.venv-windows\Scripts\python.exe -m pip install -e .')
    exit 1
}

$previousConsoleEncoding = [Console]::OutputEncoding
Push-Location -LiteralPath $PSScriptRoot
try {
    # PowerShell decodes piped native output using the console encoding.
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    & $pythonPath -m sysmon @args
    exit $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousConsoleEncoding
    Pop-Location
}
