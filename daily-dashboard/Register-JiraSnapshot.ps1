<#
  Register-JiraSnapshot.ps1
  -------------------------
  Registers a Windows Task Scheduler job that runs Run-JiraSnapshot.ps1 once a
  day (EVERY day, incl. weekends — releases accrue 7 days/week), building the
  daily JIRA history the monthly retro is computed from.

  Why Windows Task Scheduler (not Cowork's scheduled-tasks):
  the snapshot hits JIRA Cloud with the user's API token from the repo-root
  dotfiles; it must run on a machine that has those creds and internet. The
  Cowork/assistant environment cannot reach Atlassian, so the job lives here.

  Usage (one-time, normal PowerShell is fine; elevation only if your policy
  requires it to register tasks):
      .\Register-JiraSnapshot.ps1
      .\Register-JiraSnapshot.ps1 -Time "09:30" -Release "REL-AUG-26"
      .\Register-JiraSnapshot.ps1 -Unregister

  After registration:
      schtasks /Query /TN "hBITS JIRA Release Snapshot" /V /FO LIST
      schtasks /Run   /TN "hBITS JIRA Release Snapshot"     # run on demand
#>
[CmdletBinding()]
param(
  [string] $TaskName  = "hBITS JIRA Release Snapshot",
  [string] $Time      = "09:30",
  [string] $Release   = "REL-AUG-26",
  [switch] $Unregister
)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
$repoRoot  = Split-Path -Parent $scriptDir
$runner    = Join-Path $scriptDir "Run-JiraSnapshot.ps1"

if (-not (Test-Path $runner)) { throw "Runner not found: $runner" }

if ($Unregister) {
  Write-Host "Removing scheduled task '$TaskName'..." -ForegroundColor Yellow
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "Done." -ForegroundColor Green
  return
}

Write-Host "Registering '$TaskName' daily at $Time for release $Release ..." -ForegroundColor Cyan
Write-Host "  Runner: $runner"

$psPath = (Get-Command powershell.exe).Source
$action = New-ScheduledTaskAction `
  -Execute $psPath `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Release `"$Release`"" `
  -WorkingDirectory $repoRoot

# Every day (releases accrue on weekends too).
$trigger = New-ScheduledTaskTrigger -Daily -At $Time

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
  -RestartCount 2 `
  -RestartInterval (New-TimeSpan -Minutes 5)

# Run as the interactive user so the repo-root .jira_pat is reachable.
$principal = New-ScheduledTaskPrincipal `
  -UserId (whoami) `
  -LogonType Interactive `
  -RunLevel Limited

$task = New-ScheduledTask `
  -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
  -Description "Daily JIRA release snapshot ($Release) for the monthly retro history."

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

Write-Host ""
Write-Host "Registered." -ForegroundColor Green
Write-Host "Verify:    schtasks /Query /TN `"$TaskName`" /V /FO LIST" -ForegroundColor DarkGray
Write-Host "Run now:   schtasks /Run   /TN `"$TaskName`""            -ForegroundColor DarkGray
Write-Host "Snapshots: $repoRoot\snapshots\jira\$Release\"           -ForegroundColor DarkGray
