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

# BH2 (2026-08-26 audit): live-reproduced -- a real trip's Step -2 logged "[WARNING] A reclaim.exe
# process (PID 30144) was still running before this install -- stopping it first", then Step -1's
# OWN "before launch" probe got a REAL, successful response from http://127.0.0.1:8420/api/first-run
# -- BEFORE the script had launched anything. That is direct, unambiguous proof a server was
# already live on port 8420 at that moment: the same PID 30144 was later sampled continuously
# running throughout Step 1 (15s window). Every HTTP-based step in that trip (6, 7, 8, and the
# browser dashboard itself) silently ran against this survivor, not the freshly-installed binary
# -- and its activity never appeared anywhere in this account's own data\logs\reclaim.log* files,
# confirmed by direct inspection of all 6 rotated/active copies (zero entries after the timestamp
# Step 1's own tail-of-20-lines capture already showed). The root mechanism for WHY Stop-Process
# didn't take is not conclusively established (Windows permits an installer to replace a running
# .exe's on-disk bytes via delete+recreate while the old process keeps running against its own
# now-orphaned handle -- consistent with the install itself reporting exit 0 even though the kill
# silently failed) -- but the FIX doesn't depend on knowing the mechanism: verify the port is
# actually free before ever trusting a fresh launch, every time, fail closed if it isn't.
function Get-Port8420OwningProcessIds {
    # -ErrorAction SilentlyContinue: Get-NetTCPConnection throws if literally nothing is listening
    # on the port, which is the common/expected case, not an error.
    @(Get-NetTCPConnection -LocalPort 8420 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Wait-Port8420Free {
    param([int]$TimeoutSeconds = 15)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ((Get-Port8420OwningProcessIds).Count -eq 0 -and -not (Get-Process -Name 'reclaim' -ErrorAction SilentlyContinue)) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

Write-Log "==================================================================="
Write-Log "Reclaim AC3 diagnostic run -- $ts"
Write-Log "==================================================================="

# ===========================================================================
Write-Log ""
Write-Log "--- STEP -2: Install (AJ4) ---"
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

    # BE4 (2026-08-26 audit): this used to be a hardcoded "rebuild #4" label in the section
    # header above, silently going stale with every rebuild since (this trip ran rebuild #7
    # under a "#4" banner) -- now reads the actual .buildsha sidecar and prints the real source
    # commit here instead, so the log is honest about what it tested regardless of how many
    # rebuilds have happened since anyone last remembered to update a hardcoded number.
    $buildShaPath = "$InstallerPath.buildsha"
    $freshnessOk = $false
    $freshnessReason = ""
    if (-not (Test-Path $buildShaPath)) {
        Write-Log "     Build source commit: unknown (no .buildsha sidecar next to the installer)"
        $freshnessReason = "no .buildsha sidecar next to the installer (pre-AR3 build, or a copy that lost it)"
    } else {
        $recordedSha = (Get-Content -Path $buildShaPath -Raw).Trim()
        Write-Log "     Build source commit: $recordedSha"
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
        # AZ4 (2026-08-25 audit): live-reproduced -- a second trip run in the same session, with
        # a previous run's `reclaim.exe serve` (or a scan/apply worker it spawned) still holding
        # a lock, made the installer exit 5 ("user clicked Cancel/Abort during the actual
        # installation" -- Inno Setup's documented meaning, even under /SUPPRESSMSGBOXES: a
        # locked file during [Files] copy resolves to that same code, not a hang). reclaim.iss's
        # own InitializeUninstall already guards exactly this for the paired uninstall path
        # (`taskkill /F /T /IM reclaim.exe` before touching any file) -- this trip script's own
        # install step had no equivalent for a same-version reinstall over a still-running
        # process, which is exactly what happens when this script runs more than once without an
        # intervening clean shutdown. Mirrors the same fix here, at the one remaining call site
        # that needed it.
        $staleProcs = @(Get-Process -Name 'reclaim' -ErrorAction SilentlyContinue)
        foreach ($p in $staleProcs) {
            Write-Log "[WARNING] A reclaim.exe process (PID $($p.Id)) was still running before this install -- stopping it first, same as reclaim.iss's own InitializeUninstall does for the paired uninstall path."
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
        # BH2: POLL until confirmed gone (both by process name AND by port-8420 ownership --
        # belt-and-suspenders, since a process rename/handle-reuse edge case could in principle
        # separate the two), not a fixed sleep-and-hope. A survivor here means every downstream
        # HTTP-based step would silently run against it, not the fresh install -- fail closed.
        if (-not (Wait-Port8420Free -TimeoutSeconds 15)) {
            $stillAlive = @(Get-Process -Name 'reclaim' -ErrorAction SilentlyContinue)
            $stillOwningPort = Get-Port8420OwningProcessIds
            Write-Log "[ABORT] Could not confirm a clean slate after 15s: reclaim.exe PID(s) still running: $(if ($stillAlive) { $stillAlive.Id -join ',' } else { '(none)' }); PID(s) still owning port 8420: $(if ($stillOwningPort) { $stillOwningPort -join ',' } else { '(none)' }). Refusing to install and run the trip on top of an unconfirmed-dead survivor -- this is the exact BH2 finding (2026-08-26 audit): a surviving server silently served an entire trip's HTTP-based steps once, undetected until traced after the fact. Investigate manually (a locked handle, a protected/elevated process, or a scan/apply worker that outlived its parent), then re-run."
            $SkipInstall = $true  # reuses every downstream skip, same posture as the freshness-check ABORT
            $installSkippedByFreshnessCheck = $true
            $staleProcessKillFailed = $true
        }
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
Write-Log "--- STEP -1.5: Reset first-run acknowledgment (BD3, 2026-08-26 audit) ---"
Write-Log "    (first_run_state.json lives at {app}\data\, which Inno Setup's uninstaller never"
Write-Log "    removes -- it only tracks/removes files it installed via [Files], never runtime-"
Write-Log "    created data -- so a fresh install over the same {app} path leaves a prior"
Write-Log "    acknowledgment in place. This is very likely why the genuine first-run screen has"
Write-Log "    never been observed this entire engagement: 'reinstall' was never actually a clean"
Write-Log "    profile for this one file. Deleting the marker explicitly is the real reset.)"
# ===========================================================================
if ($installSkippedByFreshnessCheck -or $SkipInstall) {
    Write-Log "[SKIPPED] No fresh install this run -- nothing to reset."
} else {
    $resetUninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B6C1B6C7-6B6A-4E3B-9B7B-2B7E1E7C6A21}_is1'
    $resetAppDir = $null
    try {
        $resetAppDir = (Get-ItemProperty -Path $resetUninstallKey -Name InstallLocation -ErrorAction Stop).InstallLocation
    } catch { $resetAppDir = $null }
    if (-not $resetAppDir) {
        Write-Log "[ABORT] Could not resolve install location to reset first_run_state.json -- Step -1 below will very likely observe a stale acknowledgment, not the genuine screen."
    } else {
        $firstRunStatePath = Join-Path $resetAppDir 'data\first_run_state.json'
        if (Test-Path $firstRunStatePath) {
            # BH2 (2026-08-26 audit): live-reproduced -- a real trip logged "[OK] Deleted
            # pre-existing ...first_run_state.json" here, yet /api/first-run returned
            # {"acknowledged":true} at every single check in Step -1 that followed (before launch,
            # after launch, after acknowledgment). The "[OK] Deleted" message was NEVER actually
            # verified -- Remove-Item's own failure (if any -- e.g. the file locked by a survivor
            # process, see the port-8420 checks above/below) would just write to the error stream
            # under this script's global $ErrorActionPreference='Continue' and the very next line
            # would still print "[OK] Deleted" regardless of whether it worked. Fixed: verify with
            # a real post-delete Test-Path, and report the true outcome, not the attempt.
            try {
                Remove-Item -Path $firstRunStatePath -Force -ErrorAction Stop
            } catch {
                Write-Log "[WARNING] Remove-Item threw deleting $firstRunStatePath -- $($_.Exception.Message)"
            }
            if (Test-Path $firstRunStatePath) {
                Write-Log "[ABORT] $firstRunStatePath still exists after a delete attempt -- Step -1 below will almost certainly observe a stale acknowledgment, not the genuine first-run screen. Likely a locked handle (see BH2 above) -- investigate before trusting Step -1's result this run."
            } else {
                Write-Log "[OK] Confirmed deleted (verified via a post-delete Test-Path, not just the attempt): $firstRunStatePath -- Step -1 will now observe a genuine first-run state."
            }
        } else {
            Write-Log "[OK] $firstRunStatePath did not exist -- already a clean first-run state."
        }
    }
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP -1: Genuine first-run observation (AJ4 -- this is the ONLY chance) ---"
Write-Log "    THIS MUST HAPPEN BEFORE ANY OTHER STEP TOUCHES THIS PROFILE."
# ===========================================================================

if ($staleProcessKillFailed) {
    Write-Log "[SKIPPED] Step -2's stale-process kill-and-verify aborted -- no install attempted, so nothing below is safe to run against this profile until the survivor is cleared manually (see the ABORT line above)."
} elseif ($installSkippedByFreshnessCheck) {
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
    $preLaunchServerAlreadyLive = $false
    try {
        $before = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/first-run" -TimeoutSec 2 -ErrorAction Stop
        Write-Log "  $($before | ConvertTo-Json -Compress)"
        $preLaunchServerAlreadyLive = $true
    } catch {
        Write-Log "  (no response -- server not running yet, expected)"
    }

    if ($preLaunchServerAlreadyLive) {
        # BH2 (2026-08-26 audit): live-reproduced -- this EXACT check succeeding, right here,
        # BEFORE "Launching 'reclaim dashboard'" ever ran, is the single most direct proof a
        # stale server was already listening on port 8420 in a real trip -- it silently served
        # every subsequent HTTP-based step (6/7/8, the browser itself), and its activity never
        # once appeared in this account's own data\logs\reclaim.log* files afterward (confirmed
        # by direct inspection: zero log lines postdate the point this exact probe was reached).
        # The old code treated only a FAILURE here as expected and said nothing about a surprise
        # SUCCESS. Fixed: attempt automatic remediation (identify + kill whatever owns port 8420,
        # verify it's actually free) before launching on top of it -- fail closed if that doesn't
        # work, rather than silently proceeding to serve the rest of the trip off a survivor.
        $owningPids = Get-Port8420OwningProcessIds
        Write-Log "[WARNING] A server is ALREADY responding on port 8420 before this script launched anything -- PID(s) owning the port: $(if ($owningPids) { $owningPids -join ',' } else { '(unknown -- Get-NetTCPConnection found none, but the HTTP probe above succeeded)' }). Attempting automatic remediation: killing and re-verifying."
        foreach ($ownPid in $owningPids) { Stop-Process -Id $ownPid -Force -ErrorAction SilentlyContinue }
        Get-Process -Name 'reclaim' -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
        if (-not (Wait-Port8420Free -TimeoutSeconds 15)) {
            Write-Log "[ABORT] Automatic remediation failed -- port 8420 still owned after a 15s kill-and-wait. Refusing to launch a second dashboard on top of an unconfirmed-dead survivor (that is exactly how a prior trip's results got silently scoped to the wrong binary). Investigate manually: Get-NetTCPConnection -LocalPort 8420 | Select OwningProcess, kill it, confirm the port is free, then re-run from the top."
            Read-Host "Press Enter to acknowledge (the rest of this trip's HTTP-based steps will be SKIPPED below) and continue anyway"
            $preLaunchRemediationFailed = $true
        } else {
            Write-Log "[OK] Port 8420 confirmed free after remediation -- proceeding to launch a genuinely fresh dashboard."
        }
    }

    if ($preLaunchRemediationFailed) {
        # Deliberately not launching anything here -- Step 6's own probe (GET /api/scan/status)
        # will naturally fail against an empty port 8420 and set $serverReachable=$false itself,
        # correctly gating Steps 6/7/8/10 below through the existing mechanism.
        Write-Log "[SKIPPED] Not launching a new dashboard -- see the ABORT above. Every HTTP-based step below (6, 7, 8, 10) will report SKIPPED for the same reason."
    } else {
    Write-Log "[RUN] Launching 'reclaim dashboard' (opens your default browser automatically)..."
    $exePath = $null
    try {
        $exePath = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B6C1B6C7-6B6A-4E3B-9B7B-2B7E1E7C6A21}_is1' -Name InstallLocation -ErrorAction Stop).InstallLocation
    } catch { $exePath = $null }
    if ($exePath) {
        # BH2/BH6: -PassThru captures the NEWLY launched process's own PID -- used below (both
        # here and in Step 1) to positively confirm the server actually answering requests is
        # THIS process, not merely "a server responded" (which is exactly what went undetected
        # in a real trip: the responding server was a leftover, not the one just launched).
        $dashboardProc = Start-Process -FilePath (Join-Path $exePath 'reclaim.exe') -ArgumentList 'dashboard' -PassThru
        $script:freshDashboardPid = $dashboardProc.Id
        Start-Sleep -Seconds 3
        Write-Log "[Raw state AFTER launch, BEFORE you acknowledge] GET /api/first-run:"
        try {
            $afterLaunch = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/first-run" -TimeoutSec 5 -ErrorAction Stop
            Write-Log "  $($afterLaunch | ConvertTo-Json -Compress)"
        } catch {
            Write-Log "  (no response yet -- server may still be starting; wait a moment and check the browser)"
        }
        $portOwners = Get-Port8420OwningProcessIds
        if ($portOwners -contains $freshDashboardPid) {
            Write-Log "[OK -- BH2] Confirmed: port 8420 is owned by PID $freshDashboardPid, the process this step just launched -- every HTTP-based step below is genuinely against this freshly-installed binary, not a survivor."
        } elseif ($portOwners.Count -gt 0) {
            Write-Log "[WARNING -- BH2] Port 8420 is owned by PID(s) $($portOwners -join ',') -- NOT PID $freshDashboardPid, the process this step just launched. Every HTTP-based step below (6, 7, 8, 10) is running against a DIFFERENT process than the one just installed -- treat their results as scoped to an unconfirmed binary, exactly the 2026-08-26 BH2 finding. This can legitimately happen if the freshly-launched process exited immediately after a child re-exec'd under a new PID -- not necessarily still a stale survivor, but not verified fresh either."
        } else {
            Write-Log "[WARNING -- BH2] Could not determine which PID owns port 8420 (Get-NetTCPConnection returned nothing despite a successful HTTP response) -- cannot positively confirm the server is PID $freshDashboardPid. Treat downstream HTTP-based results as unconfirmed."
        }
    } else {
        Write-Log "[ABORT] Could not resolve install location to launch reclaim.exe -- launch it yourself: Start Menu > Reclaim, or the Reclaim desktop shortcut."
    }

    Read-Host "Press Enter once you have observed and captured the first-run screen (per points 1-4 above) and are ready to continue with the rest of this script"
    }

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

# BF1/BF2 (2026-08-26 audit): BE1 renamed the task per-account ("Reclaim Disk Space Check
# (<username>)") specifically so a second account installing on a machine that already has a
# first account's task can no longer collide with it -- this machine's own gaura account holds
# exactly that first, older-format task from BE1's own live teeth-proof, so THIS install (a
# different account, $env:USERNAME) landing on the same machine is a real, live multi-account
# collision scenario, not a synthetic one. Asserting on it here converts BE1's previously
# structural-only claim ("a different username produces a different task name, so no two
# accounts can ever collide, by construction") into an actual second live data point.
$taskName = "Reclaim Disk Space Check ($env:USERNAME)"
Write-Log "[BF1] Expecting task name '$taskName' (per-account, BE1) -- NOT the old shared"
Write-Log "  'Reclaim Disk Space Check' name, which BE1's install-time migration should have"
Write-Log "  removed if this account ever held it."
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Log "[ABORT] Task '$taskName' not found -- SKIPPING /run + process sampling below, loudly."
} else {
    Write-Log "[PASS -- BF1] Task '$taskName' exists, registered under this account -- BE1's"
    Write-Log "  per-account naming worked for a REAL second account on a machine where a first"
    Write-Log "  account's task already occupied the old shared name."

    $legacyTask = Get-ScheduledTask -TaskName 'Reclaim Disk Space Check' -ErrorAction SilentlyContinue
    if ($legacyTask) {
        Write-Log "[WARNING -- BF1] The old shared-name task 'Reclaim Disk Space Check' still exists and is visible from this account -- BE1's migration should have removed it if THIS account ever held it under the old name; a fresh account was never expected to see it at all unless Task Scheduler grants broader read access than expected. Not a collision (the names differ), but worth a look."
    } else {
        Write-Log "[OK -- BF1] No old shared-name task visible from this account (either never existed here, or this account cannot see another account's tasks at all -- see the gaura-task check below for which)."
    }

    $gauraTask = Get-ScheduledTask -TaskName 'Reclaim Disk Space Check (gaura)' -ErrorAction SilentlyContinue
    if ($gauraTask) {
        Write-Log "[PASS -- BF1] gaura's own per-account task 'Reclaim Disk Space Check (gaura)' is ALSO visible from this account and reports State=$($gauraTask.State) -- both accounts' tasks coexist on this machine with zero collision, directly confirmed, not inferred."
    } else {
        Write-Log "[INCONCLUSIVE -- BF1] gaura's task is not visible from this account (Task Scheduler may restrict cross-account task listing, or icacls' earlier finding that only gaura/Administrators/SYSTEM hold rights on it extends to read/list too) -- this does not indicate a collision (this account's own task above already exists and is Ready, which is the actual proof no collision occurred), just that this account can't independently confirm gaura's task's state from here."
    }

    $infoBefore = Get-ScheduledTaskInfo -TaskName $taskName
    Write-Log "[BEFORE] LastRunTime=$($infoBefore.LastRunTime) LastTaskResult=$($infoBefore.LastTaskResult)"

    # BH6 (2026-08-26 audit): live-reproduced -- a real trip sampled the SAME pid across the
    # entire 15s window and reported it as "reclaim.exe RUNNING", but that pid was independently
    # confirmed (BH2, same trip) to be a long-lived dashboard survivor that predated this task
    # even being started -- the old code could not and did not distinguish that from a genuine
    # task-spawned check-disk-space process, because it just listed EVERY reclaim.exe running,
    # not specifically the one Start-ScheduledTask above just caused to exist. Snapshot the
    # baseline PID set immediately BEFORE starting the task, then report only NEW pids at each
    # sample -- that is the actual task-spawned process (or its correct, honest absence).
    $basePids = @((Get-Process -Name reclaim -ErrorAction SilentlyContinue) | Select-Object -ExpandProperty Id)
    Write-Log "[Process evidence] Baseline reclaim.exe pid(s) already running before Start-ScheduledTask: $(if ($basePids) { $basePids -join ',' } else { '(none)' })"

    Write-Log "[RUN] Start-ScheduledTask -TaskName '$taskName'"
    Start-ScheduledTask -TaskName $taskName

    Write-Log "[Process evidence] Sampling for reclaim.exe every 3s for 15s (AI3: distinguishes"
    Write-Log "  'task fired, process ran to completion' from 'task fired, process crashed before"
    Write-Log "  reaching toast code' -- both look identical from LastRunTime alone; BH6: only pids"
    Write-Log "  NOT in the baseline above count as evidence of the task's OWN process, not a"
    Write-Log "  pre-existing survivor that happened to already be running):"
    $sawNewPid = $false
    for ($i = 1; $i -le 5; $i++) {
        Start-Sleep -Seconds 3
        $procs = @((Get-Process -Name reclaim -ErrorAction SilentlyContinue) | Select-Object -ExpandProperty Id)
        $newPids = @($procs | Where-Object { $_ -notin $basePids })
        if ($newPids) {
            $sawNewPid = $true
            Write-Log "  [t+$($i*3)s] NEW reclaim.exe pid(s) (not in baseline, this IS the task's own process): $($newPids -join ',')"
        } elseif ($procs) {
            Write-Log "  [t+$($i*3)s] reclaim.exe running, but only baseline pid(s) $($procs -join ',') -- NOT evidence the task's own process is still alive (it may have already exited, or the task never actually spawned a distinct process this sampling could catch)."
        } else {
            Write-Log "  [t+$($i*3)s] reclaim.exe not present in process list at all (neither baseline nor new)."
        }
    }
    if (-not $sawNewPid) {
        Write-Log "[WARNING -- BH6] No NEW reclaim.exe pid was ever observed distinct from the pre-task baseline across all 5 samples -- this run's process evidence does NOT positively confirm the task actually spawned its own process (it may have run and exited faster than this 3s sampling granularity can catch, which is a real, known limitation, not necessarily a failure)."
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
                        # BH7 (2026-08-26 audit): live-reproduced -- a real run had a 5.98% gap
                        # against a 0.31% measured drift (~19x, genuinely not "the same order of
                        # magnitude" by any normal reading of that phrase) and still correctly
                        # PASSed here, because the actual gate is 2x-drift-OR-1MB-floor
                        # (whichever is larger), not literal proximity to the drift rate -- on a
                        # small idle-drift sample, the 1MB floor is what usually does the work,
                        # not the drift multiple. The gate's own math was never wrong; only the
                        # printed message overclaimed what tripped it. State the real threshold
                        # honestly instead of a comparison the numbers don't actually support.
                        $thresholdReason = if ($noiseConsistentThresholdBytes -eq 1MB) { "the 1MB floor, not the drift multiple, is what qualified this" } else { "the measured-drift multiple qualified this" }
                        Write-Log "[Secondary check: within threshold] The OS-measured gap ($([math]::Abs($measuredVsReportedGapBytes)) bytes) is within this step's own noise-consistent threshold ($noiseConsistentThresholdBytes bytes = max(2x measured drift, 1MB floor); $thresholdReason) -- NOT independent evidence of an app-level bug. The primary check above is authoritative for this step's PASS/FAIL."
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
                # AZ4 (2026-08-25 audit): live-reproduced -- 30s was the one remaining short
                # timeout in this script. The warm-up cache being "ready" does not make the
                # follow-up GET itself free: serializing/filtering the real candidate list over
                # HTTP for this account's real index size still took longer than 30s (the warm-up
                # call above needed 96.3s on this same run). Same class of bug as AT1/AT2/AY2,
                # missed at this one remaining call site -- raised to match this script's own
                # established 600s convention rather than left as the one outlier.
                $candResp = Invoke-RestMethod -Uri "http://127.0.0.1:8420/api/candidates?tier=both&category=temp_and_browser_caches" -TimeoutSec 600
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
        # BH3 (2026-08-26 audit): the exception message alone ("(500) Internal Server Error") is
        # not enough to diagnose a real server-side failure -- $_.ErrorDetails.Message carries the
        # actual HTTP response body (Invoke-RestMethod populates it in PS 5.1+/7+ when the server
        # returned one), which may hold a FastAPI-rendered detail message even in non-debug mode.
        # Captured here for the next occurrence; a NULL/empty value here is itself informative
        # (means the server returned no body at all, not that this script failed to look).
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            Write-Log "  [ERROR -- response body] $($_.ErrorDetails.Message)"
        } else {
            Write-Log "  [ERROR -- response body] (none captured -- server returned no body, or it wasn't retrievable from this exception)"
        }
    }
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 9: copy the app's own structured log to a readable location (AM2/BD4) ---"
Write-Log "    (BD4, 2026-08-26 audit: copies the ACTIVE log plus every rotated backup --"
Write-Log "    reclaim.log.1 .. reclaim.log.5 -- not just reclaim.log. logging_config.py's"
Write-Log "    RotatingFileHandler caps the active file at 5MB; a single heavy dedup computation"
Write-Log "    (Step 8's candidates/warm on a large index) was directly confirmed capable of"
Write-Log "    producing enough log volume to rotate the active file within ~20 seconds, evicting"
Write-Log "    everything from earlier steps -- including any api.scan_initiated line that would"
Write-Log "    explain a Step 7-style unexplained-scan-trigger instance. Copying only the active"
Write-Log "    file, as this step used to, is very likely why AN1/AZ4's prior instances of that"
Write-Log "    shape were never root-caused either.)"
Write-Log "    BH4 (2026-08-26 audit): the filter below also missed reclaim_audit.log* -- BE2's"
Write-Log "    dedicated audit sink for api.scan_initiated, added the same session, sitting"
Write-Log "    right next to reclaim.log in the same directory. A real trip's own scan-trigger"
Write-Log "    mystery (a 4th instance) could not be root-caused this run BECAUSE that file was"
Write-Log "    never captured -- 'reclaim.log*' does not match a name starting 'reclaim_audit'."
# ===========================================================================

if ($appDir) {
    $realLogDir = Join-Path $appDir 'data\logs'
    # BH4: 'reclaim*.log*' (not 'reclaim.log*') so this also catches reclaim_audit.log and its
    # own rotated backups (reclaim_audit.log.1 .. .10, BE2's 10-backup budget) -- both prefixes
    # live in the same directory and nothing else does, so the wider glob is still exact, not
    # a shotgun.
    $realLogFiles = @(Get-ChildItem -Path $realLogDir -Filter 'reclaim*.log*' -ErrorAction SilentlyContinue)
    if ($realLogFiles.Count -gt 0) {
        $allCopiedText = @()
        foreach ($f in $realLogFiles) {
            $destLogFile = Join-Path $logDir "reclaim_app_$($f.Name).log"
            Copy-Item -Path $f.FullName -Destination $destLogFile -Force
            Write-Log "[OK] Copied $($f.FullName) ($([math]::Round($f.Length/1MB, 2)) MB) to $destLogFile"
            $allCopiedText += Get-Content $f.FullName
        }
        $toastFailedLines = @($allCopiedText | Select-String -Pattern 'toast_failed')
        if ($toastFailedLines.Count -gt 0) {
            Write-Log "[FAIL -- REAL FINDING, NOT A SCRIPT BUG] notifications.toast_failed appears $($toastFailedLines.Count) time(s) across the active log + rotated backups -- send_disk_space_toast raised internally at least once this run."
        } else {
            Write-Log "[Result] No 'toast_failed' line across the active log + rotated backups. BD2 (2026-08-26 audit): send_disk_space_toast never logs anything on SUCCESS, only on exception -- this absence does NOT by itself mean the call succeeded, or was even made. Steps 2/3/4 above all pass --apply-snooze, which returns before ever reaching check_disk_space/send_disk_space_toast (src/reclaim/cli.py's _run_check_disk_space, apply_snooze branch returns early) -- they structurally cannot exercise this codepath. Step 1 is the only step that reaches it, and it aborts whenever the Task Scheduler task is absent. See Step 10 below for the one trigger that actually forces this codepath and checks for it directly."
        }
    } else {
        Write-Log "[ABORT] No reclaim.log* files found at $realLogDir -- cannot settle check 3's toast_failed question this run."
    }
} else {
    Write-Log "[SKIPPED] Step 9 -- APPDIR unresolved."
}

# ===========================================================================
Write-Log ""
Write-Log "--- STEP 10: BD2 -- dedicated toast-codepath trigger (2026-08-26 audit) ---"
Write-Log "    (Steps 2/3/4 all pass --apply-snooze, which src/reclaim/cli.py's"
Write-Log "    _run_check_disk_space returns from BEFORE ever calling check_disk_space/"
Write-Log "    send_disk_space_toast -- they cannot exercise this codepath, structurally, not"
Write-Log "    just weakly. This step is the one that actually can: clears snooze state, then"
Write-Log "    invokes plain 'check-disk-space' with no flags -- the same call Step 1's Task"
Write-Log "    Scheduler task makes, without depending on that task existing.)"
# ===========================================================================

if (-not $appDir) {
    Write-Log "[SKIPPED] Step 10 -- APPDIR unresolved."
} else {
    $step10Exe = Join-Path $appDir 'reclaim.exe'
    $step10Config = Join-Path $appDir 'config.toml'
    $step10State = Join-Path $appDir 'data\notification_state.json'

    if (Test-Path $step10State) {
        Write-Log "[BEFORE] $step10State exists -- deleting to clear any snooze/debounce state (NotificationState.load() treats a missing file identically to {last_notified_at: null, snoozed_until: null}, per notifications.py's own docstring)."
        Remove-Item -Path $step10State -Force
    } else {
        Write-Log "[BEFORE] $step10State already absent -- already a clean snooze/debounce state."
    }

    $step10Out = Join-Path $env:TEMP "ac3_step10_stdout_$ts.txt"
    Write-Log "[RUN] `"$step10Exe`" check-disk-space --config `"$step10Config`" --state `"$step10State`"  (no --apply-snooze)"
    $step10Proc = Start-Process -FilePath $step10Exe `
        -ArgumentList @('check-disk-space', '--config', $step10Config, '--state', $step10State) `
        -WorkingDirectory $appDir -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $step10Out -RedirectStandardError "$step10Out.err"
    $step10Stdout = if (Test-Path $step10Out) { Get-Content $step10Out -Raw } else { '' }
    $step10Stderr = if (Test-Path "$step10Out.err") { Get-Content "$step10Out.err" -Raw } else { '' }
    Write-Log "  exit code: $($step10Proc.ExitCode)"
    Write-Log "  stdout: $step10Stdout"
    if ($step10Stderr) { Write-Log "  stderr: $step10Stderr" }
    Remove-Item -Path $step10Out, "$step10Out.err" -Force -ErrorAction SilentlyContinue

    $step10AfterState = $null
    if (Test-Path $step10State) {
        try { $step10AfterState = Get-Content $step10State -Raw | ConvertFrom-Json } catch {}
    }
    $step10Notified = $step10AfterState -and $null -ne $step10AfterState.last_notified_at

    if ($step10Stdout -match 'reason=would_notify' -and $step10Notified) {
        Write-Log "[PASS] reason=would_notify AND notification_state.json's last_notified_at is now set -- send_disk_space_toast was genuinely invoked this run, not inferred from log absence."
    } elseif ($step10Stdout -match 'reason=(disabled|below_threshold|snoozed|debounced)') {
        Write-Log "[SKIPPED -- real, not a bug] reason=$($Matches[1]) -- this machine's real disk usage/config did not cross the threshold (or state wasn't actually cleared). Not evidence the toast codepath is broken, but also not evidence it works: re-run once the real condition (percent_used >= threshold) holds, or lower disk_threshold_percent in config.toml temporarily."
    } else {
        Write-Log "[INCONCLUSIVE] Could not parse an expected reason= from stdout, or last_notified_at did not update as expected -- read the raw stdout/state above directly."
    }

    Write-Log "[RUN] Re-copying reclaim*.log* immediately (minimizing rotation-eviction risk) to check toast_failed for THIS specific invocation..."
    # BH4: same 'reclaim*.log*' widening as Step 9 above, so this re-copy also catches
    # reclaim_audit.log* rather than silently missing it a second time in the same run.
    $step10LogFiles = @(Get-ChildItem -Path (Join-Path $appDir 'data\logs') -Filter 'reclaim*.log*' -ErrorAction SilentlyContinue)
    $step10AllText = @()
    foreach ($f in $step10LogFiles) {
        $dest = Join-Path $logDir "reclaim_app_step10_$($f.Name).log"
        Copy-Item -Path $f.FullName -Destination $dest -Force
        $step10AllText += Get-Content $f.FullName
    }
    $step10ToastFailed = @($step10AllText | Select-String -Pattern 'toast_failed')
    if ($step10ToastFailed.Count -gt 0) {
        Write-Log "[FAIL -- REAL FINDING] notifications.toast_failed appears $($step10ToastFailed.Count) time(s) immediately after this specific, confirmed-invoked call."
    } elseif ($step10Notified) {
        Write-Log "[Result] No toast_failed after a confirmed-invoked call (last_notified_at updated) -- real evidence send_disk_space_toast did not raise this time. Still not proof of on-screen delivery (Windows gives no such guarantee) -- that half needs a human 'yes/no, I saw it.'"
    } else {
        Write-Log "[Result] No toast_failed, but the call was not confirmed-invoked above either -- this absence is not meaningful evidence (see BD2 finding: no-log-line is trivially true when the codepath was never reached)."
    }
}

# ===========================================================================
Write-Log ""
Write-Log "--- SUMMARY (AK4: an explicit signal, not something to infer from scrolling) ---"
# ===========================================================================
$logLines = Get-Content -Path $logPath
$aborts = $logLines | Select-String -Pattern '^\[ABORT'
$fails = $logLines | Select-String -Pattern '^\[FAIL'
$skips = $logLines | Select-String -Pattern '^\[SKIPPED'
# BF-followup (2026-08-26 audit): this run's own SUMMARY reported "SKIPPED lines: 0" while Step
# 10 had actually logged "[SKIPPED -- real, not a bug] reason=disabled..." -- the old pattern
# required an exact "[SKIPPED]" close bracket immediately, which ABORT/FAIL never did (both use
# a bare '^\[ABORT'/'^\[FAIL' prefix match), so any SKIPPED line with explanatory suffix text
# (like Step 10's own) silently evaded the tally. Same AN3/AL5 pattern as before: the harness's
# own counting logic, not the product, undercounting a real signal.
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
