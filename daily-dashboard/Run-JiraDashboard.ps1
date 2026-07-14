# Run-JiraDashboard.ps1
# Generates the daily JIRA sprint dashboard. Work items come from JIRA Cloud;
# capacity base hours + holidays come from the Team_Capacity.xlsx workbook.
#
# Usage:
#   .\Run-JiraDashboard.ps1
#   .\Run-JiraDashboard.ps1 -Release "REL-AUG-26" -Sprint "MPM Sprint 1"

param(
    [string]$Release,
    [string]$Sprint
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$pyArgs = @("generate_dashboard.py")
if ($Release) { $pyArgs += @("--release", $Release) }
if ($Sprint)  { $pyArgs += @("--sprint",  $Sprint) }

Write-Host "Running JIRA sprint dashboard..." -ForegroundColor Cyan
python @pyArgs
