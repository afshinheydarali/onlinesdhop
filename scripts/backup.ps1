param(
    [string]$DatabaseUrl = $env:TEST_DATABASE_URL,
    [string]$OutputPath = "artifacts\onlineshop.backup"
)
$ErrorActionPreference = "Stop"
$allowed = @("onlineshop_portfolio_test", "onlineshop_restore_test")
if ([string]::IsNullOrWhiteSpace($DatabaseUrl)) { throw "TEST_DATABASE_URL or -DatabaseUrl is required" }
$uri = [Uri]($DatabaseUrl -replace '^postgresql\+[^:]+://', 'postgresql://')
if ($uri.Host -notin @("localhost", "127.0.0.1", "::1") -or $uri.AbsolutePath.Trim('/') -notin $allowed) { throw "Refusing backup outside local disposable test databases" }
$db = $uri.AbsolutePath.Trim('/')
$parent = Split-Path -Parent $OutputPath
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
$pgBin = Join-Path $PSScriptRoot "..\..\postgres-runtime\pgsql\bin"
$env:PGPASSWORD = ($uri.UserInfo -split ':', 2)[1]
$env:PGHOST = $uri.Host
$env:PGPORT = $uri.Port
$env:PGUSER = ($uri.UserInfo -split ':', 2)[0]
& (Join-Path $pgBin "pg_dump.exe") --format=custom --file $OutputPath --dbname $db
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
Write-Output "Backup written to $OutputPath from $db"
