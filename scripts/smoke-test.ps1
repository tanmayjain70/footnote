<#
.SYNOPSIS
    End-to-end smoke test against a running, seeded Footnote.

.DESCRIPTION
    Asserts on answers rather than status codes. The checks that matter are
    the ones about behaviour: a question the leases answer comes back with a
    citation into the right document; a question they cannot answer comes
    back as unanswered with no citation; a manager asking for a document
    outside their portfolios gets a 404; the daily budget stops a question
    before any tokens are spent.

    Uses curl.exe for every request so that error responses (403, 404, 429)
    can be read as JSON instead of being thrown away as exceptions.

.EXAMPLE
    .\scripts\smoke-test.ps1
    .\scripts\smoke-test.ps1 -BaseUrl https://footnote-api.onrender.com
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$Password = "demo-password",
    [string]$Sample = ""
)

$ErrorActionPreference = "Stop"
if (-not $Sample) {
    # $PSScriptRoot is the scripts folder; the samples sit beside it.
    $Sample = Join-Path (Split-Path -Parent $PSScriptRoot) "samples\irwell-bank-house-ground-floor.pdf"
}
$api = "$BaseUrl/api/v1"
$pass = 0
$fail = 0
$tmp = Join-Path $env:TEMP "footnote-smoke"
New-Item -ItemType Directory -Force $tmp | Out-Null

function Check {
    param([string]$Name, [scriptblock]$Test)
    try {
        $result = & $Test
        if ($result -eq $false) { throw "assertion returned false" }
        Write-Host ("  PASS  {0}" -f $Name) -ForegroundColor Green
        $script:pass++
    }
    catch {
        Write-Host ("  FAIL  {0}" -f $Name) -ForegroundColor Red
        Write-Host ("        {0}" -f $_.Exception.Message) -ForegroundColor DarkGray
        $script:fail++
    }
}

# One request. Returns @{ Code; Body; Json } and never throws on a 4xx/5xx,
# because most of the interesting checks are about which error came back.
function Call {
    param(
        [string]$Method,
        [string]$Path,
        [string]$Token = $null,
        $Body = $null,
        [string[]]$Form = @()
    )
    $curlArgs = @("-s", "-X", $Method, "$api$Path", "-H", "Accept: application/json", "-w", "\n%{http_code}")
    if ($Token) { $curlArgs += @("-H", "Authorization: Bearer $Token") }
    if ($null -ne $Body) {
        # Quoting JSON on a native command line is unreliable in Windows
        # PowerShell; a temp file is not.
        $file = Join-Path $tmp ("body-" + [guid]::NewGuid().ToString("N") + ".json")
        [System.IO.File]::WriteAllText($file, ($Body | ConvertTo-Json -Compress -Depth 8))
        $curlArgs += @("-H", "Content-Type: application/json", "--data-binary", "@$file")
    }
    foreach ($part in $Form) { $curlArgs += @("-F", $part) }
    $out = @(& curl.exe @curlArgs)
    if ($out.Count -eq 0) { throw "no response from $Method $Path" }
    $code = [int]$out[-1]
    $text = if ($out.Count -gt 1) { ($out[0..($out.Count - 2)] -join "`n") } else { "" }
    $json = $null
    try { if ($text) { $json = $text | ConvertFrom-Json } } catch { $json = $null }
    return @{ Code = $code; Body = $text; Json = $json }
}

function Login {
    param([string]$Email)
    $r = Call -Method POST -Path "/auth/login" -Body @{ email = $Email; password = $Password }
    if ($r.Code -ne 200) { throw "login as $Email failed with $($r.Code): $($r.Body)" }
    return $r.Json.access_token
}

function Ask {
    param([string]$Token, [string]$Question, $PortfolioId = $null)
    $body = @{ question = $Question }
    if ($PortfolioId) { $body.portfolio_id = "$PortfolioId" }
    return Call -Method POST -Path "/ask?stream=false" -Token $Token -Body $body
}

Write-Host "== Footnote smoke test ==" -ForegroundColor Cyan
Write-Host "   $BaseUrl"
Write-Host ""

Write-Host "Health" -ForegroundColor Cyan
Check "healthz answers without touching the database" {
    (Call -Method GET -Path "/healthz").Json.status -eq "ok"
}
Check "readyz can reach Postgres" {
    (Call -Method GET -Path "/readyz").Json.status -eq "ready"
}

Write-Host "Authentication and roles" -ForegroundColor Cyan
$director = $null; $admin = $null; $manager = $null; $viewer = $null
Check "the director can sign in" {
    $script:director = Login "director@hallampryce.demo"
    $script:admin = Login "admin@hallampryce.demo"
    $script:manager = Login "manager@hallampryce.demo"
    $script:viewer = Login "finance@hallampryce.demo"
    $null -ne $script:director
}
Check "a wrong password is refused with the same message as an unknown account" {
    $a = Call -Method POST -Path "/auth/login" -Body @{ email = "director@hallampryce.demo"; password = "nope" }
    $b = Call -Method POST -Path "/auth/login" -Body @{ email = "nobody@hallampryce.demo"; password = "nope" }
    $a.Code -eq 401 -and $b.Code -eq 401 -and $a.Json.error.message -eq $b.Json.error.message
}
Check "an unauthenticated request is refused" {
    (Call -Method GET -Path "/health").Code -eq 401
}
Check "the viewer cannot ask questions (403)" {
    $r = Ask -Token $viewer -Question "When does the term expire?"
    $r.Code -eq 403 -and $r.Json.error.code -eq "forbidden"
}
Check "the worker is alive and the demo corpus is ready" {
    $h = (Call -Method GET -Path "/health" -Token $director).Json
    $h.worker_alive -eq $true -and $h.documents.ready -ge 48
}

Write-Host "Portfolios" -ForegroundColor Cyan
$portfolios = $null
Check "the director sees three portfolios, the manager two, the riverside manager one" {
    $script:portfolios = (Call -Method GET -Path "/portfolios" -Token $director).Json
    $riverside = Login "riverside@hallampryce.demo"
    $m = (Call -Method GET -Path "/portfolios" -Token $manager).Json
    $rv = (Call -Method GET -Path "/portfolios" -Token $riverside).Json
    @($script:portfolios).Count -eq 3 -and @($m).Count -eq 2 -and @($rv).Count -eq 1
}
$riversideId = ($portfolios | Where-Object { $_.name -eq "Riverside" }).id
$cityId = ($portfolios | Where-Object { $_.name -eq "City Centre" }).id
Check "a Riverside document is a 404 for the manager, not a 403" {
    $doc = (Call -Method GET -Path "/documents?portfolio_id=$riversideId&limit=1" -Token $director).Json.items[0]
    $r = Call -Method GET -Path "/documents/$($doc.id)" -Token $manager
    $r.Code -eq 404
}

Write-Host "Documents" -ForegroundColor Cyan
$uploaded = $null
Check "uploading a sample lease twice returns the same document the second time" {
    if (-not (Test-Path $Sample)) { throw "sample not found at $Sample" }
    $first = Call -Method POST -Path "/documents" -Token $admin -Form @("file=@$Sample", "portfolio_id=$cityId")
    if ($first.Code -notin @(200, 201)) { throw "upload failed with $($first.Code): $($first.Body)" }
    $second = Call -Method POST -Path "/documents" -Token $admin -Form @("file=@$Sample", "portfolio_id=$cityId")
    $script:uploaded = $first.Json.document
    $second.Code -eq 200 -and $second.Json.created -eq $false -and $second.Json.document.id -eq $first.Json.document.id
}
Check "the upload is parsed, chunked and embedded within two minutes" {
    $deadline = (Get-Date).AddMinutes(2)
    while ((Get-Date) -lt $deadline) {
        $d = (Call -Method GET -Path "/documents/$($uploaded.id)" -Token $admin).Json
        if ($d.status -eq "ready") { $script:uploaded = $d; return ($d.page_count -gt 0 -and $d.chunk_count -gt 0) }
        if ($d.status -eq "failed") { throw "ingest failed: $($d.error)" }
        Start-Sleep -Seconds 3
    }
    throw "still $($d.status) after two minutes"
}
Check "the pages of a document can be read back" {
    $pages = (Call -Method GET -Path "/documents/$($uploaded.id)/pages" -Token $admin).Json
    @($pages).Count -eq $uploaded.page_count -and $pages[0].text.Length -gt 100
}

Write-Host "Answers" -ForegroundColor Cyan
Check "a question the lease answers is answered with a citation into that lease" {
    $r = Ask -Token $admin -Question "When does the term of the lease of $($uploaded.title) commence and expire?"
    if ($r.Code -ne 200) { throw "ask failed with $($r.Code): $($r.Body)" }
    $a = $r.Json
    $cited = @($a.citations | Where-Object { $_.document_id -eq $uploaded.id })
    if ($a.status -ne "answered") { throw "status was $($a.status)" }
    if ($cited.Count -eq 0) { throw "answered, but the citations point at: " + (($a.citations | ForEach-Object { $_.document_title }) -join "; ") }
    $true
}
Check "a question the lease cannot answer is unanswered with no citation" {
    $r = Ask -Token $admin -Question "What are the landlord's bank account details for paying the rent at $($uploaded.title)?"
    $a = $r.Json
    $r.Code -eq 200 -and $a.status -eq "unanswered" -and @($a.citations).Count -eq 0 -and @($a.sources).Count -gt 0
}
Check "the streaming endpoint streams events and finishes with done" {
    $file = Join-Path $tmp "stream-body.json"
    [System.IO.File]::WriteAllText($file, (@{ question = "When does the term of the lease of $($uploaded.title) expire?" } | ConvertTo-Json -Compress))
    $out = @(& curl.exe -s -N --max-time 90 -D - -X POST "$api/ask" -H "Authorization: Bearer $admin" -H "Content-Type: application/json" --data-binary "@$file")
    $text = $out -join "`n"
    ($text -match "text/event-stream") -and ($text -match "event: retrieval") -and ($text -match "event: done")
}
Check "the answer is stored and the manager cannot read the admin's question" {
    $mine = (Call -Method GET -Path "/questions?limit=1" -Token $admin).Json
    $q = $mine.items[0]
    $r = Call -Method GET -Path "/questions/$($q.id)" -Token $manager
    $mine.total -ge 2 -and $r.Code -eq 404
}

Write-Host "The register" -ForegroundColor Cyan
$seeded = $null
Check "a seeded lease has all seventeen extracted terms with evidence" {
    $script:seeded = (Call -Method GET -Path "/documents?portfolio_id=$riversideId&limit=1" -Token $director).Json.items[0]
    $x = (Call -Method GET -Path "/documents/$($seeded.id)/extraction" -Token $director).Json
    $withQuote = @($x.values | Where-Object { $null -ne $_.quote -and $null -ne $_.chunk_id })
    $x.status -eq "done" -and @($x.values).Count -eq 17 -and $withQuote.Count -ge 14
}
Check "the lease administrator can confirm a pending value" {
    $x = (Call -Method GET -Path "/documents/$($seeded.id)/extraction" -Token $admin).Json
    $pending = @($x.values | Where-Object { $_.review_status -eq "pending" -and $null -ne $_.value_json })[0]
    if ($null -eq $pending) { throw "no pending value on $($seeded.title)" }
    $r = Call -Method POST -Path "/extracted-values/$($pending.id)/review" -Token $admin -Body @{ action = "confirm" }
    $r.Code -eq 200 -and $r.Json.review_status -eq "confirmed" -and $null -ne $r.Json.reviewed_at
}
Check "the manager can read extracted terms but cannot review them (403)" {
    $docs = (Call -Method GET -Path "/documents?portfolio_id=$cityId&limit=50" -Token $manager).Json.items
    $v = @($docs | Where-Object { $_.extraction_status -eq "done" })[0]
    if ($null -eq $v) { throw "no extracted City Centre document visible to the manager" }
    $xx = Call -Method GET -Path "/documents/$($v.id)/extraction" -Token $manager
    if ($xx.Code -ne 200) { throw "manager could not read the extraction: $($xx.Code)" }
    $value = $xx.Json.values[0]
    (Call -Method POST -Path "/extracted-values/$($value.id)/review" -Token $manager -Body @{ action = "confirm" }).Code -eq 403
}
Check "the register has rows, and its CSV has a header" {
    $reg = (Call -Method GET -Path "/register" -Token $director).Json
    $csv = (Call -Method GET -Path "/register/export.csv" -Token $director).Body
    $reg.counts.documents -ge 48 -and @($reg.rows).Count -ge 48 -and ($csv.Split("`n")[0] -match "tenant")
}
Check "the review queue names documents with pending values" {
    $q = (Call -Method GET -Path "/review-queue" -Token $admin).Json
    @($q).Count -gt 0 -and ($q | Measure-Object -Property pending -Sum).Sum -gt 0
}

Write-Host "Evaluation" -ForegroundColor Cyan
Check "the golden set is loaded" {
    (Call -Method GET -Path "/evals/questions?limit=1" -Token $director).Json.total -ge 150
}
Check "a retrieval evaluation finds the expected page at least four times in five" {
    $r = Call -Method POST -Path "/evals/runs" -Token $director -Body @{ mode = "retrieval" }
    if ($r.Code -notin @(200, 201)) { throw "run failed with $($r.Code): $($r.Body)" }
    $run = $r.Json
    Write-Host ("        doc hit {0}  page hit {1}  mrr {2}" -f $run.totals.doc_hit_rate, $run.totals.page_hit_rate, $run.totals.mrr) -ForegroundColor DarkGray
    $run.status -eq "done" -and [double]$run.totals.page_hit_rate -ge 0.8
}
Check "the admin cannot run evaluations (403)" {
    (Call -Method POST -Path "/evals/runs" -Token $admin -Body @{ mode = "retrieval" }).Code -eq 403
}

Write-Host "Money" -ForegroundColor Cyan
$budget = $null
Check "usage is metered" {
    $s = (Call -Method GET -Path "/usage/summary" -Token $director).Json
    $script:budget = $s.daily_budget_usd
    $null -ne $s.spent_today_usd -and @($s.by_day).Count -eq 14
}
Check "an exhausted budget refuses a question before any tokens are spent" {
    try {
        $set = Call -Method PATCH -Path "/usage/budget" -Token $director -Body @{ daily_budget_usd = "0" }
        if ($set.Code -ne 200) { throw "could not set the budget: $($set.Body)" }
        $r = Ask -Token $admin -Question "When does the term expire?"
        $r.Code -eq 429 -and $r.Json.error.code -eq "budget_exhausted"
    }
    finally {
        Call -Method PATCH -Path "/usage/budget" -Token $director -Body @{ daily_budget_usd = "$budget" } | Out-Null
    }
}
Check "the budget is back where it was" {
    (Call -Method GET -Path "/usage/summary" -Token $director).Json.daily_budget_usd -eq $budget
}

Write-Host "Tidying up" -ForegroundColor Cyan
Check "the uploaded sample is deleted, so the demo is left as it was found" {
    $r = Call -Method DELETE -Path "/documents/$($uploaded.id)" -Token $admin
    $gone = Call -Method GET -Path "/documents/$($uploaded.id)" -Token $admin
    $r.Code -eq 204 -and $gone.Code -eq 404
}

Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
Write-Host ""
if ($fail -eq 0) {
    Write-Host "$pass passed, 0 failed." -ForegroundColor Green
    exit 0
}
Write-Host "$pass passed, $fail failed." -ForegroundColor Red
exit 1
