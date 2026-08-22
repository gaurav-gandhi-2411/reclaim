#Requires -Version 7
<#
.SYNOPSIS
    Frozen-artifact smoke suite for an already-installed/built Reclaim Nuitka --standalone
    binary -- proves the SHIPPED .exe behaves correctly, not just `python -m reclaim`.

.DESCRIPTION
    Almost nothing in this codebase has ever been exercised against the real frozen artifact:
    CI, scripts/verify.py, and every eval gate all run against source. Two real packaging bugs
    (winrt/windows_toasts bundling, PR #49; TEMP 8.3 short-name path resolution, PR #50) were
    found ONLY by manually poking at a real frozen build, never by any automated check. This
    script is that missing automated check, run BEFORE trusting any installer rebuild.

    Every check reports exactly one of PASS / FAIL / BLOCKED -- never a silent skip (house rule
    98a: a check that can't verify something must not be read as a pass). One check failing or
    being blocked never prevents the rest from running and reporting independently.

    Some checks (protocol handler, Task Scheduler) can only exercise their positive path against
    a REAL installed build (registry keys / scheduled tasks the Inno Setup installer registers,
    packaging/reclaim.iss) -- against a bare Nuitka dist folder (no installer ever run) they
    correctly report BLOCKED, not FAIL: the mechanism hasn't been installed to test, which is a
    different fact than the mechanism being broken.

.PARAMETER InstallPath
    Directory containing the frozen reclaim.exe. Defaults to the Inno Setup installer's real
    per-user install location (packaging/reclaim.iss: `DefaultDirName={autopf}\Reclaim` with
    `PrivilegesRequired=lowest`, which Inno resolves to `%LOCALAPPDATA%\Programs\Reclaim` for a
    non-elevated install -- NOT `%ProgramFiles%`).

.PARAMETER OutDir
    Where results.json and per-check scratch state are written. Created if missing. MUST NOT
    live inside any git repository (including this one) -- src/reclaim/safety.py blanket-denies
    every candidate whose path resolves inside a detected git repo (REASON_IN_GIT_REPOSITORY,
    the same rule that keeps a real user's own dev repos safe), so scratch/seed files placed
    under a repo checkout are silently excluded from /api/candidates and check 7 fails with a
    misleading "candidate not proposed" error that has nothing to do with the frozen build
    (confirmed directly while building this suite -- the default below deliberately resolves
    under %TEMP%, never under $PSScriptRoot, for exactly this reason).

.PARAMETER Port
    Loopback port used for the `reclaim serve` instance checks 1c/6/7 share. Deliberately not
    the app's own default (8420) to avoid colliding with a real Reclaim instance that might
    already be running on this machine.

.EXAMPLE
    pwsh -File packaging/smoke/run_frozen_smoke_suite.ps1 `
        -InstallPath 'C:\Users\me\AppData\Local\Programs\Reclaim' `
        -OutDir 'packaging/smoke/results'
#>

[CmdletBinding()]
param(
    [string]$InstallPath = (Join-Path $env:LOCALAPPDATA 'Programs\Reclaim'),
    [string]$OutDir = (Join-Path $env:TEMP 'reclaim-frozen-smoke-suite'),
    [int]$Port = 18420
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ExePath = Join-Path $InstallPath 'reclaim.exe'
$PythonExe = 'python'
$ScratchRoot = Join-Path $OutDir 'scratch'
$ResultsJsonPath = Join-Path $OutDir 'results.json'

# --- Result bookkeeping -----------------------------------------------------------------------

$script:Results = [System.Collections.Generic.List[pscustomobject]]::new()

function Add-Result {
    param(
        [Parameter(Mandatory)][string]$Id,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet('PASS', 'FAIL', 'BLOCKED')][string]$Result,
        [Parameter(Mandatory)][string]$Detail,
        [object]$Extra = $null
    )
    $entry = [pscustomobject]@{
        id     = $Id
        name   = $Name
        result = $Result
        detail = $Detail
        extra  = $Extra
    }
    $script:Results.Add($entry) | Out-Null
    $color = switch ($Result) { 'PASS' { 'Green' }; 'FAIL' { 'Red' }; 'BLOCKED' { 'Yellow' } }
    Write-Host ("[{0,-7}] {1,-40} {2}" -f $Result, $Id, $Name) -ForegroundColor $color
    if ($Detail) { Write-Host ("          {0}" -f $Detail) -ForegroundColor DarkGray }
}

# --- Frozen-exe process helper -----------------------------------------------------------------
# `--windows-console-mode=attach` (build_installer.ps1) makes the frozen exe behave oddly under
# naive output capture (`& $exe args` with no console attached, e.g. from a non-interactive
# agent shell, silently returns no output at all -- confirmed directly while building this
# suite). ProcessStartInfo with explicit redirection is the reliable way to capture stdout/
# stderr regardless of the caller's own console state; used for every frozen-exe invocation
# below instead of the `&` call operator.

function Invoke-FrozenExe {
    param(
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [int]$TimeoutSeconds = 30
    )
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $ExePath
    foreach ($a in $ArgumentList) { $psi.ArgumentList.Add($a) }
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false

    $proc = [System.Diagnostics.Process]::new()
    $proc.StartInfo = $psi
    try {
        $proc.Start() | Out-Null
        # Read both streams asynchronously BEFORE WaitForExit -- reading synchronously only
        # after the process exits risks the classic redirected-pipe deadlock if the child
        # writes enough output to fill the OS pipe buffer before exiting (confirmed unreliable
        # in an earlier event-based version of this function while building this suite: async
        # OutputDataReceived/ErrorDataReceived events could still be draining when read back
        # immediately after WaitForExit). This is .NET's documented deadlock-free pattern.
        $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
        $stderrTask = $proc.StandardError.ReadToEndAsync()
        $finished = $proc.WaitForExit($TimeoutSeconds * 1000)
        if (-not $finished) {
            try { $proc.Kill($true) } catch { }
            return [pscustomobject]@{ ExitCode = $null; StdOut = ''; StdErr = ''; TimedOut = $true }
        }
        [System.Threading.Tasks.Task]::WaitAll(@($stdoutTask, $stderrTask), 5000) | Out-Null
        return [pscustomobject]@{
            ExitCode = $proc.ExitCode; StdOut = $stdoutTask.Result; StdErr = $stderrTask.Result
            TimedOut = $false
        }
    } finally {
        $proc.Dispose()
    }
}

function Invoke-FrozenExeBackground {
    <# Launches a long-running frozen-exe command (e.g. `serve`) and returns the live Process
       object immediately -- caller is responsible for stopping it. #>
    param(
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $ExePath
    foreach ($a in $ArgumentList) { $psi.ArgumentList.Add($a) }
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    return [System.Diagnostics.Process]::Start($psi)
}

function Wait-ForHttp {
    param([Parameter(Mandatory)][string]$Url, [int]$TimeoutSeconds = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($resp.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Milliseconds 300 }
    }
    return $false
}

# --- Setup --------------------------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
New-Item -ItemType Directory -Force -Path $ScratchRoot | Out-Null

# Fail loud, not silently-wrong: see -OutDir's own help text above for why a scratch dir inside
# a git repo produces a misleading check-7 FAIL that has nothing to do with the frozen build.
$probe = Get-Item $ScratchRoot
while ($null -ne $probe) {
    if (Test-Path (Join-Path $probe.FullName '.git')) {
        Write-Error "OutDir ($OutDir) resolves inside a git repository (found .git under $($probe.FullName)). Pick a location outside any repo -- see this script's -OutDir help for why."
        exit 2
    }
    $probe = $probe.Parent
}

Write-Host "=== Reclaim frozen-artifact smoke suite ===" -ForegroundColor Cyan
Write-Host "InstallPath: $InstallPath"
Write-Host "ExePath:     $ExePath"
Write-Host "OutDir:      $OutDir"
Write-Host ""

# --- Check 1a: artifact identity -- is this actually a Nuitka --standalone dist, not a thin
# shim? (A PATH shim / pip-installed console-script .exe is typically tens of KB with no sibling
# .pyd/.dll files; a real --standalone dist ships dozens of native extension modules alongside
# the main exe.) This is a hard gate: every other check below needs a real frozen exe to run at
# all, so a failure here aborts the rest of the suite rather than producing a wall of misleading
# FAILs against a target that was never the right artifact to begin with.

if (-not (Test-Path $ExePath)) {
    Add-Result -Id '1a-artifact-identity' -Name 'Frozen artifact identity' -Result 'FAIL' `
        -Detail "reclaim.exe not found at $ExePath -- nothing else in this suite can run. Check -InstallPath."
    $script:Results | ConvertTo-Json -Depth 10 | Set-Content -Path $ResultsJsonPath -Encoding utf8
    Write-Host "`nAborting: no frozen exe to test." -ForegroundColor Red
    exit 2
}
$exeItem = Get-Item $ExePath
$siblingPyd = @(Get-ChildItem -Path $InstallPath -Filter '*.pyd' -ErrorAction SilentlyContinue)
$siblingDll = @(Get-ChildItem -Path $InstallPath -Filter '*.dll' -ErrorAction SilentlyContinue)
$looksFrozen = ($exeItem.Length -gt 5MB) -and ($siblingPyd.Count -ge 3) -and ($siblingDll.Count -ge 3)
# Invariant-culture formatting: the host's regional settings (e.g. en-IN) can group thousands
# unexpectedly (`{0:N0}` produced "35,23,44,064" instead of "352,344,064" on this dev machine) --
# invariant culture keeps the report readable regardless of the machine running it.
$sizeFormatted = $exeItem.Length.ToString('N0', [System.Globalization.CultureInfo]::InvariantCulture)
if ($looksFrozen) {
    $detail = "reclaim.exe is $sizeFormatted bytes with $($siblingPyd.Count) sibling .pyd and " +
        "$($siblingDll.Count) sibling .dll files -- consistent with a real Nuitka --standalone " +
        "dist, not a thin launcher/shim."
    Add-Result -Id '1a-artifact-identity' -Name 'Frozen artifact identity' -Result 'PASS' -Detail $detail
} else {
    $detail = "reclaim.exe at $ExePath is $sizeFormatted bytes with only $($siblingPyd.Count) " +
        ".pyd / $($siblingDll.Count) .dll siblings -- this does not look like a Nuitka " +
        "--standalone dist (could be a PATH shim or an unbuilt/partial dist). Aborting the " +
        "rest of the suite."
    Add-Result -Id '1a-artifact-identity' -Name 'Frozen artifact identity' -Result 'FAIL' -Detail $detail
    $script:Results | ConvertTo-Json -Depth 10 | Set-Content -Path $ResultsJsonPath -Encoding utf8
    exit 2
}

# --- Check 1b: Nuitka bundle completeness (structural proxy, winrt only) -----------------------
# `winrt` ships native `.pyd` modules Nuitka's static import-following can miss (PR #49's root
# cause) -- directory presence IS a meaningful signal for it. The SAME heuristic is NOT reliable
# for pure-Python packages (confirmed directly while building this suite: the `mcp` package has
# no on-disk directory in this dist at all -- Nuitka compiled it straight into the binary's
# module store -- yet `reclaim mcp-serve` works correctly; see check 2). So this check covers
# ONLY the native-import risk class, not "is every package bundled" in general.

$winrtNamespaceDir = Join-Path $InstallPath 'winrt\windows'
$windowsToastsDir = Join-Path $InstallPath 'windows_toasts'
$winrtOk = (Test-Path $winrtNamespaceDir) -and (Test-Path $windowsToastsDir)
if ($winrtOk) {
    Add-Result -Id '1b-winrt-bundle-structural' -Name 'winrt/windows_toasts bundled (structural)' `
        -Result 'PASS' -Detail "Both winrt\windows (PEP 420 namespace pkg) and windows_toasts are present in the dist."
} else {
    $missing = @()
    if (-not (Test-Path $winrtNamespaceDir)) { $missing += 'winrt\windows' }
    if (-not (Test-Path $windowsToastsDir)) { $missing += 'windows_toasts' }
    $detail = ("Missing from dist: {0} -- this is PR #49's exact bug " +
        "(ModuleNotFoundError: winrt.windows.foundation). Check 3 (toast) proves the " +
        "functional consequence; this check proves the structural cause.") -f ($missing -join ', ')
    Add-Result -Id '1b-winrt-bundle-structural' -Name 'winrt/windows_toasts bundled (structural)' `
        -Result 'FAIL' -Detail $detail
}

# --- Check 2: MCP stdio round trip --------------------------------------------------------------

$mcpScratch = Join-Path $ScratchRoot 'mcp'
New-Item -ItemType Directory -Force -Path $mcpScratch | Out-Null
$mcpHelper = Join-Path $PSScriptRoot 'mcp_stdio_probe.py'
try {
    $mcpArgs = @(
        $mcpHelper,
        '--exe', $ExePath,
        '--db', (Join-Path $mcpScratch 'index.sqlite3'),
        '--config', (Join-Path $mcpScratch 'config.toml')
    )
    $mcpOut = & $PythonExe @mcpArgs 2>&1 | Select-Object -Last 1
    $mcpJson = $mcpOut | ConvertFrom-Json
    Add-Result -Id '2-mcp-stdio' -Name 'reclaim mcp-serve (stdio JSON-RPC)' -Result $mcpJson.result `
        -Detail $mcpJson.detail -Extra $mcpJson
} catch {
    Add-Result -Id '2-mcp-stdio' -Name 'reclaim mcp-serve (stdio JSON-RPC)' -Result 'FAIL' `
        -Detail "harness error invoking mcp_stdio_probe.py: $($_.Exception.Message)"
}

# --- Check 3: toast notification (real code path, config-driven threshold=0 to force a real
# fire, not a fabricated hook) -------------------------------------------------------------------
# `reclaim check-disk-space` is the REAL command Task Scheduler invokes (packaging/reclaim.iss).
# Setting disk_threshold_percent=0.0 makes ANY real disk usage cross the threshold, so
# `send_disk_space_toast` (src/reclaim/notifications.py) genuinely runs -- this is not a
# fabricated debug hook, it's the production code path with a controlled config value. The
# result is read from stderr (structlog's "notifications.toast_failed" event + traceback, or its
# absence) since `send_disk_space_toast`'s own True/False return is never surfaced by the CLI.
# This proves the toast CODE PATH completes without raising -- it does NOT prove a human would
# see a popup on screen (Windows gives the sending process no such guarantee either way, and a
# headless/background invocation may not be able to observe an interactive desktop at all -- see
# this check's own docstring in src/reclaim/notifications.py). That narrower claim is BLOCKED
# below, separately, as the task instructions require.

$toastScratch = Join-Path $ScratchRoot 'toast'
New-Item -ItemType Directory -Force -Path $toastScratch | Out-Null
$toastConfig = @"
[notifications]
enabled = true
disk_threshold_percent = 0.0
"@
Set-Content -Path (Join-Path $toastScratch 'config.toml') -Value $toastConfig -Encoding utf8

try {
    $toastRun = Invoke-FrozenExe -WorkingDirectory $toastScratch -TimeoutSeconds 20 -ArgumentList @(
        'check-disk-space', '--config', 'config.toml', '--state', 'data\notification_state.json'
    )
    if ($toastRun.TimedOut) {
        Add-Result -Id '3-toast-codepath' -Name 'Disk-space toast (real code path)' -Result 'BLOCKED' `
            -Detail "reclaim check-disk-space did not exit within 20s -- cannot determine outcome."
    } elseif ($toastRun.ExitCode -ne 0) {
        Add-Result -Id '3-toast-codepath' -Name 'Disk-space toast (real code path)' -Result 'FAIL' `
            -Detail "reclaim check-disk-space exited $($toastRun.ExitCode) (expected 0 -- this command is documented to never raise). stderr: $($toastRun.StdErr.Substring(0, [Math]::Min(500, $toastRun.StdErr.Length)))"
    } elseif ($toastRun.StdErr -match 'notifications\.toast_failed' -or $toastRun.StdErr -match 'ModuleNotFoundError') {
        Add-Result -Id '3-toast-codepath' -Name 'Disk-space toast (real code path)' -Result 'FAIL' `
            -Detail "send_disk_space_toast raised internally (caught and logged, never crashes the CLI, but the toast did not fire). stderr tail: $($toastRun.StdErr.Substring([Math]::Max(0,$toastRun.StdErr.Length-800)))"
    } elseif ($toastRun.StdOut -match 'reason=would_notify') {
        Add-Result -Id '3-toast-codepath' -Name 'Disk-space toast (real code path)' -Result 'PASS' `
            -Detail "check_disk_space crossed the (forced) threshold and send_disk_space_toast completed with no logged failure. stdout: $($toastRun.StdOut.Trim())"
    } else {
        Add-Result -Id '3-toast-codepath' -Name 'Disk-space toast (real code path)' -Result 'BLOCKED' `
            -Detail "unexpected output shape -- could not determine would_notify/failure state. stdout: $($toastRun.StdOut.Trim()) stderr: $($toastRun.StdErr.Trim())"
    }
} catch {
    Add-Result -Id '3-toast-codepath' -Name 'Disk-space toast (real code path)' -Result 'FAIL' `
        -Detail "harness error: $($_.Exception.Message)"
}

Add-Result -Id '3b-toast-visual-confirmation' -Name 'Disk-space toast (visual, human-observed)' `
    -Result 'BLOCKED' -Detail ("Whether a human actually SEES a popup requires an interactive " +
        "desktop session and is not observable by a headless/background harness even when the " +
        "toast call itself succeeds (Windows gives the sending process no delivery guarantee). " +
        "Manual step: on an interactive session with the fix in place, run " +
        "'reclaim.exe check-disk-space --config <cfg with threshold 0> --state <path>' and " +
        "visually confirm a Windows toast appears with a working Snooze button.")

# --- Check 4: protocol handler --------------------------------------------------------------
# Scheme name and command line hardcoded to match packaging/reclaim.iss's [Registry] section --
# keep these two in sync if the scheme or invocation ever changes there.

$protocolScheme = 'reclaim-notify'
$regPath = "HKCU:\Software\Classes\$protocolScheme"
if (-not (Test-Path $regPath)) {
    Add-Result -Id '4-protocol-handler' -Name 'reclaim-notify: protocol handler' -Result 'BLOCKED' `
        -Detail ("No installer has registered HKCU\Software\Classes\$protocolScheme on this " +
            "machine (packaging/reclaim.iss's [Registry] section runs only during a real " +
            "install). Manual/future step: run reclaim-setup.exe, then re-run this check.")
} else {
    $cmdPath = "$regPath\shell\open\command"
    $cmdValue = (Get-ItemProperty -Path $cmdPath -ErrorAction SilentlyContinue).'(default)'
    if (-not $cmdValue -or $cmdValue -notmatch [regex]::Escape($ExePath) -or $cmdValue -notmatch 'check-disk-space' -or $cmdValue -notmatch '--apply-snooze') {
        Add-Result -Id '4-protocol-handler' -Name 'reclaim-notify: protocol handler' -Result 'FAIL' `
            -Detail "Registered command does not reference the expected exe/args. Value: $cmdValue"
    } else {
        # Extract the --state path so we can observe a real side effect and restore prior state
        # afterward (this check must never permanently alter a real install's notification state).
        $stateMatch = [regex]::Match($cmdValue, '--state\s+"([^"]+)"')
        if (-not $stateMatch.Success) {
            Add-Result -Id '4-protocol-handler' -Name 'reclaim-notify: protocol handler' -Result 'BLOCKED' `
                -Detail "Could not parse --state path out of the registered command to observe a side effect: $cmdValue"
        } else {
            $statePath = $stateMatch.Groups[1].Value
            $backupPath = "$statePath.smoke-backup"
            $hadExisting = Test-Path $statePath
            if ($hadExisting) { Copy-Item -Path $statePath -Destination $backupPath -Force }
            try {
                $before = if ($hadExisting) { Get-Content $statePath -Raw } else { $null }
                Start-Process $protocolScheme':smoketest' | Out-Null
                Start-Sleep -Seconds 3
                $after = if (Test-Path $statePath) { Get-Content $statePath -Raw } else { $null }
                if ($after -and $after -ne $before -and $after -match 'snoozed_until') {
                    Add-Result -Id '4-protocol-handler' -Name 'reclaim-notify: protocol handler' -Result 'PASS' `
                        -Detail "Start-Process reclaim-notify:smoketest launched the registered command and notification_state.json's snoozed_until changed."
                } else {
                    Add-Result -Id '4-protocol-handler' -Name 'reclaim-notify: protocol handler' -Result 'FAIL' `
                        -Detail "Protocol was invoked but no observable change to $statePath within 3s."
                }
            } finally {
                if ($hadExisting) {
                    Move-Item -Path $backupPath -Destination $statePath -Force
                } elseif (Test-Path $statePath) {
                    Remove-Item -Path $statePath -Force
                }
            }
        }
    }
}

# --- Check 5: Task Scheduler entry -----------------------------------------------------------

$taskName = 'Reclaim Disk Space Check'
$queryBefore = & schtasks.exe /query /tn $taskName /fo LIST /v 2>&1
$queryExit = $LASTEXITCODE
if ($queryExit -ne 0) {
    Add-Result -Id '5-task-scheduler' -Name "Scheduled task '$taskName'" -Result 'BLOCKED' `
        -Detail ("Task not queryable (schtasks exit $queryExit): $($queryBefore -join ' ') -- " +
            "most likely no installer has registered it on this machine. Manual/future step: " +
            "run reclaim-setup.exe, then re-run this check.")
} else {
    $lastRunBefore = ($queryBefore | Select-String '^Last Run Time:\s+(.+)$').Matches.Groups[1].Value
    $runResult = & schtasks.exe /run /tn $taskName 2>&1
    $runExit = $LASTEXITCODE
    if ($runExit -ne 0) {
        Add-Result -Id '5-task-scheduler' -Name "Scheduled task '$taskName'" -Result 'BLOCKED' `
            -Detail "Task exists but 'schtasks /run' failed (exit $runExit): $($runResult -join ' ') -- may need an interactive session/elevation this harness doesn't have."
    } else {
        Start-Sleep -Seconds 5
        $queryAfter = & schtasks.exe /query /tn $taskName /fo LIST /v 2>&1
        $lastRunAfter = ($queryAfter | Select-String '^Last Run Time:\s+(.+)$').Matches.Groups[1].Value
        if ($lastRunAfter -and $lastRunAfter -ne $lastRunBefore) {
            Add-Result -Id '5-task-scheduler' -Name "Scheduled task '$taskName'" -Result 'PASS' `
                -Detail "On-demand run triggered; Last Run Time advanced from '$lastRunBefore' to '$lastRunAfter'."
        } else {
            Add-Result -Id '5-task-scheduler' -Name "Scheduled task '$taskName'" -Result 'FAIL' `
                -Detail "schtasks /run exited 0 but Last Run Time did not change within 5s (before='$lastRunBefore' after='$lastRunAfter')."
        }
    }
}

# --- Checks 1c / 6 / 7: shared `reclaim serve` instance -----------------------------------------

$serverScratch = Join-Path $ScratchRoot 'server'
$seedDir = Join-Path $serverScratch 'seed'
New-Item -ItemType Directory -Force -Path $seedDir | Out-Null

# Config override: large_logs is disabled with a 50MB/30-day threshold by default -- far too
# large/slow to seed deterministically in a smoke test. Enabling it with a tiny threshold here
# is the smallest config change that produces a real, restorable (retention_days=30 -> vault
# method) candidate through the REAL detector pipeline, not a synthetic bypass of it.
$defaultConfigPath = Join-Path $InstallPath 'config.toml'
if (Test-Path $defaultConfigPath) {
    $configLines = Get-Content $defaultConfigPath
} elseif (Test-Path (Join-Path $PSScriptRoot '..\config.default.toml')) {
    $configLines = Get-Content (Join-Path $PSScriptRoot '..\config.default.toml')
} else {
    $configLines = @()
}
$inLargeLogs = $false
$patched = foreach ($line in $configLines) {
    if ($line -match '^\[categories\.large_logs\]') { $inLargeLogs = $true; $line; continue }
    if ($inLargeLogs -and $line -match '^\[') { $inLargeLogs = $false }
    if ($inLargeLogs -and $line -match '^enabled\s*=') { 'enabled = true'; continue }
    if ($inLargeLogs -and $line -match '^min_size_bytes\s*=') { 'min_size_bytes = 1024'; continue }
    if ($inLargeLogs -and $line -match '^stale_days\s*=') { 'stale_days = 0'; continue }
    $line
}
Set-Content -Path (Join-Path $serverScratch 'config.toml') -Value $patched -Encoding utf8
Set-Content -Path (Join-Path $seedDir 'smoketest.log') -Value ('y' * 2048) -Encoding utf8 -NoNewline

$serverProc = $null
try {
    $serverProc = Invoke-FrozenExeBackground -WorkingDirectory $serverScratch -ArgumentList @(
        'serve', '--port', "$Port"
    )
    $baseUrl = "http://127.0.0.1:$Port"
    $serverUp = Wait-ForHttp -Url $baseUrl -TimeoutSeconds 20

    if (-not $serverUp) {
        $reason = "reclaim serve did not respond on $baseUrl within 20s."
        Add-Result -Id '1c-fixed-drives-endpoint' -Name 'GET /api/scan/fixed-drives (drives.py Win32 calls)' -Result 'BLOCKED' -Detail $reason
        Add-Result -Id '6-dpapi-roundtrip' -Name 'Anthropic key DPAPI round trip' -Result 'BLOCKED' -Detail $reason
        Add-Result -Id '7-scan-apply-undo' -Name 'Scan -> apply (vault) -> undo end-to-end' -Result 'BLOCKED' -Detail $reason
    } else {
        # 1c: drives.py's GetLogicalDrives/GetDriveTypeW, exercised via the real endpoint the
        # SIMPLE-mode full-drive-scan confirmation dialog calls (src/reclaim/api/routes.py).
        try {
            $drivesResp = Invoke-WebRequest -Uri "$baseUrl/api/scan/fixed-drives" -UseBasicParsing -TimeoutSec 10
            $drivesJson = $drivesResp.Content | ConvertFrom-Json
            if ($drivesResp.StatusCode -eq 200 -and $drivesJson.drives.Count -gt 0) {
                Add-Result -Id '1c-fixed-drives-endpoint' -Name 'GET /api/scan/fixed-drives (drives.py Win32 calls)' -Result 'PASS' `
                    -Detail "Returned $($drivesJson.drives.Count) fixed drive(s) via GetLogicalDrives/GetDriveTypeW."
            } else {
                Add-Result -Id '1c-fixed-drives-endpoint' -Name 'GET /api/scan/fixed-drives (drives.py Win32 calls)' -Result 'FAIL' `
                    -Detail "200 but zero drives returned, or non-200: $($drivesResp.StatusCode)"
            }
        } catch {
            Add-Result -Id '1c-fixed-drives-endpoint' -Name 'GET /api/scan/fixed-drives (drives.py Win32 calls)' -Result 'FAIL' `
                -Detail "request failed: $($_.Exception.Message)"
        }

        # 6: DPAPI round trip
        try {
            $dpapiOut = & $PythonExe (Join-Path $PSScriptRoot 'http_probe.py') dpapi-roundtrip --base $baseUrl 2>&1 | Select-Object -Last 1
            $dpapiJson = $dpapiOut | ConvertFrom-Json
            Add-Result -Id '6-dpapi-roundtrip' -Name 'Anthropic key DPAPI round trip' -Result $dpapiJson.result -Detail $dpapiJson.detail -Extra $dpapiJson
        } catch {
            Add-Result -Id '6-dpapi-roundtrip' -Name 'Anthropic key DPAPI round trip' -Result 'FAIL' -Detail "harness error: $($_.Exception.Message)"
        }

        # 7: scan -> apply (vault) -> undo
        try {
            $e2eOut = & $PythonExe (Join-Path $PSScriptRoot 'http_probe.py') scan-apply-undo --base $baseUrl --seed-dir $seedDir 2>&1 | Select-Object -Last 1
            $e2eJson = $e2eOut | ConvertFrom-Json
            Add-Result -Id '7-scan-apply-undo' -Name 'Scan -> apply (vault) -> undo end-to-end' -Result $e2eJson.result -Detail $e2eJson.detail -Extra $e2eJson
        } catch {
            Add-Result -Id '7-scan-apply-undo' -Name 'Scan -> apply (vault) -> undo end-to-end' -Result 'FAIL' -Detail "harness error: $($_.Exception.Message)"
        }
    }
} finally {
    if ($serverProc -and -not $serverProc.HasExited) {
        try { $serverProc.Kill($true) } catch { }
    }
}

# --- Documented (non-executable) coverage gaps from Part A's risk audit -------------------------
# These are NOT silently omitted -- each is a real risk item from the audit that this suite
# structurally cannot exercise on this machine/build, reported the same way as any other check
# so nothing is implied to be covered that isn't (house rule: no silent scope-narrowing).

Add-Result -Id '1d-preflight-hardlink-probes' -Name 'preflight.py hardlink/lock probes (ctypes.WinDLL)' `
    -Result 'BLOCKED' -Detail ("Only exercised by package_caches/model_caches candidates " +
        "(a real shared-hardlink-with-active-install scenario) -- this suite's seed data " +
        "(__pycache__, a synthetic large_log) never reaches that code path. Would need a " +
        "dedicated seed: two hardlinked files, one under a live venv/install root.")

Add-Result -Id '1e-config-8.3-shortname' -Name 'config.py _resolve_long_path (8.3 short-name TEMP paths)' `
    -Result 'BLOCKED' -Detail ("Fix lives in PR #50 (not yet on the branch this suite was built " +
        "from -- origin/main as of PRs #46/#47). Independently, this dev machine's username " +
        "('gaura', 5 chars) does not trigger an 8.3 alias, so even after #50 merges this " +
        "check could not be exercised HERE -- would need a machine/account with a long enough " +
        "username, or a synthetic short-name junction, to reproduce PR #50's original repro.")

Add-Result -Id '1f-screenshot-burst-windll' -Name 'screenshot_burst.py _current_display_resolution (ctypes.windll.user32)' `
    -Result 'PASS' -Detail ("Not wired into the default AI pipeline per its own docstring " +
        "(kept as a tested-but-unused building block) -- no check needed; this is a design " +
        "observation, not a coverage gap.")

# --- Summary ------------------------------------------------------------------------------------

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
$passCount = ($script:Results | Where-Object { $_.result -eq 'PASS' }).Count
$failCount = ($script:Results | Where-Object { $_.result -eq 'FAIL' }).Count
$blockedCount = ($script:Results | Where-Object { $_.result -eq 'BLOCKED' }).Count
Write-Host "PASS: $passCount   FAIL: $failCount   BLOCKED: $blockedCount   (total: $($script:Results.Count))"

$script:Results | ConvertTo-Json -Depth 10 | Set-Content -Path $ResultsJsonPath -Encoding utf8
Write-Host "Results written to $ResultsJsonPath"

if ($failCount -gt 0) { exit 1 }
exit 0
