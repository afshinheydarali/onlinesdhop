param(
    [Parameter(Mandatory=$true)][string]$BackupPath,
    [string]$DatabaseUrl = $(if ($env:RESTORE_DATABASE_URL) { $env:RESTORE_DATABASE_URL } else { $env:TEST_DATABASE_URL })
)
$ErrorActionPreference = "Stop"
$python = if ($env:PORTFOLIO_PYTHON) { $env:PORTFOLIO_PYTHON } else { "python" }
if ([string]::IsNullOrWhiteSpace($DatabaseUrl)) { throw "RESTORE_DATABASE_URL or TEST_DATABASE_URL is required" }
& $python -m scripts.pg_tools restore --database-url $DatabaseUrl --backup $BackupPath
if ($LASTEXITCODE -ne 0) { throw "restore failed" }
