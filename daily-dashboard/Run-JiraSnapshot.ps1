<#
  Run-JiraSnapshot.ps1
  --------------------
  Wraps Snapshot-JiraRelease.py for headless daily runs via Windows Task
  Scheduler. Writes a dated log under logs/.

  JIRA Cloud (motivity.atlassian.net) is reachable over the public internet,
  so this does NOT need VPN — but it DOES need the machine awake and the
  repo-root .jira_pat / .jira_email / .jira_site present.

  Manual usage:
      .\Run-JiraSnapshot.ps1                 # REL-AUG-26
      .\Run-JiraSnapshot.ps1 -Release "REL-SEP-26"
#>
[CmdletBinding()]
param(
  [string] $Release = "REL-AUG-26"
)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot                       # ...\em-standup\jira
$repoRoot  = Split-Path -Parent $scriptDir       # ...\em-standup
$logsDir   = Join-Path $repoRoot "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }

$log = Join-Path $logsDir ("jira-snapshot-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
$snap = Join-Path $scriptDir "Snapshot-JiraRelease.py"

Set-Location $repoRoot
"=== {0} · snapshot {1} ===" -f (Get-Date -Format "o"), $Release | Tee-Object -FilePath $log -Append

# Prefer 'python', fall back to 'py -3'.
$py = (Get-Command python -ErrorAction SilentlyContinue)
if ($py) {
  & $py.Source $snap $Release *>> $log
} else {
  & py -3 $snap $Release *>> $log
}
$code = $LASTEXITCODE
"=== exit $code ===" | Tee-Object -FilePath $log -Append
exit $code
