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

PARAMETERS: -InstallerPath defaults to whatever is staged at C:\Users\Public\reclaim_ac3\ -- NOT
under gaura's own profile, which a different Windows account cannot read (confirmed earlier this
session: direct filesystem access to another account's repo/venv is denied). -SkipInstall for
a second run against an already-installed profile (skips Step -2 only; first-run in Step -1
will then correctly show "already acknowledged" instead of the genuine first-run screen, which
is expected and fine on a re-run).

AR3 (2026-08-23 audit): Step -2 now refuses (ABORT, no install attempted) unless the installer's
`.buildsha` sidecar (written by packaging/build_installer.ps1) records a source commit that
matches -RepoPath's current `origin/main` tip exactly -- four real trip runs this same day
proceeded against a known-stale rebuild #4 artifact with nothing stopping them; this is the fix.
-RepoPath defaults to this script's own checkout (packaging/smoke/../..). -AllowStaleBuild
overrides deliberately (warns instead of aborting) for a genuine stale-artifact run -- e.g.
re-verifying an older release, or a git-less environment.

LOG OUTPUT: always C:\Users\Public\reclaim_ac3\ac3_run_<timestamp>.txt -- world-writable so any
account can write it, and readable from any other session (including a plain non-elevated one)
without needing access to whatever profile actually ran this script. Directory is created if
missing.
#>

param(
    [string]$InstallerPath = "C:\Users\Public\reclaim_ac3\reclaim-setup.exe",
    [switch]$SkipInstall,
    # AR3/AR5 (2026-08-23/24 audit): the repo checkout used to resolve "what does current
    # origin/main actually build from" for the freshness check below. NOT derived from
    # $PSScriptRoot -- this script's own docstring says it's meant to run from a STAGED COPY
    # (C:\Users\Public\reclaim_ac3\, readable by any account) separately from the actual repo,
    # so $PSScriptRoot\..\.. resolves to something else entirely once copied there (found live,
    # AR5: it silently resolved to C:\Users and produced a useless "git invocation... threw" error
    # instead of a clear diagnostic). Hardcoded to this machine's one known checkout location --
    # override only if running from a different one.
    [string]$RepoPath = "C:\Users\gaura\ml-projects\reclaim",
    # AR3: explicit, loud override for a deliberate stale-artifact run (e.g. re-verifying an
    # older release, or a git-less environment) -- the default is to refuse, matching rule 98a's
    # fail-closed posture for a guard whose entire purpose is catching exactly this.
    [switch]$AllowStaleBuild
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

function Get-IndexSizeDetail {
    # AT1 (2026-08-24 audit): a timeout ABORT with no size context is undiagnosable without
    # asking -- this account's real, growing index has already outgrown one raised timeout
    # (180s, PR #74) within hours of landing (docs/AUDIT-2026-08.md's AS3). Reporting the actual
    # index size at the moment of the abort at least tells the next reader whether this is the
    # same known cost class or something new, without a follow-up question.
    param([string]$AppDir)
    if (-not $AppDir) { return "index size: unknown (install directory not resolved)" }
    $indexPath = Join-Path $AppDir 'data\reclaim_index.sqlite3'
    if (-not (Test-Path $indexPath)) { return "index size: unknown (no index file at $indexPath)" }
    $bytes = (Get-Item $indexPath).Length
    $gb = [math]::Round($bytes / 1GB, 2)
    return "index size: $bytes bytes (${gb}GB) at $indexPath"
}

Write-Log "==================================================================="
Write-Log "Reclaim AC3 diagnostic run -- $ts"
Write-Log "==================================================================="

# ===========================================================================
Write-Log ""
Write-Log "--- STEP -2: Install rebuild #4 (AJ4) ---"
# ===========================================================================

$installSkippedByFreshnessCheck = $false
if ($SkipInstall) {
    Write-Log "[SKIPPED] -SkipInstall passed -- using whatever is already installed."
} elseif (-not (Test-Path $InstallerPath)) {
    Write-Log "[ABORT] Installer not found at $InstallerPath -- pass -InstallerPath explicitly. Every step below that needs a real install will be SKIPPED, loudly."
} else {
    $hash = (Get-FileHash -Path $InstallerPath -Algorithm SHA256).Hash
    Write-Log "[OK] Installer found: $InstallerPath"
    Write-Log "     SHA-256: $hash"

    # AR3 (2026-08-23 audit): four real trip runs this same day proceeded against a known-stale
    # rebuild #4 artifact -- the old check here only WARNED on a hash mismatch, never stopped
    # anything, and compared against a hardcoded rebuild-specific hash rather than "is this the
    # artifact current main actually builds today." Replaced with a real freshness check against
    # build_installer.ps1's new .buildsha sidecar (rule 98a: an unverifiable state is a refusal,
    # not a silent pass -- so a missing sidecar or unreachable git is treated the same as a
    # confirmed mismatch, all three requiring -AllowStaleBuild to proceed past).
    $buildShaPath = "$InstallerPath.buildsha"
    $freshnessOk = $false
    $freshnessReason = ""
    if (-not (Test-Path $buildShaPath)) {
        $freshnessReason = "no .buildsha sidecar next to the installer (pre-AR3 build, or a copy that lost it)"
    } else {
        $recordedSha = (Get-Content -Path $buildShaPath -Raw).Trim()
        if ($recordedSha -eq "unknown") {
            $freshnessReason = "sidecar records 'unknown' -- built outside a git checkout"
        } elseif (-not (Test-Path (Join-Path $RepoPath '.git'))) {
            # AR5: check this explicitly, with a clear reason, BEFORE ever invoking git -- a git
            # command against a non-repo path fails with a cryptic, unhelpful message (found
            # live: "You cannot call a method on a null-valued expression" when $RepoPath had
            # silently resolved to the wrong directory). This is also the expected, non-error
            # case for ReclaimSmokeTest specifically: this script's own docstring already
            # documents that account cannot read gaura's repo checkout at all -- freshness
            # genuinely cannot be verified from there, and that's a real limitation to surface
            # loudly (-AllowStaleBuild), not a bug to chase.
            $freshnessReason = "no git checkout found at -RepoPath '$RepoPath' -- if running as a " +
                "different account than the one that built this artifact, this check cannot " +
                "verify freshness here by design; the operator should confirm freshness " +
                "separately before staging, or pass -RepoPath explicitly"
        } else {
            try {
                git -C $RepoPath fetch origin main --quiet 2>$null
                $mainSha = (git -C $RepoPath rev-parse origin/main 2>$null).Trim()
                if ($LASTEXITCODE -ne 0 -or -not $mainSha) {
                    $freshnessReason = "could not resolve origin/main from $RepoPath (git fetch/rev-parse failed)"
                } elseif ($recordedSha -eq $mainSha) {
                    $freshnessOk = $true
                } else {
                    $freshnessReason = "installer built from $recordedSha, but origin/main is currently $mainSha -- this is NOT today's main, it's a prior build"
                }
            } catch {
                $freshnessReason = "git invocation against $RepoPath threw: $($_.Exception.Message)"
            }
        }
    }

    if ($freshnessOk) {
        Write-Log "[OK] Freshness check: installer's recorded source commit matches current origin/main."
    } elseif ($AllowStaleBuild) {
        Write-Log "[WARNING] Freshness check failed ($freshnessReason) -- proceeding anyway because -AllowStaleBuild was passed. Every result below is against a build that may not reflect current main."
    } else {
        Write-Log "[ABORT] Freshness check failed ($freshnessReason). Refusing to install and run the trip against a build that cannot be confirmed current -- pass -AllowStaleBuild to override deliberately (e.g. re-verifying an older release)."
        Write-Log "[SKIPPED] Every step below that needs a real install will be SKIPPED, loudly."
        $SkipInstall = $true  # not a real -SkipInstall request -- reuses every downstream skip
        $installSkippedByFreshnessCheck = $true  # gate below, but Step -1's message differs (see there)
    }

    if (-not $SkipInstall) {
        Write-Log "[RUN] Installing silently (/VERYSILENT /SUPPRESSMSGBOXES /NORESTART)..."
        $installProc = Start-Process -FilePath $InstallerPath -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
        Write-Log "  installer exit code: $($installProc.ExitCode)"
        if ($installProc.ExitCode -ne 0) {
            Write-Log "[ABORT] Installer did not exit 0 -- treat everything below as suspect until this is understood."
        }
    }
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP -1: Genuine first-run observation (AJ4 -- this is the ONLY chance) ---"
Write-Log "    THIS MUST HAPPEN BEFORE ANY OTHER STEP TOUCHES THIS PROFILE."
# ===========================================================================

if ($installSkippedByFreshnessCheck) {
    Write-Log "[SKIPPED] Freshness check aborted Step -2 -- no install attempted, so first-run state is whatever this profile already had, if anything."
} elseif ($SkipInstall) {
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
        $logFile = Join-Path $appDir 'data\logs\reclaim.log'
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
$csrfToken = $null
try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/scan/status" -TimeoutSec 5 -ErrorAction Stop
} catch {
    Write-Log "[ABORT] Could not reach the dashboard server at :8420 -- is 'reclaim dashboard' still running? SKIPPING Steps 6/7."
    $serverReachable = $false
}

if ($serverReachable) {
    # POST /api/apply is a mutating request -- reclaim.api.security's local-origin guard requires
    # a valid X-Reclaim-CSRF-Token header on every one, matching the per-process token minted in
    # AppState.csrf_token and embedded in index.html's <meta name="reclaim-csrf-token"> tag (see
    # app.js's own CSRF_TOKEN constant, which reads it the same way). A prior version of this
    # step never fetched or sent this header at all and got a 403 for it -- indistinguishable at
    # a glance from a real security-policy rejection, but it never even reached AE1's scope check;
    # the request was refused by this unrelated CSRF guard first. Fetch the real page and pull the
    # token out the same way a real browser tab would, rather than reasoning about the header from
    # source alone.
    try {
        $indexHtml = Invoke-RestMethod -Uri "http://127.0.0.1:8420/" -TimeoutSec 5 -ErrorAction Stop
        if ($indexHtml -match 'name="reclaim-csrf-token"\s+content="([^"]+)"') {
            $csrfToken = $Matches[1]
            Write-Log "[OK] Fetched CSRF token from the real page (first 8 chars: $($csrfToken.Substring(0, [Math]::Min(8, $csrfToken.Length)))...)"
        } else {
            Write-Log "[ABORT] Could not find the reclaim-csrf-token meta tag in the served page -- SKIPPING Step 6's apply attempt (it would 403)."
        }
    } catch {
        Write-Log "[ABORT] Could not fetch / to extract the CSRF token: $($_.Exception.Message) -- SKIPPING Step 6's apply attempt."
    }
}

if ($serverReachable -and $csrfToken) {
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
    $headers = @{ 'X-Reclaim-CSRF-Token' = $csrfToken }
    try {
        # AT1 (2026-08-24 audit): 600s, not 120s -- 120s (raised from an original 10s) was itself
        # already re-outgrown: GET /api/candidates measured 225.8s against this account's real,
        # ~1GB/1M+-row index (docs/AUDIT-2026-08.md's AS3), and resolve_apply_selection's explicit-
        # paths branch pays a related, index-size-dependent generate_candidates cost. 600s is real
        # headroom against the measured value, not another increment that gets outgrown again.
        $applyCallStart = Get-Date
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/apply" -Method Post -ContentType 'application/json' -Headers $headers -Body $body -TimeoutSec 600

        # AT1: this used to be a SINGLE status check after a fixed 2s sleep, with no poll loop and
        # no check that status had actually reached 'completed' before drawing a PASS/FAIL
        # conclusion -- a still-'running' apply would show the file still present (it hadn't been
        # processed yet) and get logged as a false PASS, on exactly the one check this whole trip
        # exists to prove. Real poll loop now, same pattern as Steps 7/8, with the same 600s window
        # and diagnosable ABORT message when it's genuinely never reached.
        $applyStatus = $null
        $pollDeadline = (Get-Date).AddSeconds(600)
        while ((Get-Date) -lt $pollDeadline) {
            Start-Sleep -Seconds 2
            $applyStatus = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/apply/status" -TimeoutSec 30
            if ($applyStatus.status -in @('completed', 'failed')) { break }
        }
        $elapsedSeconds = [math]::Round(((Get-Date) - $applyCallStart).TotalSeconds, 1)
        Write-Log "  apply status: $($applyStatus | ConvertTo-Json -Compress -Depth 5)"

        if ($applyStatus.status -ne 'completed') {
            $sizeDetail = Get-IndexSizeDetail -AppDir $appDir
            Write-Log "[ABORT] /api/apply/status never reached 'completed' after ${elapsedSeconds}s (status: '$($applyStatus.status)'; $sizeDetail) -- cannot draw a PASS/FAIL conclusion, SKIPPING. This is Step 6, the trip's own AE1 proof -- do not treat an ABORT here as a pass by default."
        } else {
            Write-Log "  file still exists after apply attempt: $(Test-Path $outsideScopeFile)"
            if (Test-Path $outsideScopeFile) {
                Write-Log "[PASS] File outside user scope was NOT touched -- AE1's scope check held against the real frozen server."
            } else {
                Write-Log "[FAIL -- REAL FINDING, NOT A SCRIPT BUG] File outside user scope was deleted/moved. Report this immediately, do not soften it."
            }
        }
    } catch {
        Write-Log "  [ERROR] apply request failed: $($_.Exception.Message)"
    }
    Remove-Item -Path $outsideScopeDir -Recurse -Force -ErrorAction SilentlyContinue
} elseif ($serverReachable) {
    Write-Log "[SKIPPED] Step 6 -- CSRF token unavailable, see ABORT above."
} else {
    Write-Log "[SKIPPED] Step 6 -- dashboard server unreachable."
}

if ($serverReachable) {
    Write-Log "[OPTIONAL, richer proof] If this account has a real persisted index with genuine"
    Write-Log "  cross-tenant-shaped rows (another user profile's files, scanned in a prior"
    Write-Log "  session), you can additionally point the dashboard's Review Queue at one of those"
    Write-Log "  and confirm it never appears as an applicable candidate at all -- not just that a"
    Write-Log "  synthetic outside-scope path gets skipped at apply time. Not required for Step 6"
    Write-Log "  to count as complete; the proof above already exercises the real choke point."
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 7: S2/U4 -- app-reported vs. measured free-space delta (AJ4) ---"
Write-Log "    (blocked 3x earlier this session by the apply-warm-check hang, PR #62 -- now fixed;"
Write-Log "    fully automated -- uses a synthetic disposable fixture, not real account data, so"
Write-Log "    this needs no human selection step)"
# ===========================================================================

if (-not $serverReachable -or -not $csrfToken) {
    Write-Log "[SKIPPED] Step 7 -- dashboard server unreachable or no CSRF token (see Step 6)."
} else {
    # Only direct_delete gives an immediately-measurable OS-level delta -- recycle_bin/vault
    # move the file but don't free space until emptied/purged, so a same-instant comparison
    # against either of those would legitimately show ~0 either way, proving nothing about
    # accuracy. Safe mode forces recycle_bin regardless of the requested method
    # (resolve_apply_selection's own method-resolution line), so this step needs power mode.
    $headers = @{ 'X-Reclaim-CSRF-Token' = $csrfToken }
    $modeStatus = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/mode" -TimeoutSec 5
    $originalMode = $modeStatus.mode
    Write-Log "[Mode] Currently: $originalMode"
    $switchedMode = $false
    if ($originalMode -ne 'power') {
        Write-Log "[RUN] Switching to power mode for this synthetic-fixture-only measurement (required for direct_delete)..."
        $confirmBody = @{ confirmation_text = $modeStatus.required_power_confirmation } | ConvertTo-Json
        try {
            $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/mode/power" -Method Post -ContentType 'application/json' -Headers $headers -Body $confirmBody -TimeoutSec 10
            $switchedMode = $true
            Write-Log "[OK] Switched to power mode."
        } catch {
            Write-Log "[ABORT] Could not switch to power mode: $($_.Exception.Message) -- SKIPPING Step 7."
        }
    }

    if ($originalMode -eq 'power' -or $switchedMode) {
        # AT2 (2026-08-24 audit): this fixture used to be an arbitrary standalone file requested
        # with method='direct_delete' directly -- apply_batch REJECTS that outright ("method
        # parameter must be 'vault' or 'recycle_bin' -- 'direct_delete' is only ever derived
        # per-candidate from Candidate.retention_days, never requested for a whole batch"),
        # confirmed live: every prior run of this step failed near-instantly on that validation
        # error, which the old code misreported as a 60s timeout (the poll loop had no 'failed'
        # break condition, so it just spun until the iteration count ran out). direct_delete can
        # ONLY be reached by a candidate whose CATEGORY defaults to retention_days=None
        # (dev_artifacts/temp_and_browser_caches/package_caches/crash_dumps -- see
        # executor._effective_method_and_retention_days) -- an arbitrary user-selected path can
        # never qualify, by design (ADR-0001: permanence is a property of the category, not a
        # per-run request). A `__pycache__` directory is dev_artifacts' one unconditional match
        # (no manifest-adjacency gate, no age guard, unlike temp_and_browser_caches' 7-day
        # minimum) -- the simplest real category shape that actually reaches direct_delete.
        $fixtureDir = 'C:\Users\Public\ac3_s2u4_fixture'
        $pycacheDir = Join-Path $fixtureDir '__pycache__'
        $fixtureFile = Join-Path $pycacheDir 'disposable_10mb.pyc'
        $fixtureSizeBytes = 10 * 1MB
        New-Item -ItemType Directory -Path $pycacheDir -Force | Out-Null
        $fixtureBytes = New-Object byte[] ($fixtureSizeBytes)
        [System.IO.File]::WriteAllBytes($fixtureFile, $fixtureBytes)
        Write-Log "[OK] Created a real $($fixtureSizeBytes / 1MB)MB disposable fixture (dev_artifacts/__pycache__ shape), exact known size: $fixtureFile"

        # A scan is required first -- generate_candidates' explicit-paths branch only matches
        # already-INDEXED directories (detect_dev_artifacts queries the persisted index, never a
        # live filesystem check), so the __pycache__ dir must be scanned before it can be
        # requested by path. Outside home (C:\Users\Public), so needs the same confirm-intent
        # token AN1/AO1's fix (PR #65/#66) requires of any outside-home scan.
        #
        # AZ4 (2026-08-25 audit): live-reproduced -- POST /api/scan for the fixture hit
        # routes.py's own scan_status.status == "running" single-flight guard (409), because a
        # REAL scan of C:\Users\<account> was already in flight, initiated via POST
        # /api/scan/my-files (SIMPLE mode's "Clean My Computer" button) -- confirmed in the real
        # app log (api.scan_initiated, origin "POST /api/scan/my-files"). app.js only fires that
        # endpoint from an explicit button click (startSimpleScan, wired to scanBtn's click
        # listener) -- it is never triggered on page load -- so this was a REAL click somewhere,
        # not a frontend auto-scan. Not run to ground further: this is the same unexplained-
        # trigger shape as AN1's own "stale browser tab replaying an already-confirmed click"
        # finding (docs/AUDIT-2026-08.md), and that investigation's own precedent is "fixed
        # regardless of whether the exact trigger was ever identified" -- applied the same way
        # here rather than reopening a full forensic pass. This step is a synthetic-fixture-only
        # measurement with no dependency on whatever that other scan was doing, so it cancels any
        # in-flight scan before starting its own rather than waiting out an unrelated, possibly
        # very long (a real home-directory scan on this machine) background job.
        Write-Log "[RUN] Scanning $fixtureDir so the __pycache__ fixture is indexed..."
        try {
            $preScanStatus = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/scan/status" -TimeoutSec 10
            if ($preScanStatus.status -eq 'running') {
                Write-Log "[WARNING] A scan was already running (root: '$($preScanStatus.root)') when Step 7 tried to start its own -- cancelling it. This step's fixture scan does not depend on it; see AZ4 for why the trigger is not further investigated here."
                $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/scan/cancel" -Method Post -ContentType 'application/json' -Headers $headers -Body '{}' -TimeoutSec 10
                # Cooperative cancel (routes.py's own docstring: "stops at the next safe point, a
                # batch boundary") -- not instant, so poll for the terminal state rather than a
                # flat sleep, which could race the fixture scan request below into the same 409.
                $cancelDeadline = (Get-Date).AddSeconds(30)
                while ((Get-Date) -lt $cancelDeadline) {
                    Start-Sleep -Milliseconds 500
                    $cancelPollStatus = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/scan/status" -TimeoutSec 10
                    if ($cancelPollStatus.status -ne 'running') { break }
                }
                if ($cancelPollStatus.status -eq 'running') {
                    Write-Log "[ABORT] The other scan did not stop within 30s of cancellation -- SKIPPING the rest of Step 7 rather than racing it."
                    $fixtureScanDone = $false
                    throw "other scan still running after cancel"
                }
            }
            $confirmIntentBody = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/scan/full-drive/confirm-intent" -Method Post -ContentType 'application/json' -Headers $headers -Body '{}' -TimeoutSec 10
            $scanToken = $confirmIntentBody.token
            $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/scan" -Method Post -ContentType 'application/json' -Headers $headers -Body (@{ path = $fixtureDir; token = $scanToken } | ConvertTo-Json) -TimeoutSec 30
            $fixtureScanDone = $false
            $fixturePollDeadline = (Get-Date).AddSeconds(60)
            while ((Get-Date) -lt $fixturePollDeadline) {
                Start-Sleep -Milliseconds 500
                $fixtureScanStatus = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/scan/status" -TimeoutSec 10
                if ($fixtureScanStatus.status -in @('completed', 'failed')) { $fixtureScanDone = ($fixtureScanStatus.status -eq 'completed'); break }
            }
            if (-not $fixtureScanDone) {
                Write-Log "[ABORT] Fixture scan of $fixtureDir did not complete (status: '$($fixtureScanStatus.status)') -- SKIPPING the rest of Step 7, the apply request would find no candidate."
            }
        } catch {
            $fixtureScanDone = $false
            Write-Log "[ABORT] Fixture scan request failed: $($_.Exception.Message) -- SKIPPING the rest of Step 7."
        }

        if ($fixtureScanDone) {
        # AZ2 (2026-08-25 audit): baseline free-space DRIFT sample, taken BEFORE the apply call
        # (two back-to-back reads spanning roughly the same window the real measurement below
        # will span). AV1's earlier 17.89%-difference result (recomputed correctly, post-AU2's
        # fix) was real but unexplained -- this machine is under genuine concurrent use (this
        # very session's own background activity among other things), and comparing app-reported
        # bytes against a single whole-drive-free-space snapshot pair over a multi-second window
        # cannot resolve to 2% if ordinary background churn alone moves free space by more than
        # that in either direction. Sampling drift explicitly, before touching the fixture, gives
        # a real noise-floor number to interpret the real measurement against, instead of
        # guessing whether a gap is signal or noise after the fact -- exactly AV2's lesson,
        # applied to the diagnostic's own design this time, not just its arithmetic.
        $driveDriftBaseline1 = (Get-PSDrive -Name C).Free
        Start-Sleep -Seconds 3
        $driveDriftBaseline2 = (Get-PSDrive -Name C).Free
        $baselineDriftBytes = $driveDriftBaseline2 - $driveDriftBaseline1
        Write-Log "[Baseline drift] C: free space moved by $baselineDriftBytes bytes over a 3s idle window BEFORE the fixture -- this machine's own background noise floor, not caused by this step."

        # AU2 (2026-08-24 audit): capture the scalar .Free VALUE here, not the PSDriveInfo
        # object itself. `Get-PSDrive -Name C` returns the same cached, live-backed object on
        # every call in one session (verified: [object]::ReferenceEquals returns True across
        # calls) -- its .Free property re-queries the live filesystem on every access, it is
        # NOT a snapshot taken at Get-PSDrive time. Holding the object in $driveBefore and
        # reading .Free from it again AFTER the apply (further down) silently returns the
        # POST-apply value, not the pre-apply one, making $driveAfter.Free - $driveBefore.Free
        # always evaluate to 0 regardless of what the apply actually freed -- this produced a
        # false FAIL on every real run despite the underlying apply/free-space accounting being
        # correct (recomputed by hand from this same run's raw BEFORE/AFTER numbers: exact 0.00%
        # difference, not 100%). Assigning the scalar immediately below is immune to this because
        # a [long] is a value type in PowerShell, not a reference to the live object.
        $driveBefore = (Get-PSDrive -Name C).Free
        Write-Log "[Measured BEFORE] C: free = $driveBefore bytes"

        # method='recycle_bin' here is nominal, not a request that takes effect: dev_artifacts'
        # retention_days=None means _effective_method_and_retention_days overrides it to
        # direct_delete unconditionally for this candidate, regardless of what's requested here --
        # see this block's own top comment. Requesting 'direct_delete' directly is what apply_batch
        # itself refuses; this is the correct way to reach the same real effect.
        Write-Log "[RUN] POST /api/apply for the __pycache__ fixture (retention_days=None -> auto-resolves to direct_delete), real (not dry-run):"
        $body = @{ tier = 'both'; paths = @($pycacheDir); method = 'recycle_bin'; dry_run = $false } | ConvertTo-Json
        try {
            # AT1 (2026-08-24 audit): 600s, not 120s -- same measured cost as Step 6's own apply
            # call (see that step's comment); 120s (itself already a raise from an original 10s)
            # was re-outgrown by this account's real index growth. 600s is real headroom against
            # the 225.8s GET /api/candidates measured this session (docs/AUDIT-2026-08.md's AS3).
            $applyCallStart = Get-Date
            $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/apply" -Method Post -ContentType 'application/json' -Headers $headers -Body $body -TimeoutSec 600
            $lastApply = $null
            $pollDeadline = (Get-Date).AddSeconds(600)
            while ((Get-Date) -lt $pollDeadline) {
                Start-Sleep -Seconds 2
                $lastApply = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/apply/status" -TimeoutSec 30
                if ($lastApply.status -in @('completed', 'failed')) { break }
            }
            $elapsedSeconds = [math]::Round(((Get-Date) - $applyCallStart).TotalSeconds, 1)

            $driveAfter = (Get-PSDrive -Name C).Free
            Write-Log "[Measured AFTER] C: free = $driveAfter bytes"
            $measuredDelta = $driveAfter - $driveBefore
            Write-Log "[Measured delta] $measuredDelta bytes freed (OS-reported -- secondary sanity check only, see AZ2 below)"

            if ($lastApply.status -ne 'completed') {
                $sizeDetail = Get-IndexSizeDetail -AppDir $appDir
                Write-Log "[ABORT] /api/apply/status never reached 'completed' after ${elapsedSeconds}s (status: '$($lastApply.status)'; $sizeDetail) -- the comparisons below would be meaningless, SKIPPING them."
            } else {
                $appReported = $lastApply.result.bytes_freed
                # AU2: `.result.method` is the BATCH-level nominal method echoed straight back
                # from the request body ('recycle_bin', sent as a placeholder -- see this step's
                # top comment) -- it is NOT what was actually used for this specific item.
                # `.result.items[0].method` is the per-item resolved method (BatchApplyReport's
                # top-level `method` field is set from apply_batch's own `method` PARAMETER,
                # confirmed by reading executor.py; each ItemApplyResult carries its own real
                # `item_method` from `_effective_method_and_retention_days`). Logging the batch
                # field here made every run claim "method: recycle_bin" even when the item itself
                # correctly direct-deleted.
                $itemMethod = if ($lastApply.result.items -and $lastApply.result.items.Count -gt 0) { $lastApply.result.items[0].method } else { '<no items>' }
                Write-Log "[App-reported] bytes_freed = $appReported (item method: $itemMethod; batch-nominal method: $($lastApply.result.method))"

                # AZ2 (2026-08-25 audit): the PRIMARY assertion -- comparing app-reported bytes
                # against a whole-drive free-space snapshot pair over a multi-second window on a
                # machine under real concurrent use cannot resolve to 2% (AV1's 17.89% result was
                # real, not a script bug, but the comparison it was testing was the wrong design).
                # This fixture's exact byte size is known in advance (this script created it,
                # $fixtureSizeBytes bytes exactly) -- the correct, authoritative test is a direct
                # equality against that known constant, not a comparison between two independently
                # noisy readings (AV2's lesson: assert a known value, don't compare two possibly-
                # wrong numbers to each other and hope they happen to agree).
                if ($appReported -eq $fixtureSizeBytes) {
                    Write-Log "[PASS] App-reported bytes_freed ($appReported) exactly matches the known fixture size ($fixtureSizeBytes bytes). This is the primary, authoritative check for this step."
                } else {
                    Write-Log "[FAIL -- REAL FINDING, NOT A SCRIPT BUG] App-reported bytes_freed ($appReported) does NOT match the known fixture size ($fixtureSizeBytes bytes) -- a real app-level accounting discrepancy, not a measurement artifact: the fixture size was fixed and known before this run ever started, so there is nothing for this specific comparison to be noisy about."
                }

                # SECONDARY sanity check: OS-measured delta vs app-reported, informational only,
                # interpreted against the baseline drift sampled before the fixture was even
                # created -- not a pass/fail gate on its own. Answers AZ2's actual question
                # ("is a gap here noise or signal") with a number instead of a guess.
                if ($appReported -and $appReported -gt 0) {
                    $measuredVsReportedGapBytes = $measuredDelta - $appReported
                    $pctDiff = [math]::Abs($measuredVsReportedGapBytes / $appReported) * 100
                    $baselineDriftPct = [math]::Abs($baselineDriftBytes / $appReported) * 100
                    Write-Log ("[Secondary check] OS-measured delta ($measuredDelta) vs app-reported ($appReported): {0:N2}% gap ($measuredVsReportedGapBytes bytes)." -f $pctDiff)
                    Write-Log ("[Noise floor] Pre-fixture idle background drift alone was {0:N2}% of the fixture size ($baselineDriftBytes bytes over a 3s idle window) -- a real, measured lower bound on how noisy this whole-drive comparison can be on this machine, independent of anything this step did." -f $baselineDriftPct)
                    # Threshold: within 2x the measured idle drift (floored at 1MB, so a
                    # near-zero idle sample doesn't make a small gap look suspicious) is treated
                    # as consistent with ordinary background noise, not independent evidence of a
                    # product bug. This is a judgment call, stated explicitly rather than left
                    # implicit -- 2x, not 1x, because one 3-second idle sample is itself a noisy
                    # estimate of the true drift rate, not a precise ceiling.
                    $noiseConsistentThresholdBytes = [math]::Max([math]::Abs($baselineDriftBytes) * 2, 1MB)
                    if ([math]::Abs($measuredVsReportedGapBytes) -le $noiseConsistentThresholdBytes) {
                        Write-Log "[Secondary check: consistent with noise] The OS-measured gap is within the same order of magnitude as this machine's own idle background drift -- NOT independent evidence of an app-level bug. The primary check above is authoritative for this step's PASS/FAIL."
                    } else {
                        Write-Log "[Secondary check: WORTH INVESTIGATING] The OS-measured gap is substantially larger than this machine's own measured idle drift -- report this explicitly, do not dismiss it as noise without looking. (Does not change the primary check's PASS/FAIL verdict above, which is authoritative.)"
                    }
                }
            }
        } catch {
            Write-Log "  [ERROR] apply request failed: $($_.Exception.Message)"
        }
        } else {
            Write-Log "[SKIPPED] Step 7's apply -- fixture scan never completed, see ABORT above."
        }
        Remove-Item -Path $fixtureDir -Recurse -Force -ErrorAction SilentlyContinue

        if ($switchedMode) {
            Write-Log "[RUN] Restoring original mode ($originalMode)..."
            try {
                $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/mode/safe" -Method Post -ContentType 'application/json' -Headers $headers -Body '{}' -TimeoutSec 10
                Write-Log "[OK] Mode restored to safe."
            } catch {
                Write-Log "[WARNING] Could not restore original mode: $($_.Exception.Message) -- this account is now left in power mode, restore it manually."
            }
        }
    }
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 8: check 1e -- 8.3 short-name TEMP-cache detection (AM2) ---"
Write-Log "    (scans the exact short-alias-triggering %TEMP% path itself and reports the real"
Write-Log "    temp_and_browser_caches candidate count -- the original bug was zero, permanently,"
Write-Log "    with no error, on exactly this account shape)"
# ===========================================================================

if (-not $serverReachable -or -not $csrfToken) {
    Write-Log "[SKIPPED] Step 8 -- dashboard server unreachable or no CSRF token."
} else {
    $tempPath = $env:TEMP
    Write-Log "[Scan target] `$env:TEMP = $tempPath (this IS the 8.3-aliased path, per Step 0's capture)"
    Write-Log "[RUN] POST /api/scan for $tempPath ..."
    try {
        $scanHeaders = @{ 'X-Reclaim-CSRF-Token' = $csrfToken }
        $scanCallStart = Get-Date
        # POST /api/scan itself is a background-task route (returns 202 immediately regardless of
        # index size) -- 10s stays fine here. The POLL window below and the candidates fetch after
        # it are the two calls that scale with real disk/index size; see AT1's comment on those.
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/scan" -Method Post -ContentType 'application/json' -Headers $scanHeaders -Body (@{ path = $tempPath } | ConvertTo-Json) -TimeoutSec 10
        $scanDone = $false
        $scanStatus = $null
        $pollDeadline = (Get-Date).AddSeconds(600)
        while ((Get-Date) -lt $pollDeadline) {
            Start-Sleep -Milliseconds 500
            $scanStatus = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/scan/status" -TimeoutSec 30
            if ($scanStatus.status -eq 'completed') { $scanDone = $true; break }
            if ($scanStatus.status -eq 'failed') { break }
        }
        $scanElapsedSeconds = [math]::Round(((Get-Date) - $scanCallStart).TotalSeconds, 1)
        if (-not $scanDone) {
            # AT1 (2026-08-24 audit): 600s, not 30s -- TEMP on a real, long-used dev machine can
            # genuinely hold many thousands of files; 30s was never sized against a real account,
            # only ever tested against small fixtures. Reports elapsed time + index size so a
            # future occurrence is diagnosable without a follow-up question.
            $sizeDetail = Get-IndexSizeDetail -AppDir $appDir
            Write-Log "[ABORT] Scan of $tempPath did not reach 'completed' after ${scanElapsedSeconds}s (status: '$($scanStatus.status)'; $sizeDetail) -- SKIPPING the candidate-count check."
        } else {
            Write-Log "[OK] Scan completed: $($scanStatus | ConvertTo-Json -Compress)"

            # AT2 (2026-08-24 audit): GET /api/candidates directly, even at a 600s timeout, is not
            # enough -- live-reproduced: it exceeded 600s against this exact scan's real result
            # (589,120 TEMP entries, many same-sized cache/scratch files -- exact-duplicate
            # clustering's cost scales with size-collision density, not just row count, and a
            # real TEMP directory is close to a worst case for that). A direct query issued
            # MINUTES later, once the background computation had actually finished and cached,
            # returned in 0.08s -- proving the real fix isn't a bigger timeout number (the
            # AT1-style fix already outgrown once), it's using the warm-up endpoint this codebase
            # already built for exactly this cost (PR #56/AE3, "non-blocking candidates-cache
            # warm-up + progress feedback") instead of blocking one HTTP call on it.
            Write-Log "[RUN] POST /api/candidates/warm (non-blocking; polling warm-status instead of blocking one GET on the full computation)..."
            # AZ3 (2026-08-25 audit): a 409 here means state.candidates_warm_status.status was
            # already 'computing' when this request landed (routes.py's own single-flight guard,
            # PR #62/AE3) -- a real warm-up genuinely IS already in flight, most plausibly the
            # SAME dashboard browser tab Step -1 opened (app.js's ensureCandidatesWarm fires from
            # loadOverview(), the default landing tab, and this account's real multi-GB index
            # takes minutes to warm -- easily still running by the time this step, much later in
            # the trip, gets here). Confirmed this is the guard behaving correctly, not a
            # regression of #62: routes.py's check-and-set happens under `state.lock`, and this
            # 409's own detail text ("a candidates warm-up is already running") is the literal
            # string that route raises, not a generic/ambiguous error. Live-reproduced this
            # session's own trip run. Previously this step let the 409 propagate as an uncaught
            # exception and aborted the whole step -- wrong, since "already warming" is exactly
            # the state this step's own poll loop below is built to wait out; treat 409 the same
            # as a successful start and go straight to polling the existing warm-up instead of
            # treating someone else's in-flight work as this step's own failure.
            try {
                $null = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/candidates/warm" -Method Post -ContentType 'application/json' -Headers $scanHeaders -Body '{}' -TimeoutSec 10
            } catch {
                if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 409) {
                    Write-Log "[OK] 409 -- a candidates warm-up is already in flight (most likely the dashboard browser tab from Step -1); polling its existing progress instead of starting a second one."
                } else {
                    throw
                }
            }
            $warmCallStart = Get-Date
            $warmReady = $false
            $warmStatus = $null
            $warmPollDeadline = (Get-Date).AddSeconds(1200)
            while ((Get-Date) -lt $warmPollDeadline) {
                Start-Sleep -Seconds 3
                $warmStatus = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/candidates/warm-status" -TimeoutSec 15
                if ($warmStatus.status -in @('ready', 'failed')) { $warmReady = ($warmStatus.status -eq 'ready'); break }
            }
            $warmElapsedSeconds = [math]::Round(((Get-Date) - $warmCallStart).TotalSeconds, 1)
            if (-not $warmReady) {
                $sizeDetail = Get-IndexSizeDetail -AppDir $appDir
                Write-Log "[ABORT] Candidates warm-up did not reach 'ready' after ${warmElapsedSeconds}s (status: '$($warmStatus.status)'; $sizeDetail) -- SKIPPING the candidate-count check. This is a real, disclosed cost on a real TEMP directory (docs/AUDIT-2026-08.md's AS3/AT2), not a script bug -- if this keeps happening, the real fix is a warm-status-aware dashboard UI wait, not a bigger number here."
            } else {
                Write-Log "[OK] Candidates warm-up ready after ${warmElapsedSeconds}s."
                $candResp = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/candidates?tier=both&category=temp_and_browser_caches" -TimeoutSec 30
                $count = @($candResp.candidates).Count
                Write-Log "[Result] temp_and_browser_caches candidates found: $count"
                if ($count -gt 0) {
                    Write-Log "[PASS] Nonzero candidates under the 8.3-aliased TEMP path -- PR #50's fix holds on the real account shape it was written for."
                } else {
                    Write-Log "[FAIL -- REAL FINDING, NOT A SCRIPT BUG, UNLESS TEMP is genuinely empty] Zero candidates -- this is exactly the original bug's signature. Before treating this as a regression, manually confirm $tempPath actually contains real files/subdirectories (an empty TEMP would legitimately yield zero too, and would not be this bug)."
                }
            }
        }
    } catch {
        Write-Log "  [ERROR] Step 8 request failed: $($_.Exception.Message)"
    }
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 9: copy the app's own structured log to a readable location (AM2) ---"
Write-Log "    (settles check 3's real question: does notifications.toast_failed appear anywhere,"
Write-Log "    distinguishing 'send_disk_space_toast was called' from 'it actually succeeded"
Write-Log "    internally' -- record_notified fires either way, so Step 5's notification_state.json"
Write-Log "    alone can never answer this)"
# ===========================================================================

if ($appDir) {
    $realLogFile = Join-Path $appDir 'data\logs\reclaim.log'
    if (Test-Path $realLogFile) {
        $destLogFile = Join-Path $logDir 'reclaim_app.log'
        Copy-Item -Path $realLogFile -Destination $destLogFile -Force
        Write-Log "[OK] Copied $realLogFile to $destLogFile"
        $toastFailedLines = @(Get-Content $realLogFile | Select-String -Pattern 'toast_failed')
        if ($toastFailedLines.Count -gt 0) {
            Write-Log "[FAIL -- REAL FINDING, NOT A SCRIPT BUG] notifications.toast_failed appears $($toastFailedLines.Count) time(s) in the real log -- send_disk_space_toast raised internally at least once this run. See $destLogFile for the real exception."
        } else {
            Write-Log "[Result] No 'toast_failed' line in the real log -- send_disk_space_toast was called and did not raise. This is real evidence the call succeeded internally; it is still NOT proof a toast rendered on screen (Windows gives the sending process no delivery guarantee) -- that half of check 3 stays UNVERIFIED without a human 'yes/no, I saw it.'"
        }
    } else {
        Write-Log "[ABORT] Real log not found at $realLogFile -- cannot settle check 3's toast_failed question this run."
    }
} else {
    Write-Log "[SKIPPED] Step 9 -- APPDIR unresolved."
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
# AL5: a real run's own tally missed this exact gap -- a try/catch's "[ERROR] ... failed:
# $($_.Exception.Message)" line (Steps 2/3/6's own request failures) doesn't start with any of
# the four prefixes above, so a genuine failure could pass through this tally uncounted. Counted
# separately, not folded into "aborts", since an [ERROR] line's exact meaning depends on which
# step logged it -- rolling it into one number would lose that context the report needs anyway.
$scriptErrors = $logLines | Select-String -Pattern '^\s*\[ERROR\]'
Write-Log "  ABORT lines:   $($aborts.Count)"
Write-Log "  FAIL lines:    $($fails.Count)"
Write-Log "  WARNING lines: $($warnings.Count)"
Write-Log "  SKIPPED lines: $($skips.Count)"
Write-Log "  ERROR lines:   $($scriptErrors.Count) (request/exception failures inside a try/catch -- read each one, they are not summarized further here)"
if ($aborts.Count -eq 0 -and $fails.Count -eq 0 -and $skips.Count -eq 0 -and $scriptErrors.Count -eq 0) {
    Write-Log "  Every step ran and reported a real result -- nothing was aborted, failed, skipped, or errored."
} else {
    Write-Log "  This run did NOT complete cleanly -- do not treat it as a full pass. Review each"
    Write-Log "  ABORT/FAIL/SKIPPED/ERROR line above before concluding anything about the trip's outcome."
}

Write-Log "==================================================================="
Write-Log "Run complete. Full log: $logPath"
Write-Log "==================================================================="
