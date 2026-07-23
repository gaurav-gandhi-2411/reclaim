# Safe-mode-survives-packaging smoke test -- run against the ACTUAL Nuitka --standalone build,
# never the dev tree (that's what evals/test_safe_mode_gate.py already proves exhaustively).
# This script proves the same final, observable guarantees hold in the compiled artifact:
#   1. A fresh install (no prior data/mode_log.jsonl) reports mode=safe.
#   2. A real `--apply` batch against a dev_artifacts-eligible fixture, with config.toml
#      explicitly trying to enable dev_artifacts/duplicates/model_caches, still resolves to
#      method=recycle_bin (never vault/direct_delete) -- guarantee 1, exercised end-to-end.
#   3. Entering power mode without the exact confirmation phrase fails and mode stays safe;
#      the exact phrase succeeds; typed confirmation is the only door, in the packaged exe.
#   4. Reverting to safe mode needs no confirmation and takes effect immediately.
#
# Usage: pwsh -File packaging\test_packaged_safe_mode.ps1 -DistDir packaging\build\entry_point.dist
# ASCII-only file, deliberately -- keeps this script parseable by both Windows PowerShell 5.1
# (which does not default to UTF-8 for BOM-less script files) and PowerShell 7.

param(
    [string]$DistDir = "packaging\build\entry_point.dist"
)

$ErrorActionPreference = "Stop"
$exe = Join-Path (Resolve-Path $DistDir) "reclaim.exe"
if (-not (Test-Path $exe)) { throw "reclaim.exe not found at $exe -- build it first." }

$work = Join-Path $env:TEMP ("reclaim_pkg_test_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work | Out-Null
Set-Location $work
Write-Host "== Fresh install simulation dir: $work ==" -ForegroundColor Cyan

function Run($argsLine) {
    $p = Start-Process -FilePath $exe -ArgumentList $argsLine -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput "$work\stdout.txt" -RedirectStandardError "$work\stderr.txt"
    $out = Get-Content "$work\stdout.txt" -Raw -ErrorAction SilentlyContinue
    $err = Get-Content "$work\stderr.txt" -Raw -ErrorAction SilentlyContinue
    return [pscustomobject]@{ ExitCode = $p.ExitCode; Stdout = $out; Stderr = $err }
}

$fail = $false
function Check($name, $cond) {
    if ($cond) { Write-Host "PASS: $name" -ForegroundColor Green }
    else { Write-Host "FAIL: $name" -ForegroundColor Red; $script:fail = $true }
}

# --- 1. Fresh-install default mode ---------------------------------------------------------
$r = Run "mode"
Check "fresh install reports mode=safe" ($r.Stdout -match "reclaim mode:\s*safe")

# --- 2. Build a dev_artifacts fixture + a config.toml that tries to defeat safe mode -------
$fixture = Join-Path $work "fixture"
New-Item -ItemType Directory -Path "$fixture\node_modules\pkg" -Force | Out-Null
Set-Content -Path "$fixture\package.json" -Value '{"name":"fixture"}'
Set-Content -Path "$fixture\node_modules\pkg\index.js" -Value ("x" * 2000000)  # 2MB filler

@'
[categories.dev_artifacts]
enabled = true
retention_days = 0

[categories.duplicates]
enabled = true

[categories.model_caches]
enabled = true
'@ | Set-Content -Path "$work\config.toml"

$r = Run "scan `"$fixture`" --db `"$work\index.sqlite3`""
Check "scan of fixture tree succeeds" ($r.ExitCode -eq 0)

$r = Run "apply `"$fixture`" --db `"$work\index.sqlite3`" --config `"$work\config.toml`" --apply --tier both --vault-dir `"$work\quarantine`" --manifest `"$work\quarantine\manifest.jsonl`""
Check "apply (real --apply) exits 0" ($r.ExitCode -eq 0)
Check "apply chose method=recycle_bin despite config.toml enabling dev_artifacts/duplicates/model_caches" `
    ($r.Stdout -match "method=recycle_bin")
Check "apply did NOT choose vault or direct_delete" `
    (($r.Stdout -notmatch "method=vault") -and ($r.Stdout -notmatch "method=direct_delete"))
# manifest.jsonl is written as an audit-trail entry for EVERY method (vault, recycle_bin,
# direct_delete alike) -- its existence alone proves nothing about which method ran. The real
# vault signal is the per-batch subdirectory apply_batch creates only when it actually moves
# files into the vault (data/quarantine/<batch_id>/), and the manifest's own recorded fields.
$manifestLine = Get-Content "$work\quarantine\manifest.jsonl" -Raw -ErrorAction SilentlyContinue
$manifestEntry = $null
if ($manifestLine) { $manifestEntry = $manifestLine | ConvertFrom-Json }
Check "manifest records method=recycle_bin, vault_path=null (nothing was ever vaulted)" `
    ($manifestEntry -and $manifestEntry.method -eq "recycle_bin" -and $null -eq $manifestEntry.vault_path)
$vaultBatchDirs = Get-ChildItem "$work\quarantine" -Directory -ErrorAction SilentlyContinue
Check "no per-batch vault subdirectory was created under quarantine\" `
    (-not $vaultBatchDirs -or $vaultBatchDirs.Count -eq 0)
Check "dev_artifacts fixture file no longer at its original path (recycle-bin-moved)" `
    (-not (Test-Path "$fixture\node_modules\pkg\index.js"))

# --- 3. Power mode requires the EXACT typed confirmation, nothing less ---------------------
$r = Run "mode power --confirm `"close enough`""
Check "wrong confirmation phrase is rejected (exit != 0)" ($r.ExitCode -ne 0)
$r = Run "mode"
Check "mode still safe after a rejected power-mode attempt" ($r.Stdout -match "reclaim mode:\s*safe")

$r = Run "mode power --confirm `"I understand this can permanently delete files`""
Check "exact confirmation phrase switches to power mode" ($r.ExitCode -eq 0)
$r = Run "mode"
Check "mode now reports power" ($r.Stdout -match "reclaim mode:\s*power")

# --- 4. Reverting to safe needs no confirmation and takes effect immediately ---------------
$r = Run "mode safe"
Check "switch back to safe succeeds" ($r.ExitCode -eq 0)
$r = Run "mode"
Check "mode reports safe again after revert" ($r.Stdout -match "reclaim mode:\s*safe")

Set-Location $env:TEMP
if ($fail) {
    Write-Host "`nONE OR MORE CHECKS FAILED -- see output above." -ForegroundColor Red
    exit 1
} else {
    Write-Host "`nAll packaged-artifact safe-mode checks passed." -ForegroundColor Green
    exit 0
}
