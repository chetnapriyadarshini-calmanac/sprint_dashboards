"""
retro_config.py
================
Sprint Retro Dashboard configuration.

Edit per-sprint values here, then run:
    python scripts/retro/generate_retro_dashboard.py

WHAT THIS DASHBOARD DOES (internal flavour)
--------------------------------------------
Builds an HTML retro for the sprint that just ended, covering:
    1. Sprint Summary cards
    2. Goal Achievement Analysis (Sprint{N}Goal-* tag breakdown)
    3. Commitment vs Delivery (PBI-level done/not-done by team)
    4. Estimation Accuracy (est vs spent hours, per team + per person)
    5. Bugs per Parent PBI (which PBIs attracted the most bug churn)
    6. Root Cause Analysis (top categories by RootCauseType)

Combines Calmers + Crackers + Knackers in a single dashboard.

DATA SOURCE
-----------
Shared configuration (team rosters, root-cause normalisation, custom-field
candidates) for the JIRA retro generators.
"""

# ─── Sprint Identity (the sprint that just ENDED) ───────────────────────────
SPRINT_NUMBER     = 2
SPRINT_NAME       = "Sprint 2"          # Iteration Path substring match
SPRINT_DATES      = "July 06  - July 17, 2026"
SPRINT_END_DATE   = "2026-07-17"         # ISO YYYY-MM-DD; used for "bugs open
                                         # at sprint end" via revisions walk
SPRINT_AUDIENCE   = "management"           # "internal" or "management"
SPRINT_TOTAL_DAYS = 10

# ─── Output ─────────────────────────────────────────────────────────────────
OUTPUT_HTML = f"Sprint{SPRINT_NUMBER}_Internal_Retro.html"

# ─── Team Roster (combined Calmers + Crackers + Knackers) ───────────────────
# Mirrors the DSM dashboard's TEAMS dict so members map consistently across
# both reports. Members not in any team here roll into an "Other" bucket
# in the retro (used to flag PBIs/bugs assigned to people outside the
# combined retro scope — typically QA Automation, leads, or stragglers).
TEAMS = {
    "Calmers": [
        "Priya Mandhare",
        "Sumit Anpat",
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
        "Sushant Patil",
        "Rahul Patil",
        "Suyog Joshi",
        "Rajesh Lohar",
    ],
    # QA Automation — used to be excluded as its own iteration; from Sprint 80
    # the iteration is in scope and these members are first-class.
    "QA Automation": [
        "Sudarshan Shinde",
        "Vrushali Sagare",
    ],
}

# ─── Goal Tag Patterns ──────────────────────────────────────────────────────
# We match Sprint{N}Goal-<bucket> on the PBI's Tags field. Display order
# below drives the row order in the Goal Achievement table.
GOAL_BUCKETS = [
    "Live",
    "QAComplete",
    "DevComplete",
    "DevQAComplete",
    "AnalysisComplete",
    "AnalysisAndDevComplete",
]

# ─── Release-tag exclusions ─────────────────────────────────────────────────
# PBIs carrying a release tag for a LATER release (work deferred out of this
# sprint) should not be counted in the goal totals. Matched case-insensitively
# as a substring of the PBI's Tags, so "REL-JUL" catches REL-JUL-26-1,
# REL-JUL-26-2, etc. Update each sprint to point at the next release(s) whose
# items are being pushed out (e.g. add "REL-AUG" once August work appears).
EXCLUDED_RELEASE_TAGS = [
    "REL-JUL",   # July 2026 release — not tracked in the Sprint 82 (June) retro
]

# Per-bucket "done" state lists — matches the DSM dashboard's logic so the
# two reports never disagree about whether a PBI was done.
GOAL_DONE_STATES = {
    # Workflow order near the end: ... ST To Do -> Ready For LIVE -> LIVE -> Done.
    # "ST To Do" is the bar for "goal achieved" for EVERY bucket, so every state
    # AT OR AFTER it (ST To Do, Ready For LIVE, LIVE, Done) counts as achieved.
    # The Live bucket previously omitted "Ready For LIVE" — fixed so a PBI sitting
    # at Ready For LIVE is not wrongly flagged as a missed Live goal.
    "Live":                     ["Done", "LIVE", "Ready For LIVE", "ST To Do", "ST In Progress"],
    "QAComplete":               ["Done", "LIVE", "Ready For LIVE", "ST To Do", "ST In Progress"],
    "DevComplete":              ["Done", "LIVE", "Ready For LIVE", "ST To Do", "ST In Progress",
                                 "Dev Completed", "QA To Do",
                                 "QA in progress", "QA In Progress",
                                 "QA Complete"],
    "DevQAComplete":            ["Done", "LIVE", "Ready For LIVE", "ST To Do", "ST In Progress",
                                 "Dev Completed", "QA Complete"],
    # "Analysis complete" is a lower bar than "Dev complete": any state at or
    # beyond the QA stage (QA To Do / in progress / complete) means analysis is
    # done, so these count as achieved. Kept a superset of DevComplete.
    "AnalysisComplete":         ["Done", "LIVE", "Ready For LIVE", "ST To Do", "ST In Progress",
                                 "Dev Completed", "QA To Do",
                                 "QA in progress", "QA In Progress",
                                 "QA Complete", "Analysis Complete"],
    "AnalysisAndDevComplete":   ["Done", "LIVE", "Ready For LIVE", "ST To Do", "ST In Progress",
                                 "Dev Completed", "QA To Do",
                                 "QA in progress", "QA In Progress",
                                 "QA Complete", "Analysis Complete"],
    "_default":                 ["Done", "LIVE", "Ready For LIVE", "ST To Do", "ST In Progress"],
}

# Goal label colours (mirror the existing retro dashboard for visual continuity).
GOAL_COLORS = {
    "Live":                     "#dc2626",
    "QAComplete":               "#7c3aed",
    "DevComplete":              "#1d4ed8",
    "DevQAComplete":            "#0f766e",
    "AnalysisComplete":         "#c2410c",
    "AnalysisAndDevComplete":   "#15803d",
}

TEAM_COLORS = {
    "Calmers":        "#6366f1",
    "Crackers":       "#16a34a",
    "Knackers":       "#0891b2",
    "QA Automation":  "#a855f7",
    "Other":          "#64748b",
}

# ─── Root Cause Type Normalisation ──────────────────────────────────────────
# Raw root-cause values (lowercased, trimmed) → display category.
# Anything that doesn't match falls into "Pending Investigation" if blank,
# or "Other" if it's a non-empty unknown value (which we'll surface so you
# can add it to this map next sprint).
ROOT_CAUSE_NORMALISATION = {
    # Code Logic Issue
    "code logic issue":             "Code Logic Issue",
    "code logic":                   "Code Logic Issue",
    "logic issue":                  "Code Logic Issue",
    "ui issue":                     "Code Logic Issue",
    "ui / display":                 "Code Logic Issue",
    "javascript error":             "Code Logic Issue",
    "css issue":                    "Code Logic Issue",
    # Legacy Code
    "legacy code":                  "Legacy Code",
    "legacy":                       "Legacy Code",
    "old codebase":                 "Legacy Code",
    # Configuration / Environment
    "configuration/environment issue":  "Configuration/Environment Issue",
    "configuration issue":              "Configuration/Environment Issue",
    "config":                           "Configuration/Environment Issue",
    "environment issue":                "Configuration/Environment Issue",
    "deployment issue":                 "Configuration/Environment Issue",
    "appsetting":                       "Configuration/Environment Issue",
    # Requirements
    "requirements missed":          "Requirements Missed",
    "requirement missed":           "Requirements Missed",
    "requirement mismatch":         "Requirements Missed",
    "missing requirement":          "Requirements Missed",
    "brd gap":                      "Requirements Missed",
    # Data
    "data issue":                   "Data Issue",
    "data":                         "Data Issue",
    "incorrect data":               "Data Issue",
    # Not a bug / Duplicate
    "not a bug":                    "Duplicate / Not a Bug / CNR",
    "duplicate":                    "Duplicate / Not a Bug / CNR",
    "cnr":                          "Duplicate / Not a Bug / CNR",
    "rejected":                     "Duplicate / Not a Bug / CNR",
    "cannot reproduce":             "Duplicate / Not a Bug / CNR",
    # Performance
    "performance":                  "Performance",
    "performance issue":            "Performance",
    "slow":                         "Performance",
}

# Display order for the Root Cause section (most-to-least common from S79).
ROOT_CAUSE_DISPLAY_ORDER = [
    "Code Logic Issue",
    "Legacy Code",
    "Configuration/Environment Issue",
    "Requirements Missed",
    "Data Issue",
    "Performance",
    "Duplicate / Not a Bug / CNR",
    "Pending Investigation",
    "Other",
]

ROOT_CAUSE_COLORS = {
    "Code Logic Issue":                 "#dc2626",
    "Legacy Code":                      "#7c3aed",
    "Configuration/Environment Issue":  "#d97706",
    "Requirements Missed":              "#0891b2",
    "Data Issue":                       "#16a34a",
    "Performance":                      "#0f766e",
    "Duplicate / Not a Bug / CNR":      "#64748b",
    "Pending Investigation":            "#94a3b8",
    "Other":                            "#475569",
}

ROOT_CAUSE_DESCRIPTIONS = {
    "Code Logic Issue":                 "Incorrect logic — wrong conditions, missing implementations, JS/CSS errors",
    "Legacy Code":                      "Old codebase issues surfacing as bugs",
    "Configuration/Environment Issue":  "Deployment issues, missing config values, env-specific problems",
    "Requirements Missed":              "Features/fields not in BRD/FRD — requirements gap",
    "Data Issue":                       "Incorrect/missing data in DB",
    "Performance":                      "Slow queries, timeouts, scalability issues",
    "Duplicate / Not a Bug / CNR":      "Not actual bugs — working as designed, duplicates, or could-not-reproduce",
    "Pending Investigation":            "Root cause not yet identified — bug closed without RCA",
    "Other":                            "Uncategorised — review and add to the normalisation map",
}

# ─── Custom Field Discovery ─────────────────────────────────────────────────
# Custom.* field candidates. The loader tries these
# reference names in order and uses whichever returns a value first. If you
# ever discover a new ref name during a probe, add it to the TOP of the
# relevant list (first match wins).
CUSTOM_FIELD_CANDIDATES = {
    "root_cause_type": [
        "Custom.RootCauseType",
        "Custom.RootCauseCategory",
        "Custom.RootCause",
        "Microsoft.VSTS.Common.RootCause",
    ],
    "root_cause_analysis": [
        "Custom.RootCauseAnalysis",
        "Custom.RCA",
        "Custom.RootCauseDescription",
        "Microsoft.VSTS.TCM.SystemInfo",
    ],
}

# ─── Bug-per-PBI Display Settings ───────────────────────────────────────────
# The "Bugs per Parent PBI" section is sorted desc by bug count and capped
# at this many parent PBIs so the dashboard stays scannable. Set to None for "all".
BUGS_PER_PBI_TOP_N = 30

# Root Cause Analysis: how many bug rows to show inside each category card.
# None = show all (recommended for retros so nothing gets hidden).
ROOT_CAUSE_BUGS_PER_CATEGORY = None

# ─── Iteration Scope ────────────────────────────────────────────────────────
# Which iterations count as "in-scope" for this retro. We discover every
# iteration whose NAME contains SPRINT_NAME (e.g. "Sprint 79 Calmers",
# "Sprint 79 Crackers", "Sprint 79"), then drop any whose name contains a
# token in EXCLUDED_ITERATION_TOKENS.
#
# As of Sprint 80, QA Automation is in scope (Sudarshan Shinde and
# Vrushali Sagare are full retro members under the "QA Automation" team
# in TEAMS above). Leave this empty unless we need to deliberately exclude
# a future iteration variant.
EXCLUDED_ITERATION_TOKENS = []

# ─── Excluded IDs (carry-over from corrections.json philosophy) ─────────────
EXCLUDED_IDS = []

# ─── Excluded members (non-Dev / non-QA) ────────────────────────────────────
# Members listed here are filtered out of every per-person and per-team
# retro analysis (Capacity Allocation, Estimation by Person, Bugs Logged by
# Team, Bugs Open at Sprint End, Bugs per Parent PBI, Root Cause Analysis).
# Sprint-scope counts (Summary, Goal Achievement, Commitment vs Delivery)
# are NOT filtered — those reflect the sprint as a whole.
#
# Match semantics: case-insensitive token-set subset. An entry "Sagar"
# matches any assignee whose name contains "sagar" as a whole token (e.g.
# "Sagar Patil"); an entry "Prashant Kumar" matches only assignees whose
# tokens are a superset of {"prashant", "kumar"}. Use full names if you're
# worried about collisions; the partial forms below are safe for now
# because no roster member has those first-name tokens.
EXCLUDED_MEMBERS = [
    "Sagar",              # not a Dev/QA — likely lead / BA / external helper
    "Prashant",           # not a Dev/QA — likely lead / BA / external helper
    "Anil",               # not a Dev/QA — likely lead / BA / external helper
    "Krishna Chavan",     # Customer Support team — not in dev/QA retro scope
    "Mangesh Hawaldar",   # Customer Support team — not in dev/QA retro scope
    "Parth Biramwar",     # QA Manager — out of dev/QA retro scope
    "Sahil Wadgire",      # left for another pod — leftovers only
    "Mukesh Savant",      # left for another pod — leftovers only
]

# ─── Bug "Reason" → Not-a-Bug override ──────────────────────────────────────
# When System.Reason on a bug matches one of these (case-insensitive,
# whitespace-trimmed), the bug is force-bucketed into the
# "Duplicate / Not a Bug / CNR" Root-Cause category regardless of whatever
# RootCauseType happens to be set (or unset). This is what the team really
# means by "this isn't a real bug" — it's the resolution Reason, not the
# RCA, that decides.
NOT_A_BUG_REASONS = [
    "Not a Bug",
    "Duplicate",
    "Cannot Reproduce",
    "As Designed",
    "By Design",
    "Rejected",
    "CNR",
]

# Same idea but matched against the RootCauseAnalysis (RCA) free-text field.
# Many bugs in this template have RootCauseType blank but the RCA text says
# "not a bug" / "cannot reproduce" / "as designed" — those should also land
# in the Duplicate / Not a Bug / CNR bucket. Substring-match (case-insensitive,
# whitespace-trimmed) against the cleaned RCA text.
NOT_A_BUG_RCA_KEYWORDS = [
    "not a bug",
    "not bug",
    "cannot reproduce",
    "could not reproduce",
    "couldn't reproduce",
    "cant reproduce",
    "can't reproduce",
    "as designed",
    "by design",
    "duplicate bug",
    "is duplicate",
    "cnr",
]

# ─── Bug states considered "closed" at sprint end ───────────────────────────
# Anything outside this set counts as still-open at sprint close. Comparison
# is case-insensitive against System.State.
CLOSED_BUG_STATES = [
    "Done",
    "LIVE",
    "Removed",
    "Closed",
    "Ready For LIVE",
]

# ─── Revisions-walk concurrency ─────────────────────────────────────────────
# Number of parallel HTTP calls when fetching per-item revision history (used
# to discover first_assignee + state_at_sprint_end). Kept conservative to be
# polite to the source server.
REVISIONS_THREAD_WORKERS = 8


# ─── Missed Sprint Goal (manually curated) ──────────────────────────────────
# PBIs that missed the sprint goal, with owner + reason. This isn't reliably
# maintained here per sprint and rendered as
# the "Missed Sprint Goal" section in the internal retro. `related` holds
# (work_item_id, label) tuples for blocker/dependency links.

MISSED_GOAL_ITEMS = [
    {"id": 61173, "owner": "Priya",
     "title": "[TECHNICAL] [Billing Optimization] Refactor Adjustment Invoice Workflow to Eliminate Unposted/Posted Invoice Creation",
     "reason": "", "related": []},
    {"id": 61397, "owner": "Priya",
     "title": "[TECHNICAL] Billing | Add invoice Ids related to invoice number in following table [tblCopayInvoices, tblInvoicePrimarySecondary]",
     "reason": "Partial blocker - pending amount for secondary is showing incorrect at line level (the complete billed amount is being sent to the secondary).",
     "related": [(67018, "Bug 67018")]},
    {"id": 64651, "owner": "Sumit Anpat",
     "title": "[PRODUCT] RCM | Part 2: Additional Changes in the Outstanding Patient Responsibility Report",
     "reason": "Test Case Prep is not fully complete yet.", "related": []},
    {"id": 56912, "owner": "Sandesh",
     "title": "[PRODUCT] RCM | Processing of Revised Payments (both Negative and Positive) in EDI 835",
     "reason": "", "related": []},
    {"id": 62266, "owner": "Suraj",
     "title": "[PRODUCT] RCM | Use Contracted Rate for Dashboard Earnings (62266)",
     "reason": "Requirement gap identified during development; corner cases identified during unit testing.",
     "related": []},
    {"id": 63602, "owner": "Vivek",
     "title": "[PRODUCT] AI | Optimizer: Home Based Appointment Optimization",
     "reason": "Lack of bandwidth.", "related": []},
    {"id": 63909, "owner": "Sandesh",
     "title": "[PRODUCT] RCM | Send Supervising Provider (DQ) in Claims",
     "reason": "Blocked by PBI 56912 missing its goal; limited bandwidth in Sprint 82 due to planned leaves.",
     "related": [(56912, "PBI 56912")]},
]
