# SPDX-License-Identifier: GPL-3.0-only WITH GPL-3.0-linking-exception
# Run with PowerShell; no compiler or device is required.
[CmdletBinding()]
param([string]$RepoRoot = (Join-Path $PSScriptRoot '..'))
$ErrorActionPreference = 'Stop'
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$shell = (Get-Process -Id $PID).Path
$savedSourceUrl = $env:FM1_SOURCE_URL
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('fm1-build-failure-' + [Guid]::NewGuid().ToString('N'))
$utf8 = New-Object Text.UTF8Encoding $false
$stale = 'FM1 stale build sentinel'
$failures = @()
$caseCount = 0
function Put([string]$path, [string]$text) {
    [IO.Directory]::CreateDirectory((Split-Path $path)) | Out-Null
    [IO.File]::WriteAllText($path, $text, $utf8)
}
$cases = @(
    @{name='missing'; value=$null},
    @{name='blank'; value='   '},
    @{name='http'; value='http://example.invalid/source.zip'},
    @{name='credentials'; value=('https://{0}@example.invalid/source.zip' -f 'user:password')},
    @{name='newline'; value="https://example.invalid/`nsource.zip"},
    @{name='valid'; value='https://example.invalid/source.zip'}
)
try {
    foreach ($mode in @('preview','web-package','firmware-package')) {
        $modeCases = @($cases)
        if ($mode -eq 'firmware-package') {
            $modeCases += @{name='toolchain-path'; value='https://example.invalid/source.zip'; toolchain='invalid|toolchain'}
        }
        foreach ($case in $modeCases) {
            $label = $mode + '/' + $case.name
            $fixture = Join-Path $fixtureRoot ($mode + '-' + $case.name)
            $script = if ($mode -eq 'firmware-package') { 'sdk/Build.ps1' } else { 'tools/Build-Web.ps1' }
            $scriptPath = Join-Path $fixture $script
            Put $scriptPath ([IO.File]::ReadAllText((Join-Path $RepoRoot $script)))
            Put (Join-Path $fixture 'app/version.h') ([IO.File]::ReadAllText((Join-Path $RepoRoot 'app/version.h')))
            $outputs = if ($mode -eq 'preview') {
                @('build/preview/index.html','build/preview/SHA256SUMS.txt')
            } elseif ($mode -eq 'web-package') {
                @('build/official-web/FM1-Official-Open-Source.html','build/official-web/SHA256SUMS.txt')
            } else {
                @('build/official-web/FM1-Official-Open-Source.html','build/official-web/SHA256SUMS.txt',
                    'build/dx7/generated/FM1_OFFICIAL_OPEN.fw','build/dx7/generated/FM1_OFFICIAL_OPEN.fwsc',
                    'build/dx7/sdk.elf','build/dx7/build-identity.json')
            }
            foreach ($name in $outputs) { Put (Join-Path $fixture $name) $stale }
            $reports = @{
                'firmware-report.json' = @('passed');
                'package-report.json' = @('board_access','fits');
                'web-package-report.json' = @('integrated','board_access')
            }
            if ($mode -eq 'firmware-package') {
                foreach ($name in $reports.Keys) {
                    $oldReport = @{}
                    foreach ($flag in $reports[$name]) { $oldReport[$flag] = $true }
                    Put (Join-Path $fixture ('build/dx7/' + $name)) ($oldReport | ConvertTo-Json)
                }
            }
            $keep = Join-Path $fixture 'build/tools/cache-keep.txt'
            Put $keep 'Keep the compiler cache'
            $env:FM1_SOURCE_URL = $case.value
            $arguments = @('-NoLogo','-NoProfile','-NonInteractive','-File',$scriptPath)
            if ($mode -eq 'firmware-package') {
                $toolchainPath = if ($case.toolchain) { $case.toolchain } else { Join-Path $fixture 'missing-toolchain' }
                $arguments += @('-Package','-Toolchain',$toolchainPath)
            }
            if ($mode -eq 'web-package') { $arguments += @('-PackageJson','{"schema":1}') }
            try {
                # Capture the real child process error without terminating the test runner.
                $ErrorActionPreference = 'Continue'
                $output = & $shell @arguments 2>&1
                $exitCode = $LASTEXITCODE
                $ErrorActionPreference = 'Stop'
                if ($exitCode -eq 0) { throw 'The incomplete fixture unexpectedly built successfully.' }
                $expectedError = if ($case.name -eq 'toolchain-path') { 'GetFullPath' }
                    elseif ($case.name -ne 'valid') { 'FM1_SOURCE_URL' }
                    elseif ($mode -eq 'firmware-package') { 'Install the matching JieLi' }
                    elseif ($mode -eq 'web-package') { 'Firmware and web release versions differ' }
                    else { 'Missing browser sources' }
                if (($output | Out-String) -notmatch $expectedError) {
                    throw "Build stopped at the wrong check; expected: $expectedError"
                }
                foreach ($name in $outputs) {
                    $path = Join-Path $fixture $name
                    if ([IO.File]::Exists($path) -and [IO.File]::ReadAllText($path).Contains($stale)) {
                        throw "Stale output survived: $name"
                    }
                }
                if ($mode -eq 'firmware-package') {
                    foreach ($name in $reports.Keys) {
                        $report = Get-Content -LiteralPath (Join-Path $fixture ('build/dx7/' + $name)) -Raw | ConvertFrom-Json
                        foreach ($flag in $reports[$name]) {
                            if ($report.$flag -cne $false) { throw "Success flag survived: $name / $flag" }
                        }
                    }
                }
                if (![IO.File]::Exists($keep) -or [IO.File]::ReadAllText($keep) -cne 'Keep the compiler cache') {
                    throw 'Compiler cache was removed or modified.'
                }
                Write-Host "PASS $label"
            } catch {
                $ErrorActionPreference = 'Stop'
                $failures += "$label - $($_.Exception.Message)"
                Write-Host "FAIL $($failures[-1])"
            }
            $caseCount++
        }
    }
} finally {
    $env:FM1_SOURCE_URL = $savedSourceUrl
    $cleanupPath = [IO.Path]::GetFullPath($fixtureRoot)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (!$cleanupPath.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($cleanupPath) -cnotmatch '^fm1-build-failure-[0-9a-f]{32}$') { throw 'Unsafe fixture cleanup path.' }
    if (Test-Path -LiteralPath $cleanupPath) { Remove-Item -LiteralPath $cleanupPath -Recurse -Force }
}
if ($failures.Count) { throw "$($failures.Count) of $caseCount build-failure cases failed." }
Write-Host "All $caseCount build-failure cases passed."
