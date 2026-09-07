param(
    [string]$DatabaseUrl = $env:TEST_DATABASE_URL,
    [string]$OutputPath = "artifacts\onlineshop.backup"
)
$ErrorActionPreference = "Stop"
$python = if ($env:PORTFOLIO_PYTHON) { $env:PORTFOLIO_PYTHON } else { "python" }
if ([string]::IsNullOrWhiteSpace($DatabaseUrl)) { throw "TEST_DATABASE_URL or -DatabaseUrl is required" }
& $python -m scripts.pg_tools backup --database-url $DatabaseUrl --output $OutputPath
if ($LASTEXITCODE -ne 0) { throw "backup failed" }
