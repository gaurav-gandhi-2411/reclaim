<#
.SYNOPSIS
  Move a directory to another volume and leave a directory junction at the old path
  (copy -> verify EVERY file -> freeze source -> delta re-sync -> junction; deleting the old copy is a SEPARATE run).

.DESCRIPTION
  Default is a DRY RUN. Stages:
    1 preflight : source is a real directory (not a link), contains no reparse points, `<name>.moved` does not
                  exist, target absent/empty, target volume has >= 1.2x the source size free, and neither the
                  source nor anything under it is hardlink/venv-dependent (uv cache, conda pkgs, .venv/venv
                  directories): hardlinks cannot span volumes, so a junction there would turn every venv into
                  full copies.
    2 probe     : rename the source to <name>.probe and straight back. Windows refuses a directory rename while
                  any file under it is open, so success means no live handle holders at that moment.
    3 copy      : robocopy /E /XJ /COPY:DAT.
    4 verify    : file count + total bytes equal AND SHA-256 of EVERY file equal (no sampling).
    5 freeze    : rename source -> <name>.moved. From here nothing can write to the old path (a rename fails
                  while a file is open), so this is the consistent snapshot.
    6 delta     : robocopy /MIR moved -> target picks up anything created/changed/deleted between copy and
                  freeze; every file written since the copy started is re-hashed.
    7 swap      : `mklink /J` the old path to the target (mklink, so [ ] in names are not treated as wildcards),
                  post-swap count check. Any failure in 5-7 renames `.moved` back (rollback) and exits non-zero;
                  the copy left in the target is then an orphan you must remove yourself.
  Removing <name>.moved is NOT done in an -Execute run (check and delete are separate commands). Run with
  -DeleteMoved later: it re-hashes every file of `.moved` against the target and refuses on any difference.

  Known limits: paths over 260 characters make the script throw at preflight (fail closed, nothing changed);
  /COPY:DAT does not copy ACLs/owner; tested on Windows PowerShell 5.1 only; a dry run creates the target's parent.

.EXAMPLE
  .\relocate_dir.ps1 -Source C:\Users\x\.cache\huggingface\hub -Target D:\relocated\huggingface\hub            # dry run
  .\relocate_dir.ps1 -Source ... -Target ... -Execute
  .\relocate_dir.ps1 -Source ... -Target ... -DeleteMoved                                                      # later

  Rollback after -Execute and before -DeleteMoved: `cmd /c rmdir <Source>` (removes only the junction), then
  `Rename-Item <Source>.moved <name>`; remove the target copy. After -DeleteMoved: robocopy the target back,
  then remove the junction.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Source,
    [Parameter(Mandatory)] [string]$Target,
    [switch]$Execute,
    [switch]$DeleteMoved,
    [switch]$ResumeTarget # allow a non-empty target left by an earlier rolled-back run: robocopy re-syncs it, the full-hash verify still runs
)
$ErrorActionPreference = 'Stop'
$Source = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
$moved = "$Source.moved"
$leaf = Split-Path $Source -Leaf

function Get-TreeFiles([string]$root) { Get-ChildItem -LiteralPath $root -Recurse -File -Force }
function Compare-Hashes([string]$a, [string]$b, [object[]]$files) {
    # returns the number of files whose SHA-256 differs (or which are missing) between trees a and b
    $bad = 0
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($a.Length)
        $other = $b + $rel
        if (-not (Test-Path -LiteralPath $other) -or
            (Get-FileHash -LiteralPath $f.FullName).Hash -ne (Get-FileHash -LiteralPath $other).Hash) { $bad++ }
    }
    return $bad
}

if ($DeleteMoved) {
    if (-not (Test-Path -LiteralPath $moved)) { throw "no $moved to delete" }
    $f = @(Get-TreeFiles $moved)
    $t = @(Get-TreeFiles $Target)
    $fb = ($f | Measure-Object Length -Sum).Sum; $tb = ($t | Measure-Object Length -Sum).Sum
    "CHECK: moved {0} files {1} B; target {2} files {3} B" -f $f.Count, $fb, $t.Count, $tb
    if ($f.Count -eq 0 -or $f.Count -ne $t.Count -or $fb -ne $tb) { throw "check failed or empty: NOT deleting" }
    $bad = Compare-Hashes $moved $Target $f
    "CHECK: SHA-256 of all {0} files, differences = {1}" -f $f.Count, $bad
    if ($bad) { throw "content differs: NOT deleting" }
    Remove-Item -LiteralPath $moved -Recurse -Force
    "deleted $moved"; return
}

# ---- 1 preflight
$item = Get-Item -LiteralPath $Source -Force
if ($item.LinkType) { throw "$Source is already a $($item.LinkType)" }
if (-not $item.PSIsContainer) { throw "$Source is not a directory" }
if (Test-Path -LiteralPath $moved) { throw "$moved already exists (stale from an earlier run?): inspect it first" }

$denyAbs = @("$env:LOCALAPPDATA\uv", "$env:USERPROFILE\anaconda3\pkgs", "$env:USERPROFILE\miniconda3\pkgs", "$env:USERPROFILE\.conda\pkgs")
foreach ($d in $denyAbs) {
    if ($Source -ieq $d -or $Source.StartsWith($d + '\', 'OrdinalIgnoreCase') -or $d.StartsWith($Source + '\', 'OrdinalIgnoreCase')) {
        throw "REFUSED: $Source is, is inside, or contains hardlink-dependent '$d'. Hardlinks cannot span volumes."
    }
}
if ($Source -match '(?i)\\(\.venv|venv)(\\|$)') { throw "REFUSED: $Source is inside a virtualenv" }
$inner = Get-ChildItem -LiteralPath $Source -Directory -Recurse -Force -Depth 6 -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @('.venv', 'venv', 'site-packages') } | Select-Object -First 1
if ($inner) { throw "REFUSED: $Source contains a virtualenv/site-packages ($($inner.FullName)); venvs hold hardlinks into the uv/conda cache" }

$links = Get-ChildItem -LiteralPath $Source -Recurse -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue | Select-Object -First 3
if ($links) { throw "REFUSED: $Source contains reparse points (junction/symlink), e.g. $($links[0].FullName); robocopy would mis-copy them" }
if (Test-Path -LiteralPath $Target) {
    if (-not $ResumeTarget -and (Get-ChildItem -LiteralPath $Target -Force | Measure-Object).Count -gt 0) { throw "target $Target not empty (use -ResumeTarget to re-sync a copy left by a rolled-back run)" }
}
$files = @(Get-TreeFiles $Source)
$bytes = ($files | Measure-Object Length -Sum).Sum
$tgtRoot = [System.IO.Path]::GetPathRoot((New-Item -ItemType Directory -Force -Path (Split-Path $Target -Parent)).FullName)
$free = (Get-PSDrive ($tgtRoot.Substring(0, 1))).Free
"source {0}: {1} files, {2:N2} GB; target volume {3} free {4:N1} GB" -f $Source, $files.Count, ($bytes / 1GB), $tgtRoot, ($free / 1GB)
if ($files.Count -eq 0) { throw "source has no files" }
if ($free -lt 1.2 * $bytes) { throw "target volume lacks 1.2x space" }
if ((Split-Path $Source -Qualifier) -eq (Split-Path $Target -Qualifier)) { throw "same volume: nothing to gain" }

# ---- 2 probe
try {
    Rename-Item -LiteralPath $Source "$leaf.probe" -ErrorAction Stop
    Rename-Item -LiteralPath "$Source.probe" $leaf -ErrorAction Stop
    "probe: no open handles (rename round-trip ok)"
}
catch {
    if ((Test-Path -LiteralPath "$Source.probe") -and -not (Test-Path -LiteralPath $Source)) { Rename-Item -LiteralPath "$Source.probe" $leaf }
    throw "probe failed (live holders?): $_"
}

if (-not $Execute) { "DRY RUN OK: re-run with -Execute to copy, verify and swap."; return }

# ---- 3 copy, 4 verify everything
$copyStart = Get-Date
$copyMode = if ($ResumeTarget) { '/MIR' } else { '/E' }  # /MIR also purges files in a resumed target that no longer exist in the source
robocopy $Source $Target $copyMode /XJ /COPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NP /NS /NC | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE); source untouched (partial copy left in $Target)" }
$tf = @(Get-TreeFiles $Target)
$tb = ($tf | Measure-Object Length -Sum).Sum
if ($tf.Count -ne $files.Count -or $tb -ne $bytes) { throw "verify failed: count/bytes differ ({0}/{1} vs {2}/{3}); source untouched, remove the copy in $Target" -f $tf.Count, $tb, $files.Count, $bytes }
$bad = Compare-Hashes $Source $Target $files
"verify: counts+bytes equal; SHA-256 of all $($files.Count) files, differences = $bad"
if ($bad) { throw "hash mismatch; source untouched, remove the copy in $Target" }

# ---- 5 freeze, 6 delta, 7 swap (rollback on any failure)
$frozen = $false
try {
    Rename-Item -LiteralPath $Source "$leaf.moved" -ErrorAction Stop
    $frozen = $true
    robocopy $moved $Target /MIR /XJ /COPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NP /NS /NC | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "delta robocopy failed ($LASTEXITCODE)" }
    $mf = @(Get-TreeFiles $moved)
    $changed = @($mf | Where-Object { $_.LastWriteTime -ge $copyStart.AddSeconds(-2) })
    $badDelta = Compare-Hashes $moved $Target $changed
    $tf2 = @(Get-TreeFiles $Target)
    $mb = ($mf | Measure-Object Length -Sum).Sum; $tb2 = ($tf2 | Measure-Object Length -Sum).Sum
    "delta: {0} files changed since the copy started re-hashed, differences = {1}; counts {2}/{3}, bytes {4}/{5}" -f $changed.Count, $badDelta, $mf.Count, $tf2.Count, $mb, $tb2
    if ($badDelta -or $mf.Count -ne $tf2.Count -or $mb -ne $tb2) { throw "delta verification failed" }
    cmd /c mklink /J "$Source" "$Target" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "mklink /J failed" }
    $n = @(Get-TreeFiles $Source).Count
    if ($n -ne $mf.Count) { throw "post-swap count $n != $($mf.Count)" }
    "SWAPPED: $Source -> $Target (junction). Old copy kept at $moved. Next, in a separate run: -DeleteMoved (re-hashes everything first)."
}
catch {
    "swap failed: $_ ; rolling back"
    if ($frozen) {
        if (Test-Path -LiteralPath $Source) { cmd /c rmdir "$Source" | Out-Null }
        if ((Test-Path -LiteralPath $moved) -and -not (Test-Path -LiteralPath $Source)) { Rename-Item -LiteralPath $moved $leaf }
    }
    "ROLLED BACK: $Source is the original directory again. The copy in $Target is an ORPHAN: inspect and remove it before retrying."
    exit 1
}
