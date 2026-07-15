"""
Sprint Dashboard Configuration
================================
Edit this file each sprint. The generator script reads from here.

DATA SOURCE
-----------
Work items are pulled live from JIRA Cloud (DATA_SOURCE = "jira"). Per-member
capacity is read from a maintained Excel workbook (CAPACITY_XLSX). No other
data source is used.

DAILY USAGE
-----------
    1. On sprint rollover, bump SPRINT_NUMBER, SPRINT_NAME, SPRINT_DATES,
       SPRINT_START_DATE (and SPRINT_TOTAL_DAYS if it's not the usual 10).
    2. SPRINT_DAY auto-computes from SPRINT_START_DATE (working days,
       Mon-Fri). Set SPRINT_DAY_OVERRIDE to force a specific number.
    3. Run:  .\\Run-JiraDashboard.ps1
"""

from datetime import date, datetime, timedelta

# -- Sprint Identity ----------------------------------------------------------
SPRINT_NUMBER      = 2
SPRINT_NAME        = "Sprint 2"          # must match Iteration Path substring
SPRINT_TOTAL_DAYS  = 10
SPRINT_DATES       = "July 6 - July 17, 2026"
SPRINT_START_DATE  = "2026-07-06"


def _compute_sprint_day(start_str: str, total_days: int) -> int:
    """Working-day count from sprint start (inclusive) to today.

    Counts Mon-Fri only, since hBITS sprints are 10 working days. Floored
    at 1 (so a fresh sprint reads as Day 1 even on the day it starts) and
    capped at SPRINT_TOTAL_DAYS so the header doesn't say "Day 14 of 10"
    when the sprint runs long before rollover.
    """
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d").date()
    except Exception:
        return 1
    today = date.today()
    if today <= start:
        return 1
    days = 1
    cur = start
    while cur <= today:
        if cur.weekday() < 5:           # 0=Mon .. 4=Fri
            days += 1
        cur += timedelta(days=1)
    return max(1, min(total_days, days))


# Set to an int (e.g. 3) to force a specific sprint day, otherwise leave None
# and the working-day calc above will fill SPRINT_DAY from SPRINT_START_DATE.
SPRINT_DAY_OVERRIDE = None
SPRINT_DAY = (
    SPRINT_DAY_OVERRIDE
    if SPRINT_DAY_OVERRIDE is not None
    else _compute_sprint_day(SPRINT_START_DATE, SPRINT_TOTAL_DAYS)
)

# -- Data Source --------------------------------------------------------------
# This is the JIRA-only dashboard. Work items are pulled exclusively from JIRA
# Cloud; per-member capacity (base hours + holidays) comes from the maintained
# Excel workbook (CAPACITY_XLSX), because JIRA Cloud has no native per-member
# sprint capacity. DATA_SOURCE must stay "jira".
DATA_SOURCE        = "jira"

# -- JIRA scoping -------------------------------------------------------------
#   RELEASE_NAME      : Fix Version, used ONLY for the Sprint<->Release
#                       discrepancy report (not for the board — see generator).
#   JIRA_SPRINT_NAMES : every per-team Jira sprint that makes up this delivery
#                       sprint. Each team runs its own sprint named
#                       "MPM <Team> Sprint N". Work items are pulled from ALL of
#                       them (OR-ed in the JQL). These must match the JIRA sprint
#                       names EXACTLY (case- and space-sensitive).
RELEASE_NAME       = "REL-AUG-26"      # <-- current Fix Version, or None
JIRA_SPRINT_NAMES  = [
    "MPM Calmers Sprint 2",
    "MPM Crackers Sprint 2",
    "MPM Knackers Sprint 2",
    # "MPM QA Automation Sprint 1",   # <-- add if QA Automation has its own sprint
]
# Back-compat single value (first entry); used as a display/label fallback.
JIRA_SPRINT_NAME   = JIRA_SPRINT_NAMES[0] if JIRA_SPRINT_NAMES else "MPM Sprint 1"

# -- Capacity source ----------------------------------------------------------
# The maintained capacity workbook (the team updates it each sprint). Model:
#   Sprint cap = Capacity/day × (Working days − Team days off − Days off)
# Whichever source you use must have a "Settings" sheet (Working days in B5,
# Team days off in B6) and a "Capacity" sheet (Team, Member, Activity,
# Capacity/day, Days off).
#
# CURRENT SETUP: live Google Sheet, read as YOU via OAuth. On the first run a
# browser opens to sign in with your motivity account and grant read-only Drive
# access; the token is cached (see CAPACITY_OAUTH_TOKEN) and refreshed
# automatically after that. One-time setup (see the README for detail):
#   1. Create an OAuth Client ID ("Desktop app") in a Google Cloud project and
#      enable the Google Drive API; download client_secret.json.
#   2. Save it as `.gcp_oauth_client.json` at the repo root (git-ignored), or
#      set CAPACITY_OAUTH_CLIENT below to its path.
#   3. pip install google-api-python-client google-auth google-auth-oauthlib
#   4. Set CAPACITY_XLSX to the Sheet URL.
# For an UNATTENDED daily job, seed the token once interactively; the app should
# be an "Internal" OAuth app so the refresh token does not expire weekly.
#
# CAPACITY_XLSX also accepts (fallbacks):
#   * "Team_Capacity.xlsx" — the workbook committed in this folder (no auth), or
#   * an absolute local .xlsx path, or a service-account / public Sheet URL.
CAPACITY_XLSX = "https://docs.google.com/spreadsheets/d/19rc7W5mgR9PoJVWAzdU8Y0zA8pz4mbND/edit?usp=drive_link&ouid=101446380543586458847&rtpof=true&sd=true"
#"https://docs.google.com/spreadsheets/d/REPLACE_WITH_SHEET_ID/edit"


# OAuth client-secret JSON path. None -> auto-discover `.gcp_oauth_client.json`
# (repo root or this folder). Only used when CAPACITY_XLSX is a Google Sheet URL
# and no service-account key is present.
CAPACITY_OAUTH_CLIENT = None

# Where to cache the OAuth token. None -> `.gcp_oauth_token.json` next to the
# client-secret file. Git-ignored. Delete it to force re-consent.
CAPACITY_OAUTH_TOKEN = None

# Service-account JSON key path — an alternative to OAuth for a Google Sheet.
# Leave None to use OAuth / local file.
CAPACITY_SA_KEY = None

# -- Output File --------------------------------------------------------------
# The dashboard always shows the PREVIOUS day's logged hours (today's snapshot
# minus yesterday's snapshot — see README "What 'Logged Since Yesterday'
# means"). The filename therefore reflects the day whose data is being shown,
# not today's calendar day. Floored at 1 so Day 1 stays "Day 1" rather than
# "Day 0".
DISPLAY_SPRINT_DAY = max(1, SPRINT_DAY - 1)
OUTPUT_HTML        = f"Sprint{SPRINT_NUMBER}_Dashboard_Day{DISPLAY_SPRINT_DAY}.html"

# -- Team -> Member Mapping ---------------------------------------------------
# Keys are team names shown in the dashboard.
# Values are exact assignee display names as they appear in JIRA (case-sensitive)
# and must match the names used in the capacity workbook.
TEAMS = {
    "Calmers": [
        "Priya Mandhare",
        "Sandesh Tendulkar",
        "Suraj Marathe",
        "Gautam Gehlot",
        "Sandip Sutar",
    ],
    "Crackers": [
        "AbdulGani Shaikh",
        "Mugdha.Thakare",
        "Priyanka Kusal",
    ],
    "Knackers": [
        "Abhisha Jain",
        "vivek ghorpade",
        "Heeru Gujar",
        "Sneha Dafale",
        "Rahul Patil",
        "Suyog Joshi",
    ],
    "QA Automation": [
        "Sudarshan Shinde",
        "Vrushali Sagare",
    ],
}

# -- JIRA status ladder (statuses at each pipeline rank) ----------------------
# Used to build the JIRA goal buckets below. A goal is "done" when the issue's
# status is at OR PAST the goal's stage -> cumulative lists (rank N and beyond).
# Mirrors jira_fetch.STATUS_RANK so the goal tab matches goal_met() exactly.
_J_R1 = ["PO Approved", "BA Analysis"]   # BA (business) analysis precedes tech analysis
_J_R2 = ["Ready for Tech Analysis", "Tech Analysis In Progress", "Technical Review"]
_J_R3 = ["Ready for Dev", "Selected for Development", "In Progress", "PR"]
_J_R4 = ["Ready for QA", "QA"]
_J_R5 = ["QA Passed", "ST To Do", "ST In Progress"]
_J_R6 = ["Ready for Live", "Live in Progress", "Integrated", "Done", "Monitoring"]

# -- Goal-Specific "Done" States ----------------------------------------------
# Keys are the NORMALISED goal-tag suffixes extract_goal() produces (lowercase,
# spaces/hyphens stripped) from the JIRA "Goal for the Sprint" dropdown, plus
# the legacy TFS bucket names so a flip back to DATA_SOURCE="tfs" still works.
GOAL_DONE_STATES = {
    # --- JIRA dropdown goals (cumulative ladder) ---
    "poapproved":           _J_R1 + _J_R2 + _J_R3 + _J_R4 + _J_R5 + _J_R6,
    "readyfortechanalysis":         _J_R2 + _J_R3 + _J_R4 + _J_R5 + _J_R6,
    "readyfordev":                          _J_R3 + _J_R4 + _J_R5 + _J_R6,
    "readyforqa":                                   _J_R4 + _J_R5 + _J_R6,
    "sttodo":                                               _J_R5 + _J_R6,
    "readyforlive":                                                 _J_R6,
    # --- legacy TFS buckets ---
    "Live": [
        "Done", "LIVE",
    ],
    "QAComplete": [
        "Done", "LIVE", "Ready For LIVE", "ST To Do",
    ],
    "DevComplete": [
        "Done", "LIVE", "Ready For LIVE", "ST To Do", "Dev Completed",
        "QA To Do", "QA in progress", "QA In Progress", "QA Complete",
    ],
    "DevQAComplete": [
        "Done", "LIVE", "Ready For LIVE", "ST To Do", "Dev Completed", "QA Complete",
    ],
    "AnalysisComplete": [
        "Done", "LIVE", "Ready For LIVE", "ST To Do",
        "Dev Completed", "QA Complete", "Analysis Complete",
    ],
    "AnalysisAndDevComplete": [
        "Done", "LIVE", "Ready For LIVE", "ST To Do",
        "Dev Completed", "QA Complete", "Analysis Complete",
    ],
    "_default": [
        # JIRA "shipped" states + legacy TFS ones
        "Ready for Live", "Live in Progress", "Integrated", "Done", "Monitoring",
        "LIVE", "Ready For LIVE", "ST To Do",
    ],
}

# -- "In-Progress" PBI States -------------------------------------------------
# JIRA Story-workflow mid-pipeline states first, then legacy TFS states (union
# so either data source works).
INPROGRESS_STATES = [
    # JIRA (Story workflow)
    "PO Approved", "Ready for Tech Analysis", "Tech Analysis In Progress",
    "BA Analysis", "Technical Review", "Ready for Dev", "In Progress", "PR",
    "Ready for QA", "QA", "QA Passed", "ST To Do", "ST In Progress",
    "Selected for Development"
]

# Sub-task / Task "in progress" — works for both JIRA ("In Progress") and TFS.
TASK_INPROGRESS_STATE = "In Progress"

# -- Rows to Exclude ----------------------------------------------------------
EXCLUDED_IDS = []

# -- Capacity Name Matching ---------------------------------------------------
# Map TEAMS-dict name -> name in capacity source. Empty by default since the
# TFS team-capacity API returns the same display name as System.AssignedTo.
CAPACITY_NAME_MAP = {}

# -- Capacity Adjustments -----------------------------------------------------
# {"Name": hours_lost} for unplanned leave on top of TFS daysOff.
CAPACITY_ADJUSTMENTS = {}

# -- Hotfix Tag Highlight -----------------------------------------------------
HOTFIX_TAG_PATTERN = f"Hotfix-{SPRINT_NUMBER}"

# -- Daily Tracking & Risk ----------------------------------------------------
HISTORY_FILE       = f"Sprint{SPRINT_NUMBER}_history.json"

RISK = {
    "low_effort_pct_by_day5":  20,
    "overloaded_pct":         120,
    "high_remaining_by_day6":  80,
    "no_hours_grace_days":      1,
    "spike_threshold_pct":    200,
    "drop_threshold_pct":      20,
}
