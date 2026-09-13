$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $taskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $taskPython)) {
        throw 'Create .venv and install the project with python -m pip install -e ".[dev]" first.'
    }
    & $taskPython -m pytest
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
    $taskTests = @(Get-ChildItem -LiteralPath 'extension/opera-xfeed/tests' -Filter '*.test.mjs' | ForEach-Object { $_.FullName })
    & node --test @taskTests
    if ($LASTEXITCODE -ne 0) { throw 'Extension tests failed.' }
    & $taskPython -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw 'Lint failed.' }
    & $taskPython -m ruff format --check .
    if ($LASTEXITCODE -ne 0) { throw 'Formatting check failed.' }
    & $taskPython -m mypy src
    if ($LASTEXITCODE -ne 0) { throw 'Type checking failed.' }
    & git diff --check
    if ($LASTEXITCODE -ne 0) { throw 'Git whitespace check failed.' }
    Write-Host 'All project checks passed.'
}
finally {
    Pop-Location
}
