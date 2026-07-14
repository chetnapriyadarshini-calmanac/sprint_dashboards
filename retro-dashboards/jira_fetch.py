"""
jira_fetch.py
-------------
JIRA Cloud fetch layer. Runs JQL scoped to a
sprint and/or release and returns the SAME canonical pandas DataFrame the
dashboard/retro/scoring generators already consume:

    ID | Title | Work Item Type | State | Assigned To | Iteration Path | Tags
       | Original Estimate | Completed Work

so the generators work unchanged once DATA_SOURCE points here.

MODEL (confirmed from the MPM project, issue MPM-86):
  Hierarchy:   Epic (the migrated PBI)  ->  Story ("<Epic> Implementation",
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
import jira_auth

# ── Field IDs (confirmed via Probe-JiraFields.py) ───────────────────────────
F_SPRINT       = "customfield_10020"   # Sprint (list of sprint objects)
F_GOAL         = "customfield_11559"   # Goal for the Sprint (list of option dicts)
F_STORY_POINTS = "customfield_10034"   # Story Points (confirm vs 10016 on a sized issue)
F_SIZING       = "customfield_11566"   # Sizing (T-shirt)
F_EPIC_LINK    = "customfield_10014"   # Epic Link (fallback to `parent` on newer issues)

_DASHBOARD_COLS = ["ID", "Title", "Work Item Type", "State", "Assigned To",
                   "Iteration Path", "Tags", "Original Estimate", "Completed Work"]

_FIELDS = ["summary", "issuetype", "status", "assignee", "labels", "parent",
           "fixVersions", "timeoriginalestimate", "timespent",
           F_SPRINT, F_GOAL, F_STORY_POINTS, F_SIZING, F_EPIC_LINK]

# JIRA issue type -> canonical work-item type the generators key on.
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
    # rank 0 — not started / pre-approval (present but below PO Approved)
    "Backlog": 0, "Ready For Refinement": 0, "In Refinement": 0,
    "PO Review": 0, "Scoping": 0, "To Be Estimated": 0, "Triage": 0,
    "Investigating": 0, "CSC Investigation": 0, "More Info from Customer": 0,
    "To Do": 0,
    # rank 1 — PO Approved + BA (business) Analysis, which PRECEDE tech analysis
    "PO Approved": 1, "BA Analysis": 1,
    # rank 2 — Ready for Tech Analysis (technical-analysis stage)
    "Ready for Tech Analysis": 2, "Tech Analysis In Progress": 2,
    "Technical Review": 2,
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

# Statuses that mean "do not count toward goals" (excluded / removed / cut).
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


def _sprint_name(fields: dict[str, Any]) -> str:
    """Sprint field is a list of sprint objects; prefer the active one, else last."""
    sprints = fields.get(F_SPRINT) or []
    if isinstance(sprints, list) and sprints:
        active = [s for s in sprints if isinstance(s, dict) and s.get("state") == "active"]
        chosen = active[-1] if active else sprints[-1]
        return chosen.get("name", "") if isinstance(chosen, dict) else str(chosen)
    return ""


def _goal_value(fields: dict[str, Any]) -> str:
    """customfield_11559 -> list of option dicts: [{'value': 'Ready for Dev'}].

    A story can carry MULTIPLE goals (the field accumulates them as it moves
    through the sprint). We use the LAST option — the most-recent / current goal
    — as the story's single sprint goal for bucketing. (Was raw[0] previously.)
    """
    raw = fields.get(F_GOAL)
    if isinstance(raw, list) and raw:
        last = raw[-1]
        return last.get("value", "") if isinstance(last, dict) else str(last)
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
        "Tags": ", ".join((f.get("labels") or []) + [t for t in [_goal_tag(f, sprint_number)] if t]),
        "Original Estimate": est,
        "Completed Work": spent,
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
def load_dataframe(release: str | None = None, sprint: str | None = None,
                   sprint_number: int | None = None,
                   ctx: dict[str, Any] | None = None) -> pd.DataFrame:
    """Scope by release (fixVersion) and/or sprint name. Returns the canonical
    DataFrame, ordered PBI-then-its-sub-tasks so classify_pbis() works."""
    if ctx is None:
        ctx = jira_auth.get_context()

    clauses = [f'project = "{ctx["project"]}"']
    if release:
        clauses.append(f'fixVersion = "{release}"')
    if sprint:
        clauses.append(f'Sprint = "{sprint}"')
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

    # One PBI row per Epic; representative Story = the least-advanced by status
    # rank. Hours come from ALL the Epic's Stories' sub-tasks (emitted after it
    # so the generator attributes them to this PBI).
    for epic_key, stories in epic_groups.items():
        rep = min(stories,
                  key=lambda s: STATUS_RANK.get(
                      (s["fields"].get("status") or {}).get("name", ""), -1))
        rows.append(_epic_pbi_row(epic_key, pbi_titles.get(epic_key), rep, sprint_number))
        for story in stories:
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


def load_mismatches(release: str, sprint: str,
                    ctx: dict[str, Any] | None = None) -> pd.DataFrame:
    """Issues where Sprint and Release scope disagree (for the Discrepancies tab).
    Columns: ID, Title, In Sprint, In Release, Reason."""
    if ctx is None:
        ctx = jira_auth.get_context()
    proj = f'project = "{ctx["project"]}"'

    def _keys(jql: str) -> dict[str, dict[str, Any]]:
        return {i["key"]: i for i in _search(ctx, jql)}

    in_rel    = _keys(f'{proj} AND fixVersion = "{release}"')
    in_sprint = _keys(f'{proj} AND Sprint = "{sprint}"')

    rows = []
    for k in sorted(set(in_rel) | set(in_sprint)):
        s_in, r_in = k in in_sprint, k in in_rel
        if s_in and r_in:
            continue
        src = in_sprint.get(k) or in_rel.get(k)
        reason = "In release, not in sprint" if (r_in and not s_in) else "In sprint, not in release"
        rows.append({
            "ID": k,
            "Title": src["fields"].get("summary", ""),
            "In Sprint": s_in,
            "In Release": r_in,
            "Reason": reason,
        })
    return pd.DataFrame(rows, columns=["ID", "Title", "In Sprint", "In Release", "Reason"])


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
