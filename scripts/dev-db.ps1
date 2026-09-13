<#
.SYNOPSIS
    A self-contained PostgreSQL 17 + pgvector in your user profile, without
    administrator rights.

.DESCRIPTION
    Footnote's schema needs the pgvector extension. On Windows the PostgreSQL
    installer does not ship it, and adding it to C:\Program Files needs an
    elevated prompt that a developer's machine often does not have. This
    script sidesteps both: it downloads the official PostgreSQL 17 zip
    binaries into $Root\pgsql17, compiles pgvector against them with the
    Visual Studio Build Tools, initialises a cluster on port 5433 (the system
    PostgreSQL, if any, keeps 5432), and runs the same role and database
    bootstrap as scripts\bootstrap-db.ps1.

    Actions:
      install    download, build, initdb, start, bootstrap  (idempotent per step)
      start      start the cluster (pg_ctl start)
      stop       stop it
      status     is it running?
      bootstrap  re-run the role/database bootstrap against the running cluster

    The superuser password is generated once and written to
    $Root\pgsql17\CREDENTIALS.txt, next to the start and stop commands, so it
    is never only in somebody's memory.

    The cluster is not a Windows service: it does not survive a reboot on its
    own. Run `dev-db.ps1 start` again.

    Requirements for `install`: Visual Studio 2022+ Build Tools with the
    "Desktop development with C++" workload (for nmake and cl.exe) and the
    Windows SDK. About 900 MB of downloads and disk during the build, ~200 MB
    afterwards.

.EXAMPLE
    .\scripts\dev-db.ps1 install
    .\scripts\dev-db.ps1 status
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "start", "stop", "status", "bootstrap")]
    [string]$Action = "status",
    [string]$Root = (Join-Path $env:USERPROFILE "tools"),
    [int]$Port = 5433,
    [string]$PgVersion = "17.11-1",
    [string]$PgvectorVersion = "0.8.6"
)

$ErrorActionPreference = "Stop"

$pgRoot = Join-Path $Root "pgsql17"
$bin = Join-Path $pgRoot "bin"
$data = Join-Path $pgRoot "data"
$logFile = Join-Path $pgRoot "postgres.log"
$pwFile = Join-Path $pgRoot "superuser.pwfile"
$credentials = Join-Path $pgRoot "CREDENTIALS.txt"
$downloads = Join-Path $Root "pg17-downloads"
$pgCtl = Join-Path $bin "pg_ctl.exe"
$scriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Require-Installed {
    if (-not (Test-Path $pgCtl)) {
        throw "No PostgreSQL at $pgRoot. Run: .\scripts\dev-db.ps1 install"
    }
}

function Invoke-PgCtl {
    param([string[]]$Arguments, [switch]$Quiet)
    # pg_ctl's own output must not land in the pipeline, or the caller gets
    # an array of lines with the exit code on the end instead of the code.
    if ($Quiet) { & $pgCtl @Arguments | Out-Null } else { & $pgCtl @Arguments | Out-Host }
    return $LASTEXITCODE
}

function Get-Status {
    Require-Installed
    $code = Invoke-PgCtl @("-D", $data, "status") -Quiet
    return ($code -eq 0)
}

function Start-Cluster {
    Require-Installed
    if (Get-Status) {
        Write-Host "Already running on port $Port." -ForegroundColor Green
        return
    }
    $code = Invoke-PgCtl @("-D", $data, "-l", $logFile, "-w", "start")
    if ($code -ne 0) { throw "pg_ctl start failed; see $logFile" }
    Write-Host "Started on port $Port." -ForegroundColor Green
}

function Stop-Cluster {
    Require-Installed
    if (-not (Get-Status)) {
        Write-Host "Not running."
        return
    }
    $code = Invoke-PgCtl @("-D", $data, "-m", "fast", "-w", "stop")
    if ($code -ne 0) { throw "pg_ctl stop failed" }
    Write-Host "Stopped." -ForegroundColor Green
}

function Invoke-Bootstrap {
    Require-Installed
    if (-not (Test-Path $pwFile)) {
        throw "No superuser password file at $pwFile; the cluster was not initialised by this script."
    }
    $superPassword = (Get-Content $pwFile -Raw).Trim()
    & (Join-Path $scriptsDir "bootstrap-db.ps1") -PgBin $bin -Port $Port -SuperPassword $superPassword `
        -Databases @("footnote", "footnote_test")
}

function Find-Vcvars {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $found = & $vswhere -products * -latest `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -find "VC\Auxiliary\Build\vcvars64.bat" 2>$null
        if ($found) { return ($found | Select-Object -First 1) }
    }
    foreach ($candidate in @(
            "${env:ProgramFiles(x86)}\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
            "${env:ProgramFiles}\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
            "${env:ProgramFiles}\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat")) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw ("Visual Studio Build Tools with the C++ workload were not found; pgvector cannot be " +
        "compiled. Install them from https://visualstudio.microsoft.com/visual-cpp-build-tools/ " +
        "(select 'Desktop development with C++') and re-run.")
}

function Install-All {
    New-Item -ItemType Directory -Force $Root | Out-Null
    New-Item -ItemType Directory -Force $downloads | Out-Null

    # --- 1. PostgreSQL binaries -------------------------------------------
    if (-not (Test-Path $pgCtl)) {
        $zip = Join-Path $downloads "postgresql-$PgVersion-windows-x64-binaries.zip"
        if (-not (Test-Path $zip)) {
            $url = "https://get.enterprisedb.com/postgresql/postgresql-$PgVersion-windows-x64-binaries.zip"
            Write-Host "== Downloading PostgreSQL $PgVersion (about 340 MB) ==" -ForegroundColor Cyan
            Invoke-WebRequest -Uri $url -OutFile $zip
        }
        $stage = Join-Path $downloads "pgsql-stage"
        if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
        Write-Host "== Extracting ==" -ForegroundColor Cyan
        Expand-Archive -Path $zip -DestinationPath $stage -Force
        # pgAdmin and StackBuilder are two thirds of the archive and no use to
        # a server that only has to sit on a port.
        foreach ($junk in @("pgAdmin 4", "StackBuilder", "symbols")) {
            $path = Join-Path $stage "pgsql\$junk"
            if (Test-Path $path) { Remove-Item -Recurse -Force $path }
        }
        Move-Item (Join-Path $stage "pgsql") $pgRoot
        Remove-Item -Recurse -Force $stage
        Write-Host "   binaries in $pgRoot" -ForegroundColor Green
    }
    else {
        Write-Host "PostgreSQL already present at $pgRoot"
    }

    # --- 2. pgvector, compiled against those binaries ----------------------
    $control = Join-Path $pgRoot "share\extension\vector.control"
    if (-not (Test-Path $control)) {
        $tarball = Join-Path $downloads "pgvector-$PgvectorVersion.tar.gz"
        if (-not (Test-Path $tarball)) {
            $url = "https://github.com/pgvector/pgvector/archive/refs/tags/v$PgvectorVersion.tar.gz"
            Write-Host "== Downloading pgvector $PgvectorVersion ==" -ForegroundColor Cyan
            Invoke-WebRequest -Uri $url -OutFile $tarball
        }
        $srcRoot = Join-Path $downloads "src"
        New-Item -ItemType Directory -Force $srcRoot | Out-Null
        tar -xzf $tarball -C $srcRoot
        if ($LASTEXITCODE -ne 0) { throw "tar failed to extract $tarball" }
        $src = Join-Path $srcRoot "pgvector-$PgvectorVersion"

        $vcvars = Find-Vcvars
        Write-Host "== Building pgvector with $vcvars ==" -ForegroundColor Cyan
        # nmake needs the MSVC environment that vcvars64.bat sets up, and that
        # only works inside cmd.exe, so the build is a small batch file.
        $batch = Join-Path $downloads "build-pgvector.cmd"
        @"
@echo off
call "$vcvars" >nul
if errorlevel 1 exit /b 1
set "PGROOT=$pgRoot"
cd /d "$src"
nmake /NOLOGO /F Makefile.win
if errorlevel 1 exit /b 1
nmake /NOLOGO /F Makefile.win install
if errorlevel 1 exit /b 1
"@ | Set-Content -Path $batch -Encoding ASCII
        cmd /c $batch | Select-Object -Last 5
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $control)) {
            throw "pgvector build failed; run $batch by hand to see the compiler output."
        }
        Write-Host "   pgvector installed into $pgRoot" -ForegroundColor Green
    }
    else {
        Write-Host "pgvector already installed"
    }

    # --- 3. A cluster on port $Port -----------------------------------------
    if (-not (Test-Path (Join-Path $data "PG_VERSION"))) {
        Write-Host "== Initialising the cluster ==" -ForegroundColor Cyan
        $bytes = New-Object byte[] 24
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $superPassword = "pg-" + ([Convert]::ToBase64String($bytes) -replace '[+/=]', 'x')
        [System.IO.File]::WriteAllText($pwFile, $superPassword)
        & (Join-Path $bin "initdb.exe") -D $data -U postgres --auth=scram-sha-256 --pwfile=$pwFile -E UTF8 --locale=C | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "initdb failed" }
        $conf = Join-Path $data "postgresql.conf"
        (Get-Content $conf) -replace '^#?port = 5432', "port = $Port" | Set-Content $conf -Encoding ASCII
        @"
User-space PostgreSQL 17 + pgvector $PgvectorVersion, set up by scripts\dev-db.ps1

Binaries      $bin
Data dir      $data
Port          $Port
Superuser     postgres
Password      $superPassword

Start         powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev-db.ps1 start
Stop          powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev-db.ps1 stop
Status        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev-db.ps1 status
Connect       "$bin\psql" -h localhost -p $Port -U postgres

Not a Windows service: it does not start on its own after a reboot.

Application roles (scripts\bootstrap-db.ps1):
  footnote_owner / owner_dev_2026   owns the tables, runs migrations
  footnote_app   / app_dev_2026     the API's runtime role, DML only
Databases: footnote, footnote_test
"@ | Set-Content -Path $credentials -Encoding UTF8
        Write-Host "   credentials written to $credentials" -ForegroundColor Green
    }
    else {
        Write-Host "Cluster already initialised at $data"
    }

    Start-Cluster
    Invoke-Bootstrap

    Write-Host ""
    Write-Host "Done. Point api\.env at port ${Port}:" -ForegroundColor Green
    Write-Host "  DATABASE_URL=postgresql+psycopg://footnote_app:app_dev_2026@localhost:$Port/footnote"
    Write-Host "  DATABASE_ADMIN_URL=postgresql+psycopg://footnote_owner:owner_dev_2026@localhost:$Port/footnote"
}

switch ($Action) {
    "install" { Install-All }
    "start" { Start-Cluster }
    "stop" { Stop-Cluster }
    "status" {
        if (Get-Status) { Write-Host "Running on port $Port (data: $data)." -ForegroundColor Green }
        else { Write-Host "Not running. Start with: .\scripts\dev-db.ps1 start" -ForegroundColor Yellow; exit 1 }
    }
    "bootstrap" { Invoke-Bootstrap }
}
