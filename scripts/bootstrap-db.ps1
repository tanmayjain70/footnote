<#
.SYNOPSIS
    One-time database bootstrap: creates the two roles, the two databases, and
    the vector extension in each.

.DESCRIPTION
    Run this once against a PostgreSQL 17 instance that has pgvector installed,
    as a superuser. Migrations do NOT create roles: roles are cluster-level
    objects while migrations are database-level, and mixing them makes a
    migration non-portable between environments. The extension is created here
    too, because creating it needs a superuser locally and the owner role that
    runs migrations is deliberately not one.

    Two roles, on purpose:

      footnote_owner  Owns the tables. Alembic connects as this role.
      footnote_app    The API's runtime role. Owns nothing and has no DDL
                      rights. The API asserts this at startup and refuses to
                      serve traffic if it is connected as an owner or a
                      superuser -- a service that writes thousands of rows on
                      every upload is precisely the code that should not be
                      holding DROP TABLE.

    Idempotent: safe to re-run.

    No pgvector? On Windows the installer from EDB does not ship it, and
    adding it to C:\Program Files needs administrator rights. scripts\dev-db.ps1
    builds a self-contained PostgreSQL 17 + pgvector in your user profile on
    port 5433 and runs this bootstrap against it.

.EXAMPLE
    $env:PGSUPERPASSWORD = "your-postgres-password"
    .\scripts\bootstrap-db.ps1

.EXAMPLE
    .\scripts\bootstrap-db.ps1 -Port 5433 -PgBin "$env:USERPROFILE\tools\pgsql17\bin"
#>
[CmdletBinding()]
param(
    [string]$PgBin = "C:\Program Files\PostgreSQL\17\bin",
    [string]$PgHost = "localhost",
    [int]$Port = 5432,
    [string]$SuperUser = "postgres",
    [string]$SuperPassword = $env:PGSUPERPASSWORD,
    [string]$OwnerPassword = "owner_dev_2026",
    [string]$AppPassword = "app_dev_2026",
    [string[]]$Databases = @("footnote", "footnote_test")
)

$ErrorActionPreference = "Stop"

$psql = Join-Path $PgBin "psql.exe"
if (-not (Test-Path $psql)) {
    throw "psql not found at $psql. Pass -PgBin pointing at your PostgreSQL bin directory."
}
if (-not $SuperPassword) {
    throw "Provide -SuperPassword or set the PGSUPERPASSWORD environment variable."
}

$env:PGPASSWORD = $SuperPassword

function Invoke-Psql {
    param([string]$Database = "postgres", [string]$Sql)
    $out = & $psql -U $SuperUser -h $PgHost -p $Port -d $Database -v ON_ERROR_STOP=1 -t -A -c $Sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $Sql`n$out" }
    return (($out | Where-Object { $_ -ne $null }) -join "`n")
}

Write-Host "== Checking for pgvector ==" -ForegroundColor Cyan
$available = (Invoke-Psql -Sql "SELECT count(*) FROM pg_available_extensions WHERE name = 'vector';").Trim()
if ($available -ne "1") {
    throw ("This PostgreSQL has no pgvector extension, and the schema needs it. " +
        "Install pgvector into $PgBin\.., or run scripts\dev-db.ps1 install to build a " +
        "self-contained PostgreSQL 17 + pgvector in your user profile (port 5433), then " +
        "re-run this script with -Port 5433 -PgBin `"$env:USERPROFILE\tools\pgsql17\bin`".")
}
Write-Host "   pgvector is available" -ForegroundColor Green

Write-Host "== Creating roles ==" -ForegroundColor Cyan
$roleSql = @"
DO `$`$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'footnote_owner') THEN
        CREATE ROLE footnote_owner LOGIN PASSWORD '$OwnerPassword'
            NOSUPERUSER NOCREATEDB NOCREATEROLE;
        RAISE NOTICE 'created role footnote_owner';
    ELSE
        ALTER ROLE footnote_owner PASSWORD '$OwnerPassword';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'footnote_app') THEN
        CREATE ROLE footnote_app LOGIN PASSWORD '$AppPassword'
            NOSUPERUSER NOCREATEDB NOCREATEROLE;
        RAISE NOTICE 'created role footnote_app';
    ELSE
        ALTER ROLE footnote_app PASSWORD '$AppPassword';
    END IF;
END
`$`$;
"@
Invoke-Psql -Sql $roleSql | Out-Null

# If someone ever "fixes" a permissions problem by granting SUPERUSER to the
# runtime role, fail here rather than letting the API discover it at startup.
$super = (Invoke-Psql -Sql "SELECT rolsuper FROM pg_roles WHERE rolname='footnote_app';").Trim()
if ($super -ne "f") {
    throw "footnote_app is a SUPERUSER. That defeats the least-privilege runtime role. Aborting."
}
Write-Host "   footnote_app verified: not a superuser" -ForegroundColor Green

Write-Host "== Creating databases ==" -ForegroundColor Cyan
foreach ($db in $Databases) {
    $exists = (Invoke-Psql -Sql "SELECT 1 FROM pg_database WHERE datname = '$db';").Trim()
    if ($exists -eq "1") {
        Write-Host "   $db already exists"
    }
    else {
        Invoke-Psql -Sql "CREATE DATABASE $db OWNER footnote_owner ENCODING 'UTF8';" | Out-Null
        Write-Host "   created $db" -ForegroundColor Green
    }
    # The extension is cluster software but a per-database object; the owner
    # role cannot create it, so it is done here as the superuser.
    Invoke-Psql -Database $db -Sql "CREATE EXTENSION IF NOT EXISTS vector;" | Out-Null
    # The app role connects but must never create objects.
    Invoke-Psql -Database $db -Sql "REVOKE CREATE ON SCHEMA public FROM PUBLIC;" | Out-Null
    Invoke-Psql -Database $db -Sql "GRANT CONNECT ON DATABASE $db TO footnote_app;" | Out-Null
    Invoke-Psql -Database $db -Sql "GRANT USAGE ON SCHEMA public TO footnote_app;" | Out-Null
    Invoke-Psql -Database $db -Sql "GRANT CREATE ON SCHEMA public TO footnote_owner;" | Out-Null
}

Write-Host ""
Write-Host "Bootstrap complete." -ForegroundColor Green
Write-Host "  footnote_owner -> migrations (DATABASE_ADMIN_URL)"
Write-Host "  footnote_app   -> runtime    (DATABASE_URL)"
Write-Host ""
Write-Host "Next:"
Write-Host "  cd api"
Write-Host "  copy .env.example .env    # then set JWT_SECRET, and the port if not 5432"
Write-Host "  .venv\Scripts\alembic upgrade head"
Write-Host "  .venv\Scripts\python -m app.demo.seed"
