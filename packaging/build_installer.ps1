# Builds the Nuitka --standalone binary (bundled AI layer, Wave 1 P0-B) and the Inno Setup
# installer, end to end. Replaces the previously-manual process documented in README.md's
# "Building the Windows installer" section with a single reproducible, unattended-safe script.
#
# Two real incidents on 2026-07-30 motivate everything below that isn't just "run Nuitka":
#   1. A build venv `uv sync --extra ai`'d WITHOUT `--no-dev` silently carried the whole dev
#      toolchain (mypy -- including its mypyc-compiled internals -- pytest, ruff, pyarrow,
#      ollama) into the compile. Nuitka tried to compile all of it (on top of faiss's already-
#      huge SWIG-generated C++ wrapper), and `cc1.exe` ran out of memory. Step 1 below makes
#      this class of mistake structurally impossible: the build venv is always provisioned with
#      `--no-dev`, and a hard assertion checks mypy/pytest/ruff are genuinely unimportable in it
#      BEFORE Nuitka ever starts -- a contaminated venv fails loudly here, in seconds, not
#      silently two hours into a doomed compile.
#   2. A clean retry still stalled badly: small, trivial files (a few dozen lines of FastAPI
#      middleware) took 1,300-3,000+ seconds each to compile, while system-wide CPU sat at only
#      ~33% utilized and free RAM fell from 16.7GB to 9.4GB -- the signature of a shared
#      dev-machine under contention from OTHER concurrent workloads, not something Nuitka's own
#      flags can fix. Step 3's monitor loop watches for exactly this pattern (no new compiler
#      output for a while AND system CPU not actually busy) and aborts within minutes instead of
#      running blind for hours -- see packaging/RELEASE_RUNBOOK.md for the preconditions to
#      check BEFORE starting a real release build.
#
# Usage: pwsh packaging/build_installer.ps1
# (Run from anywhere -- resolves paths relative to this script's own location.)

param(
    [string]$BuildVenvPath = "$PSScriptRoot\build\.venv-build",
    [string]$LogPath = "$PSScriptRoot\build\nuitka_build.log",
    [string]$TelemetryPath = "$PSScriptRoot\build\nuitka_build_telemetry.csv",
    [int]$StallTimeoutSeconds = 300, # no new compiler output for this long...
    [int]$StallCpuThresholdPercent = 60, # ...AND system CPU below this -> confirmed contention.
    [int]$MinFreeRamGb = 2, # abort immediately if free RAM drops below this, regardless of the
    # stall-timeout check above -- a real, imminent thrashing risk, not just "slow".
    [int]$PreflightMinFreeRamGb = 8, # refuse to even START a build without this much headroom.
    [int]$PollIntervalSeconds = 15,
    [string]$InnoSetupCompiler = "C:\Program Files\Inno Setup 7\ISCC.exe"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $RepoRoot
New-Item -ItemType Directory -Force -Path "$PSScriptRoot\build" | Out-Null

function Get-FreeRamGb {
    $os = Get-CimInstance Win32_OperatingSystem
    return [math]::Round($os.FreePhysicalMemory / 1MB, 1)
}

function Get-CpuPercent {
    try {
        return [math]::Round(
            (Get-Counter '\Processor(_Total)\% Processor Time' -ErrorAction Stop).CounterSamples[0].CookedValue, 1
        )
    } catch {
        return -1
    }
}

# --- Step 1: clean build venv, dev toolchain structurally excluded ------------------------------

Write-Output "==> [1/4] Provisioning a clean build venv (uv sync --extra ai --no-dev)..."
if (Test-Path $BuildVenvPath) { Remove-Item -Recurse -Force $BuildVenvPath -Confirm:$false }
$env:UV_PROJECT_ENVIRONMENT = $BuildVenvPath
uv sync --extra ai --no-dev
if ($LASTEXITCODE -ne 0) { throw "uv sync --extra ai --no-dev failed (exit $LASTEXITCODE)" }
uv pip install --python "$BuildVenvPath\Scripts\python.exe" nuitka
if ($LASTEXITCODE -ne 0) { throw "nuitka install into the build venv failed (exit $LASTEXITCODE)" }

Write-Output "    Asserting the dev toolchain is genuinely absent..."
$devCheckOutput = & "$BuildVenvPath\Scripts\python.exe" -c @'
import sys
contaminated = []
for mod in ("mypy", "pytest", "ruff"):
    try:
        __import__(mod)
        contaminated.append(mod)
    except ImportError:
        pass
if contaminated:
    print("CONTAMINATED:" + ",".join(contaminated))
    sys.exit(1)
print("CLEAN")
'@
if ($LASTEXITCODE -ne 0) {
    throw ("Build venv is contaminated with dev-toolchain package(s): $devCheckOutput -- this is " +
        "exactly the bug that caused the 2026-07-30 out-of-memory failure (Nuitka tried to " +
        "compile mypy's mypyc-accelerated internals). Delete $BuildVenvPath and investigate " +
        "before retrying -- never proceed past this check, even manually.")
}
Write-Output "    OK: mypy/pytest/ruff confirmed unimportable in the build venv."

# --- Step 2: pre-flight contention check ---------------------------------------------------------

$preflightFreeRam = Get-FreeRamGb
Write-Output "==> [2/4] Pre-flight: ${preflightFreeRam}GB free RAM."
if ($preflightFreeRam -lt $PreflightMinFreeRamGb) {
    throw ("Only ${preflightFreeRam}GB free RAM before the build has even started (floor: " +
        "${PreflightMinFreeRamGb}GB) -- close other heavy sessions/workloads first. See " +
        "packaging/RELEASE_RUNBOOK.md for the expected preconditions. Refusing to start a " +
        "build that's already likely to stall under contention.")
}

# --- Step 3: Nuitka standalone build, monitored for contention-driven stalls ---------------------

Write-Output "==> [3/4] Running Nuitka standalone build (bundled AI layer)..."
if (Test-Path "$PSScriptRoot\build\entry_point.build") {
    Remove-Item -Recurse -Force "$PSScriptRoot\build\entry_point.build" -Confirm:$false
}
if (Test-Path "$PSScriptRoot\build\entry_point.dist") {
    Remove-Item -Recurse -Force "$PSScriptRoot\build\entry_point.dist" -Confirm:$false
}
if (Test-Path $LogPath) { Remove-Item $LogPath -Force }
"timestamp,elapsed_s,free_ram_gb,cpu_percent,seconds_since_last_output,last_log_line" |
    Out-File -FilePath $TelemetryPath -Encoding utf8

# A plain array handed to Start-Process -ArgumentList gets re-joined into a single command-line
# STRING before Windows ever sees it -- an element containing a space ("Gaurav Gandhi") silently
# splits back into two separate argv tokens, which is exactly what broke the first run of this
# script (Nuitka saw "Gandhi" and "packaging/entry_point.py" as two stray positional arguments
# and refused to start: "specify only one positional argument"). `ProcessStartInfo.ArgumentList`
# (a real, ordered collection, not a string to re-parse) is the correct, quoting-bug-free way to
# pass argv on .NET -- used here instead.
$nuitkaArgs = @(
    "-m", "nuitka", "--standalone", "--assume-yes-for-downloads",
    "--low-memory",
    "--nofollow-import-to=*.tests", "--nofollow-import-to=*.testing",
    "--company-name=Gaurav Gandhi", "--product-name=Reclaim", "--product-version=1.3.0",
    "--windows-icon-from-ico=packaging/reclaim.ico",
    "--windows-console-mode=attach",
    "--include-package=reclaim", "--include-package=uvicorn", "--include-package=fastapi",
    "--include-package=starlette",
    "--include-package=onnxruntime", "--include-package=tokenizers", "--include-package=faiss",
    "--include-package=lightgbm", "--include-package=imagehash", "--include-package=PIL",
    "--include-package=cv2", "--include-package=numpy", "--include-package=datasketch",
    "--include-package=docx", "--include-package=pypdf", "--include-package=rapidocr_onnxruntime",
    "--include-package=scipy",
    "--include-data-dir=src/reclaim/api/static=reclaim/api/static",
    "--include-data-dir=src/reclaim/api/templates=reclaim/api/templates",
    "--include-data-dir=src/reclaim/ai/models=reclaim/ai/models",
    "--output-dir=packaging/build", "--output-filename=reclaim.exe",
    "packaging/entry_point.py"
)

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "$BuildVenvPath\Scripts\python.exe"
foreach ($a in $nuitkaArgs) { $psi.ArgumentList.Add($a) }
$psi.WorkingDirectory = $RepoRoot
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.UseShellExecute = $false

$logWriter = [System.IO.StreamWriter]::new($LogPath, $false)
$logWriter.AutoFlush = $true
$errWriter = [System.IO.StreamWriter]::new("$LogPath.stderr", $false)
$errWriter.AutoFlush = $true

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action {
    if ($EventArgs.Data) { $Event.MessageData.WriteLine($EventArgs.Data) }
} -MessageData $logWriter | Out-Null
Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action {
    if ($EventArgs.Data) { $Event.MessageData.WriteLine($EventArgs.Data) }
} -MessageData $errWriter | Out-Null
$proc.Start() | Out-Null
$proc.BeginOutputReadLine()
$proc.BeginErrorReadLine()

$startTime = Get-Date
$aborted = $false
$abortReason = ""
while (-not $proc.HasExited) {
    Start-Sleep -Seconds $PollIntervalSeconds
    $elapsed = (Get-Date) - $startTime
    $freeRamGb = Get-FreeRamGb
    $cpuPercent = Get-CpuPercent
    # Nuitka writes almost everything (inclusion warnings, plugin notices, and its own progress
    # lines) to STDERR, not stdout -- $LogPath (stdout) stays empty for the whole build. Watching
    # only $LogPath made every build look permanently stalled from t=0, which is exactly what
    # falsely killed the first monitored run (entry_point.build had 1 file / 1KB -- Nuitka was
    # still in its legitimately slow, single-core, low-CPU module-graph-building phase, not
    # actually stalled). Track the newer of the two files' write times instead.
    $stderrPath = "$LogPath.stderr"
    $lastWriteOut = (Get-Item $LogPath -ErrorAction SilentlyContinue).LastWriteTime
    $lastWriteErr = (Get-Item $stderrPath -ErrorAction SilentlyContinue).LastWriteTime
    $lastWrite = @($lastWriteOut, $lastWriteErr) | Where-Object { $_ } | Sort-Object -Descending | Select-Object -First 1
    $sinceLastWrite = if ($lastWrite) { ((Get-Date) - $lastWrite).TotalSeconds } else { $elapsed.TotalSeconds }
    $lastLine = if (Test-Path $stderrPath) {
        (Get-Content $stderrPath -Tail 1 -ErrorAction SilentlyContinue) -join " "
    } elseif (Test-Path $LogPath) {
        (Get-Content $LogPath -Tail 1 -ErrorAction SilentlyContinue) -join " "
    } else { "" }

    "$(Get-Date -Format o),$([math]::Round($elapsed.TotalSeconds,1)),$freeRamGb,$cpuPercent,$([math]::Round($sinceLastWrite,1)),`"$lastLine`"" |
        Out-File -FilePath $TelemetryPath -Append -Encoding utf8

    if ($freeRamGb -lt $MinFreeRamGb) {
        $abortReason = "free RAM dropped to ${freeRamGb}GB (floor: ${MinFreeRamGb}GB) -- real thrashing risk"
        $aborted = $true
        break
    }
    if ($sinceLastWrite -gt $StallTimeoutSeconds -and $cpuPercent -ge 0 -and $cpuPercent -lt $StallCpuThresholdPercent) {
        $abortReason = ("no compiler output for $([math]::Round($sinceLastWrite))s while system CPU is only " +
            "${cpuPercent}% -- this is machine contention, not legitimate heavy compilation " +
            "(a real heavy file, like faiss's SWIG wrapper, keeps a CPU core near 100% the " +
            "whole time it's compiling)")
        $aborted = $true
        break
    }
}

function Stop-BuildMonitoring {
    Get-EventSubscriber -ErrorAction SilentlyContinue | Where-Object { $_.SourceObject -eq $proc } |
        Unregister-Event -ErrorAction SilentlyContinue
    try { $logWriter.Flush(); $logWriter.Close() } catch {}
    try { $errWriter.Flush(); $errWriter.Close() } catch {}
}

if ($aborted) {
    Write-Output "==> ABORTING: $abortReason"
    try { $proc.Kill($true) } catch {}
    Stop-BuildMonitoring
    Get-Process -Name cc1, cc1plus, gcc, ccache, scons -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    # $proc.Kill($true) is documented to kill the whole descendant tree, but was observed NOT to
    # in practice (2026-07-30: Nuitka's re-exec'd worker interpreter survived the kill and kept
    # running in the background after the script had already thrown). Belt-and-suspenders: sweep
    # for any leftover python.exe still running out of this build's own venv, by command line,
    # regardless of what $proc.Kill(true) claims to have done.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($BuildVenvPath) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    throw ("Build aborted after $([math]::Round(((Get-Date)-$startTime).TotalMinutes,1)) minutes " +
        "due to detected machine contention: $abortReason. Full timeline: $TelemetryPath. " +
        "Retry when the machine is quiet -- see packaging/RELEASE_RUNBOOK.md's preconditions.")
}
Stop-BuildMonitoring

if ($proc.ExitCode -ne 0) {
    throw "Nuitka build failed (exit $($proc.ExitCode)) -- see $LogPath / $LogPath.stderr"
}
$buildMinutes = [math]::Round(((Get-Date) - $startTime).TotalMinutes, 1)
Write-Output "    Nuitka build succeeded in $buildMinutes minutes."

$distDir = "$PSScriptRoot\build\entry_point.dist"
$distSizeBytes = (Get-ChildItem $distDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Output ("    Dist folder size: {0:N1} MB" -f ($distSizeBytes / 1MB))

# --- Step 4: Inno Setup packaging -----------------------------------------------------------------

Write-Output "==> [4/4] Packaging with Inno Setup..."
if (-not (Test-Path $InnoSetupCompiler)) {
    throw "Inno Setup compiler not found at $InnoSetupCompiler -- pass -InnoSetupCompiler with the correct path."
}
& $InnoSetupCompiler "$PSScriptRoot\reclaim.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC.exe failed (exit $LASTEXITCODE)" }

$installerPath = "$PSScriptRoot\dist\reclaim-setup.exe"
if (-not (Test-Path $installerPath)) { throw "Expected installer not found at $installerPath" }
$installerSizeBytes = (Get-Item $installerPath).Length

Write-Output ""
Write-Output "==> DONE."
Write-Output ("    Dist folder (bundled AI layer + models): {0:N1} MB" -f ($distSizeBytes / 1MB))
Write-Output ("    Final installer: {0:N1} MB -- $installerPath" -f ($installerSizeBytes / 1MB))
Write-Output "    Build telemetry: $TelemetryPath"
