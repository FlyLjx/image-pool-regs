param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8090
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $project

$venvPython = Join-Path $project ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }

& $python -m uvicorn app.main:app --host $HostAddress --port $Port
