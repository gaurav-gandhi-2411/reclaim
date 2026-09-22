<#
BM1 (2026-08-26 audit): the atomic, hard-to-skip replacement for the ad hoc "Copy-Item ..." one-
liner that has now caused this exact same defect class TWICE -- the STALE-BASE VERIFICATION GAP
(docs/AUDIT-2026-08.md's third headline finding, 2026-08-20/21: a branch verified green against a
snapshot, not against reality) and BL7 (2026-08-26: this operating session's own local `main` sat
one commit behind `origin/main`, so re-staging `ac3_login_diagnostic.ps1` silently copied a stale
pre-#98 file -- PR #98's own AUMID diagnostic never ran on the resulting trip, and the trip's own
log gave no indication anything was wrong). A manual Copy-Item can always be run without first
re-fetching; this script cannot -- it refuses to stage anything unless the local repo's `main` is
confirmed, freshly, to equal `origin/main`'s current tip, and it writes a stage-time content hash +
commit SHA sidecar next to the staged trip script so `ac3_login_diagnostic.ps1`'s own Step -3
self-integrity check (see that file) can detect drift after the fact, and every trip log
permanently records exactly which commit actually ran -- no more reverse-engineering it from
indirect signals (grep counts, message formatting) the way BL7 had to.

No -AllowStaleBuild-style override exists here, deliberately: unlike the installer's freshness
check (a real build artifact someone might legitimately want to re-verify from an older release),
there is never a legitimate reason to stage the TRIP SCRIPT itself from anything but current
origin/main -- it is source text, rebuilding it costs nothing, and the entire point of this script
existing is to remove the one path (a manual copy) that let this happen twice.
#>

param(
    [string]$RepoPath = "C:\Users\gaura\ml-projects\reclaim",
    [string]$StageDir = "C:\Users\Public\reclaim_ac3"
)

$ErrorActionPreference = 'Stop'

function Fail($msg) {
    Write-Host "[ABORT] $msg" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $RepoPath '.git'))) {
    Fail "No git checkout found at -RepoPath '$RepoPath'. This script only stages from a real git checkout -- it cannot verify freshness any other way, and staging without that verification is exactly the defect this script exists to prevent."
}

Write-Host "[RUN] git -C '$RepoPath' fetch origin --quiet"
git -C $RepoPath fetch origin --quiet 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "git fetch failed (exit $LASTEXITCODE) -- cannot verify freshness. Check network/credentials and retry; this script will not stage against unverified state."
}

$localHead = (git -C $RepoPath rev-parse HEAD 2>$null).Trim()
$originMain = (git -C $RepoPath rev-parse origin/main 2>$null).Trim()
if (-not $localHead -or -not $originMain) {
    Fail "Could not resolve HEAD or origin/main from '$RepoPath' (git rev-parse failed)."
}
if ($localHead -ne $originMain) {
    Fail "Local '$RepoPath' HEAD ($localHead) does not equal origin/main ($originMain). Run 'git branch -f main origin/main; git checkout main' (after confirming no uncommitted work is lost, per rule 55a) and re-run this script. This is the exact check whose absence caused BL7 -- it does not get skipped."
}
Write-Host "[OK] Local main is confirmed current: $localHead"

if (-not (Test-Path $StageDir)) { New-Item -ItemType Directory -Path $StageDir -Force | Out-Null }

$scriptSource = Join-Path $RepoPath 'packaging\smoke\ac3_login_diagnostic.ps1'
if (-not (Test-Path $scriptSource)) {
    Fail "Trip script not found at '$scriptSource'."
}
$scriptDest = Join-Path $StageDir 'ac3_login_diagnostic.ps1'
Copy-Item -Path $scriptSource -Destination $scriptDest -Force

# Byte-identical verification -- do not trust the copy silently, the same discipline used
# manually this session, now enforced every time instead of relying on remembering to check.
$sourceHash = (Get-FileHash -Path $scriptSource -Algorithm SHA256).Hash
$destHash = (Get-FileHash -Path $scriptDest -Algorithm SHA256).Hash
if ($sourceHash -ne $destHash) {
    Fail "Staged copy is NOT byte-identical to the source (source=$sourceHash dest=$destHash). Something interfered with the copy -- do not trust anything staged in this run."
}
Write-Host "[OK] Staged trip script confirmed byte-identical: $destHash"

# The sidecar Step -3 (in ac3_login_diagnostic.ps1) reads: content hash on line 1, the exact
# commit this content came from on line 2 -- same BOM-less UTF8 / trailing-LF-only convention as
# build_installer.ps1's own .buildsha sidecar, for the same cross-platform-verifiable reason.
$stageHashPath = "$scriptDest.stagehash"
[System.IO.File]::WriteAllText($stageHashPath, "$destHash`n$localHead`n", [System.Text.UTF8Encoding]::new($false))
Write-Host "[OK] Wrote stage-time record: $stageHashPath (commit $localHead)"

# Also re-stage the installer artifacts alongside it, same as the manual sequence this replaces --
# a no-op copy if nothing changed, but keeps this the single command an operator needs to run.
foreach ($name in @('reclaim-setup.exe', 'reclaim-setup.exe.sha256', 'reclaim-setup.exe.buildsha')) {
    $src = Join-Path $RepoPath "packaging\dist\$name"
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination (Join-Path $StageDir $name) -Force
    } else {
        Write-Host "[WARNING] $src not found -- not staged. If this is the installer itself, no trip install can run until it exists."
    }
}

Write-Host ""
Write-Host "[DONE] Staged from commit $localHead to $StageDir. Safe to run the trip now."
