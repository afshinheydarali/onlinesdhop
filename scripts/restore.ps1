param(
    [Parameter(Mandatory=$true)][string]$BackupPath,
    [string]$DatabaseUrl = $env:TEST_DATABASE_URL
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $BackupPath)) { throw "Backup file not found: $BackupPath" }
if ([string]::IsNullOrWhiteSpace($DatabaseUrl)) { throw "TEST_DATABASE_URL or -DatabaseUrl is required" }
$uri = [Uri]($DatabaseUrl -replace '^postgresql\+[^:]+://', 'postgresql://')
if ($uri.Host -notin @("localhost", "127.0.0.1", "::1") -or $uri.AbsolutePath.Trim('/') -ne "onlineshop_restore_test") { throw "Restore target must be local onlineshop_restore_test" }
$db = "onlineshop_restore_test"
$pgBin = Join-Path $PSScriptRoot "..\..\postgres-runtime\pgsql\bin"
$env:PGPASSWORD = ($uri.UserInfo -split ':', 2)[1]
$env:PGHOST = $uri.Host
$env:PGPORT = $uri.Port
$env:PGUSER = ($uri.UserInfo -split ':', 2)[0]
$savedErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& (Join-Path $pgBin "createdb.exe") $db 2>$null
$ErrorActionPreference = $savedErrorAction
& (Join-Path $pgBin "pg_restore.exe") --clean --if-exists --no-owner --dbname $db $BackupPath
if ($LASTEXITCODE -ne 0) { throw "pg_restore failed" }
Write-Output "Backup restored into $db"
