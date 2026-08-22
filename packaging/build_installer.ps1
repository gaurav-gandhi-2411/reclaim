# Builds the Nuitka --standalone binary (bundled AI layer, Wave 1 P0-B) and the Inno Setup
# installer, end to end. Replaces the previously-manual process documented in README.md's
# "Building the Windows installer" section with a single reproducible, unattended-safe script.
#
# Three real incidents on 2026-07-30 motivate everything below that isn't just "run Nuitka":
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
#      flags can fix. Step 4's monitor loop watches for exactly this pattern (no new compiler
#      output for a while AND system CPU not actually busy) and aborts within minutes instead of
#      running blind for hours -- see packaging/RELEASE_RUNBOOK.md for the preconditions to
#      check BEFORE starting a real release build.
#   3. Even after both of the above were fixed, a third attempt aborted the same way -- and this
#      time it was genuinely contended, not a monitoring false-positive: the machine had 6+
#      other `claude` processes plus Docker Desktop and browser tabs live, confirmed via a live
#      process listing at the time of the abort. `--low-memory --jobs` were never set explicitly
#      before this (Nuitka picked its own default parallelism), which under contention just meant
#      more processes competing for the same starved cores. Once the machine is verified quiet
#      (Step 0), `--low-memory` is dropped and `--jobs` is raised deliberately -- see the
#      $NuitkaJobs default below.
#
# Usage: pwsh packaging/build_installer.ps1
# (Run from anywhere -- resolves paths relative to this script's own location.)

param(
    [string]$BuildVenvPath = "$PSScriptRoot\build\.venv-build",
    [string]$LogPath = "$PSScriptRoot\build\nuitka_build.log",
    [string]$TelemetryPath = "$PSScriptRoot\build\nuitka_build_telemetry.csv",
    [int]$StallTimeoutSeconds = 300, # no new compiler output for this long...
    [int]$StallCpuThresholdPercent = 60, # informational only now -- logged to telemetry, no longer
    # gates the abort (see $MinBuildCpuDeltaSeconds below for why: system-wide aggregate CPU% is
    # diluted by idle cores on a many-core box and false-triggered two real builds on 2026-08-04).
    [double]$MinBuildCpuDeltaSeconds = 2.0, # ...AND the build's OWN processes consumed less than
    # this many CPU-seconds in the last poll interval -> confirmed genuinely idle, not just
    # single-core-heavy compilation. On a genuinely quiet machine this margin is generous, not
    # tight: trivial modules should compile in low single-digit seconds, so 300s of true silence
    # combined with near-zero CPU from our own toolchain is already several times any expected
    # per-file compile time -- it stays armed as a safety net, not a tuned-tight gate.
    [double]$MinFreeRamGb = 0.3, # abort immediately if free RAM drops below this, regardless of
    # the stall-timeout check above -- a real, imminent thrashing risk, not just "slow". Was 2GB;
    # lowered after confirming (2026-08-04) this floor was pre-empting Windows' own paging rather
    # than catching real thrashing: the pagefile is auto-managed, C: has 93.8GB free for it to grow
    # into, and peak pagefile usage during the RAM collapses that tripped the old 2GB floor was
    # only ~1.3GB -- Windows had barely started leaning on it. The same single compilation unit
    # (almost certainly faiss's SWIG-generated Python wrapper, tens of thousands of lines that
    # Nuitka compiles into one giant C translation unit -- parallelism can't split it) reproducibly
    # collapsed free RAM from ~19GB to ~1-2GB in ~65s across three separate attempts regardless of
    # --jobs (6, 3, and 3 again), which is the signature of one file's real peak memory need
    # slightly exceeding physical RAM, not contention -- exactly the case auto-managed paging with
    # ample disk headroom exists to absorb. A true "about to crash" floor, not a comfort margin.
    [int]$PreflightMinFreeRamGb = 8, # refuse to even START a build without this much headroom.
    [int]$PollIntervalSeconds = 15,
    [int]$NuitkaJobs = 0, # 0 = auto: 6 if preflight free RAM >= 16GB, else 4. Pass explicitly to
    # override. Only meaningful on a verified-quiet machine (Step 0) -- raising parallelism under
    # real contention just adds more processes competing for the same starved cores.
    [switch]$SkipDefenderExclusions, # opt out of the Step 3 Add-MpPreference attempt entirely.
    [switch]$SkipCleanBuildDirs, # opt out of Step 5's unconditional entry_point.build/.dist
    # deletion, letting Nuitka/Scons reuse its own incremental object-file cache. Default OFF
    # (preserves the existing always-clean behavior for real release builds) -- only pass this
    # for a fast, narrowly-scoped re-verification build immediately after a prior run SUCCEEDED
    # and only Python-level import/inclusion flags changed (e.g. --nofollow-import-to scoping).
    # Do NOT use after any aborted/crashed/killed prior run: partial or corrupted intermediate
    # .o files from an interrupted build are exactly the stale state a full clean exists to rule
    # out, and Scons' own content-signature cache has no way to distinguish "unchanged since a
    # clean success" from "unchanged since a crash mid-write."
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

# System-wide aggregate CPU% is the wrong sensor for stall detection on a many-core box: a single
# huge translation unit (faiss's SWIG-generated C++ wrapper is the known case here) can peg ONE
# core at 100% while --jobs' other worker slots sit idle with no independent work left to do --
# that's genuine, expected compilation, but it averages down to a low-looking aggregate percentage
# and was observed directly causing false-positive aborts (2026-08-04: two separate builds aborted
# at 47-59% system CPU while free RAM was falling steadily and smoothly, 3GB over 5 minutes -- the
# unmistakable signature of active compilation, not an idle/hung process). This tracks the CPU-
# seconds consumed by the build's own toolchain processes; comparing it poll-to-poll tells apart
# "genuinely idle" from "one core legitimately grinding," independent of how many other cores
# happen to be idle.
#
# Sums by (PID, StartTime) into a script-scoped, never-shrinking table rather than summing
# currently-alive processes fresh each call. A naive "sum whoever's alive right now" reading is a
# gauge, not a counter: when a long-running heavy process (cc1plus compiling one huge file for
# minutes) finally exits between two polls, its accumulated CPU seconds vanish from that gauge in
# the same instant real progress happened -- producing a NEGATIVE delta that reads as "idle" at the
# exact moment a file just finished compiling. Observed directly on the first run of this detector
# (2026-08-04: delta of -8.8s, immediately re-triggering the false abort this function exists to
# prevent). Freezing each process's last-seen value once it exits, and never removing entries,
# makes the running total monotonically non-decreasing for the life of the build -- PROVIDED the
# key can't collide across different processes. Keying by PID alone isn't enough: Nuitka spawns
# many short-lived cc1.exe instances, so Windows recycles PIDs within seconds, and a bare PID key
# lets a brand-new lightweight process silently overwrite a just-exited heavy process's frozen
# high-water mark with its own low starting value -- observed immediately after the PID-only fix
# shipped (2026-08-04: delta of -35.3s on the very next run). Keying on (PID, StartTime) instead
# means a reused PID can never collide with the entry it's replacing.
function Get-BuildProcessCpuSeconds {
    param([string]$VenvPath)
    Get-Process -Name cc1, cc1plus, gcc, ld, collect2, "gcc-ranlib", "gcc-ar", windres -ErrorAction SilentlyContinue |
        ForEach-Object {
            try { $script:SeenBuildProcessCpu["$($_.Id)_$($_.StartTime.Ticks)"] = $_.CPU } catch {}
        }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($VenvPath) } |
        ForEach-Object {
            $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
            if ($p) {
                try { $script:SeenBuildProcessCpu["$($p.Id)_$($p.StartTime.Ticks)"] = $p.CPU } catch {}
            }
        }
    $total = 0.0
    foreach ($v in $script:SeenBuildProcessCpu.Values) { $total += $v }
    return $total
}
$script:SeenBuildProcessCpu = @{}

# --- Step 1: machine-quiet report (informational -- does not gate; Step 3's RAM check does) ------

Write-Output "==> [1/6] Machine state (see packaging/RELEASE_RUNBOOK.md's preconditions)..."
Write-Output "    Free RAM: $(Get-FreeRamGb)GB -- Instantaneous CPU: $(Get-CpuPercent)%"
$heavyProcs = Get-Process | Where-Object { $_.CPU -gt 30 } | Sort-Object CPU -Descending | Select-Object -First 10
if ($heavyProcs) {
    Write-Output "    Processes with >30s cumulative CPU time (lifetime counter, not live load):"
    foreach ($p in $heavyProcs) {
        Write-Output ("      {0,-24} PID {1,-8} CPU {2,10:N1}s  WS {3,8:N0}MB" -f $p.Name, $p.Id, $p.CPU, ($p.WS / 1MB))
    }
} else {
    Write-Output "    No processes found with >30s cumulative CPU time."
}

# --- Step 2: clean build venv, dev toolchain structurally excluded -------------------------------

# Retries Remove-Item -Recurse -Force with backoff -- Windows Defender's real-time scanner (never
# successfully excluded from packaging/build/ this session; every Add-MpPreference attempt in
# Step 4 below failed for lack of an elevated session) transiently locks freshly-written files
# under a venv's site-packages while it scans them, which surfaces as a hard "process cannot
# access the file" error on a plain Remove-Item -- observed twice in a row (2026-08-05) with no
# process actually holding the handle by the time it was checked afterward, the signature of a
# scanner that already released it. A handful of short retries rides out that window instead of
# failing the whole build on a transient condition outside our control.
function Remove-DirectoryWithRetry {
    param([string]$Path, [int]$MaxAttempts = 15, [int]$DelaySeconds = 10)
    # MaxAttempts=15 x DelaySeconds=10 = up to 150s of patience. Bumped from an initial 5x3s=15s
    # after that budget proved insufficient in practice (2026-08-05): confirmed no process holds
    # a loaded module from the target path (Get-Process | ... .Modules came back empty), and the
    # path isn't under OneDrive sync (a sibling folder, not a parent) -- Windows Defender's
    # real-time scanner (MsMpEng.exe, running; Step 4's exclusion attempt fails every run for
    # lack of an elevated session) is the remaining, much likelier explanation for a full
    # site-packages tree of freshly uv-synced files stinging worse than 15s of patience covers.
    for ($i = 1; $i -le $MaxAttempts; $i++) {
        if (-not (Test-Path $Path)) { return }
        try {
            Remove-Item -Recurse -Force $Path -Confirm:$false -ErrorAction Stop
            return
        } catch {
            if ($i -eq $MaxAttempts) { throw }
            Write-Output "    Remove-Item on $Path failed (attempt $i/$MaxAttempts): $($_.Exception.Message) -- retrying in ${DelaySeconds}s..."
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

Write-Output "==> [2/6] Provisioning a clean build venv (uv sync --extra ai --no-dev)..."
Remove-DirectoryWithRetry -Path $BuildVenvPath
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

Write-Output "    Patching bundled Nuitka: -O3 -> -O2 for the gcc backend compile..."
# On Windows, Nuitka's SconsCompilerSettings.py unconditionally picks -O3 for every gcc-compiled
# file ("if env.monolithpy or os.name == 'nt' or ..." -- os.name is always 'nt' here), with no
# CLI flag or working env-var override to lower it (importEnvironmentVariableSettings() reads
# CFLAGS but is dead code -- grepped the whole nuitka/ package tree, it's never called from
# Backend.scons or anywhere else). -O3's heavy inlining/template-instantiation is what pushed a
# single file (faiss's SWIG-generated swigfaiss.c, hundreds of thousands of lines) past ~23GB of
# peak compiler memory even with --low-memory and --jobs=1 alone (2026-08-04, run 6: steady
# decline from 22GB to the 0.3GB abort floor over ~400s of real, active compilation -- confirmed
# not a hang). -O2 is a legitimate release optimization level, not a debug build -- this patch is
# confined to this disposable, freshly-`uv sync`'d build venv (never touches reclaim's own Python
# logic or the shipped dependency wheels) and must be re-applied every run since the venv is
# recreated from scratch each time. Fails loudly if Nuitka's internals no longer match the
# expected text, rather than silently building at -O3 again on a future Nuitka version bump.
$sconsSettingsPath = "$BuildVenvPath\Lib\site-packages\nuitka\build\SconsCompilerSettings.py"
$patchResult = & "$BuildVenvPath\Scripts\python.exe" -c @'
import os
import sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
unpatched = (
    "            env.Append(\n"
    "                CCFLAGS=[\n"
    "                    (\n"
    "                        \"-O3\"\n"
    "                        if env.monolithpy or os.name == \"nt\" or not env.static_libpython\n"
    "                        else \"-O2\"\n"
    "                    )\n"
    "                ]\n"
    "            )"
)
patched_marker = (
    "            env.Append(\n"
    "                CCFLAGS=[\n"
    "                    (\n"
    "                        \"-O2\"\n"
    "                        if env.monolithpy or os.name == \"nt\" or not env.static_libpython\n"
    "                        else \"-O2\"\n"
    "                    )\n"
    "                ]\n"
    "            )"
)
# Idempotent: uv installs via hardlinks from its shared package cache (~/AppData/Local/uv/cache),
# not copies -- an earlier run's write here went through the hardlink and silently patched uv's
# CACHE, not just this venv's copy (confirmed via matching inode numbers, 2026-08-04). A later
# venv can therefore legitimately link to an already-patched cache entry; that's success, not the
# "Nuitka changed its internals" failure this check exists to catch, so it must be recognized
# explicitly rather than re-deriving the unpatched-target search from scratch.
if patched_marker in text:
    print("ALREADY_PATCHED")
    sys.exit(0)
count = text.count(unpatched)
if count != 1:
    print("PATCH_TARGET_NOT_FOUND:count=%d" % count)
    sys.exit(1)
patched = text.replace(unpatched, patched_marker)
# Write to a new temp file + os.replace() instead of open(path, "w") in place: replace() swaps
# the directory entry to a fresh inode rather than mutating the existing one, so it CANNOT repeat
# the hardlink-corrupts-the-shared-cache mistake above, regardless of how this file got here.
tmp_path = path + ".patchtmp"
with open(tmp_path, "w", encoding="utf-8") as f:
    f.write(patched)
os.replace(tmp_path, path)
print("PATCHED")
'@ $sconsSettingsPath
if ($LASTEXITCODE -ne 0) {
    throw ("Nuitka's SconsCompilerSettings.py no longer matches the expected -O3 block " +
        "($patchResult) -- Nuitka was likely upgraded and changed its internals. Re-derive the " +
        "patch against the new source before retrying; do NOT proceed and silently build at the " +
        "unpatched -O3, which is the exact memory wall this step exists to route around.")
}
Write-Output "    OK: $patchResult -- gcc backend compiles will now use -O2."

# --- Step 3: pre-flight contention check -----------------------------------------------------------

$preflightFreeRam = Get-FreeRamGb
Write-Output "==> [3/6] Pre-flight: ${preflightFreeRam}GB free RAM."
if ($preflightFreeRam -lt $PreflightMinFreeRamGb) {
    throw ("Only ${preflightFreeRam}GB free RAM before the build has even started (floor: " +
        "${PreflightMinFreeRamGb}GB) -- close other heavy sessions/workloads first. See " +
        "packaging/RELEASE_RUNBOOK.md for the expected preconditions. Refusing to start a " +
        "build that's already likely to stall under contention.")
}
if ($NuitkaJobs -le 0) {
    # A 4th real attempt (2026-08-04, 22:00 IST) proved --jobs alone can't fix this: on a
    # genuinely quiet machine (24.2GB free RAM, 1.2% CPU at start) with --jobs=3, cc1.exe crashed
    # with a literal "out of memory allocating 3398792 bytes" compiling module.faiss.swigfaiss.c
    # (faiss's SWIG-generated Python wrapper -- hundreds of thousands of lines, one C translation
    # unit that can't be split by parallelism) -- free RAM fell from ~20GB to 0.5GB over ~6 minutes
    # during that single file's compile, then rebounded to 22.6GB the instant cc1.exe died. This is
    # a genuine per-file memory ceiling, not contention: --jobs governs how many OTHER files
    # compile alongside this one, but this one file's own peak footprint is what crashed it
    # regardless. --jobs=1 removes concurrent competition for RAM while it compiles.
    $NuitkaJobs = 1
}
Write-Output "    Using --jobs=$NuitkaJobs for the Nuitka/Scons C compilation stage."

# --- Step 4: Windows Defender exclusions for build directories -----------------------------------
# Real-time scanning of thousands of generated .c/.o files (plus ccache/gcc's own downloaded
# toolchain under Nuitka's cache dir) is a real, measurable multiplier on Nuitka builds.
# Add-MpPreference requires an elevated (Run as Administrator) PowerShell session; this is
# attempted best-effort and never blocks the build if it fails or is skipped.

if ($SkipDefenderExclusions) {
    Write-Output "==> [4/6] Skipping Windows Defender exclusions (-SkipDefenderExclusions)."
} else {
    Write-Output "==> [4/6] Adding Windows Defender exclusions for build directories..."
    $defenderExclusionPaths = @(
        "$PSScriptRoot\build",
        "$env:LOCALAPPDATA\Nuitka\Nuitka\Cache"
    )
    try {
        foreach ($p in $defenderExclusionPaths) {
            Add-MpPreference -ExclusionPath $p -ErrorAction Stop
        }
        Write-Output "    OK: Defender real-time-scan exclusions added (elevated session) for:"
        foreach ($p in $defenderExclusionPaths) { Write-Output "      $p" }
    } catch {
        Write-Output ("    SKIPPED: Add-MpPreference failed -- {0}" -f $_.Exception.Message)
        Write-Output ("    This requires an elevated (Run as Administrator) PowerShell session. " +
            "Continuing without the exclusions; the build will still succeed, just without this " +
            "speedup. Re-run from an elevated prompt to get it.")
    }
}

# --- Step 5: Nuitka standalone build, monitored for contention-driven stalls ---------------------

Write-Output "==> [5/6] Running Nuitka standalone build (bundled AI layer)..."
if ($SkipCleanBuildDirs) {
    Write-Output "    Skipping entry_point.build/.dist cleanup (-SkipCleanBuildDirs) -- reusing Scons' incremental object-file cache."
} else {
    Remove-DirectoryWithRetry -Path "$PSScriptRoot\build\entry_point.build"
    Remove-DirectoryWithRetry -Path "$PSScriptRoot\build\entry_point.dist"
}
if (Test-Path $LogPath) { Remove-Item $LogPath -Force }
"timestamp,elapsed_s,free_ram_gb,cpu_percent,build_cpu_delta_s,seconds_since_last_output,last_log_line" |
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
    "--low-memory", # Real, targeted fix (not a contention workaround -- see $NuitkaJobs comment
    # above): in this Nuitka version its only actual effect is dropping gcc's -pipe flag, which
    # forces intermediate compiler state to disk instead of an in-memory buffer. That's exactly
    # what a single, huge, generated-C translation unit (faiss's swigfaiss.c) needs to not OOM.
    "--jobs=$NuitkaJobs",
    # NOT excluding *.tests/*.testing modules at all, despite the compile-time/size cost of
    # bundling real test-suite directories. Both wildcards were tried and both hid a genuine,
    # unconditionally-imported runtime dependency behind an innocent-looking "test" name --
    # confirmed independently for THREE separate packages, not a one-off: structlog/__init__.py
    # does `from structlog.testing import ReturnLogger, ReturnLoggerFactory` (public API, listed
    # in __all__); jinja2/defaults.py does `from . import tests` for jinja2's own built-in
    # template test functions (is_defined/is_none/etc. -- a real Jinja2 language feature, just
    # named "tests"); scipy/_lib/_array_api.py -- scipy's own internal array-API compat shim,
    # imported by many scipy submodules at their own module load time -- does `from
    # scipy._external.array_api_extra.testing import lazy_xp_function` at top level (2026-08-05:
    # verified via `ast.walk` over every .py file in the build venv's site-packages, checking for
    # ImportFrom/Import nodes referencing a *.tests/*.testing module from OUTSIDE that module's
    # own tests/testing directory -- 10 hits total, 3 of them genuine runtime code, not test
    # collection). A 4th, `numpy.testing`, WAS confirmed safe (only reachable via numpy's own
    # lazy `__getattr__` in numpy/__init__.py, never imported at module-load time) -- but three
    # false negatives out of four checked is not good enough odds to keep guessing package by
    # package. Losing the exclusion costs a real but bounded amount of dist-folder size and
    # compile time (each test-suite dir Nuitka would otherwise skip); shipping a CLI/server that
    # crashes on startup for a data-safety tool is not an acceptable trade for that saving.
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
    # windows_toasts (PR #38, R5 disk-space toast) + winrt: without these, a real installed
    # build fired `ModuleNotFoundError: No module named 'winrt.windows.foundation'` at toast
    # time (confirmed via a real traceback in the app's own reclaim.log). Root cause: Nuitka's
    # default standalone build only follows imports it can find via static AST analysis of
    # Python source. `windows_toasts` itself IS statically imported (reclaim.notifications'
    # deferred `from windows_toasts import ...` inside send_disk_space_toast, plus
    # windows_toasts' own static `from winrt.windows.ui.notifications import ...` and `from
    # winrt.windows.data.xml.dom import ...`) -- so those three winrt pieces get bundled by
    # accident. But `winrt.windows.foundation` (HResult/AsyncStatus, needed for the toast's
    # async activation/button-click machinery) is only reached at runtime from *inside* the
    # native `_winrt_windows_ui_notifications.pyd` extension module's own C-level import call,
    # invisible to Nuitka's Python-source scan -- so it, and the sibling
    # `winrt.windows.foundation.collections`, were silently dropped from every prior build.
    # `winrt` itself is a PEP 420 implicit namespace package (no `__init__.py`) assembled from
    # five separate `winrt-*` PyPI wheels that all install into one shared `winrt/` directory
    # in site-packages -- `--include-package=winrt` walks that whole merged directory
    # unconditionally, independent of what's statically reachable, which is exactly the
    # guarantee this dynamic-import gap needs. `--include-package=windows_toasts` is added
    # explicitly too (not left to the incidental discovery above) to match this list's existing
    # practice of naming every runtime dependency it needs rather than relying on transitive
    # discovery. Verified at small scale (not via a full installer rebuild -- see
    # RELEASE_RUNBOOK.md/PLAN.md for why): a scoped Nuitka --standalone compile of an isolated
    # script mirroring reclaim.notifications.send_disk_space_toast's exact import/call sequence
    # reproduced the identical `ModuleNotFoundError: No module named 'winrt.windows.foundation'`
    # without these two flags, and resolved cleanly (toast fired, no exception) with them.
    "--include-package=winrt", "--include-package=windows_toasts",
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
$lastBuildCpuSeconds = 0.0
$buildObjDir = "$PSScriptRoot\build\entry_point.build"

# Object-file mtime is a second, independent "is real work happening" signal, alongside
# buildCpuDeltaThisPoll -- added after Get-BuildProcessCpuSeconds was caught giving a false
# "genuinely idle" verdict on two consecutive, actually-healthy builds (2026-08-04, --jobs=1
# runs). Root cause: that function is a POINT-IN-TIME sampler -- it only sees a cc1/gcc process
# if it happens to still be alive at the instant of a 15s poll. At --jobs=1, Nuitka compiles
# hundreds of files serially and most are small enough to start and fully exit BETWEEN two
# polls, so their entire CPU consumption is invisible to the sampler even while real compiles
# are completing every few seconds -- confirmed directly by a live process-tree watch (cc1.exe
# PID churning roughly every 30s, memory climbing within each invocation) alongside a build
# directory where 22 of 33 .o files had been modified in the prior 2 minutes, while the sampler
# was reporting ~0 build CPU per poll the entire time. This function was written and tuned
# against the --jobs=3+ case (a few long-lived parallel compiles, each spanning many poll
# intervals) and was never validated against --jobs=1's many-short-lived-processes shape --
# exactly the kind of control whose real surface is narrower than its name implies. Object
# files on disk are a durable, poll-timing-immune progress signal: a file that's done stays
# done regardless of whether any process was alive at the instant we happened to look.
function Get-NewestObjectWriteTime {
    param([string]$Dir)
    if (-not (Test-Path $Dir)) { return $null }
    $newest = Get-ChildItem -Path $Dir -Filter "*.o" -Recurse -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest) { return $newest.LastWriteTime }
    return $null
}

while (-not $proc.HasExited) {
    Start-Sleep -Seconds $PollIntervalSeconds
    $elapsed = (Get-Date) - $startTime
    $freeRamGb = Get-FreeRamGb
    $cpuPercent = Get-CpuPercent
    $currentBuildCpuSeconds = Get-BuildProcessCpuSeconds -VenvPath $BuildVenvPath
    $buildCpuDeltaThisPoll = [math]::Round($currentBuildCpuSeconds - $lastBuildCpuSeconds, 1)
    $lastBuildCpuSeconds = $currentBuildCpuSeconds

    # Engineering fix for verified contention against other Normal-priority workloads on this
    # shared machine (2026-08-04: a long-running SargamSa recompute job intermittently burst CPU
    # usage, dropping system-wide CPU below the stall threshold for 300+s and false-triggering
    # the abort below even though the compiler itself hadn't stalled). Rather than depending on
    # the machine being fully quiet for the whole build, give the compiler toolchain preferential
    # scheduling: AboveNormal preempts default-priority processes for CPU time without the
    # starvation risk of High/Realtime. Re-applied every poll cycle because compiler subprocesses
    # (cc1/gcc/scons) come and go throughout the build and do not inherit priority from a
    # previously-boosted parent -- Windows assigns NORMAL_PRIORITY_CLASS to new child processes
    # regardless of the parent's class unless the parent explicitly requests otherwise.
    try {
        Get-Process -Name cc1, cc1plus, gcc, ld, collect2, "gcc-ranlib", "gcc-ar", windres -ErrorAction SilentlyContinue |
            Where-Object { $_.PriorityClass -ne 'AboveNormal' } |
            ForEach-Object { try { $_.PriorityClass = 'AboveNormal' } catch {} }
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains($BuildVenvPath) } |
            ForEach-Object {
                try { (Get-Process -Id $_.ProcessId -ErrorAction Stop).PriorityClass = 'AboveNormal' } catch {}
            }
    } catch {}

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

    "$(Get-Date -Format o),$([math]::Round($elapsed.TotalSeconds,1)),$freeRamGb,$cpuPercent,$buildCpuDeltaThisPoll,$([math]::Round($sinceLastWrite,1)),`"$lastLine`"" |
        Out-File -FilePath $TelemetryPath -Append -Encoding utf8

    $newestObjWrite = Get-NewestObjectWriteTime -Dir $buildObjDir
    $sinceLastObjWrite = if ($newestObjWrite) { ((Get-Date) - $newestObjWrite).TotalSeconds } else { $elapsed.TotalSeconds }

    if ($freeRamGb -lt $MinFreeRamGb) {
        $abortReason = "free RAM dropped to ${freeRamGb}GB (floor: ${MinFreeRamGb}GB) -- real thrashing risk"
        $aborted = $true
        break
    }
    if ($sinceLastWrite -gt $StallTimeoutSeconds -and $buildCpuDeltaThisPoll -lt $MinBuildCpuDeltaSeconds -and
        $sinceLastObjWrite -gt $StallTimeoutSeconds) {
        $abortReason = ("no compiler output for $([math]::Round($sinceLastWrite))s, the build's own " +
            "processes consumed only ${buildCpuDeltaThisPoll}s of CPU in the last ${PollIntervalSeconds}s " +
            "poll (floor: ${MinBuildCpuDeltaSeconds}s, system-wide CPU was ${cpuPercent}%), AND no .o file " +
            "under $buildObjDir has been written in $([math]::Round($sinceLastObjWrite))s -- genuinely " +
            "idle on all three signals, not just single-core-heavy or many-short-lived-process " +
            "compilation on a many-core box")
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

# --- Step 6: Inno Setup packaging -----------------------------------------------------------------

Write-Output "==> [6/6] Packaging with Inno Setup..."
if (-not (Test-Path $InnoSetupCompiler)) {
    throw "Inno Setup compiler not found at $InnoSetupCompiler -- pass -InnoSetupCompiler with the correct path."
}
& $InnoSetupCompiler "$PSScriptRoot\reclaim.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC.exe failed (exit $LASTEXITCODE)" }

$installerPath = "$PSScriptRoot\dist\reclaim-setup.exe"
if (-not (Test-Path $installerPath)) { throw "Expected installer not found at $installerPath" }
$installerSizeBytes = (Get-Item $installerPath).Length

# SHA-256 checksum sidecar -- was a manual, easily-forgotten post-build step (see
# RELEASE_RUNBOOK.md's former "Publishing: SHA-256 checksum sidecar (manual...)" section);
# automated here so every build produces one without relying on someone remembering at publish
# time. Byte-format matches all four real published sidecars (v1.0.0-v1.3.0), verified against
# v1.3.0's actual 84-byte asset: lowercase hex, exactly two spaces, the bare filename with no
# path, and a trailing LF only -- WriteAllText with a BOM-less UTF8Encoding and an explicit `n
# (not Set-Content, not `r`n). A BOM or CRLF would make `sha256sum -c` fail for anyone verifying
# on Linux/macOS/WSL.
$installerFileName = Split-Path -Leaf $installerPath
$sha256Path = "$installerPath.sha256"
$installerHash = (Get-FileHash $installerPath -Algorithm SHA256).Hash.ToLower()
[System.IO.File]::WriteAllText($sha256Path, "$installerHash  $installerFileName`n", [System.Text.UTF8Encoding]::new($false))

Write-Output ""
Write-Output "==> DONE."
Write-Output ("    Dist folder (bundled AI layer + models): {0:N1} MB" -f ($distSizeBytes / 1MB))
Write-Output ("    Final installer: {0:N1} MB -- $installerPath" -f ($installerSizeBytes / 1MB))
Write-Output "    SHA-256: $installerHash  $installerFileName"
Write-Output "    Checksum sidecar: $sha256Path"
Write-Output "    Build telemetry: $TelemetryPath"
