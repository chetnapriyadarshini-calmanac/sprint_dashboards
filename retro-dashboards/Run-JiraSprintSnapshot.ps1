<#
  Run-JiraSprintSnapshot.ps1
  --------------------------
  Freezes the current sprint wave into snapshots/jira_sprint/<WAVE>/<date>.json
  by wrapping Snapshot-JiraSprint.py. Run this ONCE per sprint, on the last day
  of the sprint BEFORE the boards roll over to the next sprint — the retro is
  built from this frozen point-in-time state.

  Needs the repo-root .jira_pat / .jira_email / .jira_site (public internet; no VPN).

  Manual usage:
      .\Run-JiraSprintSnapshot.ps1                      # freeze active MPM sprints now
      .\Run-JiraSprintSnapshot.ps1 -Wave "Sprint-2"     # label the wave folder
#>
[CmdletBinding()]
param(
  [string] $Wave
)
$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
$repoRoot  = Split-Path -Parent $scriptDir
$logsDir   = Join-Path $repoRoot "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }
$log = Join-Path $logsDir ("jira-sprint-snapshot-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
$snap = Join-Path $scriptDir "Snapshot-JiraSprint.py"

$pyArgs = @($snap)
if ($Wave) { $pyArgs += @("--wave", $Wave) }

Set-Location $repoRoot
"=== {0} · sprint snapshot (wave={1}) ===" -f (Get-Date -Format "o"), $Wave | Tee-Object -FilePath $log -Append
$py = (Get-Command python -ErrorAction SilentlyContinue)
if ($py) { & $py.Source @pyArgs *>> $log } else { & py -3 @pyArgs *>> $log }
"=== exit $LASTEXITCODE ===" | Tee-Object -FilePath $log -Append
