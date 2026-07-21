<#
.SYNOPSIS
    Builds the delivery ZIP for the consegna.

.DESCRIPTION
    Wraps `git archive`, which ships ONLY tracked files. Two consequences matter:

      * anything .gitignore'd (user_credentials.json, .venv/, caches, outputs.txt,
        debug.log) can never end up in the archive — no secret can leak;
      * files marked `export-ignore` in .gitattributes (CLAUDE.md, docs/lessons.md)
        are dropped even though they are tracked.

    The archive therefore reflects HEAD, not the working tree: commit before building.
#>
$ErrorActionPreference = 'Stop'

$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo

# --- the archive comes from HEAD, so uncommitted work would be silently missing ---
$dirty = git status --porcelain
if ($dirty) {
    Write-Host "Uncommitted changes — `git archive` exports HEAD, so these would NOT be included:" -ForegroundColor Yellow
    $dirty | ForEach-Object { Write-Host "  $_" }
    throw "Commit (or stash) first, then re-run."
}

$out = Join-Path $repo 'snap4city-mobility-mcp-consegna.zip'
if (Test-Path $out) { Remove-Item $out }

git archive --format=zip --prefix=snap4city-mobility-mcp/ -o $out HEAD
if ($LASTEXITCODE -ne 0) { throw "git archive failed." }

# --- verify what actually landed in the archive -------------------------------
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($out)
try   { $entries = $zip.Entries | ForEach-Object { $_.FullName } }
finally { $zip.Dispose() }

$forbidden = @(
    'snap4city-mobility-mcp/CLAUDE.md',
    'snap4city-mobility-mcp/docs/lessons.md',
    'snap4city-mobility-mcp/user_credentials.json'
)
$required = @(
    'snap4city-mobility-mcp/README.md',
    'snap4city-mobility-mcp/LICENSE',
    'snap4city-mobility-mcp/api.py',
    'snap4city-mobility-mcp/relazione/relazione.tex',
    'snap4city-mobility-mcp/docs/snap4city-api-notes.md'
)

$leaked = $forbidden | Where-Object { $entries -contains $_ }
if ($leaked) { throw "Archive contains files that must not ship: $($leaked -join ', ')" }

$missing = $required | Where-Object { $entries -notcontains $_ }
if ($missing) { throw "Archive is missing expected files: $($missing -join ', ')" }

foreach ($dir in 'docs/diagrams', 'screenshots', 'examples', 'src', 'tests', 'frontend') {
    $n = ($entries | Where-Object { $_ -like "snap4city-mobility-mcp/$dir/*" }).Count
    "{0,-16} {1,3} file" -f $dir, $n
}

# The compiled report: Overleaf names the PDF after the project, so accept any
# relazione/*.pdf rather than one fixed filename.
# (anchored to that directory: img/stemma.pdf must not count as the report)
$pdf = $entries | Where-Object { $_ -match '^snap4city-mobility-mcp/relazione/[^/]+\.pdf$' }
if (-not $pdf) {
    Write-Host "NOTE: no relazione/*.pdf in the archive — compile it (see relazione/README.md), commit it, and re-run." -ForegroundColor Yellow
}

"{0} entries -> {1} ({2:N1} MB)" -f $entries.Count, $out, ((Get-Item $out).Length / 1MB)
