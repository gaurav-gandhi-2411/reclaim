# BL2 (2026-08-26 audit): regression test for ac3_login_diagnostic.ps1's Get-SeverityTallyCount --
# the SUMMARY tally's per-severity counting logic, which has now silently undercounted a real
# signal three separate times (SKIPPED, then WARNING) because of an exact "[<SEVERITY>]"
# close-bracket match that misses any suffixed tag like "[WARNING -- BH6] ...".
#
# Extracts and tests the REAL function's REAL source from ac3_login_diagnostic.ps1 via AST
# parsing (the same [System.Management.Automation.Language.Parser] mechanism this project's own
# session already used to syntax-check that script) rather than a hand-copied duplicate that
# could silently drift from the actual implementation -- and specifically does NOT dot-source the
# whole trip script, which has real side effects (installs, scans, Task Scheduler triggers)
# entirely unsuited to being pulled in just to test a regex.
#
# Run with: Invoke-Pester (this repo's Windows machines ship Pester built-in with PowerShell
# 5.1). Not wired into CI -- this repo's CI runs on GitHub Actions' windows-latest runners for
# Python-only jobs (see .github/workflows/ci.yml); no PowerShell/Pester job exists there, and
# adding one is out of scope for this fix. Documented honestly, not silently assumed covered.

$scriptPath = Join-Path $PSScriptRoot 'ac3_login_diagnostic.ps1'
$scriptContent = Get-Content -Path $scriptPath -Raw
$errors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($scriptContent, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {
    throw "ac3_login_diagnostic.ps1 has $($errors.Count) parse error(s) -- cannot extract Get-SeverityTallyCount from a script that doesn't parse. First error: $($errors[0].Message)"
}
$functionAst = $ast.Find(
    { param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-SeverityTallyCount' },
    $true
)
if (-not $functionAst) {
    throw "Get-SeverityTallyCount not found in ac3_login_diagnostic.ps1 -- it may have been renamed or removed; update this test's function name to match."
}
# Defines the function in THIS scope from its real, current source -- not a hand-copied
# duplicate. If the real function changes, this test starts exercising the new version
# automatically, the same guarantee a normal unit test importing real application code gives.
. ([scriptblock]::Create($functionAst.Extent.Text + "`n"))

Describe 'Get-SeverityTallyCount' {
    It 'matches an exact bare severity tag with no suffix' {
        $lines = @('[WARNING] plain warning, no suffix')
        (Get-SeverityTallyCount -Lines $lines -Severity 'WARNING').Count | Should Be 1
    }

    It 'matches a severity tag with a " -- <context>" suffix (the actual bug this test guards against)' {
        $lines = @('[WARNING -- BH6] No NEW reclaim.exe pid was ever observed...')
        (Get-SeverityTallyCount -Lines $lines -Severity 'WARNING').Count | Should Be 1
    }

    It 'matches a severity tag with a leading-whitespace-indented line (the ERROR pattern this shape already needed)' {
        $lines = @('  [ERROR -- response body] some detail here')
        (Get-SeverityTallyCount -Lines $lines -Severity 'ERROR').Count | Should Be 1
    }

    It 'counts every matching line across a mixed, realistic log, not just the first' {
        $lines = @(
            '[OK] Installer found: C:\Users\Public\reclaim_ac3\reclaim-setup.exe',
            '[WARNING] Freshness check failed (...) -- proceeding anyway',
            '[BEFORE] LastRunTime=...',
            '[WARNING -- BH6] No NEW reclaim.exe pid was ever observed...',
            '[PASS] File outside user scope was NOT touched'
        )
        (Get-SeverityTallyCount -Lines $lines -Severity 'WARNING').Count | Should Be 2
    }

    It 'does not match a DIFFERENT severity''s tag (WARNING must not count as ABORT)' {
        $lines = @('[WARNING -- BH6] No NEW reclaim.exe pid was ever observed...')
        (Get-SeverityTallyCount -Lines $lines -Severity 'ABORT').Count | Should Be 0
    }

    It 'reproduces the real BL2 regression exactly: a real trip log undercounted WARNING as 1 instead of 2' {
        # The actual two WARNING lines from ac3_run_20260826_181912.txt (line 9 and line 215),
        # verbatim -- the real trip this bug was found against, not a synthetic approximation.
        $lines = @(
            '[WARNING] Freshness check failed (no git checkout found at -RepoPath ''C:\Users\gaura\ml-projects\reclaim'' -- if running as a different account than the one that built this artifact, this check cannot verify freshness here by design; the operator should confirm freshness separately before staging, or pass -RepoPath explicitly) -- proceeding anyway because -AllowStaleBuild was passed. Every result below is against a build that may not reflect current main.',
            '[WARNING -- BH6] No NEW reclaim.exe pid was ever observed distinct from the pre-task baseline across all 5 samples -- this run''s process evidence does NOT positively confirm the task actually spawned its own process (it may have run and exited faster than this 3s sampling granularity can catch, which is a real, known limitation, not necessarily a failure).'
        )
        (Get-SeverityTallyCount -Lines $lines -Severity 'WARNING').Count | Should Be 2
    }
}
