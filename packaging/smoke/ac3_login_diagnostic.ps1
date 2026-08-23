<#
Reclaim AC3 interactive-login diagnostic script.

AJ4: ONE consolidated login trip covering everything AC2 could not verify without
ReclaimSmokeTest, folded in alongside the original AC3 scope: install, genuine first-run
observation, check 5 (Task Scheduler / InteractiveToken), the CWD-independence fix (#51) via
three distinct invocation shapes, the Snooze protocol-handler path, pre-flight confounder
capture (Focus Assist / per-app notification permission), AE1's teeth-proof against the real
persisted index, and S2/U4's app-reported-vs-measured free-space delta.

Rewritten from an earlier .bat draft after that draft failed its own end-to-end verification
run under gaura's own account -- real parser corruption from batch's rem/parenthesized-block/
delayed-expansion interactions, a worse bug than the one being fixed (AI1's %REGCMD% issue).
PowerShell avoids that entire landmine class and is already this project's own packaging-script
language (packaging/smoke/run_frozen_smoke_suite.ps1).

Every lookup that can be missing (install dir, protocol handler, scheduled task) is checked
explicitly and reported LOUDLY if absent -- no step is ever silently skipped.

PARAMETERS: -InstallerPath defaults to a copy of the rebuild #4 artifact this session produced
(SHA-256 3452ca017e339e92456955dd0db4501f630649bb3c41640e575ef980e34a378f, built from main
@ 4c3521974865c444a4cdf23a01f0703b56f1f027) staged at C:\Users\Public\reclaim_ac3\ -- NOT under
gaura's own profile, which a different Windows account cannot read (confirmed earlier this
session: direct filesystem access to another account's repo/venv is denied). -SkipInstall for
a second run against an already-installed profile (skips Step -2 only; first-run in Step -1
will then correctly show "already acknowledged" instead of the genuine first-run screen, which
is expected and fine on a re-run).

LOG OUTPUT: always C:\Users\Public\reclaim_ac3\ac3_run_<timestamp>.txt -- world-writable so any
account can write it, and readable from any other session (including a plain non-elevated one)
without needing access to whatever profile actually ran this script. Directory is created if
missing.
#>

param(
    [string]$InstallerPath = "C:\Users\Public\reclaim_ac3\reclaim-setup.exe",
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Continue'

$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$logDir = 'C:\Users\Public\reclaim_ac3'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir "ac3_run_$ts.txt"
New-Item -ItemType File -Path $logPath -Force | Out-Null

function Write-Log {
    param([string]$Message)
    Write-Host $Message
    Add-Content -Path $logPath -Value $Message
}

Write-Log "==================================================================="
Write-Log "Reclaim AC3 diagnostic run -- $ts"
Write-Log "==================================================================="

# ===========================================================================
Write-Log ""
Write-Log "--- STEP -2: Install rebuild #4 (AJ4) ---"
# ===========================================================================

if ($SkipInstall) {
    Write-Log "[SKIPPED] -SkipInstall passed -- using whatever is already installed."
} elseif (-not (Test-Path $InstallerPath)) {
    Write-Log "[ABORT] Installer not found at $InstallerPath -- pass -InstallerPath explicitly. Every step below that needs a real install will be SKIPPED, loudly."
} else {
    $hash = (Get-FileHash -Path $InstallerPath -Algorithm SHA256).Hash
    Write-Log "[OK] Installer found: $InstallerPath"
    Write-Log "     SHA-256: $hash"
    if ($hash -ne "3452CA017E339E92456955DD0DB4501F630649BB3C41640E575EF980E34A378F") {
        Write-Log "[WARNING] Hash does not match the expected rebuild #4 artifact -- this is a DIFFERENT build than the one this session verified. Continuing, but note it in your report."
    }
    Write-Log "[RUN] Installing silently (/VERYSILENT /SUPPRESSMSGBOXES /NORESTART)..."
    $installProc = Start-Process -FilePath $InstallerPath -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
    Write-Log "  installer exit code: $($installProc.ExitCode)"
    if ($installProc.ExitCode -ne 0) {
        Write-Log "[ABORT] Installer did not exit 0 -- treat everything below as suspect until this is understood."
    }
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP -1: Genuine first-run observation (AJ4 -- this is the ONLY chance) ---"
Write-Log "    THIS MUST HAPPEN BEFORE ANY OTHER STEP TOUCHES THIS PROFILE."
# ===========================================================================

if ($SkipInstall) {
    Write-Log "[SKIPPED] -SkipInstall passed -- first-run state was already consumed on a prior run, if any."
} else {
    Write-Log "[WHAT TO DO] A browser window is about to open to the dashboard's first-run screen."
    Write-Log "  Look at it BEFORE touching anything else and note/capture, in order:"
    Write-Log "  1. The exact overlay/panel content -- headline text, body copy, any mode toggle"
    Write-Log "     (Simple vs Advanced), any Safe-mode-vs-Power-mode language, any Recycle-Bin-"
    Write-Log "     vs-vault language, the license link."
    Write-Log "  2. Whether Simple mode or Advanced mode is the landing view underneath/after the"
    Write-Log "     overlay (AE2's finding was retracted as browser-state contamination -- this is"
    Write-Log "     the first genuinely clean observation of it this whole engagement)."
    Write-Log "  3. A screenshot if convenient (Win+Shift+S), saved anywhere -- attach separately,"
    Write-Log "     this script cannot capture your screen."
    Write-Log "  4. Whatever happens when you dismiss/acknowledge it (click through) -- does it"
    Write-Log "     return to this screen on a manual refresh, or stay dismissed?"

    Write-Log "[Raw state BEFORE launch] GET /api/first-run (server not started yet, expect a failure -- that's fine, this just confirms nothing has answered yet):"
    try {
        $before = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/first-run" -TimeoutSec 2 -ErrorAction Stop
        Write-Log "  $($before | ConvertTo-Json -Compress)"
    } catch {
        Write-Log "  (no response -- server not running yet, expected)"
    }

    Write-Log "[RUN] Launching 'reclaim dashboard' (opens your default browser automatically)..."
    $exePath = $null
    try {
        $exePath = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B6C1B6C7-6B6A-4E3B-9B7B-2B7E1E7C6A21}_is1' -Name InstallLocation -ErrorAction Stop).InstallLocation
    } catch { $exePath = $null }
    if ($exePath) {
        Start-Process -FilePath (Join-Path $exePath 'reclaim.exe') -ArgumentList 'dashboard'
        Start-Sleep -Seconds 3
        Write-Log "[Raw state AFTER launch, BEFORE you acknowledge] GET /api/first-run:"
        try {
            $afterLaunch = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/first-run" -TimeoutSec 5 -ErrorAction Stop
            Write-Log "  $($afterLaunch | ConvertTo-Json -Compress)"
        } catch {
            Write-Log "  (no response yet -- server may still be starting; wait a moment and check the browser)"
        }
    } else {
        Write-Log "[ABORT] Could not resolve install location to launch reclaim.exe -- launch it yourself: Start Menu > Reclaim, or the Reclaim desktop shortcut."
    }

    Read-Host "Press Enter once you have observed and captured the first-run screen (per points 1-4 above) and are ready to continue with the rest of this script"

    Write-Log "[Raw state AFTER you acknowledged] GET /api/first-run:"
    try {
        $afterAck = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/first-run" -TimeoutSec 5 -ErrorAction Stop
        Write-Log "  $($afterAck | ConvertTo-Json -Compress)"
    } catch {
        Write-Log "  (no response -- note this, it's unexpected if the dashboard is still open)"
    }
}

# ---------------------------------------------------------------------------
# Resolve install directory from the Inno Setup uninstall registry key
# (AppId B6C1B6C7-6B6A-4E3B-9B7B-2B7E1E7C6A21, from packaging/reclaim.iss).
# ---------------------------------------------------------------------------
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B6C1B6C7-6B6A-4E3B-9B7B-2B7E1E7C6A21}_is1'
$appDir = $null
try {
    $appDir = (Get-ItemProperty -Path $uninstallKey -Name InstallLocation -ErrorAction Stop).InstallLocation
} catch {
    $appDir = $null
}
if ([string]::IsNullOrWhiteSpace($appDir)) {
    Write-Log "[ABORT: install-dir lookup] $uninstallKey has no InstallLocation -- Reclaim does not appear installed for this user. APPDIR-dependent steps below will be SKIPPED, loudly, not silently."
    $appDir = $null
} else {
    Write-Log "[OK] Resolved install directory: $appDir"
}

# ---------------------------------------------------------------------------
# Resolve the REAL registered protocol-handler command line (the exact command
# Windows runs for the Snooze toast button, or any reclaim-notify: URI).
# ---------------------------------------------------------------------------
$handlerKey = 'HKCU:\Software\Classes\reclaim-notify\shell\open\command'
$regCmd = $null
try {
    $regCmd = (Get-ItemProperty -Path $handlerKey -Name '(default)' -ErrorAction Stop).'(default)'
} catch {
    $regCmd = $null
}
if ([string]::IsNullOrWhiteSpace($regCmd)) {
    Write-Log "[ABORT: protocol-handler lookup] $handlerKey has no default value -- reclaim-notify is not registered. Triggers 2, 3, and the direct URI test will be SKIPPED, loudly, not silently."
    $regCmd = $null
} else {
    Write-Log "[OK] Resolved registered protocol-handler command line:"
    Write-Log "     $regCmd"
}

function Invoke-RegisteredCommand {
    param([string]$CommandLine, [string]$WorkingDirectory)
    # $CommandLine is a full quoted "<exe>" arg1 arg2 ... string, exactly as stored in the
    # registry -- split it the same way ShellExecute/cmd would: first quoted token is the exe,
    # the remainder is a single argument string.
    if ($CommandLine -match '^"([^"]+)"\s*(.*)$') {
        $exe = $Matches[1]
        $rest = $Matches[2]
    } else {
        $parts = $CommandLine.Split(' ', 2)
        $exe = $parts[0]
        $rest = if ($parts.Length -gt 1) { $parts[1] } else { '' }
    }
    $psi = @{
        FilePath     = $exe
        ArgumentList = $rest
        NoNewWindow  = $true
        Wait         = $true
        PassThru     = $true
    }
    if ($WorkingDirectory) { $psi['WorkingDirectory'] = $WorkingDirectory }
    $proc = Start-Process @psi
    return $proc.ExitCode
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 0: Pre-flight confounder capture (BEFORE any trigger) ---"
# ===========================================================================

Write-Log "[TEMP] Resolved `$env:TEMP = $env:TEMP"

Write-Log "[Focus Assist] No single documented registry flag was independently confirmed for this,"
Write-Log "  so this is a MANUAL capture: open Settings > System > Focus assist right now and write"
Write-Log "  down the exact state (Off / Priority only / Alarms only) before running any trigger."

$toastEnabled = $null
try {
    $toastEnabled = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\PushNotifications' -Name ToastEnabled -ErrorAction Stop).ToastEnabled
} catch { $toastEnabled = $null }
if ($null -eq $toastEnabled) {
    Write-Log "[Notification Center master switch] ToastEnabled value not present (older build, or never toggled) -- treat as UNKNOWN, not as disabled."
} else {
    Write-Log "[Notification Center master switch] ToastEnabled = $toastEnabled"
}

Write-Log "[Per-app notification permission] AI2 confirmed windows_toasts falls back to cmd.exe's"
Write-Log "  own pre-registered AUMID ({1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\cmd.exe) since Reclaim"
Write-Log "  passes no explicit notifierAUMID. Searching for any Notifications\Settings subkey tied"
Write-Log "  to that AUMID (a fresh profile that never showed this toast may legitimately have NO"
Write-Log "  matching entry yet -- that absence is itself AH1's data point, not a probe error):"
$notifSettings = Get-ChildItem -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings' -ErrorAction SilentlyContinue
$matched = $notifSettings | Where-Object { $_.PSChildName -match '1AC14E77|cmd\.exe' }
if ($matched) {
    foreach ($m in $matched) { Write-Log "  MATCH: $($m.PSPath)" }
} else {
    Write-Log "  No matching subkey found -- see caveat above: unestablished permission state, not a probe error."
}

if ($appDir) {
    $configPath = Join-Path $appDir 'config.toml'
    $statePath = Join-Path $appDir 'data\notification_state.json'
    Write-Log "[config.toml BEFORE] $configPath"
    if (Test-Path $configPath) { Add-Content -Path $logPath -Value (Get-Content $configPath -Raw) } else { Write-Log "  (not found)" }
    Write-Log "[notification_state.json BEFORE] $statePath"
    if (Test-Path $statePath) { Add-Content -Path $logPath -Value (Get-Content $statePath -Raw) } else { Write-Log "  (file does not exist yet -- expected if no notify has ever fired)" }
} else {
    Write-Log "[SKIPPED] config.toml / notification_state.json capture -- APPDIR unresolved."
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 1: Trigger 1 -- the REAL registered Task Scheduler task ---"
Write-Log "    (this is check 5; the task's own XML sets WorkingDirectory={app}, so this"
Write-Log "    exercises 'does Task Scheduler fire it at all', NOT the CWD-independence fix)"
# ===========================================================================

$taskName = 'Reclaim Disk Space Check'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Log "[ABORT] Task '$taskName' not found -- SKIPPING /run + process sampling below, loudly."
} else {
    $infoBefore = Get-ScheduledTaskInfo -TaskName $taskName
    Write-Log "[BEFORE] LastRunTime=$($infoBefore.LastRunTime) LastTaskResult=$($infoBefore.LastTaskResult)"

    Write-Log "[RUN] Start-ScheduledTask -TaskName '$taskName'"
    Start-ScheduledTask -TaskName $taskName

    Write-Log "[Process evidence] Sampling for reclaim.exe every 3s for 15s (AI3: distinguishes"
    Write-Log "  'task fired, process ran to completion' from 'task fired, process crashed before"
    Write-Log "  reaching toast code' -- both look identical from LastRunTime alone):"
    for ($i = 1; $i -le 5; $i++) {
        Start-Sleep -Seconds 3
        $procs = Get-Process -Name reclaim -ErrorAction SilentlyContinue
        if ($procs) {
            Write-Log "  [t+$($i*3)s] reclaim.exe RUNNING: pid(s) $($procs.Id -join ',')"
        } else {
            Write-Log "  [t+$($i*3)s] reclaim.exe not present in process list"
        }
    }

    $infoAfter = Get-ScheduledTaskInfo -TaskName $taskName
    Write-Log "[AFTER] LastRunTime=$($infoAfter.LastRunTime) LastTaskResult=$($infoAfter.LastTaskResult)"
    Write-Log "  Compare against BEFORE -- see the value-meaning legend at the end of this log."

    if ($appDir) {
        $logFile = Join-Path $appDir 'data\reclaim.log'
        if (Test-Path $logFile) {
            Write-Log "[Log file AFTER Trigger 1] last 20 lines of ${logFile}:"
            Add-Content -Path $logPath -Value (Get-Content $logFile -Tail 20)
        } else {
            Write-Log "[Log file AFTER Trigger 1] $logFile not found at this path -- if Reclaim logs elsewhere, note the real path here manually."
        }
    }
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 2: Trigger 2 -- REGCMD run with a normal working directory (baseline) ---"
# ===========================================================================

if (-not $regCmd) {
    Write-Log "[SKIPPED] Trigger 2 -- REGCMD was never resolved."
} elseif (-not $appDir) {
    Write-Log "[SKIPPED] Trigger 2 -- APPDIR was never resolved, cannot set a working directory."
} else {
    Write-Log "[RUN] cwd=$appDir : $regCmd"
    $code = Invoke-RegisteredCommand -CommandLine $regCmd -WorkingDirectory $appDir
    Write-Log "  exit code: $code"
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 3: Trigger 3 -- REGCMD run with NO working directory set (AH2) ---"
Write-Log "    (matches the real shell\open\command invocation shape exactly -- this is the"
Write-Log "    one that actually exercises the no-CWD path #51 fixed; Trigger 2 above never did)"
# ===========================================================================

if (-not $regCmd) {
    Write-Log "[SKIPPED] Trigger 3 -- REGCMD was never resolved."
} else {
    Write-Log "[RUN] cwd=C:\Windows\System32 (no app-dir cd at all) : $regCmd"
    $code = Invoke-RegisteredCommand -CommandLine $regCmd -WorkingDirectory 'C:\Windows\System32'
    Write-Log "  exit code: $code"
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 4: Direct protocol-handler URI invocation (AH3) ---"
Write-Log "    (lets Windows itself resolve+launch reclaim-notify:, exactly as a real toast's"
Write-Log "    Snooze button would -- distinct from Steps 2/3's direct command-line invocation)"
# ===========================================================================

Write-Log "[RUN] Start-Process 'reclaim-notify:snooze-disk-alert'"
try {
    Start-Process 'reclaim-notify:snooze-disk-alert' -ErrorAction Stop
    Write-Log "  ShellExecute accepted the URI (fire-and-forget -- no exit code available)."
} catch {
    Write-Log "  [ERROR] Start-Process threw: $($_.Exception.Message) -- the URI scheme is likely not registered."
}
Start-Sleep -Seconds 3

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 5: Post-flight state capture (AFTER all triggers) ---"
# ===========================================================================

if ($appDir) {
    $configPath = Join-Path $appDir 'config.toml'
    $statePath = Join-Path $appDir 'data\notification_state.json'
    Write-Log "[config.toml AFTER] $configPath"
    if (Test-Path $configPath) { Add-Content -Path $logPath -Value (Get-Content $configPath -Raw) } else { Write-Log "  (not found)" }
    Write-Log "[notification_state.json AFTER] $statePath"
    if (Test-Path $statePath) { Add-Content -Path $logPath -Value (Get-Content $statePath -Raw) } else { Write-Log "  (still not found)" }
    Write-Log "  Compare against the BEFORE capture in Step 0 -- a changed snoozed_until or"
    Write-Log "  last_notified_at is the ground-truth signal a trigger actually took effect,"
    Write-Log "  independent of whether a toast was visually seen."
} else {
    Write-Log "[SKIPPED] post-flight config.toml / notification_state.json capture -- APPDIR unresolved."
}

# ===========================================================================
Write-Log ""
Write-Log "--- REFERENCE: scheduled-task LastTaskResult value meanings (AG2) ---"
# ===========================================================================
Write-Log "  0            = SUCCESS -- the task ran and its process exited cleanly."
Write-Log "  267011 (0x41303) = SCHED_S_TASK_HAS_NOT_RUN -- task registered but never fired yet."
Write-Log "  267009 (0x41301) = SCHED_S_TASK_RUNNING -- task is currently running (sample again)."
Write-Log "  1            = generic failure -- check Event Viewer > Applications and Services Logs >"
Write-Log "                 Microsoft > Windows > TaskScheduler > Operational for the real error."
Write-Log "  Also check LastRunTime itself: if it advanced from BEFORE to AFTER, Task Scheduler"
Write-Log "  genuinely launched the process regardless of LastTaskResult -- combine with the Step 1"
Write-Log "  process samples to know whether that process then ran to completion or died immediately."

Write-Log ""
Write-Log "--- WHAT TO OBSERVE LIVE (this script cannot capture it) ---"
Write-Log "  1. Whether a toast visibly appeared on screen for Steps 1/2/3/4, and which one(s)."
Write-Log "  2. If a toast appeared: click its Snooze button and confirm THAT also updates"
Write-Log "     notification_state.json (a fifth, real-world trigger distinct from Step 4's"
Write-Log "     direct URI invocation)."
Write-Log "  3. Whether Windows showed any permission/first-time notification prompt at any point."

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 6: AE1 teeth-proof against the real running frozen server (AJ4) ---"
Write-Log "    (the prior teeth-proof this session used source, not this frozen binary --"
Write-Log "    this is that same proof, against the real installed .exe's live server)"
# ===========================================================================

$serverReachable = $true
try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/scan/status" -TimeoutSec 5 -ErrorAction Stop
} catch {
    Write-Log "[ABORT] Could not reach the dashboard server at :8420 -- is 'reclaim dashboard' still running? SKIPPING Steps 6/7."
    $serverReachable = $false
}

if ($serverReachable) {
    # Uses an EXPLICIT paths list, not a blanket apply -- resolve_apply_selection's explicit-paths
    # branch calls generate_candidates directly and never touches the candidates cache/warm-up
    # lock, so this genuinely does not need any prior scan to have completed: an explicit path
    # not already a known candidate still gets a real, independent safety pass and default-B-tier
    # candidacy via _build_user_selected_candidate (see resolve_apply_selection's own comments).
    # No Read-Host, no scan dependency -- this step is always runnable and never silently no-ops.
    $outsideScopeDir = 'C:\Users\Public\ac3_ae1_teeth_proof'
    $outsideScopeFile = Join-Path $outsideScopeDir 'proof.tmp'
    New-Item -ItemType Directory -Path $outsideScopeDir -Force | Out-Null
    Set-Content -Path $outsideScopeFile -Value 'AE1 teeth-proof fixture -- safe to delete'

    Write-Log "[RUN] POST /api/apply for a real path outside this user's home ($outsideScopeFile):"
    $body = @{ tier = 'both'; paths = @($outsideScopeFile); method = 'vault'; dry_run = $false } | ConvertTo-Json
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/apply" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 10
        Start-Sleep -Seconds 2
        $applyStatus = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/apply/status" -TimeoutSec 10
        Write-Log "  apply status: $($applyStatus | ConvertTo-Json -Compress -Depth 5)"
        Write-Log "  file still exists after apply attempt: $(Test-Path $outsideScopeFile)"
        if (Test-Path $outsideScopeFile) {
            Write-Log "[PASS] File outside user scope was NOT touched -- AE1's scope check held against the real frozen server."
        } else {
            Write-Log "[FAIL -- REAL FINDING, NOT A SCRIPT BUG] File outside user scope was deleted/moved. Report this immediately, do not soften it."
        }
    } catch {
        Write-Log "  [ERROR] apply request failed: $($_.Exception.Message)"
    }
    Remove-Item -Path $outsideScopeDir -Recurse -Force -ErrorAction SilentlyContinue

    Write-Log "[OPTIONAL, richer proof] If this account has a real persisted index with genuine"
    Write-Log "  cross-tenant-shaped rows (another user profile's files, scanned in a prior"
    Write-Log "  session), you can additionally point the dashboard's Review Queue at one of those"
    Write-Log "  and confirm it never appears as an applicable candidate at all -- not just that a"
    Write-Log "  synthetic outside-scope path gets skipped at apply time. Not required for Step 6"
    Write-Log "  to count as complete; the proof above already exercises the real choke point."
} else {
    Write-Log "[SKIPPED] Step 6 -- dashboard server unreachable."
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 7: S2/U4 -- app-reported vs. measured free-space delta (AJ4) ---"
Write-Log "    (blocked 3x earlier this session by the apply-warm-check hang, PR #62 -- now fixed)"
# ===========================================================================

if (-not $serverReachable) {
    Write-Log "[SKIPPED] Step 7 -- dashboard server unreachable (see Step 6)."
} else {
    $driveBefore = Get-PSDrive -Name C
    Write-Log "[Measured BEFORE] C: free = $($driveBefore.Free) bytes"

    Write-Log "[ACTION NEEDED] In the dashboard, scan a real path if you haven't already, then in"
    Write-Log "  the Review Queue (or Simple mode Quick Clean) select a small, real, safe-to-delete"
    Write-Log "  set of items and apply them for real (not dry-run, method=direct_delete for a"
    Write-Log "  meaningful comparison -- recycle_bin/vault don't free space immediately, so a"
    Write-Log "  same-instant OS measurement would legitimately show ~0 delta for those methods,"
    Write-Log "  not a discrepancy). Keep it small and genuinely disposable."
    Read-Host "Press Enter once a real apply has completed in the dashboard"

    $driveAfter = Get-PSDrive -Name C
    Write-Log "[Measured AFTER] C: free = $($driveAfter.Free) bytes"
    $measuredDelta = $driveAfter.Free - $driveBefore.Free
    Write-Log "[Measured delta] $measuredDelta bytes freed (OS-reported)"

    $lastApply = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/apply/status" -TimeoutSec 10
    if ($lastApply.status -ne 'completed') {
        Write-Log "[ABORT] /api/apply/status reports '$($lastApply.status)', not 'completed' -- no real"
        Write-Log "  apply was observed after the prompt above (pressed Enter too early, or the"
        Write-Log "  dashboard apply is still running). The comparison below would be meaningless --"
        Write-Log "  SKIPPING it rather than reporting a number against no real apply."
    } else {
        $appReported = $lastApply.result.bytes_freed
        Write-Log "[App-reported] bytes_freed = $appReported (method: $($lastApply.result.method))"
        if ($appReported -and $appReported -gt 0) {
            $pctDiff = [math]::Abs(($measuredDelta - $appReported) / $appReported) * 100
            Write-Log ("[Comparison] {0:N2}% difference between app-reported and OS-measured -- must be within 2% for a direct_delete apply" -f $pctDiff)
        } else {
            Write-Log "[Comparison] app reported no bytes_freed (0 or null) -- check the method used; recycle_bin/vault legitimately report 0 freed until emptied/purged."
        }
    }
}

# ===========================================================================
Write-Log ""
Write-Log "--- SUMMARY (AK4: an explicit signal, not something to infer from scrolling) ---"
# ===========================================================================
$logLines = Get-Content -Path $logPath
$aborts = $logLines | Select-String -Pattern '^\[ABORT'
$fails = $logLines | Select-String -Pattern '^\[FAIL'
$skips = $logLines | Select-String -Pattern '^\[SKIPPED\]'
$warnings = $logLines | Select-String -Pattern '^\[WARNING\]'
Write-Log "  ABORT lines:   $($aborts.Count)"
Write-Log "  FAIL lines:    $($fails.Count)"
Write-Log "  WARNING lines: $($warnings.Count)"
Write-Log "  SKIPPED lines: $($skips.Count)"
if ($aborts.Count -eq 0 -and $fails.Count -eq 0 -and $skips.Count -eq 0) {
    Write-Log "  Every step ran and reported a real result -- nothing was aborted, failed, or skipped."
} else {
    Write-Log "  This run did NOT complete cleanly -- do not treat it as a full pass. Review each"
    Write-Log "  ABORT/FAIL/SKIPPED line above before concluding anything about the trip's outcome."
}

Write-Log "==================================================================="
Write-Log "Run complete. Full log: $logPath"
Write-Log "==================================================================="
