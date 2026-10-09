<#
.SYNOPSIS
  Move a directory to another volume and leave a directory junction at the old path
  (copy -> verify -> rename old -> junction -> keep old as <name>.moved; deleting it is a SEPARATE run).

.DESCRIPTION
  Default is a DRY RUN. Stages:
    1 preflight : source is a real directory (not already a link), target absent/empty, target volume
                  has >= 1.2x the source size free, source is not on the hardlink-dependent denylist
                  (uv cache, conda pkgs: hardlinks cannot span volumes, a junction there would make
                  every venv fall back to full copies).
    2 probe     : a rename of the source to <name>.probe and straight back. Windows refuses a directory
                  rename while any file under it is open, so success means no live handle holders.
    3 copy      : robocopy /E /COPY:DAT.
    4 verify    : file count + total bytes equal, SHA-256 of a seeded random sample (default 50 files,
                  plus the 5 largest) equal.
    5 swap      : rename source -> <name>.moved, create junction at the old path -> target.
                  Any failure renames .moved back (rollback) and exits non-zero.
  Removing <name>.moved is NOT done here (check and delete are separate commands): run the printed
  check, read it, then Remove-Item it yourself, or pass -DeleteMoved in a LATER invocation.

.EXAMPLE
  .\relocate_dir.ps1 -Source C:\Users\x\.cache\huggingface -Target D:\relocated\huggingface            # dry run
  .\relocate_dir.ps1 -Source ... -Target ... -Execute
  .\relocate_dir.ps1 -Source ... -Target ... -DeleteMoved                                               # later, after reading the check

  Rollback after -Execute: Remove-Item <Source> (the junction only: `(Get-Item <Source>).Delete()` or
  `cmd /c rmdir <Source>`) ; Rename-Item <Source>.moved <Source>.   After -DeleteMoved: robocopy the
  target back, then remove the junction.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Source,
    [Parameter(Mandatory)] [string]$Target,
    [switch]$Execute,
    [switch]$DeleteMoved,
    [int]$SampleFiles = 50
)
$ErrorActionPreference = 'Stop'
$Source = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
$moved = "$Source.moved"

$denylist = @('\AppData\Local\uv', '\anaconda3\pkgs', '\miniconda3\pkgs', '\.conda\pkgs', '\.venv', '\venv')
foreach ($d in $denylist) {
    if ($Source -like "*$d" -or $Source -like "*$d\*") {
        throw "REFUSED: $Source matches hardlink/venv-dependent '$d'. Hardlinks cannot span volumes."
    }
}

if ($DeleteMoved) {
    if (-not (Test-Path -LiteralPath $moved)) { throw "no $moved to delete" }
    $f = Get-ChildItem -LiteralPath $moved -Recurse -File -Force
    $t = Get-ChildItem -LiteralPath $Target -Recurse -File -Force
    $fb = ($f | Measure-Object Length -Sum).Sum; $tb = ($t | Measure-Object Length -Sum).Sum
    "CHECK: moved {0} files {1} B; target {2} files {3} B" -f $f.Count, $fb, $t.Count, $tb
    if ($f.Count -eq 0 -or $f.Count -ne $t.Count -or $fb -ne $tb) { throw "check failed or empty: NOT deleting" }
    Remove-Item -LiteralPath $moved -Recurse -Force
    "deleted $moved"; return
}

$item = Get-Item -LiteralPath $Source -Force
if ($item.LinkType) { throw "$Source is already a $($item.LinkType)" }
if (-not $item.PSIsContainer) { throw "$Source is not a directory" }
if (Test-Path -LiteralPath $Target) {
    if ((Get-ChildItem -LiteralPath $Target -Force | Measure-Object).Count -gt 0) { throw "target $Target not empty" }
}
$links = Get-ChildItem -LiteralPath $Source -Recurse -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue | Select-Object -First 3
if ($links) { throw "REFUSED: $Source contains reparse points (junction/symlink), e.g. $($links[0].FullName); robocopy would mis-copy them" }
$files = Get-ChildItem -LiteralPath $Source -Recurse -File -Force
$bytes = ($files | Measure-Object Length -Sum).Sum
$tgtRoot = [System.IO.Path]::GetPathRoot((New-Item -ItemType Directory -Force -Path (Split-Path $Target -Parent)).FullName)
$free = (Get-PSDrive ($tgtRoot.Substring(0, 1))).Free
"source {0}: {1} files, {2:N2} GB; target volume {3} free {4:N1} GB" -f $Source, $files.Count, ($bytes / 1GB), $tgtRoot, ($free / 1GB)
if ($free -lt 1.2 * $bytes) { throw "target volume lacks 1.2x space" }
if ((Split-Path $Source -Qualifier) -eq (Split-Path $Target -Qualifier)) { throw "same volume: nothing to gain" }

# probe: a directory cannot be renamed while any descendant file is open
try { Rename-Item -LiteralPath $Source "$(Split-Path $Source -Leaf).probe" -ErrorAction Stop; Rename-Item -LiteralPath "$Source.probe" (Split-Path $Source -Leaf) -ErrorAction Stop; "probe: no open handles (rename round-trip ok)" }
catch { if ((Test-Path "$Source.probe") -and -not (Test-Path $Source)) { Rename-Item -LiteralPath "$Source.probe" (Split-Path $Source -Leaf) }; throw "probe failed (live holders?): $_" }

if (-not $Execute) { "DRY RUN OK: re-run with -Execute to copy, verify and swap."; return }

robocopy $Source $Target /E /XJ /COPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NP /NS /NC | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE); source untouched" }
$tf = Get-ChildItem -LiteralPath $Target -Recurse -File -Force
$tb = ($tf | Measure-Object Length -Sum).Sum
if ($tf.Count -ne $files.Count -or $tb -ne $bytes) { throw "verify failed: count/bytes differ ({0}/{1} vs {2}/{3}); source untouched" -f $tf.Count, $tb, $files.Count, $bytes }
$sample = @($files | Sort-Object Length -Descending | Select-Object -First 5) + @($files | Get-Random -Count ([Math]::Min($SampleFiles, $files.Count)) -SetSeed 42)
$bad = 0
foreach ($s in ($sample | Select-Object -Unique -Property FullName)) {
    $rel = $s.FullName.Substring($Source.Length)
    if ((Get-FileHash -LiteralPath $s.FullName).Hash -ne (Get-FileHash -LiteralPath ($Target + $rel)).Hash) { $bad++ }
}
"verify: counts+bytes equal; hash sample mismatches = $bad"
if ($bad) { throw "hash mismatch; source untouched" }

try {
    Rename-Item -LiteralPath $Source (Split-Path $moved -Leaf) -ErrorAction Stop
    New-Item -ItemType Junction -Path $Source -Target $Target | Out-Null
    $n = (Get-ChildItem -LiteralPath $Source -Recurse -File -Force | Measure-Object).Count
    if ($n -ne $files.Count) { throw "post-swap count $n != $($files.Count)" }
    "SWAPPED: $Source -> $Target (junction). Old copy kept at $moved. Next: run with -DeleteMoved (it re-checks counts first)."
}
catch {
    "swap failed: $_ ; rolling back"
    if (Test-Path -LiteralPath $Source) { cmd /c rmdir "$Source" }
    if (Test-Path -LiteralPath $moved) { Rename-Item -LiteralPath $moved (Split-Path $Source -Leaf) }
    exit 1
}
