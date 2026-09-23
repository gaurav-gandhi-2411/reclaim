# Frozen serve + scan + AI smoke test -- run against the ACTUAL Nuitka --standalone build.
#
# Why this exists: test_packaged_safe_mode.ps1 only drives CLI subcommands, so it passed on the
# 2026-08-05 build whose `*.tests`/`*.testing` exclusion crashed `reclaim.exe serve` on every
# start (RELEASE_RUNBOOK.md). Now that named test-suite exclusions are back
# (packaging/nofollow_allowlist.txt), this is the runtime half of that gate:
# scripts/check_nofollow_allowlist.py sees only Python source, and an import made from inside a
# compiled .pyd (the winrt.windows.foundation shape) is only caught by running the exe.
#
# What it proves, in the packaged exe:
#   1. `reclaim.exe serve` starts and serves the dashboard (FastAPI + jinja2 template render --
#      jinja2.tests is imported by jinja2 at runtime).
#   2. One real scan completes through POST /api/scan.
#   3. One AI analysis completes with no pipeline skipped for an unexpected error, and the
#      lightgbm clutter ranker runs. That path imports imagehash (-> scipy.fftpack),
#      datasketch (-> scipy.integrate), and lightgbm (-> scipy.sparse, narwhals). ai_orchestration
#      turns pipeline exceptions into "skipped" entries rather than failing, so a
#      ModuleNotFoundError there would show up only as a skip reason. That is why the skip
#      reasons are asserted on explicitly.
#
# Usage: pwsh -File packaging\test_packaged_serve.ps1 -DistDir packaging\build\entry_point.dist
# ASCII-only, same reason as test_packaged_safe_mode.ps1 (Windows PowerShell 5.1 parsing).

param(
    [string]$DistDir = "packaging\build\entry_point.dist",
    [int]$StartupTimeoutSeconds = 120,
    [int]$ScanTimeoutSeconds = 180,
    [int]$AiTimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
$exe = Join-Path (Resolve-Path $DistDir) "reclaim.exe"
if (-not (Test-Path $exe)) { throw "reclaim.exe not found at $exe -- build it first." }

# Under %TEMP% (inside the user profile): POST /api/scan needs no outside-home token there.
$work = Join-Path $env:TEMP ("reclaim_serve_smoke_" + [guid]::NewGuid().ToString("N"))
$fixture = Join-Path $work "fixture"
New-Item -ItemType Directory -Path $fixture -Force | Out-Null
Write-Host "== Work dir: $work ==" -ForegroundColor Cyan

$fail = $false
function Check($name, $cond) {
    if ($cond) { Write-Host "PASS: $name" -ForegroundColor Green }
    else { Write-Host "FAIL: $name" -ForegroundColor Red; $script:fail = $true }
}

# --- Fixture: near-identical images + near-duplicate documents ---------------------------------
Add-Type -AssemblyName System.Drawing
foreach ($i in 1..2) {
    $bmp = New-Object System.Drawing.Bitmap 256, 256
    for ($x = 0; $x -lt 256; $x += 4) {
        for ($y = 0; $y -lt 256; $y += 4) {
            $c = [System.Drawing.Color]::FromArgb(255, $x, $y, (($x + $y) % 256))
            for ($dx = 0; $dx -lt 4; $dx++) { for ($dy = 0; $dy -lt 4; $dy++) { $bmp.SetPixel($x + $dx, $y + $dy, $c) } }
        }
    }
    if ($i -eq 2) { $bmp.SetPixel(0, 0, [System.Drawing.Color]::White) }  # near-identical, not byte-identical
    $bmp.Save((Join-Path $fixture "photo_$i.png"), [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
}
# Two visually distinct images the near-identical pass will NOT cluster. semantic_image (CLIP via
# onnxruntime) only embeds residual images, so without these it "ran" with nothing to embed.
# That is how the 2026-09-23 broken build still reported semantic_image in tracks_run while
# onnxruntime could not load at all.
foreach ($spec in @(@{ Name = "stripes"; Mod = 16 }, @{ Name = "checks"; Mod = 64 })) {
    $bmp = New-Object System.Drawing.Bitmap 256, 256
    for ($x = 0; $x -lt 256; $x++) {
        for ($y = 0; $y -lt 256; $y += 2) {
            $on = if ($spec.Name -eq "stripes") { ($x % $spec.Mod) -lt ($spec.Mod / 2) } else { ((($x / $spec.Mod) -bxor ($y / $spec.Mod)) -band 1) -eq 1 }
            $c = if ($on) { [System.Drawing.Color]::FromArgb(255, 200, 30, 30) } else { [System.Drawing.Color]::FromArgb(255, 20, 20, 220) }
            $bmp.SetPixel($x, $y, $c); $bmp.SetPixel($x, $y + 1, $c)
        }
    }
    $bmp.Save((Join-Path $fixture "$($spec.Name).png"), [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
}
$para = ("The quarterly storage review covers every shared drive, archive volume, and backup " +
    "target the team maintains. Each section lists current usage, growth over the last period, " +
    "and the retention rule that applies. ") * 12
Set-Content -Path (Join-Path $fixture "report_v1.txt") -Value $para -Encoding ascii
Set-Content -Path (Join-Path $fixture "report_v2.txt") -Value ($para + " Final revision.") -Encoding ascii

# --- 1. serve starts ------------------------------------------------------------------------------
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
$base = "http://127.0.0.1:$port"
$serverArgs = "serve --port $port --db `"$work\index.sqlite3`" --config `"$work\config.toml`""
$server = Start-Process -FilePath $exe -ArgumentList $serverArgs -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput "$work\serve_stdout.txt" -RedirectStandardError "$work\serve_stderr.txt"

try {
    $page = $null
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline -and -not $server.HasExited) {
        try { $page = Invoke-WebRequest -Uri "$base/" -UseBasicParsing -TimeoutSec 5; break }
        catch { Start-Sleep -Milliseconds 500 }
    }
    Check "serve answered GET / with 200 within ${StartupTimeoutSeconds}s" ($page -and $page.StatusCode -eq 200)
    if (-not $page) { throw "server never came up (exited: $($server.HasExited))" }
    $csrf = [regex]::Match($page.Content, 'name="reclaim-csrf-token" content="([^"]+)"').Groups[1].Value
    Check "dashboard rendered a CSRF token (template render path works)" ([bool]$csrf)
    $headers = @{ "x-reclaim-csrf-token" = $csrf }

    # --- 2. one scan completes ---------------------------------------------------------------------
    $body = @{ path = $fixture } | ConvertTo-Json
    $null = Invoke-RestMethod -Method Post -Uri "$base/api/scan" -Headers $headers `
        -ContentType "application/json" -Body $body
    $scan = $null
    $deadline = (Get-Date).AddSeconds($ScanTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $scan = Invoke-RestMethod -Uri "$base/api/scan/status"
    } while ($scan.status -eq "running" -and (Get-Date) -lt $deadline)
    Check "scan completed (status=$($scan.status), error=$($scan.error))" ($scan.status -eq "completed")
    Check "scan indexed the fixture files (files_written=$($scan.files_written))" ($scan.files_written -ge 6)

    # --- 3. one AI analysis completes, no import-shaped skips ----------------------------------------
    $ai = Invoke-RestMethod -Method Post -Uri "$base/api/ai/analyze" -Headers $headers
    Check "AI layer available in the packaged build (status=$($ai.status), reason=$($ai.unavailable_reason))" `
        ($ai.status -ne "unavailable")
    $deadline = (Get-Date).AddSeconds($AiTimeoutSeconds)
    while ($ai.status -eq "running" -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        $ai = Invoke-RestMethod -Uri "$base/api/ai/status"
    }
    # A response missing these fields would make every skip/track assertion below vacuous
    # (@($null | Where-Object ...) has Count 0), so assert the shape first.
    $aiFields = $ai.PSObject.Properties.Name
    Check "AI status response carries tracks_run and tracks_skipped" `
        (($aiFields -contains "tracks_run") -and ($aiFields -contains "tracks_skipped"))
    Write-Host ("    tracks_run: " + ($ai.tracks_run -join ", "))
    foreach ($s in $ai.tracks_skipped) { Write-Host "    skipped: $($s.track) -- $($s.reason)" }
    Check "AI analysis completed (status=$($ai.status), error=$($ai.error))" ($ai.status -eq "completed")
    # reclaim.ai._optional.require() re-raises ANY ImportError raised while importing a package
    # as "needs the optional '<pkg>' package, which isn't installed". In a frozen build that is
    # exactly how a missing excluded submodule deep inside lightgbm/datasketch/imagehash would
    # show up, so that wording is a failure here, not an expected degraded mode.
    # "is missing" = AIModelMissingError (a bundled ONNX model absent), also a packaging defect.
    $badSkips = @($ai.tracks_skipped | Where-Object {
        $_.reason -match "unexpected error|No module named|ImportError|DLL load failed|isn't installed|is missing|failed to import" })
    Check "no AI pipeline skipped for an unexpected/import/missing-model error ($($badSkips.Count) found)" `
        ($badSkips.Count -eq 0)
    # Fresh installs ship no trained clutter_ranker.txt, so the ranker normally skips with
    # "no trained clutter-ranker model". ClutterRanker.__init__ calls require("lightgbm")
    # BEFORE that file check, so that skip still proves lightgbm (-> scipy.sparse, narwhals)
    # imported cleanly in the frozen exe.
    $rankerSkip = @($ai.tracks_skipped | Where-Object {
        $_.track -eq "ranked_clutter_ordering" -and $_.reason -match '^no trained clutter-ranker model' })
    Check "lightgbm imported: clutter ranker ran, or skipped only for its absent model file" `
        (($ai.tracks_run -contains "ranked_clutter_ordering") -or $rankerSkip.Count -eq 1)
    $trackImports = @{
        "near_identical_image"                = "imagehash -> scipy.fftpack"
        "near_dup_document_and_version_chain" = "datasketch -> scipy.integrate; MiniLM via onnxruntime"
        "semantic_image"                      = "CLIP via onnxruntime + faiss, on 2 residual images"
    }
    foreach ($track in $trackImports.Keys) {
        Check "AI track ran: $track ($($trackImports[$track]))" ($ai.tracks_run -contains $track)
    }
} finally {
    if (-not $server.HasExited) { & taskkill.exe /PID $server.Id /T /F | Out-Null }
}

# faiss's loader probes for optional CPU-specific builds (faiss.swigfaiss_avx2/_avx512) and logs a
# ModuleNotFoundError before falling back to the generic build. The faiss-cpu Windows wheel ships
# neither variant (only _swigfaiss.pyd; verified 2026-09-23 against the build venv and the
# 2026-08-26 dist), so that exact probe is expected and is filtered out. Every other import
# error still fails this check.
$serverOutput = ((Get-Content "$work\serve_stdout.txt", "$work\serve_stderr.txt" -ErrorAction SilentlyContinue) |
    Where-Object { $_ -notmatch "No module named 'faiss\.swigfaiss_avx(2|512)'" }) -join "`n"
Check "server output has no ModuleNotFoundError/ImportError" `
    ($serverOutput -notmatch 'ModuleNotFoundError|ImportError|No module named')

if ($fail) {
    Write-Host "`nONE OR MORE CHECKS FAILED -- server output: $work\serve_*.txt" -ForegroundColor Red
    exit 1
}
Write-Host "`nPackaged serve/scan/AI smoke passed." -ForegroundColor Green
exit 0
