"""
jira_fetch.py
-------------
JIRA Cloud equivalent of scripts/dashboard/tfs_fetch.py. Runs JQL scoped to a
sprint and/or release and returns the SAME canonical pandas DataFrame the
dashboard/retro/scoring generators already consume:

    ID | Title | Work Item Type | State | Assigned To | Iteration Path | Tags
       | Original Estimate | Completed Work

so the generators work unchanged once DATA_SOURCE points here.

MODEL (confirmed from the MPM project, issue MPM-86):
  Hierarchy:   Epic (the migrated TFS PBI)  ->  Story ("<Epic> Implementation",
               the per-sprint unit, carries Sprint + Goal)  ->  Sub-task
               (carries the hours; rolls up to the Story via aggregate fields).

  Rules (per Chetna):
    * EPIC ROLLUP: the dashboard shows ONE PBI row per EPIC, not per Story.
      In-sprint Stories are grouped under their parent Epic; the Epic row takes
      status + goal from its LEAST-ADVANCED in-sprint Story (weakest link), and
      hours roll up from all that Epic's Stories' sub-tasks.
    * Standalone Tasks that are in the sprint are GROUNDED IN SPRINT: they count
      as their own PBI unit (velocity + goals), not just hour-carriers.
    * Sub-tasks remain hour-carriers (canonical type "Task").

  Hours live on sub-tasks, so we fetch in two passes (parents in scope, then
  their sub-tasks by parent key) and give each row its OWN
  timeoriginalestimate / timespent — the generator's per-task rollup sums them.

Public API:
    load_dataframe(release=None, sprint=None, ctx=None) -> pd.DataFrame
    load_mismatches(release, sprint, ctx=None) -> pd.DataFrame
        Sprint <-> Release scope discrepancies for the Discrepancies tab.

CLI:
    python jira/jira_fetch.py "REL-AUG-26" "MPM Sprint 1"
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
# jira_auth is imported lazily (only when no ctx is passed in) so callers that
# build their own context — e.g. generate_dashboard.py, which inlines auth — do
# not need jira_auth.py present at all.

# ── Field IDs (confirmed via Probe-JiraFields.py) ───────────────────────────
F_SPRINT       = "customfield_10020"   # Sprint (list of sprint objects)
F_GOAL         = "customfield_11559"   # Goal for the Sprint (list of option dicts)
F_STORY_POINTS = "customfield_10034"   # Story Points (confirm vs 10016 on a sized issue)
F_SIZING       = "customfield_11566"   # Sizing (T-shirt)
F_EPIC_LINK    = "customfield_10014"   # Epic Link (fallback to `parent` on newer issues)

# Completion-date custom fields live on the issue (e.g. "Dev Complete Date",
# "QA Complete Date") — NOT in tags. Their customfield IDs vary per instance, so
# we resolve them by NAME at runtime via /rest/api/3/field (see _resolve_date_fields).
_F_TECH_DATE = None   # resolved id for "Tech Analysis Complete Date"
_F_DEV_DATE  = None    # resolved id for "Dev Complete Date"
_F_QA_DATE   = None    # resolved id for "QA Complete Date"
_F_TEAMS: list[str] = []   # resolved ids of ALL "Team"-like fields (first non-empty wins)
_DATE_FIELDS_RESOLVED = False

# Field names that indicate a team-assignment field.
_TEAM_FIELD_NAMES = ("team", "team name", "squad", "delivery team")

_DASHBOARD_COLS = ["ID", "Title", "Work Item Type", "State", "Assigned To",
                   "Iteration Path", "Tags", "Original Estimate", "Completed Work",
                   "Sizing", "Release", "Team"]

_FIELDS = ["summary", "issuetype", "status", "assignee", "labels", "parent",
           "fixVersions", "timeoriginalestimate", "timespent",
           F_SPRINT, F_GOAL, F_STORY_POINTS, F_SIZING, F_EPIC_LINK]


def _resolve_date_fields(ctx) -> None:
    """Resolve the 'Dev Complete Date' / 'QA Complete Date' custom-field IDs by
    name (once) and add them to the requested fields, so each issue's actual
    completion dates come through. Safe to call repeatedly."""
    global _F_TECH_DATE, _F_DEV_DATE, _F_QA_DATE, _F_TEAMS, _DATE_FIELDS_RESOLVED
    if _DATE_FIELDS_RESOLVED:
        return
    _DATE_FIELDS_RESOLVED = True
    try:
        r = ctx["session"].get(f"{ctx['api_v3']}/field", timeout=ctx["timeout"])
        r.raise_for_status()
        raw_fields = r.json()
        by_name = {(f.get("name") or "").strip().lower(): f.get("id") for f in raw_fields}
        _F_TECH_DATE = (by_name.get("tech analysis complete date")
                        or by_name.get("refinement complete date")
                        or by_name.get("refinement complete"))
        _F_DEV_DATE  = by_name.get("dev complete date")
        _F_QA_DATE   = by_name.get("qa complete date")
        # Collect EVERY team-like field (exact-name candidates first, then any
        # whose name contains 'team'/'squad'). _team() reads them in this order
        # and uses the first that's populated — teams may live on the Story in
        # one field and on sub-tasks in another.
        team_ids: list[str] = []
        for f in raw_fields:
            nm = (f.get("name") or "").strip().lower()
            if nm in _TEAM_FIELD_NAMES:
                fid = f.get("id")
                if fid and fid not in team_ids:
                    team_ids.append(fid)
        for f in raw_fields:
            nm = (f.get("name") or "").strip().lower()
            if ("team" in nm or "squad" in nm):
                fid = f.get("id")
                if fid and fid not in team_ids:
                    team_ids.append(fid)
        _F_TEAMS = team_ids
        for fid in [_F_TECH_DATE, _F_DEV_DATE, _F_QA_DATE, *_F_TEAMS]:
            if fid and fid not in _FIELDS:
                _FIELDS.append(fid)
        print(f"   📅 Completion-date fields — Tech Analysis: "
              f"{_F_TECH_DATE or '⚠ NOT FOUND'}, Dev: {_F_DEV_DATE or '⚠ NOT FOUND'}, "
              f"QA: {_F_QA_DATE or '⚠ NOT FOUND'}")
        if not (_F_TECH_DATE and _F_DEV_DATE and _F_QA_DATE):
            print("      (a 'NOT FOUND' means the JIRA field name differs from "
                  "'Tech Analysis Complete Date'/'Dev Complete Date'/'QA Complete "
                  "Date' — tell me the exact name.)")
    except Exception as e:
        print(f"   ⚠ Could not resolve completion-date fields ({e}); "
              f"the tracker will treat dates as missing.")


def _date_token(fields: dict, field_id, label: str):
    """Turn a JIRA date field value ('2026-06-26') into a tag token the dashboard
    date parsers understand, e.g. 'DevComplete-06/26/2026'."""
    if not field_id:
        return None
    v = fields.get(field_id)
    if not v:
        return None
    s = str(v)[:10]
    try:
        y, m, d = s.split("-")
        return f"{label}-{int(m):02d}/{int(d):02d}/{y}"
    except Exception:
        return None


def _date_tokens(fields: dict) -> list[str]:
    return [t for t in (_date_token(fields, _F_TECH_DATE, "TechAnalysisComplete"),
                        _date_token(fields, _F_DEV_DATE, "DevComplete"),
                        _date_token(fields, _F_QA_DATE, "QAComplete")) if t]

# JIRA issue type -> canonical TFS-style type the generators key on.
TYPE_MAP = {
    "Story":       "Product Backlog Item",   # sprint unit (status source)
    "Epic":        "Product Backlog Item",   # only if scoped by fixVersion
    "Task":        "Task",                   # standalone -> promoted to PBI (see _row)
    "Sub-task":    "Task",                   # hour-carrier
    "Bug":         "Bug",
    "Defect":      "Bug",
    "Field Issue": "Product Backlog Item",
}

# ── Goal ladder + status ladder ─────────────────────────────────────────────
# A goal is MET when the issue's status rank >= the goal's rank.
GOAL_RANK = {
    "PO Approved": 1,
    "Ready for Tech Analysis": 2,
    "Ready for Dev": 3,
    "Ready for QA": 4,
    "ST To Do": 5,
    "Ready for Live": 6,
}

# JIRA workflow STATUS -> ladder rank. AUTHORITATIVE: built from the project's
# real statuses (Probe-JiraFields.py "Workflow statuses by issue type").
# A goal is MET when an issue's status rank >= the goal's rank.
#
# This is a UNION of the Story workflow (the analysis/dev/ST/live gates) and the
# Task/Bug workflow (the leaner Selected-for-Dev/QA/Integrated gates), because a
# PBI row can come from either a Story (status source) or a grounded standalone
# Task. Statuses are mapped to the highest ladder milestone they imply.
#
# Excluded / non-delivery closes (Won't Do, Duplicate, Cannot Reproduce) and the
# stage-agnostic "On Hold" are intentionally LEFT OUT -> they score as not-met
# and surface for attention rather than silently counting toward a goal.
STATUS_RANK = {
    # rank 0 — not started / pre-approval (present but below PO Approved).
    # BA Analysis is pre-approval (PO Approved happens AFTER it). "Technical
    # Review" lives here too because it can occur during BA Analysis, so it is
    # NOT treated as having reached the tech-analysis stage.
    "Backlog": 0, "Ready For Refinement": 0, "In Refinement": 0,
    "PO Review": 0, "Scoping": 0, "To Be Estimated": 0, "Triage": 0,
    "Investigating": 0, "CSC Investigation": 0, "More Info from Customer": 0,
    "To Do": 0, "BA Analysis": 0, "Technical Review": 0,
    # rank 1 — PO Approved (comes after BA Analysis)
    "PO Approved": 1,
    # rank 2 — Ready for Tech Analysis (technical-analysis stage)
    "Ready for Tech Analysis": 2, "Tech Analysis In Progress": 2,
    # rank 3 — Ready for Dev (development stage)
    "Ready for Dev": 3, "Selected for Development": 3, "In Progress": 3, "PR": 3,
    # rank 4 — Ready for QA (QA stage)
    "Ready for QA": 4, "QA": 4,
    # rank 5 — QA passed / ST stage
    "QA Passed": 5, "ST To Do": 5, "ST In Progress": 5,
    # rank 6 — Ready for Live / live / done
    "Ready for Live": 6, "Live in Progress": 6, "Integrated": 6, "Done": 6,
    "Monitoring": 6, "Reopened": 3,   # reopened drops back to dev
}

# Statuses that mean "do not count toward goals" (excluded, like TFS Removed/Cut).
GOAL_EXCLUDED_STATUSES = {"Won't Do", "Duplicate", "Cannot Reproduce", "On Hold"}


def goal_met(goal_value: str, status: str) -> bool:
    if status in GOAL_EXCLUDED_STATUSES:
        return False
    gr = GOAL_RANK.get(goal_value)
    sr = STATUS_RANK.get(status)
    return gr is not None and sr is not None and sr >= gr


# ── Field extractors ────────────────────────────────────────────────────────
def _hours(fields: dict[str, Any]) -> tuple[float, float]:
    """Each row's OWN estimate/spent in hours (seconds -> hours). Sub-tasks hold
    the real numbers; Stories/Epics are usually 0 and roll up via the generator."""
    est_s   = fields.get("timeoriginalestimate") or 0
    spent_s = fields.get("timespent") or 0
    return est_s / 3600.0, spent_s / 3600.0


def _assignee(fields: dict[str, Any]) -> str:
    a = fields.get("assignee") or {}
    return a.get("displayName", "") if isinstance(a, dict) else ""


def _sizing(fields: dict[str, Any]) -> str:
    """T-shirt Sizing (customfield_11566). Option fields come back as a dict
    {'value': 'M'} (or a list of such); tolerate a plain string too."""
    v = fields.get(F_SIZING)
    if isinstance(v, dict):
        return str(v.get("value") or v.get("name") or "").strip()
    if isinstance(v, list) and v:
        first = v[0]
        if isinstance(first, dict):
            return str(first.get("value") or first.get("name") or "").strip()
        return str(first).strip()
    return str(v).strip() if v else ""


def _fix_version(fields: dict[str, Any]) -> str:
    """Release tag(s) from fixVersions -> comma-joined names (e.g. 'REL-AUG-26')."""
    fvs = fields.get("fixVersions") or []
    names = []
    for fv in fvs:
        n = fv.get("name") if isinstance(fv, dict) else str(fv)
        if n:
            names.append(str(n).strip())
    return ", ".join(names)


def _team_value(v: Any) -> str:
    """Extract a team name from one field value. Handles a team object
    {'name'/'title': ...}, a select-option {'value': ...}, a list of those, or a
    plain string. Returns '' if empty."""
    if v is None:
        return ""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        return str(v.get("name") or v.get("title") or v.get("value")
                   or v.get("displayName") or "").strip()
    return str(v).strip()


def _team(fields: dict[str, Any]) -> str:
    """Team assignment: read each resolved team-like field (see
    _resolve_date_fields) and return the first that's populated. '' if none."""
    for fid in _F_TEAMS:
        val = _team_value(fields.get(fid))
        if val:
            return val
    return ""


def _sprint_name(fields: dict[str, Any]) -> str:
    """Sprint field is a list of sprint objects; prefer the active one, else last."""
    sprints = fields.get(F_SPRINT) or []
    if isinstance(sprints, list) and sprints:
        active = [s for s in sprints if isinstance(s, dict) and s.get("state") == "active"]
        chosen = active[-1] if active else sprints[-1]
        return chosen.get("name", "") if isinstance(chosen, dict) else str(chosen)
    return ""


def _goal_value(fields: dict[str, Any]) -> str:
    """customfield_11559 -> list of option dicts: [{'value': 'Ready for Dev'}]."""
    raw = fields.get(F_GOAL)
    if isinstance(raw, list) and raw:
        first = raw[0]
        return first.get("value", "") if isinstance(first, dict) else str(first)
    if isinstance(raw, dict):
        return raw.get("value", "")
    return raw or ""


def _parent_key(fields: dict[str, Any]) -> str | None:
    """Parent issue key (Epic for a Story, Story for a Sub-task)."""
    p = fields.get("parent")
    if isinstance(p, dict) and p.get("key"):
        return p["key"]
    epic = fields.get(F_EPIC_LINK)
    return epic if isinstance(epic, str) and epic else None


def _goal_tag(fields: dict[str, Any], sprint_number: int) -> str:
    val = _goal_value(fields)
    return f"Sprint{sprint_number}Goal-{val}" if val else ""


# ── HTTP search ─────────────────────────────────────────────────────────────
def _search(ctx: dict[str, Any], jql: str) -> list[dict[str, Any]]:
    """Page through POST /rest/api/3/search/jql.

    The classic /rest/api/3/search was RETIRED (HTTP 410, Atlassian CHANGE-2046)
    in favour of this token-paginated endpoint. Differences handled here:
      * pagination is by `nextPageToken`, not `startAt`;
      * the response has NO `total` — we loop until there's no next token;
      * `fields` is passed as a list in the POST body."""
    out: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        payload: dict[str, Any] = {"jql": jql, "maxResults": 100, "fields": _FIELDS}
        if next_token:
            payload["nextPageToken"] = next_token
        r = ctx["session"].post(f"{ctx['api_v3']}/search/jql",
                                json=payload, timeout=ctx["timeout"])
        if not r.ok:
            raise RuntimeError(f"JQL search failed ({r.status_code}): {r.text[:300]}\nJQL: {jql}")
        body = r.json()
        out.extend(body.get("issues", []))
        next_token = body.get("nextPageToken")
        if not next_token or body.get("isLast"):
            break
    return out


# ── Row builders ────────────────────────────────────────────────────────────
def _pbi_row(issue: dict[str, Any], sprint_number: int,
             pbi_titles: dict[str, str]) -> dict[str, Any]:
    """Build a PBI-unit row. For a Story, identify it by its parent Epic (the
    "PBI") but take status + goal from the Story itself. For a standalone Task
    (grounded in sprint), it is its own PBI."""
    f = issue["fields"]
    itype = (f.get("issuetype") or {}).get("name", "")
    est, spent = _hours(f)
    parent = _parent_key(f)

    if itype == "Story" and parent:
        pbi_id = parent                                   # refer by PBI (Epic)
        title = pbi_titles.get(parent) or f.get("summary", "")
    else:
        pbi_id = issue["key"]                             # standalone Task / Epic / Bug
        title = f.get("summary", "")

    return {
        "ID": pbi_id,
        "Title": title,
        "Work Item Type": "Product Backlog Item",
        "State": (f.get("status") or {}).get("name", "") or "",
        "Assigned To": _assignee(f),
        "Iteration Path": _sprint_name(f),
        "Tags": ", ".join((f.get("labels") or [])
                          + [t for t in [_goal_tag(f, sprint_number)] if t]
                          + _date_tokens(f)),
        "Original Estimate": est,
        "Completed Work": spent,
        "Sizing": _sizing(f),
        "Release": _fix_version(f),
        "Team": _team(f),
    }


def _epic_pbi_row(epic_key: str, epic_title: str | None,
                  rep_issue: dict[str, Any], sprint_number: int) -> dict[str, Any]:
    """One PBI row representing an EPIC (the dashboard shows one row per Epic,
    not per Story). Status + goal come from the Epic's least-advanced in-sprint
    Story (rep_issue) — an Epic is only as 'done' as its weakest in-sprint story.
    Hours stay 0 here and roll up from the Epic's sub-task rows via the
    generator's per-task attribution."""
    f = rep_issue["fields"]
    return {
        "ID": epic_key,
        "Title": epic_title or f.get("summary", ""),
        "Work Item Type": "Product Backlog Item",
        "State": (f.get("status") or {}).get("name", "") or "",
        "Assigned To": _assignee(f),
        "Iteration Path": _sprint_name(f),
        "Tags": ", ".join((f.get("labels") or []) + [t for t in [_goal_tag(f, sprint_number)] if t]),
        "Original Estimate": 0.0,
        "Completed Work": 0.0,
        "Sizing": _sizing(f),
        "Release": _fix_version(f),
        "Team": _team(f),
    }


def _story_pbi_row(issue: dict[str, Any], epic_name: str | None,
                   sprint_number: int) -> dict[str, Any]:
    """One PBI-unit row per STORY (the per-sprint unit). Stories are children of
    the Epic, so the Epic is REPEATED across its stories: the Title carries the
    Epic name (which holds the [PRODUCT]/[TECHNICAL] prefix the dashboard filters
    on) plus the story/task name, and each story is its own goal-bearing row
    judged by its OWN status. Hours stay 0 here and roll up from the story's
    sub-tasks (emitted right after this row)."""
    f = issue["fields"]
    summary = f.get("summary", "") or ""
    title = f"{epic_name} — {summary}" if epic_name else summary
    return {
        "ID": issue["key"],
        "Title": title,
        "Work Item Type": "Product Backlog Item",
        "State": (f.get("status") or {}).get("name", "") or "",
        "Assigned To": _assignee(f),
        "Iteration Path": _sprint_name(f),
        "Tags": ", ".join((f.get("labels") or [])
                          + [t for t in [_goal_tag(f, sprint_number)] if t]
                          + _date_tokens(f)),
        "Original Estimate": 0.0,
        "Completed Work": 0.0,
        "Sizing": _sizing(f),
        "Release": _fix_version(f),
        "Team": _team(f),
    }


def _task_row(issue: dict[str, Any]) -> dict[str, Any]:
    """Sub-task / hour-carrier row (canonical type Task)."""
    f = issue["fields"]
    est, spent = _hours(f)
    return {
        "ID": issue["key"],
        "Title": f.get("summary", ""),
        "Work Item Type": "Task",
        "State": (f.get("status") or {}).get("name", "") or "",
        "Assigned To": _assignee(f),
        "Iteration Path": _sprint_name(f),
        "Tags": ", ".join(f.get("labels") or []),
        "Original Estimate": est,
        "Completed Work": spent,
        "Team": _team(f),
    }


def _bug_row(issue: dict[str, Any]) -> dict[str, Any]:
    f = issue["fields"]
    est, spent = _hours(f)
    return {
        "ID": issue["key"],
        "Title": f.get("summary", ""),
        "Work Item Type": "Bug",
        "State": (f.get("status") or {}).get("name", "") or "",
        "Assigned To": _assignee(f),
        "Iteration Path": _sprint_name(f),
        "Tags": ", ".join(f.get("labels") or []),
        "Original Estimate": est,
        "Completed Work": spent,
        "Sizing": _sizing(f),
        "Release": _fix_version(f),
        "Team": _team(f),
    }


def _fetch_titles(ctx: dict[str, Any], keys: list[str]) -> dict[str, str]:
    """Fetch parent Epic summaries so PBI rows can be referred to by the PBI title."""
    titles: dict[str, str] = {}
    keys = [k for k in keys if k]
    for i in range(0, len(keys), 50):
        chunk = ",".join(keys[i:i+50])
        for it in _search(ctx, f"key in ({chunk})"):
            titles[it["key"]] = it["fields"].get("summary", "")
    return titles


# ── Public loaders ──────────────────────────────────────────────────────────
def _sprint_clause(sprint) -> str | None:
    """Build a JQL Sprint clause from a single name or a list of names.
    A list is OR-ed: (Sprint = "a" OR Sprint = "b" ...), which is how each team's
    sprint ("MPM <Team> Sprint N") is combined into one delivery sprint."""
    if not sprint:
        return None
    names = [sprint] if isinstance(sprint, str) else [s for s in sprint if s]
    if not names:
        return None
    if len(names) == 1:
        return f'Sprint = "{names[0]}"'
    return "(" + " OR ".join(f'Sprint = "{n}"' for n in names) + ")"


def load_dataframe(release: str | None = None, sprint=None,
                   sprint_number: int | None = None,
                   ctx: dict[str, Any] | None = None) -> pd.DataFrame:
    """Scope by release (fixVersion) and/or sprint. `sprint` may be a single
    name or a LIST of per-team sprint names (OR-ed). Returns the canonical
    DataFrame, ordered PBI-then-its-sub-tasks so classify_pbis() works."""
    if ctx is None:
        import jira_auth
        ctx = jira_auth.get_context()

    _resolve_date_fields(ctx)   # add Dev/QA Complete Date fields to the fetch
    clauses = [f'project = "{ctx["project"]}"']
    if release:
        clauses.append(f'fixVersion = "{release}"')
    sprint_clause = _sprint_clause(sprint)
    if sprint_clause:
        clauses.append(sprint_clause)
    jql = " AND ".join(clauses) + " ORDER BY Rank ASC"
    parents = _search(ctx, jql)

    if sprint_number is None:
        # Derive "83" from "MPM Sprint 1"? No — keep the literal trailing number
        # if present, else 0. The generator passes CFG.SPRINT_NUMBER explicitly.
        sprint_number = 0

    # Pass 2: sub-tasks of in-scope parents (they carry the hours).
    parent_keys = [i["key"] for i in parents]
    subs: list[dict[str, Any]] = []
    for i in range(0, len(parent_keys), 50):
        chunk = ",".join(parent_keys[i:i+50])
        subs += _search(ctx, f"parent in ({chunk})")

    # Pass 3: parent Epic titles, so Stories can be referred to by their PBI.
    epic_keys = sorted({_parent_key(i["fields"]) for i in parents
                        if (i["fields"].get("issuetype") or {}).get("name") == "Story"
                        and _parent_key(i["fields"])})
    pbi_titles = _fetch_titles(ctx, epic_keys) if epic_keys else {}

    # Index sub-tasks by their parent key (the Story) so we can group hours.
    subs_by_parent: dict[str, list[dict[str, Any]]] = {}
    for s in subs:
        pk = _parent_key(s["fields"])
        subs_by_parent.setdefault(pk, []).append(s)

    # EPIC ROLLUP: the dashboard shows ONE PBI row per Epic, not per Story.
    # Group the in-sprint Stories under their parent Epic. Standalone Tasks (no
    # Epic) stay their own PBI unit (grounded in sprint); Bugs are emitted directly.
    epic_groups: dict[str, list[dict[str, Any]]] = {}
    standalone:  list[dict[str, Any]] = []
    bug_issues:  list[dict[str, Any]] = []
    for issue in parents:
        f = issue["fields"]
        itype = (f.get("issuetype") or {}).get("name", "")
        if itype in ("Bug", "Defect"):
            bug_issues.append(issue)
        elif itype == "Story" and _parent_key(f):
            epic_groups.setdefault(_parent_key(f), []).append(issue)
        else:
            standalone.append(issue)

    rows: list[dict[str, Any]] = []
    seen_sub: set[str] = set()

    def _emit_subs_for(parent_key: str) -> None:
        for s in subs_by_parent.get(parent_key, []):
            if s["key"] in seen_sub:
                continue
            seen_sub.add(s["key"])
            rows.append(_task_row(s))

    # ONE PBI ROW PER STORY (stories are children of the Epic). If an Epic has
    # several in-sprint stories, the Epic is REPEATED — one row per story, each
    # labelled with the story/task name and judged by its OWN status/goal. Hours
    # roll up from each story's sub-tasks (emitted right after the story row).
    for epic_key, stories in epic_groups.items():
        epic_name = pbi_titles.get(epic_key, "")
        for story in stories:
            rows.append(_story_pbi_row(story, epic_name, sprint_number))
            _emit_subs_for(story["key"])

    # Standalone Tasks (grounded in sprint) keep their own PBI row + sub-tasks.
    for issue in standalone:
        rows.append(_pbi_row(issue, sprint_number, pbi_titles))
        _emit_subs_for(issue["key"])

    # Bugs.
    for issue in bug_issues:
        rows.append(_bug_row(issue))

    # Any sub-tasks whose parent wasn't grouped above (defensive).
    for s in subs:
        if s["key"] not in seen_sub:
            seen_sub.add(s["key"])
            rows.append(_task_row(s))

    return pd.DataFrame(rows, columns=_DASHBOARD_COLS)


# Issue types worth flagging in the Sprint<->Release discrepancy report.
# Only Stories and Tasks carry the Sprint field, so only they can be compared
# against the release. EPICS are cross-sprint containers that never sit in a
# sprint themselves (their child stories do) — including them flags every epic as
# "in release, not in sprint" even when its work IS in the sprint. Sub-tasks
# (inherit parent scope) and Bugs are likewise excluded as noise.
MISMATCH_TYPES = {"story", "task"}


def load_mismatches(release: str, sprint: str,
                    ctx: dict[str, Any] | None = None) -> pd.DataFrame:
    """Issues where Sprint and Release scope disagree (for the Discrepancies tab).
    Columns: ID, Title, In Sprint, In Release, Reason."""
    if ctx is None:
        import jira_auth
        ctx = jira_auth.get_context()
    proj = f'project = "{ctx["project"]}"'

    def _keys(jql: str) -> dict[str, dict[str, Any]]:
        return {i["key"]: i for i in _search(ctx, jql)}

    in_rel    = _keys(f'{proj} AND fixVersion = "{release}"')
    in_sprint = _keys(f'{proj} AND {_sprint_clause(sprint)}')

    rows = []
    for k in sorted(set(in_rel) | set(in_sprint)):
        s_in, r_in = k in in_sprint, k in in_rel
        if s_in and r_in:
            continue
        src = in_sprint.get(k) or in_rel.get(k)
        # Only flag Story and Task (the issue types that actually carry a Sprint).
        # Epics, Sub-tasks and Bugs are excluded — see MISMATCH_TYPES note above.
        itype = (src["fields"].get("issuetype") or {}).get("name", "")
        if itype.strip().lower() not in MISMATCH_TYPES:
            continue
        reason = "In release, not in sprint" if (r_in and not s_in) else "In sprint, not in release"
        rows.append({
            "ID": k,
            "Title": src["fields"].get("summary", ""),
            "Type": itype,
            "In Sprint": s_in,
            "In Release": r_in,
            "Reason": reason,
        })
    return pd.DataFrame(rows, columns=["ID", "Title", "Type", "In Sprint", "In Release", "Reason"])


if __name__ == "__main__":
    rel = sys.argv[1] if len(sys.argv) > 1 else None
    spr = sys.argv[2] if len(sys.argv) > 2 else None
    df = load_dataframe(release=rel, sprint=spr)
    print(f"Rows: {len(df)}")
    print(df["Work Item Type"].value_counts().to_string())
    print()
    print(df.head(15).to_string(index=False))
    if rel and spr:
        mm = load_mismatches(rel, spr)
        print(f"\nSprint<->Release mismatches: {len(mm)}")
        if not mm.empty:
            print(mm.to_string(index=False))
